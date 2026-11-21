"""Local dashboard with public market feeds and optional read-only browser quotes."""
import copy
import math
import os
import sys
import threading
import json
import time
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template, request, Response, stream_with_context, g, send_file
from src.common.config_loader import ConfigLoader
from src.common.market_data import utc_now, age_seconds
from src.common.live_feed import VariationalPriceFeed, LighterBookFeed
from src.common.comparison import compare, estimate_entry_cost, history_point
from src.common.allocation_preview import allocation_preview
from src.common.hedge_cost import entry_breakdown
from src.execution.status import implementation_status
from src.common.history import HistoryStore
from src.variational.quote_bridge import QuoteBridge
from src.variational.manual_events import normalize_manual_event
from src.variational.capture_report import report as capture_report
from src.web.diagnostics import Diagnostics
from src.execution.console import ExecutionConsole
from src.execution.browser_session import debugger_status
from src.variational.public_client import VariationalPublicClient
from src.lighter.api_trader import LighterApiTrader
from src.execution.platform_clients import deployment_for
from src.strategy.arbitrage_strategy import ArbitrageStrategy
from src.web import local_credentials
from src.execution.journal import execution_lease
from src.web.readiness import readiness_report
from src.web.market_demand import MarketDemand

app = Flask(__name__)


@app.get('/favicon.ico')
def favicon():
    return send_file(ROOT / 'src/web/static/favicon.svg', mimetype='image/svg+xml')


config_loader = None
variational_trader = None
lighter_trader = None
strategy = None
symbols = []
current_prices = {}
spread_history = {}
state_lock = threading.RLock()
refresh_lock = threading.Lock()
monitor_stop = threading.Event()
monitor_thread = None
trading_status = {'running': False}
book_cache = {}
book_errors = {}
v_feed = None
l_feed = None
history_store = None
market_demand = MarketDemand()
from src.web.history_cache import ProjectionCache
history_projection_cache = ProjectionCache()
history_detail_cache = ProjectionCache(capacity=2048)
proxy_url = None
lighter_proxy_url = None
quote_bridge = QuoteBridge(ROOT / 'data/bridge-token')
from src.variational.passive_bridge import PassiveQuoteBridge
passive_bridge = PassiveQuoteBridge(quote_bridge)
manual_events_path = ROOT / 'data/variational-manual-events.jsonl'
manual_events_lock = threading.Lock()
credentials_path = ROOT / '.env'
execution_profile_path = ROOT / 'config/variational-execution.local.json'
credentials_token = secrets.token_urlsafe(32)
# Full-catalog execution requests carry up to 2,000 symbols plus policy.
# Small control/observer requests keep their original 1 KiB limit.
EXECUTION_SCOPE_PATHS = frozenset('/api/execution/' + action for action in (
    'check', 'prepare', 'preview', 'once', 'wait-once', 'monitor', 'scope'))
EXECUTION_SCOPE_MAX_BYTES = 128 * 1024
EXECUTION_SCOPE_MAX_SYMBOLS = 2000
EXECUTION_SYMBOL_MAX_LENGTH = 64
credentials_verified = False
lighter_verification = {}
credentials_last_result = ''
credentials_lock = threading.Lock()
platform_verification = {'lighter_rh': {}, 'standx': {}, 'arcus': {}}
platform_verification_path = ROOT / 'data/platform-verification.json'
verification_refresh_thread = None
verification_refresh_stop = threading.Event()
diagnostics = Diagnostics(ROOT / 'data/diagnostics.sqlite')
execution_console = ExecutionConsole(ROOT, diagnostics)


from src.web.history_marks import HistoryMarks, HistoryMarkAdapter
history_marks = HistoryMarks()
from src.web.history_sync import HistorySync
from src.execution.pending_accounting import read_allowed as accounting_read_allowed


def _history_execution_active():
    try:
        data = json.loads(execution_console.state_path.read_text())
        state = data.get('state')
        return state == 'starting' or (state == 'running' and execution_console._worker_alive(data)) or state not in ('running', 'finished', 'failed', 'idle')
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True


history_sync = HistorySync(lambda venues, symbols, limit: execution_console.platform_history(venues, symbols, limit),
                           _history_execution_active,
                           pending_fetch=lambda row: execution_console.pending_accounting(row),
                           read_ready=lambda: accounting_read_allowed(ROOT))


def init_system(config_path=None):
    global config_loader, variational_trader, lighter_trader, strategy, symbols, proxy_url, lighter_proxy_url, platform_verification
    with state_lock:
        if platform_verification_path.exists():
            try:
                stored = json.loads(platform_verification_path.read_text())
                if isinstance(stored, dict):
                    platform_verification.update({k: v for k, v in stored.items() if k in platform_verification})
            except (OSError, ValueError):
                pass
        config_loader = ConfigLoader(config_path or ROOT / 'config/config.yaml')
        v = copy.deepcopy(config_loader.get_variational_config())
        l = copy.deepcopy(config_loader.get_lighter_config())
        proxy_url = config_loader.get('common.network.proxy_url')
        v.setdefault('api', {})
        l.setdefault('api', {})
        if 'proxy_url' not in v['api']:
            v['api']['proxy_url'] = proxy_url
        if 'proxy_url' not in l['api']:
            l['api']['proxy_url'] = proxy_url
        lighter_proxy_url = l['api'].get('proxy_url')
        variational_trader = VariationalPublicClient(v)
        # Account index 0 in the original .env was a placeholder, not proof of ownership.
        index = os.getenv('LIGHTER_ACCOUNT_INDEX')
        key = os.getenv('LIGHTER_API_PRIVATE_KEY', '')
        account = int(index) if index and key and not key.startswith('your_') else None
        lighter_trader = LighterApiTrader(l, account_index=account)
        symbols = list(dict.fromkeys(v.get('markets', []) + l.get('markets', []))) or ['BTC']
        settings = copy.deepcopy(config_loader.get_strategy_config())
        settings['config_path'] = str(config_loader.config_path)
        settings['state_path'] = str(ROOT / settings.get('state_path', 'data/hedge_state.json'))
        strategy = ArbitrageStrategy(settings, variational_trader, lighter_trader)
        strategy.history.prepare_indexes()


def ensure_initialized():
    with state_lock:
        if config_loader is None:
            init_system()


def apply_network_proxy():
    """Rebuild public clients and feeds after the shared proxy changes."""
    global proxy_url, lighter_proxy_url, variational_trader, lighter_trader, v_feed, l_feed
    new_proxy = config_loader.get('common.network.proxy_url')
    old_v_feed, old_l_feed = v_feed, l_feed
    if old_v_feed:
        old_v_feed.stop()
    if old_l_feed:
        old_l_feed.stop()
    for old_client in (variational_trader, lighter_trader):
        try:
            old_client.close()
        except (AttributeError, OSError):
            pass
    proxy_url = new_proxy
    lighter_proxy_url = new_proxy
    v_cfg = copy.deepcopy(config_loader.get_variational_config())
    l_cfg = copy.deepcopy(config_loader.get_lighter_config())
    v_cfg.setdefault('api', {})['proxy_url'] = new_proxy
    v_cfg.setdefault('browser', {})['proxy'] = new_proxy
    l_cfg.setdefault('api', {})['proxy_url'] = new_proxy
    variational_trader = VariationalPublicClient(v_cfg)
    index = os.getenv('LIGHTER_ACCOUNT_INDEX')
    key = os.getenv('LIGHTER_API_PRIVATE_KEY', '')
    account = int(index) if index and key and not key.startswith('your_') else None
    lighter_trader = LighterApiTrader(l_cfg, account_index=account)
    v_feed, l_feed = None, None


def refresh_prices(requested_symbols=None):
    ensure_initialized()
    selected = symbols if requested_symbols is None else [s for s in requested_symbols if s in symbols]
    if not selected:
        return {}
    with refresh_lock:
        result = {symbol: {'lighter': None, 'variational': None, 'timestamp': utc_now(),
                           'demo': False, 'executable': False, 'errors': {}, 'details': {}}
                  for symbol in selected}
        def fetch(venue, trader):
            values = {}
            for symbol in selected:
                try:
                    values[symbol] = (trader.get_snapshot(symbol), None)
                except Exception as exc:
                    # Public clients return sanitized errors only.
                    values[symbol] = (None, str(exc))
            return venue, values
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fetch, name, trader) for name, trader in
                       [('variational', variational_trader), ('lighter', lighter_trader)]]
            for future in futures:
                name, values = future.result()
                for symbol, (snapshot, error) in values.items():
                    if error:
                        result[symbol]['errors'][name] = error
                    else:
                        result[symbol][name] = snapshot['mark_price']
                        result[symbol]['details'][name] = snapshot
        with state_lock:
            for symbol, row in result.items():
                a, b = row['variational'], row['lighter']
                details = row['details']
                v, l = details.get('variational', {}), details.get('lighter', {})
                row['instrument_mismatch'] = bool(v and l and v['instrument_type'] != l['instrument_type'])
                row['spread_bps'] = abs(a-b) / ((a+b)/2) * 10000 if a and b else None
                if a and b:
                    # De-duplicate cached observations and mark gaps as gaps, not zeroes.
                    key = (v.get('fetched_at'), l.get('fetched_at'))
                    history = spread_history.setdefault(symbol, [])
                    if not history or tuple(history[-1].get('observation', [])) != key:
                        history.append({'lighter_price': b, 'variational_price': a,
                                        'spread': row['spread_bps'], 'timestamp': row['timestamp'],
                                        'observation': key, 'reference_only': True})
                        spread_history[symbol] = history[-8640:]
                else:
                    history = spread_history.setdefault(symbol, [])
                    if not history or history[-1].get('spread') is not None:
                        history.append({'lighter_price': b, 'variational_price': a,
                                        'spread': None, 'timestamp': row['timestamp'],
                                        'reference_only': True})
                        spread_history[symbol] = history[-8640:]
                current_prices[symbol] = row
        return result


@app.before_request
def local_write_guard():
    # No wildcard CORS; an unrelated webpage cannot operate this local service.
    from urllib.parse import urlsplit
    g.diagnostic_id = secrets.token_hex(6)
    if request.path == '/' or request.path.startswith('/api/'):
        if (urlsplit('http://' + request.host).hostname not in ('localhost', '127.0.0.1', '::1')
                or request.remote_addr not in ('127.0.0.1', '::1')):
            return jsonify(success=False, error='仅允许本机地址访问'), 403
    if request.path.startswith(('/api/local-credentials', '/api/platform-credentials', '/api/network')):
        if request.content_length and request.content_length > 2048:
            return jsonify(success=False, error='请求过大'), 413
        if request.method == 'POST' and not secrets.compare_digest(
                request.headers.get('X-Local-Token', ''), credentials_token):
            return jsonify(success=False, error='本机页面已失效，请刷新后重试'), 403
    if request.path.startswith(('/api/execution/','/api/quote-observer/','/api/strategy-preview')) and request.method == 'POST':
        if not secrets.compare_digest(request.headers.get('X-Local-Token', ''), credentials_token):
            return jsonify(success=False, error='本机页面已失效，请刷新后重试'), 403
        limit = EXECUTION_SCOPE_MAX_BYTES if request.path in EXECUTION_SCOPE_PATHS else 1024
        if request.content_length and request.content_length > limit:
            error = '执行范围请求过大（最多 128 KiB）' if request.path in EXECUTION_SCOPE_PATHS else '请求过大'
            return jsonify(success=False, error=error), 413
    if request.path == '/api/bridge/quote':
        if request.method == 'OPTIONS':
            return Response(status=204)
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(token, quote_bridge.token):
            return jsonify(error='报价桥接配对码错误'), 403
        if request.content_length and request.content_length > 4096:
            return jsonify(error='报价过大'), 413
        if not request.is_json:
            return jsonify(error='只接受 JSON'), 415
        return None
    if request.path == '/api/bridge/manual-event':
        if request.method == 'OPTIONS':
            return Response(status=204)
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(token, quote_bridge.token):
            return jsonify(error='观测桥接配对码错误'), 403
        if request.content_length and request.content_length > 200000:
            return jsonify(error='观测事件过大'), 413
        if not request.is_json:
            return jsonify(error='只接受 JSON'), 415
        return None
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify(success=False, error='不接受跨站操作'), 403
        if not request.is_json:
            return jsonify(success=False, error='操作必须使用 application/json'), 415


@app.route('/')
def index():
    ensure_initialized()
    return render_template('index.html', symbols=symbols, local_token=credentials_token,
                           notional_cap=config_loader.get('strategy.trading.position_size', 12),
                           max_total_position_notional=config_loader.get('strategy.trading.max_total_position_notional', 0))


@app.after_request
def protect_local_settings(response):
    if request.path == '/' or request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
    if request.path.startswith('/api/'):
        response.headers['X-Diagnostic-ID'] = g.get('diagnostic_id', '')
        if response.status_code >= 400:
            diagnostics.record('HTTP_REJECTED', request.url_rule.rule if request.url_rule else None,
                               response.status_code, g.get('diagnostic_id'))
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    diagnostics.record('UNHANDLED_ERROR', request.url_rule.rule if request.url_rule else None,
                       500, g.get('diagnostic_id'), exc)
    return jsonify(error='服务异常，请查看运行诊断', diagnostic_id=g.get('diagnostic_id')), 500


@app.route('/api/operations')
@app.route('/api/operations/export')
def operations():
    ensure_initialized()
    requested_venues = [item.strip() for item in (request.args.get('venues') or '').split(',') if item.strip()]
    requested_symbol = request.args.get('symbol') or None
    credentials = local_credentials.status(credentials_path)
    checked_age = age_seconds(lighter_verification.get('checked_at'))
    verified = credentials_verified and credentials['saved'] and checked_age is not None and 0 <= checked_age <= 300
    with manual_events_lock:
        capture = capture_report(manual_events_path)
    acceptance = None
    acceptance_path = ROOT/'data/variational-manual-acceptance.json'
    if acceptance_path.exists() and acceptance_path.stat().st_size < 20000:
        try:
            acceptance = json.loads(acceptance_path.read_text())
        except (ValueError, OSError):
            pass
    from src.execution.runtime import runtime_blockers
    try:
        profile = json.loads(execution_profile_path.read_text())
        # The operations panel is also used for multi-venue runs.  Applying
        # only a two-venue override here left the saved legacy Variational
        # profile in charge whenever three venues were selected, so the UI
        # reported stale browser/zero-fee blockers even while the worker was
        # scanning signed venues (including Arcus).
        if len(requested_venues) >= 2 and len(set(requested_venues)) == len(requested_venues):
            profile = {**profile, 'venues': requested_venues, 'live_enabled': True}
            if requested_symbol:
                profile['symbol'] = requested_symbol
                profile['symbols'] = [requested_symbol]
        blockers = runtime_blockers(profile, require_enabled=False)
    except (OSError, ValueError):
        profile = {}
        blockers = ['执行配置无法读取，禁止提交']
    # Signed venue runs do not depend on the local Chrome bridge.  Returning
    # its stale status for Arcus/Lighter makes a healthy API run look blocked.
    browser = ({'reachable': False, 'mode': '签名平台执行；不需要本机 Chrome'}
               if profile.get('venues') else
               debugger_status(profile.get('browser_debugger_address'), profile.get('symbol')) if profile else {
                   'reachable': False, 'error': '执行配置无法读取'
               })
    if not verified:
        blockers.insert(0, 'Lighter 需要只读复查已保存密钥与当前费率（有效期 5 分钟）')
    execution = execution_console.status()
    execution_events = strategy.history.recent(200)
    started_at = execution.get('started_at')
    if started_at:
        execution_events = [event for event in execution_events
                            if event.get('timestamp', '') >= started_at]
    latest_execution_event = execution_events[0] if execution_events else None
    # Keep the last rejection/quote evaluation visible while a monitor is
    # between scan stages.  The newest event is often just a successful
    # reference-price read, which previously made the UI look idle even when
    # the preceding candidate had been rejected for a concrete reason.
    last_execution_rejection = next(
        (event for event in execution_events
         if event.get('event') in {'blocked', 'stage_failed', 'wait_once_skipped',
                                   'single_attempt_skipped'}),
        None,
    )
    last_execution_quote = next(
        (event for event in execution_events if event.get('event') == 'quote_evaluation'),
        None,
    )
    # The coordinator historically called its two generic adapters
    # ``variational``/``lighter``.  For a signed pair such as
    # Lighter mainnet + Robinhood Chain, translate those legacy telemetry
    # labels to the actual selected venues before returning diagnostics.
    if latest_execution_event and len(execution.get('venues') or []) == 2:
        legacy = {'variational': execution['venues'][0], 'lighter': execution['venues'][1]}
        detail = latest_execution_event.get('detail') or {}
        if detail.get('venue') in legacy:
            latest_execution_event = {**latest_execution_event,
                                      'detail': {**detail, 'venue': legacy[detail['venue']]}}
    if latest_execution_event and latest_execution_event.get('event') == 'accounting_pending':
        detail = latest_execution_event.get('detail') or {}
        venue = detail.get('venue') or '首腿平台'
        venue = {'lighter': 'Lighter 主网', 'lighter_rh': 'Lighter RH · Robinhood Chain',
                 'standx': 'StandX', 'variational': 'Variational'}.get(venue, venue)
        reason = detail.get('reason') or '成交费用回执未知，第二腿未继续提交'
        execution['note'] = ('首腿已确认成交（' + str(venue) + '），第二腿未提交或未确认；'
                             + str(reason) + '。已暂停，必须先核对两边账户，不能据此判断已平仓')
    execution['latest_event'] = latest_execution_event
    execution['last_rejection'] = last_execution_rejection
    execution['last_quote_evaluation'] = last_execution_quote
    from src.execution.telemetry import latest_reference_failure, rejection_quote
    execution['rejection_quote_evaluation'] = rejection_quote(execution_events, last_execution_rejection)
    execution['reference_blocker'] = latest_reference_failure(execution_events)
    from src.execution.position_monitor import read_position_monitor
    execution_venues = execution.get('venues') or []
    execution['position_monitor'] = read_position_monitor(
        ROOT / ('data/hedge_state_' + '_'.join(execution_venues) + '.monitor.json'),
        execution.get('pid')) if len(execution_venues) >= 2 else None
    from src.execution.running_resume import resume_control
    execution['resume_control'] = resume_control(execution_console, execution)
    monitor_limits = (execution.get('position_monitor') or {}).get('entry_limits')
    if (isinstance(monitor_limits, dict) and monitor_limits.get('version') == 1 and
            not execution['position_monitor'].get('heartbeat_stale')):
        execution['single_position_cycle'] = monitor_limits.get('single_position_cycle', False)
    pair_reconcile_required = False
    pair_has_open_positions = False
    pair_resume_ready = False
    pair_reconciled_at = None
    tracked_venues = list(execution.get('venues') or requested_venues)
    if len(tracked_venues) >= 2 and len(set(tracked_venues)) == len(tracked_venues):
        pair_state_path = ROOT / ('data/hedge_state_' + '_'.join(tracked_venues) + '.json')
        try:
            pair_state = json.loads(pair_state_path.read_text())
            pair_reconciled_at = pair_state.get('last_reconciled_at')
            pair_has_open_positions = any(
                item.get('status') != 'closed' for item in pair_state.get('positions', []))
            from src.execution.position_recovery import resume_proof_valid
            active = [p for p in pair_state.get('positions', []) if p.get('status') != 'closed']
            resume_pair = ([active[0].get('variational_venue'), active[0].get('lighter_venue')]
                           if active else tracked_venues)
            pair_resume_ready = resume_proof_valid(pair_state, resume_pair)
            pair_reconcile_required = bool(
                (pair_state.get('halted_reason') or pair_has_open_positions) and
                not pair_resume_ready)
            if list(execution.get('venues') or []) == tracked_venues:
                execution['tracked_position_count'] = len(active)
                execution['managed_positions'] = [
                    {key: p.get(key) for key in ('id', 'symbol', 'resumed_at', 'variational_size', 'lighter_size')}
                    for p in active if p.get('status') == 'open' and p.get('resumed_at')]
                execution['recovery_blocked'] = any(p.get('status') != 'open' for p in active)
                if active and not execution.get('running'):
                    execution['note'] = ('已有 '+ '、'.join(dict.fromkeys(p.get('symbol', '?') for p in active)) +
                        ' 成交持仓，工作进程已退出，自动平仓监控已停止；请核对原仓位后恢复监控。')
                if execution.get('running') and active and not execution['recovery_blocked']:
                    pair_reconcile_required = False
        except (OSError, ValueError, TypeError):
            pair_reconcile_required = False
    if (not execution.get('running') and pair_reconciled_at and not pair_reconcile_required
            and not execution.get('stop_completed') and not pair_has_open_positions):
        execution['note'] = ('历史记录中有单腿成交并已回补；两平台当前账户已完成只读核对并确认空仓；'
                             '历史成交费用或资金费字段未完整返回，最终收益仍需按平台账单核对，未自动重下单。')
    selected_execution_venues = set(requested_venues)
    recorded_execution_venues = set(execution.get('venues') or [])
    if len(selected_execution_venues) > 2 and selected_execution_venues != recorded_execution_venues:
        execution['latest_event'] = None
        execution['note'] = '当前选择了多个行情平台；实盘启动时会按最新可执行净价差绑定两腿，历史执行记录已折叠。'
    from src.execution.scan_status import annotate_entry_scan
    annotate_entry_scan(execution, execution_events)
    platform_status = {}
    for venue in ('lighter_rh', 'standx', 'arcus'):
        saved = local_credentials.status(credentials_path, venue)
        verification = platform_verification.get(venue, {})
        fee_age = age_seconds(verification.get('checked_at'))
        platform_status[venue] = {
            'saved': saved.get('saved', False),
            'authenticated': bool(saved.get('saved') and verification.get('authenticated') is True
                                  and fee_age is not None and 0 <= fee_age <= 300),
            'checked_at': verification.get('checked_at'),
            'active_orders': verification.get('active_orders'),
            'taker_fee_bps': verification.get('taker_fee_bps'),
        }
    platform_status['lighter'] = {
        'saved': credentials.get('saved', False),
        'authenticated': verified,
        'checked_at': lighter_verification.get('checked_at'),
        'active_orders': lighter_verification.get('active_orders') if verified else None,
        'taker_fee_bps': lighter_verification.get('taker_fee_bps') if verified else None,
    }
    strategy_state = strategy.get_strategy_status()
    data = {'observed_at': utc_now(), 'mode': 'monitor', 'live_enabled': False,
            'monitoring': monitor_thread is not None and monitor_thread.is_alive(),
            'notional_cap': config_loader.get('strategy.trading.position_size', 12),
            'lighter': {'saved': credentials['saved'], 'verified_fresh': verified,
                        'checked_at': lighter_verification.get('checked_at'),
                        'active_orders': lighter_verification.get('active_orders') if verified else None,
                        'taker_fee_bps': lighter_verification.get('taker_fee_bps') if verified else None,
                        'last_result': credentials_last_result},
            'platforms': platform_status,
            'bridge': {**quote_bridge.status(),'observer':passive_bridge.status()}, 'browser': browser,
            'capture': capture, 'blockers': blockers,
            'manual_acceptance': acceptance,
            'readiness': readiness_report(verified, credentials['saved'], quote_bridge.status(),
                                          bool(acceptance and acceptance.get('manual_round_trip_observed')), profile),
            'execution': execution,
            'strategy': strategy_state,
            'entry_scan_backoff_until': (execution if execution.get('running') else strategy_state).get('entry_scan_backoff_until'),
            'entry_scan_backoff_reason': (execution if execution.get('running') else strategy_state).get('entry_scan_backoff_reason'),
            'pair_reconcile_required': pair_reconcile_required,
            'pair_has_open_positions': pair_has_open_positions,
            'pair_resume_ready': pair_resume_ready,
            'diagnostics': diagnostics.recent(),
            'feeds': {'variational': v_feed.health() if v_feed else {'connected': False},
                      'lighter': l_feed.health() if l_feed else {'connected': False}}}
    response = jsonify(data)
    if request.path.endswith('/export'):
        response.headers['Content-Disposition'] = 'attachment; filename="hedge-diagnostics.json"'
    return response


@app.route('/api/execution/check', methods=['POST'])
@app.route('/api/execution/prepare', methods=['POST'])
@app.route('/api/execution/preview', methods=['POST'])
@app.route('/api/execution/once', methods=['POST'])
@app.route('/api/execution/wait-once', methods=['POST'])
@app.route('/api/execution/monitor', methods=['POST'])
def execution_control():
    ensure_initialized()
    payload = request.get_json(silent=True)
    allowed = {'symbol', 'symbols', 'mode', 'max_notional', 'venues', 'scan_venues', 'follow_saved_cap',
               'target_notional', 'candidate_fallback', 'single_position_cycle',
               'gross_spread_min_bps', 'gross_spread_max_bps', 'close_only',
               'reversion_target_mode', 'reversion_window_seconds',
               'reversion_min_count', 'reversion_min_deviation_bps',
               'reversion_max_age_seconds', 'reversion_min_samples', 'reversion_recency_mode'}
    if not isinstance(payload, dict) or not {'symbol', 'max_notional'}.issubset(payload):
        return jsonify(error='请指定执行范围和每腿名义金额上限'), 400
    if set(payload) - allowed:
        return jsonify(error='执行请求包含当前服务不支持的参数；请确认网页服务已重启并刷新页面'), 400
    mode = payload.get('mode', 'selected')
    symbols = payload.get('symbols')
    if symbols is None:
        symbols = [payload.get('symbol')]
    if mode not in ('all', 'selected'):
        return jsonify(error='执行范围只能是全品种或指定品种'), 400
    if not isinstance(symbols, list) or not symbols or any(not isinstance(item, str) or not item.strip() for item in symbols):
        return jsonify(error='全品种模式需要先完成共同品种扫描'), 400
    if len(symbols) > EXECUTION_SCOPE_MAX_SYMBOLS:
        return jsonify(error='执行范围最多支持 2000 个品种；请缩小范围后重新检查'), 400
    if any(len(item) > EXECUTION_SYMBOL_MAX_LENGTH for item in symbols):
        return jsonify(error='执行品种名称过长'), 400
    symbols = list(dict.fromkeys(item.strip().upper().replace('-PERP', '') for item in symbols))
    if mode == 'selected' and len(symbols) != 1:
        return jsonify(error='指定品种模式只能选择一个执行品种'), 400
    venues = payload.get('venues')
    supported_venues = {'lighter', 'lighter_rh', 'standx', 'arcus', 'variational'}
    if venues is not None and (
            not isinstance(venues, list) or len(venues) < 2 or len(venues) > 8 or
            len(set(venues)) != len(venues) or any(not isinstance(item, str) for item in venues) or
            any(item.strip() not in supported_venues for item in venues)):
        return jsonify(error='实盘执行至少选择两个不同平台，最多八个平台；程序会按只读通过的两平台路线自动选优'), 400
    scan_venues = payload.get('scan_venues')
    if scan_venues is not None and (not isinstance(scan_venues, list) or len(scan_venues) < 2 or
                                    len(scan_venues) > 8 or any(not isinstance(item, str) for item in scan_venues)):
        return jsonify(error='行情扫描至少需要两个、最多八个平台'), 400
    if scan_venues is not None:
        scan_venues = list(dict.fromkeys(item.strip() for item in scan_venues if item.strip()))
        if len(scan_venues) < 2 or len(set(scan_venues)) != len(scan_venues) or any(item not in supported_venues for item in scan_venues):
            return jsonify(error='行情扫描至少需要两个不同平台'), 400
    target_notional = payload.get('target_notional', 0)
    candidate_fallback = payload.get('candidate_fallback', True)
    single_position_cycle = payload.get('single_position_cycle', True)
    gross_spread_min_bps = payload.get('gross_spread_min_bps', 0)
    gross_spread_max_bps = payload.get('gross_spread_max_bps', 0)
    close_only = payload.get('close_only', False)
    follow_saved_cap = payload.get('follow_saved_cap', False)
    if not isinstance(follow_saved_cap, bool):
        return jsonify(error='金额跟随设置开关无效'), 400
    cap_options = {'follow_saved_cap': True} if follow_saved_cap else {}
    reversion_recency_mode = payload.get('reversion_recency_mode', 'hard')
    reversion_target_mode = payload.get('reversion_target_mode', 'center')
    reversion_window_seconds = payload.get('reversion_window_seconds', 604800)
    reversion_min_count = payload.get('reversion_min_count', 0)
    reversion_min_deviation_bps = payload.get('reversion_min_deviation_bps', 0)
    reversion_max_age_seconds = payload.get('reversion_max_age_seconds', 0)
    reversion_min_samples = payload.get('reversion_min_samples', 0)
    try:
        target_notional = float(target_notional or 0)
    except (TypeError, ValueError):
        return jsonify(error='目标累计金额必须为 0 或正数'), 400
    if target_notional < 0 or not math.isfinite(target_notional):
        return jsonify(error='目标累计金额必须为 0 或正数'), 400
    try:
        cap_value = float(payload['max_notional'])
    except (TypeError, ValueError):
        return jsonify(error='每腿上限金额无效'), 400
    if target_notional and target_notional + 1e-9 < cap_value:
        return jsonify(error='目标累计金额不能小于每腿上限，否则首单会超过目标'), 400
    if not isinstance(candidate_fallback, bool) or not isinstance(single_position_cycle, bool):
        return jsonify(error='执行策略开关无效'), 400
    if not isinstance(close_only, bool):
        return jsonify(error='只平仓开关无效'), 400
    if reversion_recency_mode not in ('hard', 'soft'):
        return jsonify(error='回归时效模式无效'), 400
    if reversion_target_mode not in ('center', 'zero', 'either'):
        return jsonify(error='回归目标无效'), 400
    try:
        reversion_window_seconds = int(reversion_window_seconds or 604800)
        reversion_min_count = int(reversion_min_count or 0)
        reversion_min_deviation_bps = float(reversion_min_deviation_bps or 0)
        reversion_max_age_seconds = float(reversion_max_age_seconds or 0)
        reversion_min_samples = int(reversion_min_samples or 0)
    except (TypeError, ValueError):
        return jsonify(error='历史回归筛选无效'), 400
    if (not 300 <= reversion_window_seconds <= 7776000 or reversion_min_count < 0 or
            reversion_min_deviation_bps < 0 or reversion_max_age_seconds < 0 or
            reversion_min_samples < 0 or
            not all(math.isfinite(value) for value in (reversion_min_deviation_bps, reversion_max_age_seconds))):
        return jsonify(error='历史回归筛选必须为非负数，窗口应在 5 分钟至 90 天'), 400
    try:
        gross_spread_min_bps = float(gross_spread_min_bps or 0)
        gross_spread_max_bps = float(gross_spread_max_bps or 0)
    except (TypeError, ValueError):
        return jsonify(error='毛价差筛选无效'), 400
    if (not math.isfinite(gross_spread_min_bps) or gross_spread_min_bps < 0 or
            not math.isfinite(gross_spread_max_bps) or gross_spread_max_bps < 0 or
            (gross_spread_max_bps and gross_spread_max_bps < gross_spread_min_bps)):
        return jsonify(error='毛价差筛选必须为正数，且上限不能低于下限'), 400
    if credentials_lock.locked():
        return jsonify(error='鉴权检查正在运行，请等待结束'), 409
    try:
        if request.path.endswith('/preview'):
            if mode != 'selected' or len(symbols) != 1 or payload['symbol'] != symbols[0]:
                return jsonify(error='只读计划复测请选择一个指定品种'), 400
            return jsonify(execution_console.preview_plan(payload['symbol'],payload['max_notional'],venues))
        if request.path.endswith('/prepare'):
            return jsonify(execution_console.prepare_browser(payload['symbol'],payload['max_notional']))
        if request.path.endswith('/wait-once'):
            result = execution_console.launch_wait_once(payload['symbol'], payload['max_notional'], venues, symbols=symbols,
                                                        scan_venues=scan_venues,
                                                        target_notional=target_notional,
                                                        candidate_fallback=candidate_fallback,
                                                        single_position_cycle=single_position_cycle,
                                                        gross_spread_min_bps=gross_spread_min_bps,
                                                        gross_spread_max_bps=gross_spread_max_bps,
                                                        reversion_target_mode=reversion_target_mode,
                                                        reversion_window_seconds=reversion_window_seconds,
                                                        reversion_min_count=reversion_min_count,
                                                        reversion_min_deviation_bps=reversion_min_deviation_bps,
                                                        reversion_recency_mode=reversion_recency_mode,
                                                        reversion_max_age_seconds=reversion_max_age_seconds,
                                                        reversion_min_samples=reversion_min_samples,
                                                        close_only=close_only, **cap_options)
            return jsonify(result), 202 if result['started'] else 409
        if request.path.endswith('/once'):
            result = execution_console.launch_once(payload['symbol'], payload['max_notional'], venues, symbols=symbols,
                                                   scan_venues=scan_venues,
                                                   target_notional=target_notional,
                                                   candidate_fallback=candidate_fallback,
                                                   single_position_cycle=single_position_cycle,
                                                   gross_spread_min_bps=gross_spread_min_bps,
                                                   gross_spread_max_bps=gross_spread_max_bps,
                                                   reversion_target_mode=reversion_target_mode,
                                                   reversion_window_seconds=reversion_window_seconds,
                                                   reversion_min_count=reversion_min_count,
                                                   reversion_min_deviation_bps=reversion_min_deviation_bps,
                                                   reversion_recency_mode=reversion_recency_mode,
                                                   reversion_max_age_seconds=reversion_max_age_seconds,
                                                   reversion_min_samples=reversion_min_samples,
                                                   close_only=close_only, **cap_options)
            return jsonify(result), 202 if result['started'] else 409
        if request.path.endswith('/monitor'):
            result = execution_console.launch_monitor(payload['symbol'], payload['max_notional'], venues, symbols=symbols,
                                                      scan_venues=scan_venues,
                                                      target_notional=target_notional,
                                                      candidate_fallback=candidate_fallback,
                                                      single_position_cycle=single_position_cycle,
                                                      gross_spread_min_bps=gross_spread_min_bps,
                                                      gross_spread_max_bps=gross_spread_max_bps,
                                                      reversion_target_mode=reversion_target_mode,
                                                      reversion_window_seconds=reversion_window_seconds,
                                                      reversion_min_count=reversion_min_count,
                                                      reversion_min_deviation_bps=reversion_min_deviation_bps,
                                                      reversion_recency_mode=reversion_recency_mode,
                                                      reversion_max_age_seconds=reversion_max_age_seconds,
                                                      reversion_min_samples=reversion_min_samples,
                                                      close_only=close_only, **cap_options)
            return jsonify(result), 202 if result['started'] else 409
        if request.path.endswith('/check') and venues:
            # A read-only execution check must use fresh account-bound fee
            # evidence.  Previously it only inspected the last saved report,
            # so a perfectly valid key could remain locked after the five
            # minute evidence window until the user visited each credential
            # panel and refreshed it manually.
            if not credentials_lock.acquire(blocking=False):
                return jsonify(error='平台费率复查正在运行，请等待结束'), 409
            try:
                refresh_saved_verifications()
            finally:
                credentials_lock.release()
        result = execution_console.check(payload['symbol'], payload['max_notional'], venues, symbols=symbols,
                                         scan_venues=scan_venues,
                                         target_notional=target_notional,
                                         candidate_fallback=candidate_fallback,
                                         single_position_cycle=single_position_cycle,
                                         gross_spread_min_bps=gross_spread_min_bps,
                                         gross_spread_max_bps=gross_spread_max_bps,
                                         reversion_target_mode=reversion_target_mode,
                                         reversion_window_seconds=reversion_window_seconds,
                                         reversion_min_count=reversion_min_count,
                                         reversion_min_deviation_bps=reversion_min_deviation_bps,
                                         reversion_recency_mode=reversion_recency_mode,
                                         reversion_max_age_seconds=reversion_max_age_seconds,
                                         reversion_min_samples=reversion_min_samples,
                                         close_only=close_only, **cap_options)
        diagnostics.record('EXECUTION_CHECKED')
        return jsonify(result)
    except (ValueError, TypeError, ArithmeticError):
        return jsonify(error='金额或市场不合法，或超过已保存上限；未启动执行'), 400


@app.route('/api/execution/stop', methods=['POST'])
def execution_stop():
    return jsonify(execution_console.request_stop())


@app.route('/api/execution/resume', methods=['POST'])
def execution_resume():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'pid', 'started_at', 'stop_generation'}:
        return jsonify(success=False, error='恢复请求不完整，请刷新状态后重试'), 400
    result = execution_console.request_resume(**payload)
    if not result['success']:
        return jsonify(**result, error=result['message']), 409
    return jsonify(result)


@app.route('/api/execution/manual-close', methods=['POST'])
def execution_manual_close():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'position_id', 'symbol', 'venues'}:
        return jsonify(success=False, orders_submitted=False, error='双平请求不完整，请刷新本组记录后重试'), 400
    result = execution_console.request_manual_close(**payload)
    return jsonify(result), 200 if result['success'] else 409


@app.route('/api/execution/scope', methods=['POST'])
def execution_scope():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'pid', 'started_at', 'venues', 'symbols'}:
        return jsonify(success=False, error='新仓范围请求不完整，请刷新后重试'), 400
    result = execution_console.request_scope(**payload)
    if not result['success']:
        return jsonify(**result, error=result['message']), 409
    return jsonify(result)


@app.route('/api/execution/browser-open', methods=['POST'])
def execution_browser_open():
    ensure_initialized()
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict) or set(payload)!={'symbol'} or payload['symbol'] not in ('ETH','BTC'):
        return jsonify(error='浏览器连接仅支持 ETH / BTC'),400
    try:
        with execution_console.lock, execution_lease(ROOT/'data/runtime.lock'):
            if execution_console.status()['running']:
                return jsonify(error='执行正在运行或待核对，请先停止等待并核对状态后再操作浏览器。'),409
            from src.execution.browser_launcher import open_browser
            profile=json.loads(execution_profile_path.read_text())
            result=open_browser(ROOT,profile['browser_debugger_address'],payload['symbol'],proxy_url)
            passive_bridge.start(profile['browser_debugger_address'])
            (ROOT/'data/quote-observer.enabled').touch(mode=0o600)
            return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    except (OSError,KeyError,RuntimeError):
        return jsonify(error='浏览器正在被执行程序使用，或本机配置不可用；未发单。'),409


@app.route('/api/execution/reset-stop', methods=['POST'])
def execution_reset_stop():
    ensure_initialized()
    if not credentials_lock.acquire(blocking=False):
        return jsonify(error='鉴权检查正在运行，请等待结束'), 409
    try:
        payload = request.get_json(silent=True) or {}
        venues = payload.get('venues')
        if isinstance(venues, list) and len(venues) == 2:
            return jsonify(execution_console.reconcile_pair(venues))
        return jsonify(execution_console.reset_stop())
    finally:
        credentials_lock.release()


@app.route('/api/local-credentials', methods=['GET', 'POST'])
@app.route('/api/local-credentials/check', methods=['POST'])
def handle_local_credentials():
    global credentials_verified, lighter_verification, credentials_last_result
    ensure_initialized()
    if request.method == 'GET':
        result = local_credentials.status(credentials_path)
        checked_age = age_seconds(lighter_verification.get('checked_at'))
        result['authenticated'] = bool(credentials_verified and result['saved'] and
                                       checked_age is not None and 0 <= checked_age <= 300)
        result['verification'] = lighter_verification
        result['last_result'] = credentials_last_result
        return jsonify(result)
    if not credentials_lock.acquire(blocking=False):
        return jsonify(success=False, error='正在验证，请勿重复提交'), 409
    try:
        with execution_lease(ROOT/'data/runtime.lock'):
            with state_lock, strategy.lock:
                if trading_status['running'] or execution_console.status()['running'] or any(p.status != 'closed' for p in strategy.positions):
                    return jsonify(success=False, error='运行中或存在待核对持仓，不能更换密钥'), 409
            recheck = request.path.endswith('/check')
            if recheck:
                from dotenv import dotenv_values
                stored = dotenv_values(credentials_path)
                try:
                    payload = {'account_index': int(stored.get('LIGHTER_ACCOUNT_INDEX', '')),
                               'key_index': int(stored.get('LIGHTER_API_KEY_INDEX', '')),
                               'private_key': stored.get('LIGHTER_API_PRIVATE_KEY')}
                except (ValueError, TypeError):
                    raise local_credentials.CredentialError('还没有已保存的有效密钥，请先填写并验证') from None
            else:
                payload = request.get_json(silent=True)
            values = local_credentials.validate(payload)
            credentials_last_result = '正在只读验证'
            verification = local_credentials.verify(ROOT, values, lighter_proxy_url)
            with state_lock, strategy.lock:
                if trading_status['running'] or execution_console.status()['running'] or any(p.status != 'closed' for p in strategy.positions):
                    return jsonify(success=False, error='账户状态已改变，密钥未保存'), 409
                if not recheck:
                    local_credentials.save(credentials_path, values)
                local_credentials.bind_fee_verification(
                    execution_profile_path, values, verification or {})
                credentials_verified = True
                lighter_verification = verification or {}
                credentials_last_result = '只读鉴权通过；未启用交易'
                diagnostics.record('LIGHTER_VERIFIED')
        return jsonify(success=True, saved=True, authenticated=True, live_enabled=False,
                       message=('已重新通过只读鉴权；未启用交易' if recheck else
                                '已通过只读鉴权并保存到本机 .env（权限 600）；未启用交易'))
    except local_credentials.CredentialError as exc:
        credentials_verified = False
        # A failed background recheck means the old evidence is stale; keep
        # its timestamp so the UI can explain that it expired instead of
        # flickering between “missing” and “expired”. New saves still clear it.
        if not request.path.endswith('/check'):
            lighter_verification = {}
        diagnostics.record('LIGHTER_VERIFY_FAILED')
        credentials_last_result = str(exc)
        return jsonify(success=False, error=str(exc)), 400
    except Exception:
        credentials_verified = False
        if not request.path.endswith('/check'):
            lighter_verification = {}
        diagnostics.record('LIGHTER_VERIFY_FAILED')
        credentials_last_result = '本地验证或保存失败；未启用交易。请核对网络和文件权限'
        return jsonify(success=False, error='本地验证或保存失败；未启用交易。请核对网络和文件权限'), 503
    finally:
        credentials_lock.release()


@app.route('/api/platform-credentials', methods=['GET', 'POST'])
@app.route('/api/platform-credentials/check', methods=['POST'])
def handle_platform_credentials():
    """Store and verify credentials for independent execution deployments.

    The endpoint never returns private keys.  Verification is authentication
    only; it does not create an order or enable the live gate.
    """
    global platform_verification
    ensure_initialized()
    if request.method == 'GET':
        result = {}
        for venue in ('lighter_rh', 'standx', 'arcus'):
            result[venue] = local_credentials.status(credentials_path, venue)
            verification = platform_verification.get(venue, {})
            result[venue]['verification'] = verification
            checked_age = age_seconds(verification.get('checked_at'))
            result[venue]['authenticated'] = bool(
                result[venue].get('saved') and verification.get('authenticated') is True and
                checked_age is not None and 0 <= checked_age <= 300)
        return jsonify(result)
    if not credentials_lock.acquire(blocking=False):
        return jsonify(success=False, error='正在验证，请勿重复提交'), 409
    try:
        payload = request.get_json(silent=True) or {}
        recheck = request.path.endswith('/check')
        venue = payload.pop('venue', None)
        requested_venue = venue
        if venue not in ('lighter_rh', 'standx', 'arcus'):
            raise local_credentials.CredentialError('只支持 Lighter RH、StandX 或 Arcus')
        if recheck:
            from dotenv import dotenv_values
            stored = dotenv_values(credentials_path)
            if venue == 'lighter_rh':
                payload = {'account_index': int(stored.get('LIGHTER_RH_ACCOUNT_INDEX', '')),
                           'key_index': int(stored.get('LIGHTER_RH_API_KEY_INDEX', '')),
                           'private_key': stored.get('LIGHTER_RH_API_PRIVATE_KEY')}
            elif venue == 'standx':
                payload = {'wallet_address': stored.get('STANDX_WALLET_ADDRESS'),
                           'wallet_private_key': stored.get('STANDX_WALLET_PRIVATE_KEY'),
                           'chain': stored.get('STANDX_CHAIN', 'bsc')}
            else:
                payload = {'wallet_address': stored.get('ARCUS_WALLET_ADDRESS'),
                           'account_index': int(stored.get('ARCUS_ACCOUNT_INDEX', '')),
                           'api_signing_key': stored.get('ARCUS_API_SIGNING_KEY')}
        values = local_credentials.validate(payload, venue)
        if venue == 'arcus':
            verification = local_credentials.verify(
                ROOT, values, lighter_proxy_url, venue,
                arcus_base_url=config_loader.get('arcus.api.base_url', 'https://api.arcus.xyz'))
        else:
            verification = local_credentials.verify(ROOT, values, lighter_proxy_url, venue)
        if not recheck:
            local_credentials.save(credentials_path, values, venue)
        safe = {key: value for key, value in (verification or {}).items()
                if key not in ('token', 'private_key', 'signature')}
        platform_verification[venue] = safe
        platform_verification_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = platform_verification_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(platform_verification, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        os.replace(temporary, platform_verification_path)
        diagnostics.record(venue.upper() + '_VERIFIED')
        return jsonify(success=True, venue=venue, saved=True, authenticated=True,
                       live_enabled=False, verification=safe,
                       message=('已重新通过只读鉴权；尚未启用实盘' if recheck else
                                '已通过只读鉴权并保存到本机；尚未启用实盘'))
    except local_credentials.CredentialError as exc:
        if requested_venue in ('lighter_rh', 'standx', 'arcus') and not recheck:
            platform_verification[requested_venue] = {}
        return jsonify(success=False, error=str(exc)), 400
    except Exception:
        return jsonify(success=False, error='本地验证或保存失败；未启用交易。请核对网络和文件权限'), 503
    finally:
        credentials_lock.release()


@app.route('/api/prices')
def get_prices():
    ensure_initialized()
    market_demand.touch(symbols)
    if monitor_thread and monitor_thread.is_alive():
        return jsonify({s: live_row(s, 1000) for s in symbols})
    return jsonify(refresh_prices())


@app.route('/api/orderbook/<symbol>')
def get_orderbook(symbol):
    ensure_initialized()
    if symbol not in symbols:
        return jsonify(error='未配置的市场', bids=[], asks=[]), 404
    market_demand.touch([symbol])
    try:
        live = l_feed.snapshot(symbol) if l_feed else None
        return jsonify(live or lighter_trader.get_order_book(symbol, discovery=True))
    except Exception as exc:
        return jsonify(error=str(exc), bids=[], asks=[], demo=False), 503


@app.route('/api/spread-history')
def get_spread_history():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with state_lock:
        return jsonify({symbol: [row for row in rows if datetime.fromisoformat(row['timestamp']) > cutoff]
                        for symbol, rows in spread_history.items()})


@app.route('/api/status')
@app.route('/api/preflight')
def get_status():
    ensure_initialized()
    with state_lock:
        return jsonify({**strategy.get_strategy_status(), **trading_status,
                        'execution_development': implementation_status(),
                        'timestamp': utc_now(), 'symbols': symbols,
                        'monitoring': monitor_thread is not None and monitor_thread.is_alive()})


def _position_monitor_payload():
    from src.execution.position_monitor import read_position_monitor
    try:
        state = json.loads(execution_console.state_path.read_text())
        venues = state.get('venues') or []
        if not 2 <= len(venues) <= 3 or any(v not in ('lighter', 'lighter_rh', 'arcus', 'standx', 'variational') for v in venues):
            return {'monitor': None, 'position_count': 0}
        path = ROOT / ('data/hedge_state_' + '_'.join(venues) + '.json')
        count = 0
        if path.exists() and path.stat().st_size < 1000000:
            count = sum(p.get('status') != 'closed' for p in json.loads(path.read_text()).get('positions', []))
        monitor = read_position_monitor(path.with_suffix('.monitor.json'), state.get('pid'))
        if monitor and monitor.get('heartbeat_status') == 'stopped':
            event = state.get('latest_event') or {}
            reason = monitor.get('stop_reason') or (event.get('detail') or {}).get('reason') or state.get('note')
            monitor['stop_reason'] = reason
            for row in monitor.get('positions', []):
                row['monitor_stop_reason'] = reason
        return {'monitor': monitor, 'position_count': count}
    except (OSError, ValueError, TypeError):
        return {'monitor': None, 'position_count': 0, 'error': '监控状态暂不可读'}


@app.get('/api/position-monitor')
def position_monitor_status():
    return jsonify(_position_monitor_payload())


@app.get('/api/position-monitor/stream')
def position_monitor_stream():
    def events():
        while True:
            yield 'data: ' + json.dumps(_position_monitor_payload(), ensure_ascii=False) + '\n\n'
            time.sleep(1)
    return Response(stream_with_context(events()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _build_history_pair(path, venues, settings):
    """Build durable accounting once; adapters only expose public marks."""
    from src.web.execution_book import VENUES
    saved = json.loads(path.read_text())
    if not isinstance(saved.get('positions'), list):
        raise ValueError('持仓历史格式无效')
    if any(venue not in VENUES for venue in venues):
        raise ValueError('未知历史平台')
    clients = [HistoryMarkAdapter(venue, history_marks) for venue in venues]
    settings = copy.deepcopy(settings)
    settings['state_path'] = str(path)
    view = ArbitrageStrategy(settings, *clients)
    if len(view.positions) != len(saved['positions']):
        raise ValueError('持仓历史无法完整读取')
    if path.name != 'hedge_state_' + '_'.join(venues) + '.json':
        view.positions = [p for p in view.positions
                          if [p.variational_venue, p.lighter_venue] == list(venues)]
    from src.execution.entry_accounting import read_entry_evidence, apply_entry_evidence
    for position in view.positions:
        position.variational_venue = position.variational_venue or venues[0]
        position.lighter_venue = position.lighter_venue or venues[1]
        try:
            apply_entry_evidence(position, read_entry_evidence(position, path.parent, view.history))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            pass
    overview = view.get_execution_overview(mark_open=False)
    from src.execution.history_recovery import apply_close_history
    apply_close_history(overview, path.parent)
    return view, overview, clients


def _history_pair_overview(path, venues, evidence=None):
    from src.web.history_cache import file_version, evidence_version
    settings = config_loader.get_strategy_config()
    version = (file_version(path), json.dumps(settings, sort_keys=True),
               evidence if evidence is not None else evidence_version(path.parent))
    view, base, clients = history_projection_cache.get(
        (str(path.resolve()), tuple(venues)), version,
        lambda: _build_history_pair(path, venues, settings))
    # Monitor heartbeat and quote age are always read again. A cache hit never
    # prolongs the life of a mark or masks a stopped/replaced live worker.
    from src.execution.position_monitor import read_position_monitor
    try:
        worker_pid = json.loads(execution_console.state_path.read_text()).get('pid')
    except (OSError, ValueError):
        worker_pid = None
    monitor = read_position_monitor(path.with_suffix('.monitor.json'), worker_pid)
    position_quotes = {}
    if monitor and not monitor['heartbeat_stale']:
        for row in monitor.get('positions', []):
            for leg in row.get('legs', {}).values():
                try:
                    stamp = datetime.fromisoformat(leg['updated_at'].replace('Z', '+00:00'))
                    bid, ask = leg['bid'], leg['ask']
                    if stamp.tzinfo is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
                        continue
                    position_quotes[(leg['venue'], row['symbol'])] = {
                        'bid': bid, 'ask': ask, 'price': (bid + ask) / 2, 'fee_bps': leg.get('fee_bps'),
                        'observed_at': stamp.timestamp(), 'updated_at': leg['updated_at'],
                        'source': leg.get('source')}
                except (KeyError, ValueError, TypeError):
                    continue
    with view.lock:
        for client in clients:
            client.position_quotes = position_quotes
            client.marks = history_marks
        overview = copy.deepcopy(base)
        originals = {position.id: position for position in view.positions}
        for row in overview['open_positions']:
            marked = view._position_mark(originals[row['id']])
            row.update({key: value for key, value in marked.items() if key.startswith('mark_') or key in
                        ('unrealized_pnl_usdc', 'current_spread_bps', 'pnl_error', 'known_realized_pnl_usdc',
                         'last_estimate', 'estimated_exit_pnl_usdc', 'estimated_exit_fee_usdc', 'exit_estimate_note')})
            row['position_monitor'] = copy.deepcopy(next((item for item in (monitor or {}).get('positions', [])
                                            if item.get('id') == row['id']), None))
            if row['position_monitor'] is not None:
                row['position_monitor']['heartbeat_stale'] = monitor['heartbeat_stale']
                runtime_prices = row['position_monitor'].get('entry_prices')
                corrected_prices = [row['entry_price_variational'], row['entry_price_lighter']]
                row['entry_accounting_pending_reload'] = bool(row.get('entry_accounting') and
                    (runtime_prices != corrected_prices) and not monitor['heartbeat_stale'])
        return overview


@app.route('/api/execution-history')
def execution_history():
    ensure_initialized()
    from src.web.execution_book import VENUES, pair_sources, merge_books, unmatched_fills
    try:
        event_page = max(1, int(request.args.get('event_page', 1)))
    except (TypeError, ValueError):
        event_page = 1
    try:
        event_page_size = min(200, max(5, int(request.args.get('event_page_size', 20))))
    except (TypeError, ValueError):
        event_page_size = 20
    requested = [v.strip() for v in (request.args.get('venues') or '').split(',') if v.strip()]
    if requested and (len(requested) != 2 or len(set(requested)) != 2 or any(v not in VENUES for v in requested)):
        return jsonify({'error': '请选择两个有效的历史平台'}), 400
    # History defaults to all persisted pairs, independently of the active
    # worker or scanner selection. Explicit filters never relabel another file.
    books, sources, errors = [], [], []
    data_dir = execution_console.root / 'data'
    from src.web.history_cache import evidence_version
    evidence = evidence_version(data_dir)
    persisted_sources = list(pair_sources(data_dir))
    for path, venues in persisted_sources:
        try:
            books.append((_history_pair_overview(path, venues, evidence), venues, path.name))
            if not requested or set(requested) == set(venues):
                sources.append({'file': path.name, 'venues': venues})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({'file': path.name, 'error': type(exc).__name__})
    default_venues = ['variational', 'lighter']
    books.append((strategy.get_execution_overview(), default_venues, strategy.state_path.name))
    all_overview = merge_books(books)
    sync_status = history_sync.poll(all_overview)
    overview = (merge_books([book for book in books if set(book[1]) == set(requested)])
                if requested else all_overview)
    try:
        identifiers = tuple((key, r.get('id'), r.get('variational_venue'), r.get('lighter_venue'),
                             json.dumps([r.get('entry_order_ids'), r.get('close_order_ids')], sort_keys=True))
                            for key in ('open_positions','closed_positions') for r in all_overview.get(key, []))
        from src.web.history_cache import file_version
        fills = copy.deepcopy(history_detail_cache.get(
            ('fills', str(data_dir.resolve()), tuple(requested)),
            (evidence, identifiers, file_version(data_dir/'arcus-round-trip-test.json')),
            lambda: unmatched_fills(data_dir, [(all_overview, [], '')], requested)))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        fills = []
        errors.append({'file': 'execution_orders.sqlite', 'error': type(exc).__name__})
    if errors:
        overview['summary']['realized_pnl_usdc'] = None
        overview['summary']['estimated_total_pnl_usdc'] = None
    from src.web.history_cache import enrich_history
    try:
        enrich_history(overview, data_dir, history_detail_cache)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        for row in overview.get('closed_positions', []) + overview.get('open_positions', []):
            if row.get('hedge_status') == 'single_leg_recovered':
                row.setdefault('rollback_explanation', {'text': '回补详情暂时无法读取，请稍后重试；具体数量和触发原因待核对。'})
            else:
                row.setdefault('close_diagnostics', {'status': 'unavailable', 'text': '平仓决策诊断暂不可读；不影响交易执行，请稍后查看。'})
    from src.execution.manual_close import annotate_controls
    annotate_controls(execution_console, overview.get('open_positions', []))
    from src.web.history_fee_estimates import apply_fee_estimates, verified_lighter_rate
    apply_fee_estimates(overview, verified_lighter_rate(execution_profile_path, credentials_path))
    return jsonify({'events': strategy.history.recent(event_page_size, (event_page - 1) * event_page_size),
                    'events_total': strategy.history.count(), 'events_page': event_page,
                    'events_page_size': event_page_size, 'events_scope': 'all',
                    **overview, 'other_fills': fills, 'sources': sources, 'history_errors': errors,
                    'history_sync': sync_status,
                    'available_pairs': [v for _, v in persisted_sources],
                    'scope': 'pair' if requested else 'all', 'venues': requested, 'mode': 'monitor'})


@app.route('/api/execution/platform-history', methods=['POST'])
def execution_platform_history():
    """Read authenticated platform orders on demand; never part of polling."""
    ensure_initialized()
    payload = request.get_json(silent=True) or {}
    venues = payload.get('venues')
    symbols = payload.get('symbols')
    if not isinstance(venues, list) or not isinstance(symbols, list):
        return jsonify(success=False, orders_submitted=False,
                       message='平台历史查询需要平台和品种列表。'), 400
    return jsonify(execution_console.platform_history(venues, symbols, payload.get('limit', 100)))


@app.post('/api/strategy-preview')
def get_strategy_preview():
    ensure_initialized()
    from src.web.strategy_preview import strategy_preview
    try:
        return jsonify(strategy_preview(strategy.market_history, request.get_json() or {}, ROOT / 'data'))
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc),orders_submitted=False),400
    except (sqlite3.Error,OSError):
        return jsonify(error='本地历史暂不可读，请稍后重试',orders_submitted=False),503


@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    global strategy
    ensure_initialized()
    if request.method == 'GET':
        return jsonify({'variational': config_loader.get_variational_config(),
                        'lighter': config_loader.get_lighter_config(),
                        'strategy': config_loader.get_strategy_config()})
    with state_lock, strategy.lock:
        execution = execution_console.status()
        active = (trading_status['running'] or execution['running'] or
                  any(p.status != 'closed' for p in strategy.positions))
        try:
            payload = request.get_json()
            requested = None
            if isinstance(payload,dict) and isinstance(payload.get('strategy'),dict):
                entry = payload['strategy'].get('entry_policy',{})
                if isinstance(entry,dict):
                    requested = entry.get('mode',config_loader.get('strategy.entry_policy.mode'))
            if requested == 'channel' and execution['running']:
                monitor = _position_monitor_payload().get('monitor') or {}
                if monitor.get('heartbeat_stale') or monitor.get('channel_policy_version',0) < 1:
                    return jsonify(success=False,error='当前实盘进程尚未加载通道策略；请待原仓按原规则处理完并结束本次运行，再保存通道策略及重新启动。配置未修改。'),409
                channel = payload['strategy'].get('channel_policy',{})
                method = (channel.get('method',config_loader.get('strategy.channel_policy.method','quantile'))
                          if isinstance(channel,dict) else None)
                if method == 'touches' and monitor.get('channel_policy_version',0) < 2:
                    return jsonify(success=False,error='当前实盘进程尚未加载反复触碰通道；请待原仓按原规则处理完并结束本次运行，重新加载服务后再保存。配置未修改。'),409
            if execution['running'] and isinstance(payload,dict) and isinstance(payload.get('strategy'),dict):
                from src.execution.capital_budget import capital_policy
                trading = payload['strategy'].get('trading', {})
                if isinstance(trading,dict):
                    policy = capital_policy(dict(config_loader.get('strategy.trading', {}), **trading))
                    enabled = (policy['capital_reserve_percent'] or policy['capital_reserve_usdc'] or
                               policy['capital_allocation_mode'] != 'per_leg')
                    if enabled:
                        monitor = _position_monitor_payload().get('monitor') or {}
                        if (monitor.get('entry_limits') or {}).get('capital_policy_version') != 1:
                            return jsonify(success=False,error='当前实盘进程未加载资金预留功能；请在本次执行安全结束后启动新版，再保存预留设置。配置未修改。'),409
            config_loader.update_strategy(payload)
        except (ValueError, TypeError) as exc:
            return jsonify(success=False, error=str(exc)), 400
        if active:
            # The child acknowledges entry-only limits in its monitor file.
            # Saving must not mutate or restart the in-flight worker here.
            return jsonify(success=True, pending=True,
                           apply_scope='new_entries',
                           message='配置已保存；持仓数量、金额上限及资金预留在下次开仓检查前应用，其他策略设置待全部平仓后生效。已有仓位继续按原规则管理；实际生效值见持仓上限状态。')
        strategy.config = copy.deepcopy(config_loader.get_strategy_config())
        strategy.config['config_path'] = str(config_loader.config_path)
        # Rebuild only after a validated, durable write.
        settings = copy.deepcopy(strategy.config)
        settings['state_path'] = str(ROOT / settings.get('state_path', 'data/hedge_state.json'))
        strategy = ArbitrageStrategy(settings, variational_trader, lighter_trader)
    return jsonify(success=True, message='配置已保存')


@app.route('/api/network', methods=['GET', 'POST'])
def handle_network():
    ensure_initialized()
    if request.method == 'GET':
        value = config_loader.get('common.network.proxy_url')
        return jsonify(proxy_url=value, enabled=bool(value), applied_proxy_url=proxy_url)
    if not credentials_lock.acquire(blocking=False):
        return jsonify(success=False, error='鉴权检查正在运行，请等待结束'), 409
    try:
        with state_lock, strategy.lock:
            if trading_status['running'] or execution_console.status()['running'] or any(p.status != 'closed' for p in strategy.positions):
                return jsonify(success=False, error='运行中或存在待核对持仓，不能修改网络配置'), 409
            payload = request.get_json(silent=True) or {}
            config_loader.update_network(payload)
            apply_network_proxy()
        diagnostics.record('NETWORK_PROXY_UPDATED')
        value = config_loader.get('common.network.proxy_url')
        return jsonify(success=True, proxy_url=value, enabled=bool(value), message='本地代理已保存并应用到行情、API 和新浏览器连接')
    except (ValueError, TypeError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    finally:
        credentials_lock.release()


@app.route('/api/trading/start', methods=['POST'])
def start_trading():
    ensure_initialized()
    blockers = strategy.live_blockers()
    if blockers:
        return jsonify(success=False, error='；'.join(blockers), blockers=blockers), 409
    # Public adapters cannot pass the gate. Future adapters must add a reconciled
    # execution worker rather than only toggling a status flag.
    return jsonify(success=False, error='实盘执行工作线程尚未验收，未发送任何订单'), 409


@app.route('/api/trading/stop', methods=['POST'])
def stop_trading():
    trading_status['running'] = False
    return jsonify(execution_console.request_stop())


@app.route('/api/variational/connect', methods=['POST'])
def connect_variational():
    return jsonify(success=False, error='行情无需钱包。网页钱包须在 Variational 官网连接并完成账户启用；此服务不会把网页登录误报为交易就绪。'), 409


@app.route('/api/test/lighter')
def test_lighter():
    ensure_initialized()
    try:
        markets, at = lighter_trader.get_markets()
        return jsonify(success=True, message='公开行情连接成功，未验证交易账户',
                       markets=len(markets), fetched_at=at, live_ready=lighter_trader.live_ready)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 503


def live_row(symbol, notional=1000):
    with state_lock:
        row = copy.deepcopy(current_prices.get(symbol, {'lighter': None, 'variational': None,
            'errors': {}, 'details': {}, 'demo': False, 'executable': False}))
        book = copy.deepcopy(book_cache.get(symbol))
    row['timestamp'] = utc_now()
    row['symbol'] = symbol
    if v_feed:
        live = v_feed.snapshot(symbol)
        if live:
            v = row['details'].setdefault('variational', {'exchange_symbol': symbol, 'instrument_type': 'perpetual'})
            v.update(live)
            row['variational'] = live['mark_price']
            row['errors'].pop('variational', None)
    v = row['details'].setdefault('variational', {})
    bridged = quote_bridge.snapshot(symbol)
    if bridged:
        v.update(bridged)
    v.setdefault('quote_source', 'variational_public_api')
    if l_feed:
        live_book = l_feed.snapshot(symbol)
        if live_book:
            book = live_book
    if book:
        age = age_seconds(book.get('fetched_at'))
        if age is None or age > 10:
            book = None
    row['book'] = book
    row['book_error'] = None if book else book_errors.get(symbol, '等待 Lighter 盘口')
    a, b = row.get('variational'), row.get('lighter')
    row['spread_bps'] = (a-b)/b*10000 if a and b else None
    row['comparison'] = compare(row, book, notional)
    row['entry_cost'] = estimate_entry_cost(row['comparison'], row.get('variational'), v.get('mark_updated_at'))
    checked_age = age_seconds(lighter_verification.get('checked_at'))
    fee = (lighter_verification.get('taker_fee_bps')
           if credentials_verified and checked_age is not None and 0 <= checked_age <= 300 else None)
    cost_model = config_loader.get('strategy.cost_model', {}) or {}
    website_fee = bridged is not None and 'fee_bps' in bridged
    website_funding = bridged.get('funding') if bridged else None
    row['hedge_cost'] = entry_breakdown(
        row['comparison'], fee,
        variational_fee_bps=bridged['fee_bps'] if website_fee else cost_model.get('variational_fee_bps', 0),
        funding_bps_per_8h=(website_funding['rate_bps'] * 28800 / website_funding['interval_s']
                           if website_funding else cost_model.get('funding_bps_per_8h')),
        funding_interval_s=website_funding['interval_s'] if website_funding else 28800,
        variational_fee_source='website_quote_estimate' if website_fee else 'manual_prediction',
        funding_source='website_display' if website_funding else 'manual_prediction')
    row['history_point'] = history_point(row)
    try:
        row['allocation'] = allocation_preview(row, config_loader.get_strategy_config())
    except (ValueError, TypeError, ArithmeticError):
        row['allocation'] = {'cap_usdc': config_loader.get('strategy.trading.position_size', 1), 'executable': False, 'plans': [], 'status': '报价或精度不满足缩量条件'}
    row['feeds'] = {
        'variational': v_feed.health() if v_feed else {'connected': False, 'error': '价格流未启动'},
        'lighter': l_feed.health() if l_feed else {'connected': False, 'error': '盘口流未启动'},
        'bridge': quote_bridge.status()}
    return row


def dashboard_parameters():
    symbol = request.args.get('symbol', symbols[0])
    notional = float(request.args.get('notional', '1000'))
    if 'slippage_bps' in request.args or 'fee_bps' in request.args:
        raise ValueError('费用假设参数已移除')
    if symbol not in symbols or not 1 <= notional <= 1000000:
        raise ValueError()
    return symbol, notional


@app.route('/api/dashboard')
def dashboard_snapshot():
    ensure_initialized()
    try:
        args = dashboard_parameters()
    except ValueError:
        return jsonify(error='市场、金额或费用假设参数无效'), 400
    market_demand.touch([args[0]])
    return jsonify(live_row(*args))


@app.route('/api/health')
def health():
    return jsonify(service='hedge-monitor', version=2, mode='monitor',
                   monitoring=bool(monitor_thread and monitor_thread.is_alive()))


@app.route('/api/stream')
def dashboard_stream():
    ensure_initialized()
    try:
        args = dashboard_parameters()
    except ValueError:
        return jsonify(error='市场、金额或费用假设参数无效'), 400
    def events():
        token = market_demand.acquire(args[0])
        try:
            yield 'retry: 2000\n\n'
            while not monitor_stop.is_set():
                yield 'event: quote\ndata: ' + json.dumps(live_row(*args), allow_nan=False) + '\n\n'
                if monitor_stop.wait(1):
                    break
        finally:
            market_demand.release(token)
    return Response(stream_with_context(events()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})


@app.route('/api/history/<symbol>')
def chart_history(symbol):
    ensure_initialized()
    if symbol not in symbols:
        return jsonify(error='未知市场'), 404
    try:
        seconds = int(request.args.get('seconds', '3600'))
        if seconds not in (300, 900, 3600, 21600, 86400):
            raise ValueError()
    except ValueError:
        return jsonify(error='时间范围无效'), 400
    return jsonify(history_store.read(symbol, seconds) if history_store else [])


@app.route('/api/bridge/pair', methods=['POST'])
def bridge_pair():
    # Same-origin callers receive a local, receive-only token that survives restarts.
    return jsonify(token=quote_bridge.token, expires='保存在本机，服务重启后继续有效', url='http://127.0.0.1:5001/api/bridge/quote')


@app.route('/api/quote-observer/start', methods=['POST'])
def start_quote_observer():
    try:
        profile=json.loads(execution_profile_path.read_text())
        status=passive_bridge.start(profile['browser_debugger_address'])
        (ROOT/'data/quote-observer.enabled').touch(mode=0o600)
        return jsonify(success=True,status=status,message='只读接口监听已启动；等待官网产生新报价，不会切换市场或下单。')
    except (OSError,ValueError,KeyError):
        return jsonify(error='未找到有效的本机程序浏览器配置'),400


@app.route('/api/quote-observer/stop', methods=['POST'])
def stop_quote_observer():
    passive_bridge.stop_event.set()
    (ROOT/'data/quote-observer.enabled').unlink(missing_ok=True)
    return jsonify(success=True,message='已停止只读接口监听；不影响订单或持仓。')


@app.route('/api/bridge/quote', methods=['POST', 'OPTIONS'])
def bridge_quote():
    try:
        quote_bridge.ingest(request.get_json())
    except (ValueError, TypeError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(success=True)


@app.route('/api/bridge/manual-event', methods=['POST', 'OPTIONS'])
def bridge_manual_event():
    event = normalize_manual_event(request.get_json())
    if event is None:
        return jsonify(error='观测事件不符合本地白名单'), 400
    manual_events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n'
    with manual_events_lock:
        fd = os.open(manual_events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode('utf-8'))
        finally:
            os.close(fd)
        manual_events_path.chmod(0o600)
    if event.get('kind') == 'submit_response':
        diagnostics.record('CAPTURE_RECEIVED')
    return jsonify(success=True)


@app.after_request
def bridge_headers(response):
    origin = request.headers.get('Origin', '')
    if request.path in ('/api/bridge/quote', '/api/bridge/manual-event') and origin.startswith('chrome-extension://'):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Vary'] = 'Origin'
    return response


def reference_worker():
    global v_feed
    subscribed, refreshed = (), None
    while not monitor_stop.is_set():
        try:
            active = market_demand.active()
            if active != subscribed or (active and v_feed is None):
                if v_feed:
                    v_feed.stop()
                v_feed = VariationalPriceFeed(list(active), proxy_url) if active else None
                if v_feed:
                    v_feed.start()
                subscribed, refreshed = active, None
            if active and (refreshed is None or time.monotonic() - refreshed >= 10):
                refresh_prices(active)
                refreshed = time.monotonic()
        except Exception as exc:
            diagnostics.record('REFERENCE_FAILED', exc=exc)
        monitor_stop.wait(1)
    if v_feed:
        v_feed.stop()


def book_worker():
    global l_feed
    subscribed = ()
    while not monitor_stop.is_set():
        active = market_demand.active()
        if active != subscribed:
            if l_feed:
                l_feed.stop()
            l_feed, subscribed = None, active
        if not active:
            monitor_stop.wait(1)
            continue
        if l_feed is None:
            try:
                markets, _ = lighter_trader.get_markets()
                l_feed = LighterBookFeed({s: markets[s]['market_id'] for s in active if s in markets}, lighter_proxy_url)
                l_feed.start()
            except Exception as exc:
                diagnostics.record('FEED_INIT_FAILED', exc=exc)
        def fetch_book(symbol):
            if l_feed and l_feed.snapshot(symbol):
                return
            try:
                book = lighter_trader.get_order_book(symbol, discovery=True)
                with state_lock:
                    book_cache[symbol] = book
                    book_errors.pop(symbol, None)
            except Exception as exc:
                diagnostics.record('BOOK_FAILED', exc=exc)
                with state_lock:
                    book_cache.pop(symbol, None)
                    book_errors[symbol] = str(exc)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(fetch_book, active))
        monitor_stop.wait(2)
    if l_feed:
        l_feed.stop()


def background_monitor():
    for target in (reference_worker, book_worker):
        threading.Thread(target=target, daemon=True).start()
    while not monitor_stop.is_set():
        try:
            for symbol in market_demand.active():
                row = live_row(symbol)
                if history_store:
                    history_store.append(symbol, row['history_point'])
        except Exception as exc:
            diagnostics.record('SAMPLE_FAILED', exc=exc)
        monitor_stop.wait(1)


def _write_platform_verification(venue, report):
    """Persist sanitized platform evidence without changing the live gate."""
    global platform_verification
    safe = {key: value for key, value in (report or {}).items()
            if key not in ('token', 'private_key', 'signature')}
    with state_lock:
        platform_verification[venue] = safe
        platform_verification_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = platform_verification_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(platform_verification, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        os.replace(temporary, platform_verification_path)


def refresh_saved_verifications():
    """Refresh saved account/fee evidence in the background, read-only."""
    global credentials_verified, lighter_verification, credentials_last_result
    from dotenv import dotenv_values
    stored = dotenv_values(credentials_path)
    # Core Lighter evidence is kept in the execution profile; this refresh
    # never enables trading and never starts an execution worker.
    try:
        values = local_credentials.validate({
            'account_index': int(stored.get('LIGHTER_ACCOUNT_INDEX', '')),
            'key_index': int(stored.get('LIGHTER_API_KEY_INDEX', '')),
            'private_key': stored.get('LIGHTER_API_PRIVATE_KEY'),
        })
        report = local_credentials.verify(ROOT, values, lighter_proxy_url)
        local_credentials.bind_fee_verification(execution_profile_path, values, report or {})
        with state_lock:
            credentials_verified = True
            lighter_verification = report or {}
            credentials_last_result = '后台只读复查通过；未启用交易'
    except (ValueError, TypeError, local_credentials.CredentialError, OSError):
        pass
    for venue in ('lighter_rh', 'standx', 'arcus'):
        try:
            if venue == 'lighter_rh':
                payload = {
                    'account_index': int(stored.get('LIGHTER_RH_ACCOUNT_INDEX', '')),
                    'key_index': int(stored.get('LIGHTER_RH_API_KEY_INDEX', '')),
                    'private_key': stored.get('LIGHTER_RH_API_PRIVATE_KEY'),
                }
            elif venue == 'standx':
                payload = {
                    'wallet_address': stored.get('STANDX_WALLET_ADDRESS'),
                    'wallet_private_key': stored.get('STANDX_WALLET_PRIVATE_KEY'),
                    'chain': stored.get('STANDX_CHAIN', 'bsc'),
                }
            else:
                payload = {
                    'wallet_address': stored.get('ARCUS_WALLET_ADDRESS'),
                    'account_index': int(stored.get('ARCUS_ACCOUNT_INDEX', '')),
                    'api_signing_key': stored.get('ARCUS_API_SIGNING_KEY'),
                }
            values = local_credentials.validate(payload, venue)
            if venue == 'arcus':
                report = local_credentials.verify(
                    ROOT, values, lighter_proxy_url, venue,
                    arcus_base_url=config_loader.get('arcus.api.base_url', 'https://api.arcus.xyz'))
            else:
                report = local_credentials.verify(ROOT, values, lighter_proxy_url, venue)
            _write_platform_verification(venue, report)
        except (ValueError, TypeError, local_credentials.CredentialError, OSError):
            continue


def verification_refresher():
    """Keep read-only fee evidence warm while the local service is running."""
    first = True
    while first or not verification_refresh_stop.wait(240):
        first = False
        if not credentials_lock.acquire(blocking=False):
            continue
        try:
            refresh_saved_verifications()
        finally:
            credentials_lock.release()


def start_monitor():
    global monitor_thread, v_feed, history_store, verification_refresh_thread
    ensure_initialized()
    with state_lock:
        if monitor_thread is None or not monitor_thread.is_alive():
            (ROOT / 'data').mkdir(exist_ok=True)
            history_store = HistoryStore(ROOT / 'data/market_history.sqlite')
            monitor_stop.clear()
            monitor_thread = threading.Thread(target=background_monitor, daemon=True)
            monitor_thread.start()
            if verification_refresh_thread is None or not verification_refresh_thread.is_alive():
                verification_refresh_stop.clear()
                verification_refresh_thread = threading.Thread(target=verification_refresher, daemon=True)
                verification_refresh_thread.start()
            diagnostics.record('SERVICE_STARTED')


if __name__ == '__main__':
    from src.common.quote_clock import quote_clock
    quote_clock.start()
    init_system()
    start_monitor()
    if (ROOT/'data/quote-observer.enabled').exists():
        try:
            passive_bridge.start(json.loads(execution_profile_path.read_text())['browser_debugger_address'])
        except (OSError,ValueError,KeyError):
            pass
    app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False, threaded=True)
