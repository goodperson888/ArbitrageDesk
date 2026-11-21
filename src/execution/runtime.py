"""Explicit runtime assembly. Importing this module never accesses an account."""
import json
import math
import os
import hashlib
import sqlite3
from pathlib import Path
from src.execution.journal import OrderJournal
from src.execution.worker import ExecutionWorker
from src.common.config_loader import ConfigLoader
from src.lighter.api_trader import LighterApiTrader
from src.lighter.execution import LighterExecutionAdapter, OfficialSDKTransport, SDKLoop
from src.variational.execution import OmniForm, VariationalExecutionAdapter
from src.variational.receipt_reader import OmniResponseReader
from src.variational.public_client import VariationalPublicClient
from src.strategy.arbitrage_strategy import ArbitrageStrategy
from src.execution.browser_session import (select_market_tab, valid_debugger_address,
                                            attach_browser, background_market_window)
from src.common.market_data import age_seconds
from src.execution.position_recovery import resume_proof_valid


def pair_runtime_blockers(profile, *, recover_at_start=False):
    """Validate a non-browser pair before opening any signer session."""
    blockers = []
    if profile.get('live_enabled') is not True:
        blockers.append('执行配置默认关闭')
    venues = profile.get('venues') or []
    if len(venues) < 2 or len(set(venues)) != len(venues):
        blockers.append('实盘执行至少需要选择两个不同平台')
        return blockers
    if 'variational' in venues:
        blockers.append('Variational 仍使用独立浏览器验收路径，不能混入通用签名路径')
    if any(venue not in ('lighter', 'lighter_rh', 'standx', 'arcus') for venue in venues):
        blockers.append('所选平台尚无通用签名执行器')
    symbols = profile.get('symbols') or ([profile.get('symbol')] if profile.get('symbol') else [])
    if not symbols:
        blockers.append('没有可执行的共同品种')
    elif len(symbols) > 2000:
        blockers.append('执行品种过多，请缩小扫描范围')
    elif any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
        blockers.append('执行品种列表无效')
    import json as _json
    from pathlib import Path as _Path
    from dotenv import dotenv_values
    evidence = {}
    root = _Path(__file__).resolve().parents[2]
    path = root / 'data/platform-verification.json'
    # A previous attempt may have filled only one leg. The worker persists
    # that state per venue pair and deliberately halts; surface the halt
    # during the read-only check so the UI cannot compound the exposure.
    pair_state_path = root / ('data/hedge_state_' + '_'.join(venues) + '.json')
    try:
        pair_state = _json.loads(pair_state_path.read_text())
        open_positions = [item for item in pair_state.get('positions', [])
                          if item.get('status') != 'closed']
        # The dashboard can check the whole three-venue scope. Its proof
        # covers the portfolio, while each held position still has two legs.
        # Validate using a persisted held pair, never treat all scan venues
        # as the legs of one position.
        proof_venues = venues
        if len(venues) > 2 and open_positions and (pair_state.get('resume_proof') or {}).get('portfolio') is True:
            held = open_positions[0]
            proof_venues = [held.get('variational_venue'), held.get('lighter_venue')]
        resume_verified = resume_proof_valid(pair_state, proof_venues)
        if (pair_state.get('halted_reason') or open_positions) and not resume_verified and not recover_at_start:
            reason = pair_state.get('halted_reason') or '存在未完成持仓'
            blockers.append('该平台组合有未核对成交/持仓：' + str(reason))
    except (OSError, ValueError, TypeError):
        # Missing or malformed state is handled by the normal checks; never
        # infer that an account is flat from a failed read.
        pass
    core_profile = {}
    try:
        core_profile = _json.loads((_Path(__file__).resolve().parents[2] / 'config/variational-execution.local.json').read_text())
    except (OSError, ValueError):
        pass
    env = dict(os.environ)
    # The web process may have loaded an older .env before the user replaced
    # a credential.  The on-disk file is authoritative for this local check.
    env.update({key: value for key, value in dotenv_values(_Path(__file__).resolve().parents[2] / '.env').items()
                if value is not None})
    try:
        evidence = _json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    for venue in venues:
        if venue == 'lighter':
            prefix = 'LIGHTER_'
            required = ('LIGHTER_ACCOUNT_INDEX', 'LIGHTER_API_KEY_INDEX', 'LIGHTER_API_PRIVATE_KEY')
        elif venue == 'lighter_rh':
            prefix = 'LIGHTER_RH_'
            required = ('LIGHTER_RH_ACCOUNT_INDEX', 'LIGHTER_RH_API_KEY_INDEX', 'LIGHTER_RH_API_PRIVATE_KEY')
        elif venue == 'arcus':
            prefix = 'ARCUS_'
            required = ('ARCUS_WALLET_ADDRESS', 'ARCUS_ACCOUNT_INDEX', 'ARCUS_API_SIGNING_KEY')
        else:
            prefix = 'STANDX_'
            required = ('STANDX_WALLET_ADDRESS', 'STANDX_WALLET_PRIVATE_KEY', 'STANDX_CHAIN')
        for key in required:
            if not env.get(key):
                blockers.append(key + ' 未配置')
        if venue == 'lighter':
            # Core Lighter credentials use the existing local verifier and
            # store evidence in the execution profile rather than the
            # multi-platform evidence file.
            if core_profile.get('lighter_fee_verified') is not True:
                blockers.append('lighter 尚未通过本机只读鉴权')
            if not isinstance(core_profile.get('lighter_fee_bps'), (int, float)):
                blockers.append('lighter 当前账户费率未核验，不能按零费用下单')
            core_age = age_seconds(core_profile.get('lighter_fee_checked_at'))
            key = str(env.get('LIGHTER_API_PRIVATE_KEY') or '').removeprefix('0x')
            bound = (str(core_profile.get('lighter_fee_account_index')) == str(env.get('LIGHTER_ACCOUNT_INDEX')) and
                     str(core_profile.get('lighter_fee_key_index')) == str(env.get('LIGHTER_API_KEY_INDEX')) and
                     core_profile.get('lighter_fee_key_digest') == __import__('hashlib').sha256(key.encode()).hexdigest())
            if core_age is None or not 0 <= core_age <= 300 or not bound:
                blockers.append('lighter 费率证据已过期或与当前密钥不匹配，请重新验证')
            continue
        report = evidence.get(venue, {}) if isinstance(evidence, dict) else {}
        if report.get('authenticated') is not True:
            blockers.append(venue + ' 尚未通过本机只读鉴权')
        if not isinstance(report.get('taker_fee_bps'), (int, float)):
            blockers.append(venue + ' 当前账户费率未核验，不能按零费用下单')
        fee_age = age_seconds(report.get('checked_at'))
        if fee_age is None or not 0 <= fee_age <= 300:
            blockers.append(venue + ' 费率证据已过期，请重新验证密钥')
    return list(dict.fromkeys(blockers))


def runtime_blockers(profile, *, require_enabled=True, recover_at_start=False):
    if profile.get('venues'):
        return pair_runtime_blockers(profile, recover_at_start=recover_at_start) if require_enabled else [
            item for item in pair_runtime_blockers({**profile, 'live_enabled': True}, recover_at_start=recover_at_start)
            if item != '执行配置默认关闭']
    blockers = []
    if require_enabled and profile.get('live_enabled') is not True:
        blockers.append('执行配置默认关闭')
    if not profile.get('account_verified'):
        blockers.append('Variational 登录账户绑定未核对')
    if not profile.get('receipt_schema_verified'):
        blockers.append('Variational 执行回执解析尚未完成验收')
    symbol = profile.get('symbol')
    if symbol not in ('BTC', 'ETH') or symbol not in profile.get('size_decimals', {}):
        blockers.append('仅支持已验收精度的单一 BTC/ETH 市场')
    if not profile.get('account_id') or not all(profile.get('submit_labels', {}).get(s) for s in ('buy', 'sell')):
        blockers.append('账户身份或登录态按钮文案缺失')
    schema = profile.get('response_schema', {})
    required_quote = ('account_id', 'symbol', 'size', 'quote_id', 'bid', 'ask', 'mark_price', 'fee_bps')
    required_account = ('account_id', 'available_balance', 'positions', 'pending_orders')
    required_receipt = ('account_id', 'symbol', 'side', 'quote_id', 'requested_size', 'reduce_only',
                        'order_id', 'status', 'filled_size', 'average_price', 'fees_usdc', 'funding_usdc')
    if schema.get('adapter') == 'omni_v1':
        import re
        if not re.fullmatch(r'[0-9a-f]{16}:[0-9a-f]{16}', profile.get('account_id') or ''):
            blockers.append('Variational 账户/资金池指纹尚未绑定')
        if schema.get('account_session_verified') is not True:
            blockers.append('当前登录会话账户快照未核对')
        if schema.get('executable_quote_verified') is not True:
            blockers.append('数量绑定报价、买卖表单及滑点限制待核对')
        from src.variational.omni_contract import zero_trade_fee_policy
        if not zero_trade_fee_policy(schema.get('fee_policy')):
            blockers.append('Variational 官方零交易费口径未核验或已超过 30 天，暂不允许提交')
        if profile.get('funding_accounting_mode') not in ('reconcile_after_close', 'defer_settlement'):
            blockers.append('资金费结算字段仍缺失，尚未配置平仓后待核账停机策略')
    else:
        for group, fields in (('quote_fields', required_quote), ('account_fields', required_account), ('receipt_fields', required_receipt)):
            if not all(isinstance(schema.get(group, {}).get(k), str) and schema[group][k] for k in fields):
                blockers.append(group+' 登录字段路径未验收')
        if not schema.get('executable_quote_verified') or not schema.get('terminal_statuses') or not schema.get('receipt_collection'):
            blockers.append('RFQ 执行资格或终态映射未验收')
        if not all(isinstance(schema.get(k), str) and schema[k].startswith('/api/') for k in ('quote_endpoint','account_endpoint','receipt_endpoint')):
            blockers.append('官网账户/报价/成交观察路径未验收')
    if profile.get('lighter_fee_verified') is not True or profile.get('lighter_fee_bps') != 0:
        blockers.append('当前实现仅允许已核验零交易费的 Lighter 账户；非零费用单位待验收')
    else:
        fee_age = age_seconds(profile.get('lighter_fee_checked_at'))
        key = os.getenv('LIGHTER_API_PRIVATE_KEY', '').strip().removeprefix('0x')
        bound = (str(profile.get('lighter_fee_account_index')) == os.getenv('LIGHTER_ACCOUNT_INDEX') and
                 str(profile.get('lighter_fee_key_index')) == os.getenv('LIGHTER_API_KEY_INDEX') and
                 profile.get('lighter_fee_key_digest') == hashlib.sha256(key.encode()).hexdigest())
        if not bound or fee_age is None or not 0 <= fee_age <= 300:
            blockers.append('Lighter 费率证据过期或与当前密钥账户不匹配；请执行只读复查')
    for key in ('LIGHTER_API_PRIVATE_KEY','LIGHTER_ACCOUNT_INDEX','LIGHTER_API_KEY_INDEX'):
        if not os.getenv(key) or os.getenv(key).startswith('your_'):
            blockers.append(key+' 未配置（不要在聊天发送私钥）')
    if not valid_debugger_address(profile.get('browser_debugger_address')):
        blockers.append('需要用户独立启动、仅绑定本机的已登录浏览器调试会话')
    return blockers


def run_runtime(config_path, profile_path, once=False, wait_once=False, max_notional=None,
                expected_symbol=None, arm_once=False, venues=None, symbols=None,
                target_notional=0, candidate_fallback=True,
                single_position_cycle=True, gross_spread_min_bps=0,
                gross_spread_max_bps=0, close_only=False,
                reversion_target_mode='center', reversion_window_seconds=604800,
                reversion_min_count=0, reversion_min_deviation_bps=0,
                reversion_max_age_seconds=0, reversion_min_samples=0, reversion_recency_mode='hard',
                follow_saved_cap=False):
    if not isinstance(follow_saved_cap, bool):
        raise ValueError('金额跟随设置开关无效')
    try:
        target_notional = float(target_notional or 0)
    except (TypeError, ValueError):
        raise ValueError('目标累计金额无效')
    if target_notional < 0 or not math.isfinite(target_notional):
        raise ValueError('目标累计金额必须为 0 或正数')
    try:
        gross_spread_min_bps = float(gross_spread_min_bps or 0)
        gross_spread_max_bps = float(gross_spread_max_bps or 0)
    except (TypeError, ValueError):
        raise ValueError('毛价差筛选无效')
    if (not math.isfinite(gross_spread_min_bps) or gross_spread_min_bps < 0 or
            not math.isfinite(gross_spread_max_bps) or gross_spread_max_bps < 0 or
            (gross_spread_max_bps and gross_spread_max_bps < gross_spread_min_bps)):
        raise ValueError('毛价差筛选必须为正数，且上限不能低于下限')
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
    if (not 300 <= reversion_window_seconds <= 7776000 or reversion_min_count < 0 or
            not math.isfinite(reversion_min_deviation_bps) or reversion_min_deviation_bps < 0 or
            not math.isfinite(reversion_max_age_seconds) or reversion_max_age_seconds < 0 or
            reversion_min_samples < 0):
        raise ValueError('历史回归筛选必须为非负数，窗口应在 5 分钟至 90 天')
    if target_notional and max_notional is not None and target_notional + 1e-9 < float(max_notional):
        raise ValueError('目标累计金额不能小于每腿上限')
    if not isinstance(close_only, bool):
        raise ValueError('只平仓开关无效')
    config = ConfigLoader(config_path)
    profile = json.loads(Path(profile_path).read_text())
    if once and wait_once:
        raise ValueError('不能同时选择立即单次和等待单次执行')
    if arm_once:
        if not (once or wait_once):
            raise ValueError('临时授权只允许单次或等待单次执行')
        profile['live_enabled'] = True
    if venues:
        profile['venues'] = list(venues)
        # Generic pairs are opt-in per process; the saved profile remains
        # fail-closed so a restart cannot silently resume trading.
        profile['live_enabled'] = True
    if symbols:
        profile['symbols'] = list(dict.fromkeys(symbols))
        profile['symbol'] = profile['symbols'][0]
    # Actual starts defer only the persisted-position check to the locked,
    # mandatory account recovery below. Credential, fee and risk gates still
    # run here; a legacy pending row cannot bypass the startup account read.
    blockers = runtime_blockers(profile, recover_at_start=True)
    if blockers:
        raise ValueError('；'.join(blockers))
    # ``ALL`` is the explicit full-catalog marker from the web console.  The
    # profile still stores the first symbol for backwards compatibility, so
    # comparing ALL to that first symbol would reject every full-scan launch.
    if expected_symbol not in (None, 'ALL') and profile.get('symbol') != expected_symbol:
        raise ValueError('执行配置市场与命令指定市场不符')
    if max_notional is not None:
        from src.execution.orders import decimal
        cap = decimal(max_notional, True)
        configured = decimal(config.get('strategy.trading.position_size'), True)
        if cap > configured:
            raise ValueError('命令不能提高已保存的每腿上限')
        if not follow_saved_cap:
            config.config['strategy']['execution_notional_cap'] = float(cap)
    # Lock covers account session construction, reconciliation and execution.
    from src.execution.journal import execution_lease
    root = Path(config_path).resolve().parent.parent
    with execution_lease(root/'data/runtime.lock'):
        if profile.get('venues'):
            return _run_pair_verified(config, profile, root, once=once, wait_once=wait_once,
                                      target_notional=target_notional,
                                      candidate_fallback=candidate_fallback,
                                      single_position_cycle=single_position_cycle,
                                      gross_spread_min_bps=gross_spread_min_bps,
                                      gross_spread_max_bps=gross_spread_max_bps,
                                      close_only=close_only,
                                      reversion_target_mode=reversion_target_mode,
                                      reversion_window_seconds=reversion_window_seconds,
                                      reversion_min_count=reversion_min_count,
                                      reversion_min_deviation_bps=reversion_min_deviation_bps,
                                      reversion_recency_mode=reversion_recency_mode,
                                      reversion_max_age_seconds=reversion_max_age_seconds,
                                      reversion_min_samples=reversion_min_samples)
        return _run_verified(config, profile, root, once=once, wait_once=wait_once,
                             target_notional=target_notional,
                             candidate_fallback=candidate_fallback,
                             single_position_cycle=single_position_cycle,
                             gross_spread_min_bps=gross_spread_min_bps,
                             gross_spread_max_bps=gross_spread_max_bps,
                             close_only=close_only,
                             reversion_target_mode=reversion_target_mode,
                             reversion_window_seconds=reversion_window_seconds,
                             reversion_min_count=reversion_min_count,
                             reversion_min_deviation_bps=reversion_min_deviation_bps,
                             reversion_recency_mode=reversion_recency_mode,
                             reversion_max_age_seconds=reversion_max_age_seconds,
                             reversion_min_samples=reversion_min_samples)


def _close_history_statistics(strategy):
    history = getattr(strategy, 'market_history', None)
    if isinstance(getattr(history, 'path', None), (str, Path)):
        from src.api_v2.center_statistics import close_service
        close_service(history)


def _run_pair_verified(config, profile, root, once=False, wait_once=False,
                       target_notional=0, candidate_fallback=True,
                       single_position_cycle=True, gross_spread_min_bps=0,
                       gross_spread_max_bps=0, close_only=False,
                       reversion_target_mode='center', reversion_window_seconds=604800,
                       reversion_min_count=0, reversion_min_deviation_bps=0,
                       reversion_max_age_seconds=0, reversion_min_samples=0,
                       plan_only=False, reversion_recency_mode='hard'):
    """Run two independent signed venues through the same hedge coordinator."""
    from dotenv import dotenv_values
    from src.execution.platform_clients import LighterCredentials, build_lighter_session
    from src.standx.client import StandXClient, StandXCredentials
    from src.standx.execution import StandXExecutionAdapter
    from src.arcus.execution import ArcusClient, ArcusExecutionAdapter
    values = dotenv_values(root / '.env')
    venues = profile['venues']
    symbols = profile.get('symbols') or [profile['symbol']]
    journal = OrderJournal(root / 'data/execution_orders.sqlite')
    clients, adapters, lighter_adapters, started_feeds = [], [], [], []
    position_monitor = None
    scope_controller = None
    strategy = None
    try:
        evidence = json.loads((root / 'data/platform-verification.json').read_text())
        for venue in venues:
            # Mainnet Lighter keeps its account-bound fee proof in the core
            # execution profile; RH and StandX use the multi-platform report.
            # Reading every venue from the latter caused the first Lighter↔RH
            # start to exit with a KeyError because mainnet has no report row.
            fee_value = (profile.get('lighter_fee_bps') if venue == 'lighter'
                         else evidence.get(venue, {}).get('taker_fee_bps'))
            if not isinstance(fee_value, (int, float)):
                raise ValueError(venue + ' 费率证据缺失')
            fee_bps = float(fee_value)
            if venue in ('lighter', 'lighter_rh'):
                prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
                credentials = LighterCredentials(int(values[prefix + 'ACCOUNT_INDEX']),
                    int(values[prefix + 'API_KEY_INDEX']), values[prefix + 'API_PRIVATE_KEY'])
                public, sdk = build_lighter_session(venue, credentials, config.get('common.network.proxy_url'))
                adapter = LighterExecutionAdapter(public, sdk, journal, credentials.account_index,
                                                   enabled=True, venue=venue)
                adapter.verified_fee_bps = fee_bps
                clients.extend([public, sdk]); adapters.append(adapter)
                lighter_adapters.append(adapter)
            elif venue == 'standx':
                client = StandXClient(StandXCredentials(values['STANDX_WALLET_ADDRESS'],
                    values['STANDX_WALLET_PRIVATE_KEY'], values.get('STANDX_CHAIN', 'bsc')))
                client.authenticate()
                adapter = StandXExecutionAdapter(client, journal, values['STANDX_WALLET_ADDRESS'], enabled=True)
                adapter.verified_fee_bps = fee_bps
                clients.append(client); adapters.append(adapter)
            elif venue == 'arcus':
                client = ArcusClient(
                    values['ARCUS_WALLET_ADDRESS'], int(values['ARCUS_ACCOUNT_INDEX']),
                    values['ARCUS_API_SIGNING_KEY'],
                    base_url=config.get('arcus.api.base_url', 'https://api.arcus.xyz'),
                    ws_url=config.get('arcus.api.ws_url', 'wss://api.arcus.xyz/v1/ws'),
                    proxy_url=config.get('arcus.api.proxy_url', config.get('common.network.proxy_url')),
                    order_timeout=config.get('arcus.execution.order_timeout_seconds', 15),
                )
                adapter = ArcusExecutionAdapter(client, journal, values['ARCUS_WALLET_ADDRESS'],
                                                enabled=True, fee_bps=fee_bps)
                clients.append(client); adapters.append(adapter)
        anchor_path = root / ('data/hedge_state_' + '_'.join(venues) + '.json')
        saved_scope = {}
        if anchor_path.exists():
            saved = json.loads(anchor_path.read_text())
            if any(p.get('status') != 'closed' for p in saved.get('positions', [])):
                saved_scope = saved.get('runtime_scope') or {}
        if saved_scope.get('version') == 1:
            from src.execution.live_scope import validate_scope
            from src.execution.scope_preparation import build_scope_adapter
            managed, _ = validate_scope(saved_scope.get('managed_venues'), symbols)
            for venue in managed:
                if venue not in {adapter.venue for adapter in adapters}:
                    adapter, handles = build_scope_adapter(root, venue, journal)
                    adapters.append(adapter); clients.extend(handles)
        settings = dict(config.get_strategy_config())
        settings['config_path'] = str(config.config_path)
        settings.update(mode='live', state_path=str(root / ('data/hedge_state_' + '_'.join(venues) + '.json')),
                        execution_filters={'gross_min_bps': gross_spread_min_bps,
                                           'gross_max_bps': gross_spread_max_bps or None,
                                           'reversion_target_mode': reversion_target_mode,
                                           'reversion_window_seconds': reversion_window_seconds,
                                           'reversion_min_count': reversion_min_count,
                                           'reversion_min_deviation_bps': reversion_min_deviation_bps,
                                           'reversion_recency_mode': reversion_recency_mode,
                                           'reversion_max_age_seconds': reversion_max_age_seconds,
                                           'reversion_min_samples': reversion_min_samples})
        recovery_report = None
        state_path = Path(settings['state_path'])
        if not plan_only and state_path.exists():
            saved_state = json.loads(state_path.read_text())
            if saved_state.get('halted_reason') or any(
                    p.get('status') != 'closed' for p in saved_state.get('positions', [])):
                # run_runtime holds the process-wide lease. Re-read accounts
                # at startup, regardless of an earlier dashboard check.
                from src.execution.reconcile_pair import reconcile_pair
                recovery_venues = list(venues[:2])
                active_rows = [row for row in saved_state.get('positions', [])
                               if row.get('status') != 'closed']
                if active_rows:
                    row = active_rows[0]
                    pair = [row.get('variational_venue'), row.get('lighter_venue')]
                    if all(isinstance(item, str) and item for item in pair):
                        recovery_venues = pair
                recovery_report = (reconcile_pair(root, recovery_venues, state_venues=venues)
                                   if len(venues) > 2 or saved_scope.get('version') == 1 else reconcile_pair(root, recovery_venues))
                if recovery_report.get('success') is not True or recovery_report.get('orders_submitted') is not False:
                    raise ValueError('启动前原仓位核对未通过；禁止启动实盘')
                if not recovery_report.get('resume_ready') and recovery_report.get('accounts_flat') is True:
                    from src.execution.reset_stop import reconcile_unknown_orders
                    reconcile_unknown_orders(root, recovery_venues)
        strategy = ArbitrageStrategy(settings, adapters[0], adapters[1])
        if len(adapters) > 2:
            from itertools import combinations
            strategy.configure_routes(list(combinations(adapters, 2)))
        if plan_only:
            if len(symbols) != 1:
                raise ValueError('只读计划复测请选择一个品种')
            result = _preview_strategy_plan(strategy, symbols[0])
            result.update(venues=list(venues), symbol=symbols[0])
            if result.get('ready'):
                buy_first = result['plan']['direction'] == 'buy_variational'
                result['plan'].update(buy_venue=venues[0 if buy_first else 1],
                                      sell_venue=venues[1 if buy_first else 0])
                plan = result['plan']
                plan.update(buy_price=plan['variational_price'] if buy_first else plan['lighter_vwap'],
                            sell_price=plan['lighter_vwap'] if buy_first else plan['variational_price'],
                            slippage_breakdown='买 ' + plan['buy_venue'] + ' 同数量盘口冲击 + 卖 ' +
                                               plan['sell_venue'] + ' 同数量盘口冲击')
            return result
        held_positions = strategy.positions if isinstance(strategy.positions, list) else []
        reference_symbols = list(dict.fromkeys(
            [p.symbol for p in held_positions if p.status != 'closed'] + list(symbols)))
        for adapter in adapters:
            if not callable(getattr(adapter, 'start_reference_feed', None)):
                continue
            started_feeds.append(adapter)
            try:
                adapter.start_reference_feed(reference_symbols)
            except Exception:
                # Public stream setup is optional; REST and all final
                # quote/depth/account guards remain the fallback.
                adapter.close_reference_feed()
        # A previous process may have been stopped with a live hedge.  The
        # explicit read-only recovery command writes a one-shot marker only
        # after both accounts match that hedge; consume it before management.
        resumed = strategy.resume_verified_position()
        if recovery_report and recovery_report.get('resume_ready') and resumed is not True:
            raise ValueError('原仓位恢复凭据已过期或记录已变化；请重新核对，未发送订单')
        # The resume proof is consumed first. Cost correction never changes
        # exposure and uses only already captured platform history.
        from src.execution.entry_accounting import read_entry_evidence, apply_entry_evidence
        corrected = False
        for position in strategy.positions:
            try:
                corrected |= apply_entry_evidence(position, read_entry_evidence(position, root/'data', strategy.history))
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                pass
        if corrected:
            strategy._save()
        worker = ExecutionWorker(strategy, symbols, root / ('data/execution_' + '_'.join(venues) + '.lock'),
                                 root / 'data/STOP_EXECUTION', live=True,
                                 target_notional=target_notional,
                                 candidate_fallback=candidate_fallback,
                                 single_position_cycle=single_position_cycle,
                                 gross_spread_min_bps=gross_spread_min_bps,
                                 gross_spread_max_bps=gross_spread_max_bps,
                                 close_only=close_only)
        if isinstance(strategy.state_path, Path) and all(callable(getattr(type(adapter), 'get_position_mark_snapshot', None))
                            for adapter in adapters):
            from src.execution.position_monitor import PositionMonitor
            position_monitor = PositionMonitor(strategy)
            strategy.position_monitor = position_monitor
            from src.execution.manual_close import ManualCloseController
            worker.manual_close_controller = ManualCloseController(worker, position_monitor)
            if not once and not wait_once and set(venues) <= {'lighter', 'lighter_rh', 'arcus'}:
                from src.execution.live_scope import LiveScopeController
                from src.execution.scope_preparation import ScopePreparer
                prepare = ScopePreparer(root, journal, adapters, clients, started_feeds, strategy)
                scope_controller = LiveScopeController(worker, position_monitor, venues, prepare, prepare.dispose)
                if saved_scope.get('version') == 1:
                    selected_venues = saved_scope.get('entry_venues', venues)
                    from itertools import combinations
                    strategy.set_entry_routes(list(combinations([prepare.by_venue[v] for v in selected_venues], 2)))
                    scope_controller.status.update(entry_venues=selected_venues,
                        managed_venues=list(prepare.by_venue))
                worker.scope_controller = scope_controller
        return worker.run_until_opportunity() if wait_once else worker.run_once() if once else worker.run()
    finally:
        if scope_controller is not None:
            scope_controller.close()
        for adapter in adapters:
            if callable(getattr(type(adapter), 'stop_account_snapshots', None)):
                adapter.stop_account_snapshots()
        if position_monitor is not None:
            position_monitor.close()
        _close_history_statistics(strategy)
        for adapter in reversed(started_feeds):
            try:
                adapter.close_reference_feed()
            except Exception:
                pass
        for client in reversed(clients):
            try:
                close = getattr(client, 'close', None)
                if close:
                    result = close()
                    if hasattr(result, '__await__'):
                        # SDKLoop.close is synchronous; async clients are not
                        # used by this worker yet.
                        pass
            except Exception:
                pass


def _preview_strategy_plan(strategy, symbol):
    """Quote and size only; never create a worker or alter position state."""
    from src.execution.telemetry import stage
    with stage(strategy.history, '只读计划复测', symbol):
        opportunity = strategy.check_arbitrage_opportunity(symbol)
        if opportunity is None:
            return {'ready': False, 'orders_submitted': False,
                    'message': '未获取可用于测算的行情'}
        plan, _, _, _ = strategy.plan_entry(opportunity)
        return {'ready': True, 'orders_submitted': False, 'plan': plan}


def _run_verified(config, profile, root, once=False, wait_once=False, plan_only=False,
                  target_notional=0, candidate_fallback=True,
                  single_position_cycle=True, gross_spread_min_bps=0,
                  gross_spread_max_bps=0, close_only=False,
                  reversion_target_mode='center', reversion_window_seconds=604800,
                  reversion_min_count=0, reversion_min_deviation_bps=0,
                  reversion_max_age_seconds=0, reversion_min_samples=0, reversion_recency_mode='hard'):
    symbol = profile['symbol']
    driver, sdk, public, v_public = None, None, None, None
    strategy = None
    try:
        from src.execution.history import TradeHistory
        from src.execution.telemetry import stage
        timing_history=TradeHistory(root/'data/hedge_events.sqlite')
        with stage(timing_history,'连接 V 浏览器',symbol):
            # Selenium may activate a tab while attaching/selecting it.  Hide
            # the dedicated Variational window before that operation as well
            # as after it, so a live toggle stays a background operation.
            background_market_window(symbol)
            driver = attach_browser(profile['browser_debugger_address'])
            background_market_window(symbol)
            select_market_tab(driver, symbol)
            # Selenium may activate the selected tab.  Keep the dedicated V
            # window minimized while retaining the live DOM target.  Wallet
            # confirmation dialogs can still require a user-visible window.
            background_market_window(symbol)
            driver.execute_cdp_cmd('Network.enable', {})
        account = int(os.environ['LIGHTER_ACCOUNT_INDEX'])
        key_index = int(os.environ['LIGHTER_API_KEY_INDEX'])
        proxy = config.get('common.network.proxy_url')
        with stage(timing_history,'L 签名器初始化与密钥核验',symbol):
            sdk = SDKLoop(lambda: OfficialSDKTransport.from_credentials(account, key_index,
                os.environ['LIGHTER_API_PRIVATE_KEY'], proxy_url=proxy))
        lighter_config = config.get_lighter_config()
        lighter_config.setdefault('api', {})
        if 'proxy_url' not in lighter_config['api']:
            lighter_config['api']['proxy_url'] = proxy
        public = LighterApiTrader(lighter_config, account_index=account)
        journal = OrderJournal(root/'data/execution_orders.sqlite')
        lighter = LighterExecutionAdapter(public, sdk, journal, account, enabled=True)
        lighter.verified_fee_bps = profile['lighter_fee_bps']
        reader = OmniResponseReader(driver, profile['response_schema'], profile['account_id'])
        v_config = config.get_variational_config()
        v_config.setdefault('api', {})
        if 'proxy_url' not in v_config['api']:
            v_config['api']['proxy_url'] = proxy
        v_public = VariationalPublicClient(v_config)
        variational = VariationalExecutionAdapter(OmniForm(driver), reader, journal,
                                                  profile['account_id'], profile, enabled=True, public=v_public)
        variational.history=timing_history
        settings = dict(config.get_strategy_config())
        settings['funding_accounting_mode'] = profile.get('funding_accounting_mode')
        settings['mode'] = 'live'
        settings['config_path'] = str(config.config_path)
        settings['state_path'] = str(root/'data/hedge_state.json')
        settings['execution_filters'] = {
            'gross_min_bps': gross_spread_min_bps,
            'gross_max_bps': gross_spread_max_bps or None,
            'reversion_target_mode': reversion_target_mode,
            'reversion_window_seconds': reversion_window_seconds,
            'reversion_min_count': reversion_min_count,
            'reversion_min_deviation_bps': reversion_min_deviation_bps,
            'reversion_recency_mode': reversion_recency_mode,
            'reversion_max_age_seconds': reversion_max_age_seconds,
            'reversion_min_samples': reversion_min_samples,
        }
        strategy = ArbitrageStrategy(settings, variational, lighter)
        if plan_only:
            # Explicitly stop before any execution/worker call. No order submit,
            # position creation or strategy state save occurs in this branch.
            return _preview_strategy_plan(strategy, symbol)
        worker = ExecutionWorker(strategy, [symbol], root/'data/execution.lock',
                                 root/'data/STOP_EXECUTION', live=True,
                                 target_notional=target_notional,
                                 candidate_fallback=candidate_fallback,
                                 single_position_cycle=single_position_cycle,
                                 gross_spread_min_bps=gross_spread_min_bps,
                                 gross_spread_max_bps=gross_spread_max_bps,
                                 close_only=close_only)
        if wait_once:
            return worker.run_until_opportunity()
        return worker.run_once() if once else worker.run()
    finally:
        _close_history_statistics(strategy)
        if public:
            public.close()
        if v_public:
            v_public.close()
        if sdk:
            sdk.close()
        # Disconnect the automation service, leave the user's browser open.
        if driver:
            driver.service.stop()
