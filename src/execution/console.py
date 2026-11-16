"""User-triggered single-run process control. Importing never starts execution."""
from src.common.platform_support import execution_python, process_alive
import json
import math
import os
import subprocess
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from src.common.config_loader import ConfigLoader
from src.common.market_data import utc_now
from src.execution.runtime import runtime_blockers
from src.execution.preflight import budget_check
from src.execution.orders import decimal
from src.lighter.api_trader import LighterApiTrader
from src.execution.browser_session import debugger_status


class ExecutionConsole:
    def __init__(self, root, diagnostics):
        self.root, self.diagnostics = Path(root), diagnostics
        self.lock = threading.Lock()
        self.process = None
        self.process_log = None
        self.state_path = self.root/'data/console_execution.json'
        self.stop_path = self.root/'data/STOP_EXECUTION'
        self.log_path = self.root/'logs/execution-runtime.log'
        # A failed signed execution can leave a real hedge open while the
        # child process is gone.  Keep a separate read-only watcher so a
        # later flat account snapshot can reconcile the local state without
        # starting another execution process or submitting an order.
        self._reconcile_guard = threading.Lock()
        self._reconcile_thread = None
        self._history_guard = threading.Lock()

    def status(self):
        if not self.state_path.exists():
            return {'state': 'idle', 'running': False, 'stop_requested': self.stop_path.exists(), 'note': '尚未从控制台发起实盘执行'}
        try:
            data = json.loads(self.state_path.read_text())
            state = data['state']
            if state not in ('starting', 'running', 'finished', 'failed'):
                raise ValueError()
            running = state in ('starting', 'running')
            # The web process may restart before its child exits. Recover the
            # worker's explicit terminal event instead of losing its result.
            if data.get('started_at') and (running or state == 'failed') and not self._worker_alive(data):
                completed = self._completed_stop(data)
                if completed:
                    data = {**data, 'state': 'finished', 'running': False,
                            'stop_completed': True, 'finished_at': completed['timestamp'],
                            'latest_event': completed,
                            'note': '全局实盘已停止；工作进程已退出，停止前已有仓位已确认结束。停止标记保留，重新启动前需核对账户并解除停止。'}
                    self._save(data)
                    running = False
            # Legacy records without a start timestamp are intentionally kept
            # fail-closed; they have no evidence that the child is stale.
            if (running or (data['state'] == 'failed' and self.stop_path.exists())) and data.get('started_at') and not self._worker_alive(data):
                # A previous web-server restart can leave a JSON marker while
                # its execution child is gone.  Treat it as stale and clear
                # the startup blocker; never infer a fill or flattening.
                stopped = self.stop_path.exists()
                data = {**data, 'state': 'finished' if stopped else 'failed', 'running': False,
                        'finished_at': utc_now(),
                        'stop_requires_reconcile': stopped,
                        'stop_completed': False,
                        'note': ('执行进程已停止；仓位或订单仍需核对，停止保护已保留。点击“核对原仓位（只读）”后重新检查。' if stopped else
                                 '执行记录是陈旧状态，工作进程已退出；未据此推定成交，请按账户核对后重新检查')}
                self._save(data)
                result = {**data, 'stop_requested': self.stop_path.exists()}
                self._maybe_start_auto_reconcile(result)
                return result
            result = {**data, 'running': running,
                      'stop_requested': self.stop_path.exists(),
                      'position_policy': self._recorded_position_policy(data.get('venues')),
                      **self._route_risk_status(data.get('venues'))}
            # A multi-venue worker scans several platforms but submits each
            # opportunity on one concrete two-leg route.  The stop/recovery
            # flow must use that route rather than treating the whole scan
            # set as an impossible "three-platform pair".
            result['execution_pair'] = self._execution_pair(data)
            if not running and data.get('stop_completed') and not result['stop_requested']:
                result['note'] = '全局实盘已停止；停止标记已解除，请重新只读检查后手动开启实盘。'
            if not running and data.get('stop_requires_reconcile') and not result['stop_requested']:
                result['note'] = '执行进程已停止；原仓位的接管资格以本次只读检查为准，检查通过后需手动开启实盘。'
            self._maybe_start_auto_reconcile(result)
            return result
        except (ValueError, OSError, KeyError):
            return {'state': 'unknown', 'running': True, 'note': '执行记录无法核对，禁止再次启动'}

    def _execution_pair(self, data):
        """Recover the concrete two-leg route bound to the last worker."""
        allowed = {'lighter', 'lighter_rh', 'arcus', 'standx'}

        def valid(value):
            values = [str(item).strip() for item in (value or []) if str(item).strip()]
            return values if len(values) == 2 and len(set(values)) == 2 and set(values) <= allowed else None

        recorded = data.get('venues') if isinstance(data, dict) else None
        if isinstance(recorded, list) and len(recorded) >= 2 and set(recorded) <= allowed:
            try:
                saved = json.loads(self._pair_state_path(recorded).read_text())
                active = [row for row in saved.get('positions', []) if row.get('status') != 'closed']
                if active:
                    pair = valid([active[0].get('variational_venue'), active[0].get('lighter_venue')])
                    if pair and (saved.get('runtime_scope') or {}).get('version') == 1:
                        return pair
                    if pair and all([row.get('variational_venue'), row.get('lighter_venue')] == pair
                                    for row in active):
                        return pair
                    return []
            except (OSError, ValueError, TypeError):
                pass
        direct = valid(data.get('execution_pair')) if isinstance(data, dict) else None
        if direct:
            return direct
        recorded = data.get('venues') if isinstance(data, dict) else None
        pair_routes = data.get('route_candidates') if isinstance(data, dict) else None
        if isinstance(pair_routes, list) and pair_routes:
            try:
                index = int(data.get('route_index', 0) or 0)
            except (TypeError, ValueError):
                index = 0
            ordered = []
            if 0 <= index < len(pair_routes):
                ordered.append(pair_routes[index])
            ordered.extend(pair_routes)
            for item in ordered:
                candidate = item.get('venues') if isinstance(item, dict) else item
                pair = valid(candidate)
                if pair:
                    return pair
        return valid(recorded)

    def _recovery_scope(self, venues, previous):
        scope = list(previous.get('venues') or [])
        if len(scope) >= 2:
            try:
                saved = json.loads(self._pair_state_path(scope).read_text())
                runtime = saved.get('runtime_scope') or {}
                if runtime.get('version') == 1 and set(venues) <= set(runtime['managed_venues']):
                    return scope
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if (len(scope) > 2 and set(scope) <= {'lighter', 'lighter_rh', 'arcus'} and
                self._execution_pair(previous) == list(venues)):
            try:
                saved = json.loads(self._pair_state_path(scope).read_text())
                if isinstance(saved.get('positions'), list):
                    return scope
            except (OSError, ValueError, TypeError):
                pass
        return list(venues)

    def _recorded_position_policy(self, venues):
        try:
            venues = self._execution_pair({'venues': venues}) or venues
            if not isinstance(venues, list) or len(venues) < 2 or len(set(venues)) != len(venues):
                return None
            if any(venue not in ('lighter','lighter_rh','arcus','standx','variational') for venue in venues):
                return None
            saved = json.loads(self._pair_state_path(venues).read_text())
            policy = saved.get('position_policy')
            return policy if isinstance(policy, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _active_route_pauses(self, venues):
        return self._route_risk_status(venues)['route_pauses']

    def _route_risk_status(self, venues):
        empty = {'route_pauses': [], 'entry_risk_routes': []}
        if (not isinstance(venues, (list, tuple)) or len(venues) < 2 or
                len(set(venues)) != len(venues) or not set(venues) <= {'lighter', 'lighter_rh', 'arcus'}):
            return empty
        path = self.root / ('data/hedge_state_' + '_'.join(venues) + '.json')
        try:
            saved = json.loads(path.read_text())
            guards = saved.get('route_rollback_guards', {})
            pauses = [{'route': key, 'symbol': key.split('|', 1)[0],
                     'buy_venue': key.split('|', 1)[1].split('->')[0],
                     'sell_venue': key.split('|', 1)[1].split('->')[1],
                     'rollback_count': int(item['rollback_count']),
                     'reason_code': item.get('reason_code'),
                     'paused_until': float(item['paused_until'])}
                    for key, item in guards.items()
                    if math.isfinite(float(item['paused_until'])) and float(item['paused_until']) > time.time()]
            from src.execution.window_risk import ExecutionWindowRisk
            risk = ExecutionWindowRisk(saved.get('execution_window_risk'))
            routes = [dict(route=key, **risk.evidence(key), counts=profile['counts'], size_cap_usdc=risk.size_cap(key))
                      for key, profile in risk.routes.items()]
            return {'route_pauses': pauses, 'entry_risk_routes': routes}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            return empty

    def _completed_stop(self, data):
        try:
            from src.execution.history import TradeHistory
            events = TradeHistory(self.root/'data/hedge_events.sqlite').recent(1)
            event = events[0] if events else None
            if (event and event['event'] == 'execution_stop_completed' and
                    event['timestamp'] >= str(data['started_at'])):
                return event
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass
        return None

    @staticmethod
    def _worker_alive(data):
        pid = data.get('pid')
        if isinstance(pid, int) and pid > 0:
            return process_alive(pid)
        # Older records did not persist a PID.  Only accept a matching
        # execution_cli process; otherwise the record is stale after restart.
        try:
            output = subprocess.check_output(['ps', '-axo', 'pid=,command='],
                                             text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            return False
        symbol = str(data.get('symbol') or '')
        return any('execution_cli.py' in line and ('--symbol '+symbol) in line
                   for line in output.splitlines())

    def _save(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(state, out, ensure_ascii=False, allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, self.state_path)

    @staticmethod
    def _signed_pair(venues):
        values = {str(item).strip() for item in (venues or []) if str(item).strip()}
        return values == {'lighter', 'lighter_rh'}

    @staticmethod
    def _connection_note(venues, action):
        """Describe the actual connection path used by the child process."""
        labels = {
            'lighter': 'Lighter 主网',
            'lighter_rh': 'Lighter RH',
            'standx': 'StandX',
            'arcus': 'Arcus',
            'variational': 'Variational',
        }
        selected = [labels.get(str(item).strip(), str(item).strip())
                    for item in (venues or []) if str(item).strip()]
        if selected:
            return '正在连接 ' + '、'.join(selected) + ' 签名平台；' + action
        return '正在连接已登录浏览器；' + action

    @staticmethod
    def _approved_execution_venues(check, fallback=None, scan_venues=None):
        """Return every venue in the read-only-approved route set.

        The web form keeps ``venues`` as the preferred two-leg pair for
        backwards compatibility.  For a three-platform run the child process
        must receive the union of all approved pairs; otherwise it starts only
        the first pair and silently leaves the other selected platform out of
        live discovery.
        """
        if check.get('recovery_anchor'):
            return list(check['recovery_anchor'])
        routes = [item for item in (check.get('ready_routes') or [])
                  if item.get('ready') and len(item.get('venues') or []) == 2]
        requested = list(dict.fromkeys(str(item).strip() for item in
                                      (scan_venues or check.get('scan_venues') or []) if str(item).strip()))
        ready_values = {venue for item in routes for venue in (item.get('venues') or [])}
        if len(requested) > 2 and set(requested) <= ready_values:
            return requested
        values = []
        for item in routes:
            for venue in item.get('venues') or []:
                if venue not in values:
                    values.append(venue)
        if len(values) >= 2:
            return values
        return list(check.get('venues') or fallback or [])

    def _pair_state_path(self, venues):
        values = [str(item).strip() for item in (venues or []) if str(item).strip()]
        candidates = [self.root/('data/hedge_state_' + '_'.join(values) + '.json')]
        canonical = self.root/'data/hedge_state_lighter_lighter_rh.json'
        if set(values) == {'lighter', 'lighter_rh'} and canonical not in candidates:
            candidates.append(canonical)
        return next((path for path in candidates if path.exists()), candidates[0])

    def _pair_has_pending_state(self, venues):
        if not isinstance(venues, (list, tuple)) or len(venues) < 2 or len(set(venues)) != len(venues):
            return False
        try:
            data = json.loads(self._pair_state_path(venues).read_text())
            return any(item.get('status') != 'closed' or item.get('accounting_pending') or
                       item.get('pending_funding_venues') for item in data.get('positions', []))
        except (OSError, TypeError, ValueError):
            return False

    def _update_reconcile_state(self, started_at, **fields):
        """Persist watcher progress only for the execution it belongs to."""
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, TypeError, ValueError):
            return
        if started_at and str(data.get('started_at') or '') != str(started_at):
            return
        self._save({**data, **fields})

    def _auto_reconcile_loop(self, venues, started_at, state_venues=None):
        # The first checks are quick; after that use a one-minute cadence so a
        # stuck account or exchange outage does not create request pressure.
        delays = (3, 5, 10, 20, 30, 60)
        attempt = 0
        state_venues = list(state_venues or venues)
        while True:
            try:
                current = json.loads(self.state_path.read_text())
            except (OSError, TypeError, ValueError):
                return
            if (str(current.get('started_at') or '') != str(started_at) or
                    current.get('state') in ('starting', 'running')):
                return
            if not self._pair_has_pending_state(state_venues):
                return
            attempt += 1
            self._update_reconcile_state(
                started_at, reconciliation_state='waiting',
                reconciliation_attempts=attempt,
                reconciliation_checked_at=utc_now(),
                reconciliation_message='自动只读复核中；未发送订单')
            try:
                from src.execution.reconcile_pair import reconcile_pair
                from src.execution.reset_stop import reconcile_unknown_orders
                # Serialize the read-only pass with the manual recovery
                # button so two checks cannot write the pair state together.
                from src.execution.journal import execution_lease
                with self.lock, execution_lease(self.root/'data/runtime.lock'):
                    report = (reconcile_pair(self.root, venues, state_venues=state_venues)
                              if list(state_venues) != list(venues) else reconcile_pair(self.root, venues))
                    reconciled_orders = (reconcile_unknown_orders(self.root, venues)
                        if report.get('success') is True and report.get('orders_submitted') is False
                        and report.get('accounts_flat') is True else 0)
                if (report.get('success') is True and report.get('resume_ready') is True and
                        report.get('orders_submitted') is False):
                    self._update_reconcile_state(
                        started_at, reconciliation_state='resume_ready',
                        reconciliation_checked_at=utc_now(),
                        reconciliation_message='自动只读核对确认未平仓记录与两平台账户匹配；可恢复监控，未发送订单')
                    self.diagnostics.record('EXECUTION_PAIR_RESUME_READY')
                    return
                if (report.get('success') is True and report.get('orders_submitted') is False and
                        not report.get('pending_accounting_count')):
                    self._update_reconcile_state(
                        started_at, reconciliation_state='flat_confirmed',
                        reconciliation_checked_at=utc_now(),
                        reconciliation_message='自动只读核对确认两边空仓和活动订单为空；已收敛 %d 条未知订单回执；未发送订单' %
                        reconciled_orders)
                    self.diagnostics.record('EXECUTION_PAIR_AUTO_RECONCILED')
                    return
                if report.get('success') is True and report.get('orders_submitted') is False:
                    self._update_reconcile_state(
                        started_at, reconciliation_state='waiting_accounting',
                        reconciliation_checked_at=utc_now(),
                        reconciliation_message='账户已确认空仓；费用或资金费尚未完整返回，继续自动核账；未发送订单')
                    delay = delays[min(attempt - 1, len(delays) - 1)]
                    time.sleep(delay)
                    continue
                raise ValueError('只读核对未通过')
            except Exception as exc:
                # Keep the reason bounded: the detailed SDK response must not
                # be copied into the dashboard or execution state file.
                self._update_reconcile_state(
                    started_at, reconciliation_state='waiting',
                    reconciliation_checked_at=utc_now(),
                    reconciliation_message='账户仍有持仓、活动订单或快照未完成；继续保留停机保护（%s）' %
                    type(exc).__name__)
            delay = delays[min(attempt - 1, len(delays) - 1)]
            time.sleep(delay)

    def _maybe_start_auto_reconcile(self, data):
        """Start one read-only watcher for a stale signed-venue execution."""
        venues = list(data.get('venues') or [])
        if data.get('running') or len(venues) < 2 or not set(venues) <= {'lighter', 'lighter_rh', 'arcus'}:
            return
        if not self._pair_has_pending_state(venues):
            return
        try:
            from src.execution.position_recovery import resume_proof_valid
            state = json.loads(self._pair_state_path(venues).read_text())
            reconcile_venues = list(venues[:2])
            active = next((row for row in state.get('positions', [])
                           if row.get('status') != 'closed'), None)
            if active:
                pair = [active.get('variational_venue'), active.get('lighter_venue')]
                if all(isinstance(item, str) and item for item in pair):
                    reconcile_venues = pair
            if resume_proof_valid(state, reconcile_venues):
                return
        except (OSError, ValueError, TypeError):
            pass
        with self._reconcile_guard:
            if self._reconcile_thread and self._reconcile_thread.is_alive():
                return
            started_at = data.get('started_at') or utc_now()
            self._reconcile_thread = threading.Thread(
                target=self._auto_reconcile_loop,
                args=(reconcile_venues, started_at, venues),
                name='execution-auto-reconcile', daemon=True)
            self._reconcile_thread.start()

    @staticmethod
    def _normalize_symbols(symbol, symbols=None):
        values = list(symbols or ([symbol] if symbol and symbol != 'ALL' else []))
        values = list(dict.fromkeys(str(item).strip().upper().replace('-PERP', '') for item in values if str(item).strip()))
        return values

    @staticmethod
    def _policy(cap, target_notional=0, candidate_fallback=True, single_position_cycle=True,
                gross_spread_min_bps=0, gross_spread_max_bps=0, close_only=False,
                reversion_target_mode='center', reversion_window_seconds=604800,
                reversion_min_count=0, reversion_min_deviation_bps=0,
                reversion_max_age_seconds=0, reversion_min_samples=0, reversion_recency_mode='hard',
                follow_saved_cap=False):
        try:
            cap_value = float(cap)
            target_value = float(target_notional or 0)
        except (TypeError, ValueError):
            raise ValueError('金额策略无效')
        if target_value < 0 or not math.isfinite(target_value):
            raise ValueError('目标累计金额必须为 0 或正数')
        if target_value and target_value + 1e-9 < cap_value:
            raise ValueError('目标累计金额不能小于每腿上限，否则首单会超过目标')
        try:
            gross_min = float(gross_spread_min_bps or 0)
            gross_max = float(gross_spread_max_bps or 0)
        except (TypeError, ValueError):
            raise ValueError('毛价差筛选无效')
        if not math.isfinite(gross_min) or gross_min < 0 or not math.isfinite(gross_max) or gross_max < 0:
            raise ValueError('毛价差筛选必须为 0 或正数')
        if gross_max and gross_max < gross_min:
            raise ValueError('毛价差上限不能小于下限')
        if not isinstance(close_only, bool):
            raise ValueError('只平仓开关无效')
        if not isinstance(follow_saved_cap, bool):
            raise ValueError('金额跟随设置开关无效')
        if reversion_recency_mode not in ('hard', 'soft'):
            raise ValueError('回归时效模式无效')
        if reversion_target_mode not in ('center', 'zero', 'either'):
            raise ValueError('回归目标无效')
        try:
            reversion_window_seconds = int(reversion_window_seconds or 604800)
            reversion_min_count = int(reversion_min_count or 0)
            reversion_min_deviation_bps = float(reversion_min_deviation_bps or 0)
            reversion_max_age_seconds = float(reversion_max_age_seconds or 0)
            reversion_min_samples = int(reversion_min_samples or 0)
        except (TypeError, ValueError):
            raise ValueError('历史回归筛选无效')
        if not 300 <= reversion_window_seconds <= 604800 or reversion_min_count < 0 or reversion_min_deviation_bps < 0 or reversion_max_age_seconds < 0 or reversion_min_samples < 0:
            raise ValueError('历史回归筛选必须为非负数，窗口应在 5 分钟至 7 天')
        return {**({'follow_saved_cap': True} if follow_saved_cap else {}),
                'target_notional': target_value,
                'candidate_fallback': bool(candidate_fallback),
                'single_position_cycle': bool(single_position_cycle),
                'gross_spread_min_bps': gross_min,
                'gross_spread_max_bps': gross_max,
                'close_only': close_only,
                'reversion_target_mode': reversion_target_mode,
                'reversion_window_seconds': reversion_window_seconds,
                'reversion_min_count': reversion_min_count,
                'reversion_min_deviation_bps': reversion_min_deviation_bps,
                'reversion_recency_mode': reversion_recency_mode,
                'reversion_max_age_seconds': reversion_max_age_seconds,
                'reversion_min_samples': reversion_min_samples}

    @staticmethod
    def _policy_command(command, policy):
        if policy.get('follow_saved_cap'):
            command.append('--follow-saved-cap')
        if policy['target_notional']:
            command += ['--target-notional', str(policy['target_notional'])]
        if not policy['candidate_fallback']:
            command.append('--no-candidate-fallback')
        if not policy['single_position_cycle']:
            command.append('--allow-multiple-positions')
        if policy['gross_spread_min_bps']:
            command += ['--gross-spread-min-bps', str(policy['gross_spread_min_bps'])]
        if policy['gross_spread_max_bps']:
            command += ['--gross-spread-max-bps', str(policy['gross_spread_max_bps'])]
        if policy['close_only']:
            command.append('--close-only')
        if policy['reversion_target_mode'] != 'center':
            command += ['--reversion-target-mode', policy['reversion_target_mode']]
        if policy['reversion_window_seconds'] != 604800:
            command += ['--reversion-window-seconds', str(policy['reversion_window_seconds'])]
        if policy['reversion_min_count']:
            command += ['--reversion-min-count', str(policy['reversion_min_count'])]
        if policy['reversion_min_deviation_bps']:
            command += ['--reversion-min-deviation-bps', str(policy['reversion_min_deviation_bps'])]
        if policy['reversion_max_age_seconds']:
            command += ['--reversion-max-age-seconds', str(policy['reversion_max_age_seconds'])]
        if policy.get('reversion_recency_mode', 'hard') != 'hard':
            command += ['--reversion-recency-mode', policy['reversion_recency_mode']]
        if policy['reversion_min_samples']:
            command += ['--reversion-min-samples', str(policy['reversion_min_samples'])]
        return command

    @staticmethod
    def _minimum_symbol(value):
        return str(value or '').strip().upper().replace('-PERP', '')

    @staticmethod
    def _minimum_number(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    def _public_pair_budget(self, venues, symbols, cap):
        """Read public instrument limits for a selected signed pair.

        The dashboard's market-data service already maintains the public
        instrument catalogs. Reuse it for the read-only execution check so
        the UI and the worker see the same venue minimums, without creating
        authenticated clients or touching an account. If that service is
        unavailable, return an unknown budget and let the execution worker's
        final per-order validation remain authoritative.
        """
        selected_symbols = [self._minimum_symbol(item) for item in (symbols or []) if self._minimum_symbol(item)]
        result = {'symbol': selected_symbols[0] if len(selected_symbols) == 1 else None,
                  'cap_usdc': float(cap), 'venues': list(venues or []), 'entries': [],
                  'effective_minimum_notional': None, 'blockers': [],
                  'orders_submitted': False, 'source': 'public instrument catalog'}
        if len(venues or []) != 2 or len(selected_symbols) != 1:
            result['note'] = '全品种执行按每个候选品种在下单前重新核对；当前不把某一个品种的门槛当成全局门槛。'
            return result
        symbol = selected_symbols[0]
        api_base = os.environ.get('ARB_MARKET_API_BASE', 'http://127.0.0.1:5002/api/v2').rstrip('/')

        def read_json(path):
            url = api_base + path
            with urllib.request.urlopen(url, timeout=4) as response:
                return json.loads(response.read().decode('utf-8'))

        entries = []
        try:
            for venue in venues:
                query = urllib.parse.urlencode({'venue': venue})
                instruments = read_json('/instruments?' + query)
                instrument = next((row for row in (instruments if isinstance(instruments, list) else [])
                                   if self._minimum_symbol(row.get('canonical_symbol') or row.get('venue_symbol')) == symbol
                                   and row.get('enabled', True) is not False), None)
                quote = None
                if instrument and (instrument.get('min_quote_amount') is None or instrument.get('min_base_amount') is not None):
                    try:
                        query = urllib.parse.urlencode({'symbol': symbol, 'venue': venue})
                        quotes = read_json('/quotes?' + query)
                        quote = quotes[0] if isinstance(quotes, list) and quotes else None
                    except Exception:
                        quote = None
                min_base = self._minimum_number(instrument.get('min_base_amount')) if instrument else None
                min_quote = self._minimum_number(instrument.get('min_quote_amount')) if instrument else None
                ask = self._minimum_number((quote or {}).get('ask'))
                bid = self._minimum_number((quote or {}).get('bid'))
                mark = self._minimum_number((quote or {}).get('mark_price'))
                price = ask or bid or mark
                decimals = self._minimum_number(instrument.get('size_decimals')) if instrument else None
                required_base = max(min_base or 0, (min_quote / price) if min_quote is not None and price else 0)
                if required_base > 0 and decimals is not None:
                    step = 10 ** (-max(0, int(decimals)))
                    required_base = math.ceil((required_base - 1e-12) / step) * step
                by_base = required_base * price if required_base > 0 and price else None
                effective = max(min_quote or 0, by_base or 0) or None
                entries.append({'venue': venue, 'symbol': symbol, 'min_base_amount': min_base,
                                'min_quote_amount': min_quote, 'reference_price': price,
                                'minimum_quantity': required_base or None,
                                'estimated_minimum_notional': effective})
        except Exception:
            result['note'] = '公开平台门槛暂未读取成功；执行前仍会按每个平台实时规则核对。'
            return result
        result['entries'] = entries
        known = [row['estimated_minimum_notional'] for row in entries
                 if row['estimated_minimum_notional'] is not None]
        result['effective_minimum_notional'] = max(known) if known else None
        if result['effective_minimum_notional'] is not None and float(cap) + 1e-9 < result['effective_minimum_notional']:
            result['blockers'].append('两腿有效最低开仓约 %.2f USDC，高于每腿上限 %.2f USDC；不会提高上限' %
                                     (result['effective_minimum_notional'], float(cap)))
        result['note'] = '最低门槛按平台最小数量、最小名义金额、数量步长和当前公开参考价估算；账户可用保证金仍需单独核对。'
        return result

    def _check_pair_recovery(self, venues, previous):
        """A user's readiness/start request also verifies its persisted hedge.

        The recovery child holds the runtime lease and never sends orders.
        A running worker or another recorded pair must never be redirected by
        changing scanner settings.
        """
        if previous.get('running') or any(v not in ('lighter', 'lighter_rh', 'arcus') for v in venues):
            return None
        recorded = previous.get('venues') or []
        scope = self._recovery_scope(venues, previous)
        if recorded and list(recorded) != list(venues) and scope == list(venues):
            return None
        path = self._pair_state_path(scope)
        if not path or not path.exists():
            return None
        try:
            state = json.loads(path.read_text())
            if not (state.get('halted_reason') or self.stop_path.exists() or
                    any(p.get('status') != 'closed' for p in state.get('positions', []))):
                return None
            from src.execution.position_recovery import resume_proof_valid
            if not self.stop_path.exists() and resume_proof_valid(state, venues):
                return {'success': True, 'orders_submitted': False, 'resume_ready': True,
                        'message': '原持仓已通过只读核对；启动时将再次核验并接管监控'}
            command = [
                str(execution_python(self.root)), '-B',
                str(self.root/'scripts/reconcile_pair_state.py'),
                '--venues', ','.join(venues)]
            if scope != list(venues):
                command += ['--state-venues', ','.join(scope)]
            result = subprocess.run(command, cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=60)
            report = json.loads(result.stdout)
            if (result.returncode or report.get('success') is not True or
                    report.get('orders_submitted') is not False):
                raise ValueError('recovery_failed')
            return report
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
            return {'success': False, 'orders_submitted': False,
                    'message': '原仓位只读核对未通过，保留待核对并禁止新开仓（%s）' % type(exc).__name__}

    def check_pair(self, symbol, cap, venues, symbols=None, **policy_values):
        """Readiness check for two non-browser signed venues; never opens a signer."""
        policy = self._policy(cap, **policy_values)
        if len(venues or []) != 2 or len(set(venues or [])) != 2:
            return {'symbol': symbol, 'max_notional': float(cap), 'ready': False,
                    'blockers': ['通用签名执行必须恰好选择两个不同平台'], 'budget': None,
                    'browser': {}, 'orders_submitted': False, 'checked_at': utc_now(), **policy}
        from src.execution.runtime import pair_runtime_blockers
        selected_symbols = self._normalize_symbols(symbol, symbols)
        if not selected_symbols:
            return {'symbol': symbol, 'max_notional': float(cap), 'ready': False,
                    'blockers': ['没有可执行的共同品种，请先完成扫描'], 'budget': None,
                    'browser': {}, 'orders_submitted': False, 'checked_at': utc_now(), **policy}
        profile = {'symbol': selected_symbols[0], 'symbols': selected_symbols,
                   'venues': list(venues), 'live_enabled': True}
        previous = self.status()
        recovery = self._check_pair_recovery(venues, previous)
        grid_blockers = []
        effective_cap = float(cap)
        try:
            from src.execution.grid_policy import GridPolicy
            position_policy = GridPolicy.from_config(
                ConfigLoader(self.root/'config/config.yaml').get_strategy_config())
            if position_policy.mode == 'spread_grid':
                effective_cap = min(effective_cap, position_policy.layer_notional_usdc,
                                    position_policy.total_notional_usdc)
                if any(venue not in ('lighter','lighter_rh','arcus') for venue in venues):
                    grid_blockers.append('价差网格目前仅支持 Arcus / Lighter 主网 / Lighter RH；此组合请选择单开')
        except (OSError, ValueError, TypeError) as exc:
            grid_blockers.append('持仓模式配置无效：' + str(exc))
        budget = self._public_pair_budget(venues, selected_symbols, effective_cap)
        blockers = (list(budget.get('blockers') or []) +
                    grid_blockers +
                    pair_runtime_blockers(profile))
        if recovery and recovery.get('success') is not True:
            blockers.append(recovery['message'])
        # The child runtime enforces the saved strategy position size as a
        # hard ceiling. Check it here as well so a button cannot appear to
        # start and then exit immediately after the process is spawned.
        try:
            configured_cap = decimal(
                ConfigLoader(self.root/'config/config.yaml').get('strategy.trading.position_size'), True)
            requested_cap = decimal(cap, True)
            if requested_cap > configured_cap:
                blockers.insert(0, '每腿上限 %.2f USDC 超过已保存上限 %.2f USDC；请先降低上限或修改保存配置' %
                                (float(requested_cap), float(configured_cap)))
        except (TypeError, ValueError, ArithmeticError):
            blockers.insert(0, '无法核对已保存的每腿上限，禁止启动执行')
        old_venues = previous.get('venues') or []
        if previous['running']:
            blockers.append('执行正在运行或上次状态未核对；禁止重复启动；实际组合：' +
                            ' ↔ '.join(old_venues) + '；改选不会自动切换实盘，需先停止旧组合新开仓并等待退出')
        elif len(old_venues) == 2 and set(old_venues) != set(venues):
            old_path = self.root / ('data/hedge_state_' + '_'.join(old_venues) + '.json')
            try:
                old_state = json.loads(old_path.read_text()) if old_path.exists() else {}
                if old_state.get('halted_reason') or any(p.get('status') != 'closed' for p in old_state.get('positions', [])):
                    blockers.append('上一组合 ' + ' ↔ '.join(old_venues) +
                                    ' 仍有未平仓或待核对状态；先核对原平台并处理旧仓，再启动新组合')
            except (OSError, ValueError, TypeError, AttributeError):
                blockers.append('上一组合持仓状态无法核对；先核对原平台再切换组合')
        if self.stop_path.exists():
            blockers.append('停止标志存在，需核对账户后由操作者解除')
        return {'symbol': symbol, 'max_notional': float(cap), 'ready': not blockers,
                'blockers': list(dict.fromkeys(blockers)), 'budget': budget, 'browser': {},
                'venues': list(venues), 'symbols': selected_symbols,
                'orders_submitted': False, 'checked_at': utc_now(), 'recovery': recovery, **policy}

    @staticmethod
    def _route_pairs(venues, preferred=None):
        """Return concrete two-leg routes in preferred-first order.

        The scanner may contain three or more venues, while the signed
        execution worker must remain bound to exactly two.  Keep this helper
        pure so route ordering is deterministic and does not add any network
        work to the worker's hot path.
        """
        values = list(dict.fromkeys(str(item).strip() for item in (venues or [])
                                   if str(item).strip()))
        pairs = []
        preferred_values = tuple(str(item).strip() for item in (preferred or []) if str(item).strip())
        if len(preferred_values) == 2 and preferred_values[0] != preferred_values[1]:
            pairs.append(list(preferred_values))
        for index, first in enumerate(values):
            for second in values[index + 1:]:
                pair = [first, second]
                if frozenset(pair) not in {frozenset(item) for item in pairs}:
                    pairs.append(pair)
        return pairs

    def check_routes(self, symbol, cap, venues, *, preferred=None, symbols=None, **policy_values):
        """Check every selected route and bind the first ready pair.

        This is called by the user-triggered read-only check only.  It never
        creates a signer or submits an order.  A later worker receives the
        selected pair and therefore keeps the normal two-adapter, two-leg
        execution path unchanged.
        """
        pairs = self._route_pairs(venues, preferred=preferred)
        if len(pairs) < 1:
            return {'symbol': symbol, 'max_notional': float(cap), 'ready': False,
                    'blockers': ['至少选择两个不同平台'], 'route_candidates': [],
                    'orders_submitted': False, 'checked_at': utc_now(), **self._policy(cap, **policy_values)}
        reports = []
        first_report = None
        selected_report = None
        for pair in pairs:
            report = self.check_pair(symbol, cap, pair, symbols=symbols, **policy_values)
            if first_report is None:
                first_report = report
            reports.append({
                'venues': pair,
                'ready': bool(report.get('ready')),
                'blockers': list(report.get('blockers') or []),
                'budget': report.get('budget'),
            })
            if report.get('ready') and selected_report is None:
                selected_report = report
        if selected_report is not None:
            ready_routes = [item for item in reports if item.get('ready')]
            selected_report['route_candidates'] = reports
            selected_report['ready_routes'] = ready_routes
            selected_report['scan_venues'] = list(dict.fromkeys(venues))
            selected_report['message'] = ('已从 %d 条候选路线中绑定可执行组合：%s；'
                                          '首腿提交前若该路线未通过最终核验，将按顺序尝试其他已通过只读检查的路线；'
                                          '一旦首腿提交，组合立即冻结。' %
                                          (len(pairs), ' ↔ '.join(selected_report.get('venues') or [])))
            return selected_report
        blockers = []
        for item in reports:
            label = ' ↔ '.join(item['venues'])
            reasons = item['blockers'] or ['未通过只读条件']
            blockers.append(label + '：' + '；'.join(reasons))
        base = dict(first_report or {})
        base['ready'] = False
        base['blockers'] = blockers
        base['route_candidates'] = reports
        base['scan_venues'] = list(dict.fromkeys(venues))
        base['message'] = '已核对 %d 条候选路线，当前没有通过只读检查的两平台组合；未启动执行。' % len(pairs)
        return base

    def check(self, symbol, cap, venues=None, symbols=None, scan_venues=None, **policy_values):
        policy = self._policy(cap, **policy_values)
        previous = self.status()
        anchor = list(previous.get('venues') or [])
        recovery_anchor = None
        if not previous.get('running') and len(anchor) >= 2:
            try:
                state = json.loads(self._pair_state_path(anchor).read_text())
            except (OSError, ValueError, TypeError):
                state = {}
            if ((state.get('runtime_scope') or {}).get('version') == 1 and
                    any(p.get('status') != 'closed' for p in state.get('positions', []))):
                if set(scan_venues or venues or []) != set(anchor):
                    return dict(ready=False, orders_submitted=False, checked_at=utc_now(),
                        blockers=['原执行仍有持仓，请先选择原管理范围（'+ '、'.join(anchor) +
                                  '）核对并恢复监控；恢复后可带仓切换新仓平台'], **policy)
                recovery_anchor = anchor
                venues, scan_venues = anchor[:2], anchor
        if venues:
            if scan_venues and len(set(scan_venues)) > 2:
                result = self.check_routes(symbol, cap, scan_venues, preferred=venues,
                                         symbols=symbols, **policy)
            else:
                result = self.check_pair(symbol, cap, venues, symbols=symbols, **policy)
            if recovery_anchor:
                result['recovery_anchor'] = recovery_anchor
            return result
        if symbol not in ('ETH', 'BTC'):
            raise ValueError('单次验收只支持 ETH / BTC')
        amount = decimal(cap, True)
        config = ConfigLoader(self.root/'config/config.yaml')
        if config.get('strategy.position_policy.mode', 'single') == 'spread_grid':
            return {'ready':False,'orders_submitted':False,
                    'blockers':['Variational 网页执行请选择单开；价差网格目前仅支持 Arcus / Lighter 签名平台组合']}
        if amount > decimal(config.get('strategy.trading.position_size'), True):
            raise ValueError('验收金额不能超过已保存的每腿上限')
        profile = json.loads((self.root/'config/variational-execution.local.json').read_text())
        blockers = runtime_blockers(profile, require_enabled=False)
        if profile.get('symbol') != symbol:
            blockers.append('市场与已验收执行配置不一致')
        browser = debugger_status(profile.get('browser_debugger_address'), symbol)
        if not browser['reachable']:
            blockers.append('无法连接已登录的本机 Chrome；请用 CDP 启动浏览器后重试')
        elif browser['matching_pages'] != 1:
            blockers.append('浏览器中必须保留一个 Variational ' + symbol + ' 官网标签页')
        if self.status()['running']:
            blockers.append('执行正在运行或上次状态未核对；禁止重复启动')
        if self.stop_path.exists():
            blockers.append('停止标志存在，需核对账户后由操作者清除；不会自动解除')
        settings = config.get_lighter_config()
        settings.setdefault('api', {})
        if 'proxy_url' not in settings['api']:
            settings['api']['proxy_url'] = config.get('common.network.proxy_url')
        public = LighterApiTrader(settings)
        budget = None
        try:
            market, _ = public.get_market(symbol)
            budget = budget_check(symbol, amount, market, market.get('mark_price'))
            blockers = budget['blockers'] + blockers
        except Exception:
            blockers.insert(0, '无法获取 Lighter 当前最低金额和精度，禁止执行')
        finally:
            public.close()
        return {'symbol': symbol, 'max_notional': float(amount), 'ready': not blockers,
                'blockers': blockers, 'budget': budget, 'browser': browser,
                'orders_submitted': False, 'checked_at': utc_now(), **policy}

    def prepare_browser(self, symbol, cap):
        """User-triggered form preparation. This command contains no order submission."""
        with self.lock:
            if self.status()['running']:
                raise ValueError('执行进行中，不能调整表单')
            config=ConfigLoader(self.root/'config/config.yaml')
            if symbol!='ETH' or decimal(cap,True)>decimal(config.get('strategy.trading.position_size'),True):
                raise ValueError('准备市场或金额超出配置')
            try:
                result=subprocess.run([str(execution_python(self.root)),'-B',
                    str(self.root/'scripts/prepare_variational_execution.py')],cwd=self.root,
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    text=True,timeout=45)
                report=json.loads(result.stdout)
                if report.get('orders_submitted') is not False:
                    raise ValueError('invalid_preparation_report')
            except (subprocess.TimeoutExpired,ValueError,OSError):
                return {'ready':False,'orders_submitted':False,'message':'会话准备未完成或超时；没有调用交易提交。请查看会话诊断。'}
            if report.get('ready') is True:
                path=self.root/'config/variational-execution.local.json'
                profile=json.loads(path.read_text())
                profile['submit_labels']=report['checks']['submit_labels']
                profile['account_verified']=True
                profile['response_schema']['account_session_verified']=True
                # Records form + qty observations only. Receipt verification is separate.
                profile['form_preparation_evidence']=report
                receipt=report['checks'].get('receipt_schema',{})
                if receipt.get('verified') is True and receipt.get('kind')=='historical_parser_replay':
                    profile['receipt_schema_verified']=True
                    profile['response_schema']['executable_quote_verified']=True
                    profile['receipt_schema_evidence']=receipt
                temp=path.with_suffix('.prepare.tmp')
                fd=os.open(temp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
                with os.fdopen(fd,'w') as out:
                    json.dump(profile,out,ensure_ascii=False,indent=2)
                os.replace(temp,path)
            return {**report,'message':'执行会话已准备；未下单。' if report.get('ready') else
                    '准备未通过：'+str(report.get('stage','连接'))+'；请查看缺项。'}

    def preview_plan(self, symbol, cap, venues=None):
        with self.lock:
            config=ConfigLoader(self.root/'config/config.yaml')
            signed_pair = venues is not None
            if signed_pair and (len(venues) != 2 or len(set(venues)) != 2 or
                                any(v not in ('lighter','lighter_rh','standx','arcus') for v in venues)):
                raise ValueError('只读计划复测需要两个不同的签名平台')
            if (self.status()['running'] or not isinstance(symbol,str) or not symbol.strip() or symbol=='ALL' or
                    (not signed_pair and symbol not in ('ETH','BTC')) or
                    decimal(cap,True)>decimal(config.get('strategy.trading.position_size'),True)):
                raise ValueError('无法进行只读计划复测')
            command=[str(execution_python(self.root)),'-B',
                     str(self.root/'scripts/preview_execution_plan.py'),str(decimal(cap,True)),symbol]
            if signed_pair:
                command += ['--venues', ','.join(venues)]
            try:
                result=subprocess.run(command,cwd=self.root,
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=45)
                report=json.loads(result.stdout)
                if result.returncode or report.get('orders_submitted') is not False:
                    raise ValueError('invalid_preview_report')
                return report
            except (subprocess.TimeoutExpired,ValueError,OSError):
                return {'ready':False,'orders_submitted':False,'message':'只读计划复测超时或失败；未调用下单。请查看执行历史中的阶段记录。'}

    def launch_once(self, symbol, cap, venues=None, symbols=None, scan_venues=None, **policy_values):
        # Called by the user's POST only, never by a poll or automatic startup.
        with self.lock:
            policy = self._policy(cap, **policy_values)
            if policy['close_only']:
                raise ValueError('仅管理已有仓位模式不执行开仓验证，请启动只平仓监控')
            check = self.check(symbol, cap, venues, symbols=symbols, scan_venues=scan_venues, **policy)
            if not check['ready']:
                return {**check, 'started': False}
            venues = self._approved_execution_venues(check, venues, scan_venues)
            route_candidates = list(check.get('ready_routes') or [{'venues': venues, 'ready': True}])
            state = dict(state='starting', symbol=symbol, max_notional=check['max_notional'], started_at=utc_now(), mode='once',
                         phase='连接签名平台' if venues else '连接已登录浏览器', browser_address=check.get('browser', {}).get('address'),
                         venues=list(venues or []), symbols=list(check.get('symbols') or [symbol]),
                         route_candidates=route_candidates, route_index=0,
                         note=self._connection_note(venues, '达到实时净价差条件后才提交双腿'), **policy)
            self._save(state)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.touch(mode=0o600, exist_ok=True)
            self.log_path.chmod(0o600)
            log_handle = self.log_path.open('a', encoding='utf-8', buffering=1)
            try:
                command = [
                    str(execution_python(self.root)), '-B', str(self.root/'scripts/execution_cli.py'),
                    '--profile', str(self.root/'config/variational-execution.local.json'),
                    '--config', str(self.root/'config/config.yaml'), '--once', '--run', '--arm-once',
                    '--symbol', symbol, '--max-notional', str(check['max_notional'])]
                if venues: command += ['--venues', ','.join(venues)]
                if check.get('symbols'): command += ['--symbols', ','.join(check['symbols'])]
                command = self._policy_command(command, policy)
                self.process = subprocess.Popen(command,
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT)
                self.process_log = log_handle
            except Exception:
                log_handle.close()
                self._save({**state, 'state':'failed', 'note':'执行进程未能启动；未自动重试'})
                self.diagnostics.record('EXECUTION_FAILED')
                raise ValueError('执行进程未能启动') from None
            pid = self.process.pid if type(self.process.pid) is int else None
            state = {**state, 'state': 'running', **({'pid': pid} if pid else {})}
            self._save(state)
            self.diagnostics.record('EXECUTION_STARTED')
            process = self.process
            threading.Thread(target=self._watch, args=(process, state, log_handle), daemon=True).start()
            return {**check, 'started': True, 'orders_submitted': None}

    def launch_monitor(self, symbol, cap, venues=None, symbols=None, scan_venues=None, **policy_values):
        """Start the user-requested continuous monitor; never starts on polling."""
        with self.lock:
            policy = self._policy(cap, **policy_values)
            check = self.check(symbol, cap, venues, symbols=symbols, scan_venues=scan_venues, **policy)
            if not check['ready']:
                return {**check, 'started': False}
            venues = self._approved_execution_venues(check, venues, scan_venues)
            route_candidates = list(check.get('ready_routes') or [{'venues': venues, 'ready': True}])
            if not venues:
                profile = json.loads((self.root/'config/variational-execution.local.json').read_text())
                enabled_blockers = runtime_blockers(profile, require_enabled=True)
                blockers = list(dict.fromkeys(check['blockers'] + enabled_blockers))
                if blockers:
                    return {**check, 'blockers': blockers, 'started': False}
            state = dict(state='starting', symbol=symbol, max_notional=check['max_notional'],
                         started_at=utc_now(), mode='continuous_monitor',
                         phase='连接签名平台' if venues else '连接已登录浏览器',
                         browser_address=check.get('browser', {}).get('address'),
                         venues=list(venues or []), symbols=list(check.get('symbols') or [symbol]),
                         route_candidates=route_candidates, route_index=0,
                         note=('只平仓模式：正在连接账户并管理已有仓位，不会提交新入场订单' if policy['close_only'] else
                               self._connection_note(venues, '达到实时净价差条件才提交双腿')), **policy)
            self._save(state)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.touch(mode=0o600, exist_ok=True)
            self.log_path.chmod(0o600)
            log_handle = self.log_path.open('a', encoding='utf-8', buffering=1)
            try:
                command = [
                    str(execution_python(self.root)), '-B', str(self.root/'scripts/execution_cli.py'),
                    '--profile', str(self.root/'config/variational-execution.local.json'),
                    '--config', str(self.root/'config/config.yaml'), '--run',
                    '--symbol', symbol, '--max-notional', str(check['max_notional'])]
                if venues:
                    command += ['--venues', ','.join(venues)]
                if check.get('symbols'): command += ['--symbols', ','.join(check['symbols'])]
                command = self._policy_command(command, policy)
                self.process = subprocess.Popen(command,
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT)
                self.process_log = log_handle
            except Exception:
                log_handle.close()
                self._save({**state, 'state':'failed', 'note':'监控进程未能启动；未自动重试'})
                self.diagnostics.record('EXECUTION_FAILED')
                raise ValueError('监控进程未能启动') from None
            pid = self.process.pid if type(self.process.pid) is int else None
            state = {**state, 'state': 'running', **({'pid': pid} if pid else {})}
            self._save(state)
            self.diagnostics.record('EXECUTION_MONITOR_STARTED')
            process = self.process
            threading.Thread(target=self._watch, args=(process, state, log_handle), daemon=True).start()
            return {**check, 'started': True, 'orders_submitted': None,
                    'message': ('只平仓监控已启动；继续管理已有仓位，不会提交新入场订单' if policy['close_only'] else
                                '自动监控已启动；达到实时净价差条件才会提交双腿订单')}

    def launch_wait_once(self, symbol, cap, venues=None, symbols=None, scan_venues=None, **policy_values):
        """Start a transient, one-opportunity waiter; it submits at most one entry."""
        with self.lock:
            policy = self._policy(cap, **policy_values)
            if policy['close_only']:
                raise ValueError('仅管理已有仓位模式不执行开仓验证，请启动只平仓监控')
            check = self.check(symbol, cap, venues, symbols=symbols, scan_venues=scan_venues, **policy)
            if not check['ready']:
                return {**check, 'started': False}
            venues = self._approved_execution_venues(check, venues, scan_venues)
            route_candidates = list(check.get('ready_routes') or [{'venues': venues, 'ready': True}])
            state = dict(state='starting', symbol=symbol, max_notional=check['max_notional'],
                         started_at=utc_now(), mode='wait_once',
                         phase='连接签名平台' if venues else '连接已登录浏览器',
                         browser_address=check.get('browser', {}).get('address'),
                         venues=list(venues or []), symbols=list(check.get('symbols') or [symbol]),
                         route_candidates=route_candidates, route_index=0,
                         note=self._connection_note(venues, '等待下一次合格净价差后只提交一次双腿'), **policy)
            self._save(state)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.touch(mode=0o600, exist_ok=True)
            self.log_path.chmod(0o600)
            log_handle = self.log_path.open('a', encoding='utf-8', buffering=1)
            try:
                command = [
                    str(execution_python(self.root)), '-B', str(self.root/'scripts/execution_cli.py'),
                    '--profile', str(self.root/'config/variational-execution.local.json'),
                    '--config', str(self.root/'config/config.yaml'), '--wait-once', '--run', '--arm-once',
                    '--symbol', symbol, '--max-notional', str(check['max_notional'])]
                if venues: command += ['--venues', ','.join(venues)]
                if check.get('symbols'): command += ['--symbols', ','.join(check['symbols'])]
                command = self._policy_command(command, policy)
                self.process = subprocess.Popen(command,
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT)
                self.process_log = log_handle
            except Exception:
                log_handle.close()
                self._save({**state, 'state':'failed', 'note':'自动等待进程未能启动；未自动重试'})
                self.diagnostics.record('EXECUTION_FAILED')
                raise ValueError('自动等待进程未能启动') from None
            pid = self.process.pid if type(self.process.pid) is int else None
            state = {**state, 'state': 'running', **({'pid': pid} if pid else {})}
            self._save(state)
            self.diagnostics.record('EXECUTION_WAIT_ONCE_STARTED')
            process = self.process
            threading.Thread(target=self._watch, args=(process, state, log_handle), daemon=True).start()
            return {**check, 'started': True, 'orders_submitted': None,
                    'message': '已自动等待下一次合格机会；达到条件后只提交一次双腿订单'}

    def _route_command(self, state, venues):
        """Build a child command for a pre-submit route fallback."""
        mode = state.get('mode') or 'once'
        command = [
            str(execution_python(self.root)), '-B', str(self.root/'scripts/execution_cli.py'),
            '--profile', str(self.root/'config/variational-execution.local.json'),
            '--config', str(self.root/'config/config.yaml'), '--run',
            '--symbol', str(state.get('symbol') or 'ALL'),
            '--max-notional', str(state.get('max_notional')),
        ]
        if mode == 'once':
            command += ['--once', '--arm-once']
        elif mode == 'wait_once':
            command += ['--wait-once', '--arm-once']
        if venues:
            command += ['--venues', ','.join(venues)]
        if state.get('symbols'):
            command += ['--symbols', ','.join(state['symbols'])]
        policy = self._policy(
            state.get('max_notional'),
            follow_saved_cap=state.get('follow_saved_cap', False),
            target_notional=state.get('target_notional', 0),
            candidate_fallback=state.get('candidate_fallback', True),
            single_position_cycle=state.get('single_position_cycle', True),
            gross_spread_min_bps=state.get('gross_spread_min_bps', 0),
            gross_spread_max_bps=state.get('gross_spread_max_bps', 0),
            close_only=state.get('close_only', False),
            reversion_target_mode=state.get('reversion_target_mode', 'center'),
            reversion_window_seconds=state.get('reversion_window_seconds', 604800),
            reversion_min_count=state.get('reversion_min_count', 0),
            reversion_min_deviation_bps=state.get('reversion_min_deviation_bps', 0),
            reversion_max_age_seconds=state.get('reversion_max_age_seconds', 0),
            reversion_min_samples=state.get('reversion_min_samples', 0),
            reversion_recency_mode=state.get('reversion_recency_mode', 'hard'))
        return self._policy_command(command, policy)

    def _start_route_fallback(self, state, run_events, log_handle):
        """Advance to another read-only-approved route before any submit.

        This is intentionally conservative: an explicit submit-intent or any
        entry receipt blocks fallback because the first child may have sent an
        ambiguous request.  Only a clean pre-submit exit can move to the next
        route, and the route index is persisted before spawning the child.
        """
        candidates = state.get('route_candidates') or []
        index = int(state.get('route_index', 0) or 0)
        if self.stop_path.exists() or state.get('close_only'):
            return None
        if index + 1 >= len(candidates):
            return None
        if any(event.get('event') in ('entry_submit_intent', 'entry_receipt', 'hedged')
               for event in run_events):
            return None
        if self._pair_has_pending_state(state.get('venues') or []):
            return None
        next_item = candidates[index + 1]
        next_venues = list(next_item.get('venues') or [])
        if len(next_venues) != 2:
            return None
        next_state = {**state, 'state': 'starting', 'venues': next_venues,
                      'route_index': index + 1,
                      'phase': '切换下一条已核验路线',
                      'note': '首腿提交前上一条路线未通过；顺延到 ' + ' ↔ '.join(next_venues) +
                             '；仍未提交订单'}
        self._save(next_state)
        next_log = None
        try:
            command = self._route_command(next_state, next_venues)
            next_log = self.log_path.open('a', encoding='utf-8', buffering=1)
            process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                       stdout=next_log, stderr=subprocess.STDOUT)
        except Exception:
            if next_log is not None:
                next_log.close()
            self._save({**next_state, 'state': 'failed',
                        'note': '下一条候选路线进程未能启动；未自动重试'})
            return None
        pid = process.pid if type(process.pid) is int else None
        running_state = {**next_state, 'state': 'running', **({'pid': pid} if pid else {})}
        self._save(running_state)
        self.diagnostics.record('EXECUTION_ROUTE_FALLBACK')
        self.process = process
        self.process_log = next_log
        threading.Thread(target=self._watch, args=(process, running_state, next_log), daemon=True).start()
        return running_state

    def _watch(self, process, state, log_handle):
        try:
            code = process.wait()
            saved_state = None
            with self.lock:
                latest = None
                try:
                    from src.execution.history import TradeHistory
                    events = TradeHistory(self.root/'data/hedge_events.sqlite').recent(200)
                    started_at = str(state.get('started_at') or '')
                    # Do not attach an event from a previous run to a fresh
                    # process exit.  The old behavior made a new startup
                    # failure look like the stale 9/29 RFQ rejection.
                    run_events = [event for event in events
                                  if not started_at or str(event.get('timestamp') or '') >= started_at]
                    latest = run_events[0] if run_events else None
                    fallback_state = self._start_route_fallback(state, run_events, log_handle)
                    if fallback_state:
                        return
                except Exception:
                    run_events = []
                    latest = None
                entry_filled = any(event.get('event') == 'entry_receipt' and
                                   float((event.get('detail') or {}).get('filled_size') or 0) > 0
                                   for event in run_events)
                hedge_completed = any(event.get('event') == 'hedged' for event in run_events)
                close_filled = any(event.get('event') == 'close_receipt' and
                                   float((event.get('detail') or {}).get('filled_size') or 0) > 0
                                   for event in run_events)
                hedge_skip = next((event for event in run_events
                                   if event.get('event') == 'hedge_leg_skipped'), None)
                stop_completed = bool(code == 0 and latest and latest.get('event') == 'execution_stop_completed')
                stop_requires_reconcile = bool(self.stop_path.exists() and not stop_completed)
                if stop_completed:
                    note = '全局实盘已停止；工作进程已退出，停止前已有仓位已确认结束。停止标记保留，重新启动前需核对账户并解除停止。'
                elif latest and latest.get('event') == 'execution_stop_requires_reconcile':
                    note = '执行进程已停止；仓位或订单仍需核对，停止保护已保留。点击“核对原仓位（只读）”后重新检查。'
                elif code == 0 and entry_filled and close_filled and not hedge_completed:
                    detail = hedge_skip.get('detail') if hedge_skip else {}
                    reason = detail.get('reason') if isinstance(detail, dict) else None
                    suffix = ('；第二腿保护拦截：' + str(reason)) if reason else '；第二腿未成对'
                    note = '首腿曾成交' + suffix + '，已回补首腿；当前未确认保留持仓，请以账户核对为准'
                elif code == 0 and hedge_completed and state.get('mode') in ('once', 'wait_once'):
                    note = ('单次验证已完成一轮开仓与平仓；进程按单次模式正常结束'
                            if close_filled else
                            '单次验证已完成双腿开仓，但平仓尚未确认；请核对当前持仓')
                elif code == 0 and self.stop_path.exists():
                    note = '已检测到停止标志，未继续新开仓；实际成交与持仓仍需账户核对'
                elif code == 0 and latest and latest.get('event') == 'wait_once_skipped':
                    reason = latest.get('detail', {}).get('reason', '本轮报价未达到条件')
                    note = '等待结束；最后一轮未达标：' + reason + '；未确认成交'
                elif latest and latest.get('event') == 'accounting_pending':
                    detail = latest.get('detail') or {}
                    venue = detail.get('venue') or '首腿平台'
                    reason = detail.get('reason') or '成交费用回执未知，第二腿未继续提交'
                    note = ('首腿已确认成交（' + str(venue) + '），第二腿未提交或未确认；'
                            + str(reason) + '。已暂停，必须先核对两边账户，不能据此判断已平仓')
                elif code == 0:
                    if self.stop_path.exists():
                        note = '全局实盘已停止；新开仓已停止，已有仓位已按平仓监控处理。实际成交与持仓仍以执行历史和账户核对为准'
                    else:
                        note = '进程已退出；实际成交与持仓以执行历史和账户核对为准，不表示已平仓'
                else:
                    note = '执行进程异常退出；未确认成交，请查看当前进度和账户状态'
                saved_state = {**state, 'state':'finished' if code == 0 else 'failed', 'finished_at':utc_now(),
                               'exit_code': code, 'latest_event': latest, 'note': note,
                               'stop_completed': stop_completed,
                               'stop_requires_reconcile': stop_requires_reconcile}
                self._save(saved_state)
                self.diagnostics.record('EXECUTION_FINISHED' if code == 0 else 'EXECUTION_FAILED')
                self.process = None
                self.process_log = None
            if saved_state:
                self._maybe_start_auto_reconcile(saved_state)
        finally:
            log_handle.close()

    def request_stop(self):
        # Never terminate between the two hedge legs or erase an uncertain send.
        # The worker treats this marker as a graceful drain request: no new
        # entries, but existing positions continue through the close monitor.
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        from src.execution.reset_stop import stop_guard
        with stop_guard(self.root):
            self.stop_path.touch(mode=0o600, exist_ok=True)
        self.diagnostics.record('EXECUTION_STOP_REQUESTED')
        return {'success': True, 'message': '已请求停止全局实盘：停止新开仓，已有仓位继续监控平仓；不会中断正在核对的订单'}

    def request_manual_close(self, position_id, symbol, venues):
        from src.execution.manual_close import request_close
        try:
            return request_close(self, position_id, symbol, venues)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return dict(success=False, orders_submitted=False,
                        message=str(exc) if type(exc) is ValueError else '双平请求未写入，请刷新并核对当前监控状态')

    def request_resume(self, pid, started_at, stop_generation):
        """Explicitly resume this run; never launch a replacement worker."""
        from src.execution.reset_stop import stop_guard
        from src.execution.running_resume import resume_control, stop_generation as generation
        with self.lock, stop_guard(self.root):
            current = self.status()
            control = resume_control(self, current)
            if not control['available']:
                return {'success': False, 'message': control['reason']}
            if (type(pid) is not int or pid != control['pid'] or
                    started_at != control['started_at'] or
                    stop_generation != control['stop_generation']):
                return {'success': False, 'message': '运行进程或停止请求已变化，请刷新状态后重试'}
            if not self._worker_alive(current):
                return {'success': False, 'message': '原工作进程已结束，请核对账户后重新启动'}
            # A concurrent stop holds the same file lock. A newer stop must
            # win over an old browser click, including one from another tab.
            try:
                if generation(self.stop_path) != stop_generation:
                    return {'success': False, 'message': '停止请求已变化，请刷新状态后重试'}
                self.stop_path.unlink()
            except OSError:
                return {'success': False, 'message': '停止标记正在更新，请刷新状态后重试'}
        self.diagnostics.record('EXECUTION_ENTRIES_RESUMED')
        return {'success': True, 'pid': pid, 'venues': current['venues'],
                'message': '已恢复原运行范围的新开仓；已有仓位继续监控，后续入场按已保存持仓上限及实时检查执行'}

    def request_scope(self, pid, started_at, venues, symbols):
        from uuid import uuid4
        from src.execution.live_scope import validate_scope, write_scope_request
        from src.execution.position_monitor import read_position_monitor
        try:
            venues, symbols = validate_scope(venues, symbols)
        except (ValueError, TypeError):
            return {'success':False, 'message':'新仓范围无效：请选择至少两个支持的签名平台和有效共同品种'}
        with self.lock:
            current = self.status()
            if (not current.get('running') or current.get('mode') != 'continuous_monitor' or
                    type(pid) is not int or pid != current.get('pid') or started_at != current.get('started_at')):
                return {'success':False, 'message':'原进程已变化或不支持持续执行，请刷新状态后重试'}
            if current.get('close_only'):
                return {'success':False, 'message':'本次只管理已有仓位；调整平台不会授权新开仓'}
            path = self._pair_state_path(current.get('venues'))
            monitor = read_position_monitor(path.with_suffix('.monitor.json'), pid)
            if not monitor or monitor.get('heartbeat_stale'):
                return {'success':False, 'message':'持仓监控心跳暂未核验，未切换新仓范围'}
            if (monitor.get('runtime_scope') or {}).get('version') != 1:
                return {'success':False, 'message':'当前旧进程不支持带仓切换平台；原仓继续监控，新功能在下一次启动实盘时加载'}
            if monitor.get('stop_reason'):
                return {'success':False, 'message':'当前持仓待核对，未切换新仓范围'}
            request_id = uuid4().hex
            write_scope_request(path.with_suffix('.scope-request.json'), dict(id=request_id,
                pid=pid, worker_started_at=monitor['started_at'], requested_at=utc_now(),
                venues=venues, symbols=symbols))
        return {'success':True, 'request_id':request_id,
                'message':'正在后台核验新仓平台；通过后用于后续新仓，旧仓仍在原平台管理，停止状态保持不变'}

    def reset_stop(self):
        current = self.status()
        recorded = current.get('execution_pair') or self._execution_pair(current) or []
        if self.stop_path.exists() and len(recorded) == 2:
            return self.reconcile_pair(recorded)
        with self.lock:
            if self.status()['running']:
                return {'success':False,'orders_submitted':False,'message':'执行仍在运行或状态未知，不能解除停止。'}
            try:
                result=subprocess.run([str(execution_python(self.root)),'-B',
                    str(self.root/'scripts/reset_execution_stop.py')],cwd=self.root,
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                    text=True,timeout=45)
                report=json.loads(result.stdout)
                if result.returncode or report.get('orders_submitted') is not False:
                    raise ValueError('invalid_reset_report')
            except (subprocess.TimeoutExpired,ValueError,OSError):
                return {'success':False,'orders_submitted':False,'message':'解除检查未完成；请刷新停止状态，核对账户后重试只读检查。'}
            self.diagnostics.record('EXECUTION_STOP_RESET' if report.get('success') else 'EXECUTION_STOP_RESET_BLOCKED')
        return report

    def reconcile_pair(self, venues):
        """Read-only recovery for a signed pair after a one-leg interruption."""
        with self.lock:
            previous = self.status()
            if previous['running']:
                return {'success': False, 'orders_submitted': False,
                        'message': '执行仍在运行，不能进行核对。'}
            values = [str(item).strip() for item in (venues or []) if str(item).strip()]
            # A global stop belongs to the last worker's accounts. Scanner
            # changes must not redirect its account check to a new venue.
            recorded = previous.get('execution_pair') or self._execution_pair(previous)
            recorded_scope = self._recovery_scope(recorded or [], previous)
            if (self.stop_path.exists() or len(recorded_scope) > 2) and len(recorded or []) == 2:
                values = list(recorded)
            if len(values) != 2 or len(set(values)) != 2:
                return {'success': False, 'orders_submitted': False,
                        'message': '请选择两个平台后再核对。'}
            try:
                command = [
                    str(execution_python(self.root)), '-B',
                    str(self.root/'scripts/reconcile_pair_state.py'),
                    '--venues', ','.join(values)]
                scope = self._recovery_scope(values, previous)
                if scope != values:
                    command += ['--state-venues', ','.join(scope)]
                result = subprocess.run(command, cwd=self.root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, timeout=60)
                report = json.loads(result.stdout)
                report['venues'] = values
                if result.returncode or report.get('orders_submitted') is not False:
                    raise ValueError('invalid_reconcile_report')
                self.diagnostics.record('EXECUTION_PAIR_RECONCILED' if report.get('success')
                                       else 'EXECUTION_PAIR_RECONCILE_BLOCKED')
                return report
            except (subprocess.TimeoutExpired, ValueError, OSError):
                return {'success': False, 'orders_submitted': False,
                        'message': '只读核对超时或失败；本地停机状态保留，未发送订单。'}

    def platform_history(self, venues, symbols, limit=100):
        """Serialize manual/background evidence reads outside the trade loop."""
        if not self._history_guard.acquire(blocking=False):
            return {'success': False, 'orders_submitted': False,
                    'message': '平台历史正在补查，请等待当前查询完成。'}
        try:
            return self._platform_history_read(venues, symbols, limit)
        finally:
            self._history_guard.release()

    def _platform_history_read(self, venues, symbols, limit=100):
        values = [str(item).strip() for item in (venues or []) if str(item).strip()]
        symbols = list(dict.fromkeys(str(item).strip().upper().replace('-PERP', '')
                                     for item in (symbols or []) if str(item).strip()))
        if len(values) != 2 or len(set(values)) != 2 or any(v not in ('lighter', 'lighter_rh', 'arcus') for v in values):
            return {'success': False, 'orders_submitted': False,
                    'message': '平台历史支持 Lighter 主网、Lighter RH 与 Arcus 的两平台组合。'}
        if not symbols or len(symbols) > 20:
            return {'success': False, 'orders_submitted': False,
                    'message': '平台历史查询请选择 1 到 20 个品种。'}
        try:
            result = subprocess.run([
                str(execution_python(self.root)), '-B',
                str(self.root/'scripts/query_platform_history.py'),
                '--venues', ','.join(values), '--symbols', ','.join(symbols),
                '--limit', str(max(1, min(int(limit), 100)))], cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=75)
            report = json.loads(result.stdout or '{}')
            if result.returncode or report.get('orders_submitted') is not False:
                raise ValueError('invalid_platform_history_report')
            self.diagnostics.record('PLATFORM_HISTORY_READ')
            return report
        except (subprocess.TimeoutExpired, ValueError, OSError, TypeError):
            return {'success': False, 'orders_submitted': False,
                    'message': '平台成交历史读取超时或失败；未发送订单。'}
