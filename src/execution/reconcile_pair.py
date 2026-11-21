"""Read-only reconciliation for signed venue pair state.

This module never submits, cancels, or retries an order.  It verifies the
accounts through fresh venue account snapshots. A flat snapshot settles a
stale local hedge; a matching non-flat snapshot authorizes the persisted hedge
to be resumed after a process restart.
"""
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from src.common.config_loader import ConfigLoader
from src.common.market_data import utc_now
from src.lighter.api_trader import LighterApiTrader
from src.execution.platform_clients import deployment_for
from src.lighter.execution import LighterExecutionAdapter, SDKLoop, OfficialSDKTransport
from src.execution.orders import decimal
from src.execution.grid_policy import layer_funding
from src.arcus.execution import ArcusClient
from src.execution.position_recovery import position_fingerprint, validate_paired_positions
from src.execution.close_recovery import reconcile_unsent_closes, validate_resume_closes


def _atomic_json(path, payload):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _settle_flat_position(position, observed_at):
    """Record account-proven closure without inventing missing fill evidence."""
    position['reconciled_at'] = observed_at
    position['reconciliation_note'] = '原平台该品种持仓与活动订单已只读核对为空；历史费用字段仍保留未知'
    position['reconciled_sizes'] = {key: position.get(key, 0)
                                    for key in ('variational_size', 'lighter_size')}
    position.update(variational_size=0, lighter_size=0, status='closed')
    if ((position.get('entry_size_variational') or position.get('entry_size_lighter')) and
            (position.get('close_price_variational') is None or position.get('close_price_lighter') is None)):
        position['accounting_pending'] = True
        position['reconciliation_note'] = '原平台该品种已确认空仓；实际平仓成交与费用仍待历史核账'


def _timestamp_ms(value):
    """Convert the strategy's ISO timestamp (usually local-naive) to ms."""
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        # The strategy historically persisted ``datetime.now()`` without a
        # zone.  ``timestamp`` applies this host's local timezone, preserving
        # the meaning of those old records.
        return int(parsed.timestamp() * 1000)
    return int(parsed.timestamp() * 1000)


def _funding_snapshot(reader, position, venue, now_ms):
    """Return a safe, complete funding settlement snapshot or ``None``."""
    if not hasattr(reader, 'reconcile_funding') and not hasattr(reader, 'get_position_funding'):
        return None
    start = position.get('open_time')
    if not start:
        return None
    try:
        symbol = str(position.get('symbol', '')).upper()
        start_ms = _timestamp_ms(start)
        if hasattr(reader, 'reconcile_funding'):
            start_time = datetime.fromtimestamp(start_ms / 1000)
            end_time = datetime.fromtimestamp(now_ms / 1000)
            result = reader.reconcile_funding(symbol, start_time, end_time)
        else:
            result = reader.get_position_funding(symbol, start_ms, now_ms)
        if result.get('complete') is not True:
            return None
        rows = []
        total = decimal('0')
        for row in result.get('rows', []):
            if not isinstance(row, dict) or row.get('change') is None:
                return None
            change = decimal(row['change'])
            rows.append({key: row.get(key) for key in
                         ('timestamp', 'market_id', 'funding_id', 'change', 'rate',
                          'position_size', 'position_side')})
            total += change
        return {'venue': venue, 'source': 'lighter.positionFunding',
                'symbol': result.get('symbol'), 'market_id': result.get('market_id'),
                'funding_usdc': float(total), 'rows': rows,
                'observed_at': result.get('fetched_at')}
    except Exception:
        # A missing/temporarily unavailable settlement must remain pending.
        return None


def reconcile_pair(root, venues, state_venues=None):
    """Verify a supported signed pair and reconcile its durable local state.

    Account snapshots are read-only.  The normalizer below keeps the
    comparison logic venue-neutral while preserving the signed Arcus
    position's side and quantity semantics.
    """
    root = Path(root)
    venues = list(venues or [])
    if len(venues) != 2 or len(set(venues)) != 2:
        raise ValueError('需要选择两个不同平台')
    if any(venue not in ('lighter', 'lighter_rh', 'arcus') for venue in venues):
        raise ValueError('当前只读恢复支持 Lighter 主网、Lighter RH 与 Arcus 组合')
    scope = list(state_venues or venues)
    if (len(scope) < 2 or len(set(scope)) != len(scope) or
            not set(scope) <= {'lighter', 'lighter_rh', 'arcus'}):
        raise ValueError('原执行范围与核对平台不匹配')
    state_path = root / ('data/hedge_state_' + '_'.join(scope) + '.json')
    if not state_path.exists():
        raise ValueError('未找到该平台组合的本地执行状态')
    state = json.loads(state_path.read_text(encoding='utf-8'))
    runtime_scope = state.get('runtime_scope') or {}
    portfolio = runtime_scope.get('version') == 1
    managed_scope = runtime_scope.get('managed_venues') if portfolio else scope
    if (not isinstance(managed_scope, list) or len(set(managed_scope)) != len(managed_scope) or
            not set(managed_scope) <= {'lighter', 'lighter_rh', 'arcus'} or
            not set(scope) <= set(managed_scope) or not set(venues) <= set(managed_scope)):
        raise ValueError('原执行范围与核对平台不匹配')
    flat_scope = False
    if len(scope) > 2 or portfolio:
        active = [row for row in state.get('positions', []) if row.get('status') != 'closed']
        flat_scope = not active
        if not portfolio and any([row.get('variational_venue'), row.get('lighter_venue')] != venues for row in active):
            raise ValueError('原持仓平台与本次执行平台不匹配')
    # A later failed read must never leave an earlier success usable. Callers
    # serialize reconciliation with the runtime lease before touching state.
    if state.get('resume_verified') or state.get('resume_proof'):
        state['resume_verified'] = False
        state.pop('resume_proof', None)
        _atomic_json(state_path, state)
    positions = [item for item in state.get('positions', [])
                 if item.get('status') != 'closed']
    resumable_positions = [item for item in positions
                           if decimal(item.get('variational_size', 0)) != 0 or
                           decimal(item.get('lighter_size', 0)) != 0]
    symbols = list(dict.fromkeys(str(item.get('symbol', '')).upper() for item in positions
                                if item.get('symbol')))
    config = ConfigLoader(root / 'config/config.yaml')
    values = dotenv_values(root / '.env')
    clients = {}
    account_snapshots = {}
    funding_readers = {}
    funding_sdks = {}
    try:
        for venue in (managed_scope if portfolio or flat_scope else venues):
            if venue == 'arcus':
                required = ('ARCUS_WALLET_ADDRESS', 'ARCUS_ACCOUNT_INDEX', 'ARCUS_API_SIGNING_KEY')
                if any(not values.get(key) for key in required):
                    raise ValueError('arcus 凭据未配置')
                client = ArcusClient(
                    values['ARCUS_WALLET_ADDRESS'], int(values['ARCUS_ACCOUNT_INDEX']),
                    values['ARCUS_API_SIGNING_KEY'],
                    base_url=config.get('arcus.api.base_url', 'https://api.arcus.xyz'),
                    ws_url=config.get('arcus.api.ws_url', 'wss://api.arcus.xyz/v1/ws'),
                    proxy_url=config.get('arcus.api.proxy_url', config.get('common.network.proxy_url')),
                )
                clients[venue] = client
                account = client.account()
                raw_positions = account.get('positions') if isinstance(account, dict) else None
                if isinstance(raw_positions, dict):
                    raw_positions = list(raw_positions.values())
                if not isinstance(raw_positions, list):
                    raise ValueError('Arcus 账户快照缺少持仓信息')
                order_payload = client.orders()
                if not isinstance(order_payload, dict) or not any(
                        isinstance(order_payload.get(key), list)
                        for key in ('openOrders', 'orders', 'data', 'items', 'records')):
                    raise ValueError('Arcus 活动订单快照未确认')
                raw_orders = []
                if isinstance(order_payload, dict):
                    for key in ('openOrders', 'orders', 'data', 'items', 'records'):
                        rows = order_payload.get(key)
                        if isinstance(rows, list):
                            raw_orders.extend(rows)
                            # ``orders`` is the usual response; do not count
                            # the same rows again under an alias key.
                            if key == 'orders':
                                break
                terminal = {'FILLED', 'PARTIAL', 'PARTIALLY_FILLED', 'CANCELED',
                            'CANCELLED', 'REJECTED', 'EXPIRED', 'MARGIN_CANCELED'}
                active_orders = [row for row in raw_orders if isinstance(row, dict) and
                                 str(row.get('status') or row.get('orderStatus') or '').upper() not in terminal]
                by_market = {}
                for row in active_orders:
                    market_id = row.get('marketId') or row.get('market_id')
                    if market_id is not None:
                        by_market[str(market_id)] = by_market.get(str(market_id), 0) + 1
                normalized_positions = []
                for item in raw_positions:
                    if not isinstance(item, dict):
                        continue
                    size = item.get('size', item.get('positionSize', item.get('quantity')))
                    side = str(item.get('side') or item.get('positionSide') or '').lower()
                    if side not in ('long', 'short'):
                        side = 'long' if decimal(size or 0) >= 0 else 'short'
                    normalized_positions.append({
                        'market_id': item.get('marketId') or item.get('market_id'),
                        'position': str(abs(decimal(size or 0))),
                        'sign': 1 if side == 'long' else -1,
                        'open_order_count': by_market.get(str(item.get('marketId') or item.get('market_id')), 0),
                        'pending_order_count': 0,
                    })
                account_snapshots[venue] = {
                    'positions': normalized_positions,
                    'pending_order_count': len(active_orders),
                }
                if active_orders:
                    raise ValueError('Arcus 仍有活动订单')
                continue

            prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
            required = [prefix + key for key in ('ACCOUNT_INDEX', 'API_KEY_INDEX', 'API_PRIVATE_KEY')]
            if not values.get(required[0]):
                raise ValueError(venue + ' 凭据未配置')
            account_index = int(values[required[0]])
            deployment = deployment_for(venue)
            public = LighterApiTrader(
                {'api': {'base_url': deployment.rest_url,
                         'proxy_url': config.get('common.network.proxy_url'),
                         'cache_seconds': 0, 'request_timeout': 15}},
                account_index=account_index)
            clients[venue] = public
            # The account snapshot is an unauthenticated, fresh read and
            # includes pending-order counts and per-market order counts.  It
            # avoids creating a signer during recovery and never has a write
            # path. Keep positions for the explicit resume check below.
            data, fetched_at = public.client.get('/api/v1/account',
                                                  {'by': 'index', 'value': account_index}, ttl=0)
            accounts = [item for item in data.get('accounts', [])
                        if item.get('account_index', item.get('index')) == account_index]
            if len(accounts) != 1:
                raise ValueError(venue + ' 账户快照身份未确认')
            account = accounts[0]
            if (not isinstance(account.get('positions'), list) or
                    account.get('pending_order_count') is None):
                raise ValueError(venue + ' 账户快照缺少持仓或挂单信息')
            if int(account['pending_order_count']) != 0:
                raise ValueError(venue + ' 仍有活动订单')
            account_snapshots[venue] = account
            for item in account['positions']:
                if item.get('position') is None or item.get('open_order_count') is None:
                    raise ValueError(venue + ' 账户快照缺少持仓或挂单信息')
                if int(item['open_order_count']) != 0 or int(item.get('pending_order_count', 0) or 0) != 0:
                    raise ValueError(venue + ' 仍有活动订单')

        if flat_scope:
            # With no open hedge there is no concrete pair to recover. Verify
            # every account from the original worker before clearing its stop.
            for venue, account in account_snapshots.items():
                if any(decimal(item.get('position', 0)) != 0 for item in account.get('positions', [])):
                    raise ValueError(venue + ' 仍有持仓或挂单；原执行范围尚未确认空仓')
            state['halted_reason'] = None
            state['resume_verified'] = False
            state.pop('resume_proof', None)
            state['last_reconciled_at'] = utc_now()
            state['last_reconciliation'] = '原执行范围内全部平台账户、持仓与活动订单已只读核对为空；未发送订单'
            _atomic_json(state_path, state)
            return {'success': True, 'orders_submitted': False, 'changed': False,
                    'accounts_flat': True, 'checked_venues': scope, 'symbols': [],
                    'pending_accounting_count': sum(bool(item.get('accounting_pending') or
                        item.get('pending_funding_venues')) for item in state.get('positions', [])),
                    'message': '原执行范围内全部平台已核对空仓；未发送订单。'}

        # A locally open hedge can be flat on both accounts because the user
        # closed both legs manually.  Treat that as the safe flat-reconcile
        # path below; only attempt resume matching when an account still has
        # a non-zero position to compare against the local record.
        def market_for(venue, local):
            symbol = str(local.get('symbol', '')).upper()
            if venue == 'arcus':
                return clients[venue].market(symbol)
            market, _ = clients[venue].get_market(symbol)
            return market

        def matching_position(venue, local):
            market = market_for(venue, local)
            return [item for item in account_snapshots[venue].get('positions', [])
                    if str(item.get('market_id')) == str(market.get('market_id')) and
                    decimal(item.get('position', '0')) != 0]

        # A manually completed group must not strand another live group.
        # Test each group's original markets before requiring paired local
        # quantities; the stale row may contain just one remaining leg.
        # Mutations stay in memory until the whole account scope validates.
        closed_symbols = []
        observed_at = utc_now()
        for local in positions:
            pair = [local.get('variational_venue'), local.get('lighter_venue')]
            if not portfolio:
                pair = venues
            if (len(set(pair)) == 2 and all(venue in account_snapshots for venue in pair) and
                    not any(matching_position(venue, local) for venue in pair)):
                _settle_flat_position(local, observed_at)
                closed_symbols.append(str(local.get('symbol', '')).upper())
        resumable_positions = [p for p in resumable_positions if p.get('status') != 'closed']

        # Other user positions must not prevent a local record from being
        # reconciled when the specific symbol is flat on both selected venues.
        local_has_live_position = any(
            matching_position(venue, local)
            for local in resumable_positions
            for venue in ([local['variational_venue'], local['lighter_venue']] if portfolio else venues))
        if portfolio and not local_has_live_position and any(
                decimal(row.get('position', 0)) != 0
                for snapshot in account_snapshots.values() for row in snapshot.get('positions', [])):
            raise ValueError('原组合已无对应持仓，但管理范围内有其他敞口，请先核对账户')
        if resumable_positions and local_has_live_position and portfolio:
            from src.execution.position_recovery import validate_portfolio_accounts
            validate_portfolio_accounts(state['positions'], managed_scope, account_snapshots, matching_position)
            observed_at = utc_now()
            reconcile_unsent_closes(root, state['positions'], observed_at)
            validate_resume_closes(state['positions'])
            state.update(halted_reason=None, resume_verified=True, resume_verified_at=observed_at,
                resume_proof=dict(portfolio=True, venues=managed_scope, checked_at=observed_at,
                                  fingerprint=position_fingerprint(state['positions'])),
                last_reconciled_at=observed_at,
                last_reconciliation='各组原平台账户、数量、方向及活动订单均已核对，允许恢复原仓位监控')
            _atomic_json(state_path, state)
            return dict(success=True, orders_submitted=False, changed=bool(closed_symbols), resume_ready=True,
                        symbols=symbols, reconciled_closed_symbols=closed_symbols,
                        pending_accounting_count=sum(bool(p.get('accounting_pending') or
                            p.get('pending_funding_venues')) for p in state['positions']),
                        message='各组持仓已按原平台核对匹配；可恢复监控，未发送订单')
        if resumable_positions and local_has_live_position:
            validate_paired_positions(state.get('positions', []), venues)
            # Resume is safe only when every persisted leg has exactly one
            # matching account position with the same side and remaining
            # quantity, and there are no unrelated positions on either account.
            local_groups = {}
            for local in resumable_positions:
                local_groups.setdefault(str(local.get('symbol', '')).upper(), []).append(local)
            for venue in venues:
                nonzero = [item for item in account_snapshots[venue].get('positions', [])
                           if decimal(item.get('position', '0')) != 0]
                if len(nonzero) != len(local_groups):
                    raise ValueError(venue + ' 账户持仓与本地未平仓记录不匹配')
            for group in local_groups.values():
                local = group[0]
                for index, venue in enumerate(venues):
                    rows = matching_position(venue, local)
                    if len(rows) != 1:
                        raise ValueError(venue + ' ' + str(local.get('symbol')) + '账户持仓未匹配')
                    row = rows[0]
                    if venue == local.get('variational_venue'):
                        expected_size = local.get('variational_size', 0)
                        expected_long = local.get('variational_side') == 'long'
                    elif venue == local.get('lighter_venue'):
                        expected_size = local.get('lighter_size', 0)
                        expected_long = local.get('lighter_side') == 'buy'
                    else:
                        expected_size = (local.get('variational_size', 0)
                                         if index == 0 else local.get('lighter_size', 0))
                        expected_long = (local.get('variational_side') == 'long'
                                         if index == 0 else local.get('lighter_side') == 'buy')
                    leg = ('variational' if venue == local.get('variational_venue') or
                           (venue != local.get('lighter_venue') and index == 0) else 'lighter')
                    expected_size = sum(decimal(p.get(leg + '_size', 0)) for p in group)
                    if any((p.get(leg + '_side') in ('buy', 'long')) != expected_long for p in group):
                        raise ValueError('同市场本地层方向不一致')
                    expected_sign = 1 if expected_long else -1
                    actual_size = decimal(row.get('position', '0'))
                    actual_sign = int(row.get('sign', 1 if actual_size > 0 else -1))
                    if (actual_sign != expected_sign or
                            abs(abs(actual_size) - decimal(expected_size)) > decimal('0.000001')):
                        raise ValueError(venue + ' ' + str(local.get('symbol')) + '持仓方向或数量不匹配')
            observed_at = utc_now()
            reconcile_unsent_closes(root, state['positions'], observed_at)
            validate_resume_closes(state['positions'])
            state['halted_reason'] = None
            state['resume_verified'] = True
            state['resume_verified_at'] = observed_at
            state['resume_proof'] = {'venues': list(venues), 'checked_at': observed_at,
                                     'fingerprint': position_fingerprint(state['positions'])}
            state['last_reconciled_at'] = observed_at
            state['last_reconciliation'] = '只读核对确认未平仓记录与两平台账户持仓匹配；允许恢复监控，未发送订单'
            _atomic_json(state_path, state)
            return {'success': True, 'orders_submitted': False, 'changed': bool(closed_symbols),
                    'resume_ready': True, 'symbols': symbols,
                    'reconciled_closed_symbols': closed_symbols,
                    'pending_accounting_count': sum(bool(p.get('accounting_pending') or
                        p.get('pending_funding_venues')) for p in state['positions']),
                    'message': '两平台账户持仓与本地未平仓记录已匹配；可恢复监控，未发送订单。'}
        # Funding history for main accounts requires an authenticated,
        # read-only SDK token even though the account/position snapshot above
        # is public.  Build these sessions only when a pending settlement
        # exists; no order or cancel method is called here.
        pending_venues = set()
        for item in state.get('positions', []):
            for legacy_name in item.get('pending_funding_venues') or []:
                pending_venues.add(
                    venues[0] if legacy_name == 'variational' else
                    venues[1] if legacy_name == 'lighter' and 'lighter_rh' in venues else
                    legacy_name)
        for venue in pending_venues.intersection(venues):
            try:
                prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
                private_key = values.get(prefix + 'API_PRIVATE_KEY')
                key_index = values.get(prefix + 'API_KEY_INDEX')
                account_index = values.get(prefix + 'ACCOUNT_INDEX')
                if not private_key or key_index is None or account_index is None:
                    continue
                sdk = SDKLoop(lambda: OfficialSDKTransport.from_credentials(
                    int(account_index), int(key_index), private_key,
                    proxy_url=config.get('common.network.proxy_url'),
                    chain_id=deployment_for(venue).signing_chain_id,
                    url=deployment_for(venue).rest_url))
                funding_sdks[venue] = sdk
                funding_readers[venue] = LighterExecutionAdapter(
                    clients[venue], sdk, None, int(account_index), enabled=False, venue=venue)
            except Exception:
                # Keep the local accounting state pending if a read-only token
                # cannot be created; account-flat proof remains valid.
                continue

        observed_at = utc_now()
        now_ms = _timestamp_ms(observed_at)
        changed = bool(closed_symbols)
        for position in state.get('positions', []):
            # A pre-schema closed row with zero entry quantity cannot
            # represent a filled hedge.  Do not leave it as an eternal
            # accounting blocker after the account itself is proven flat.
            if (position.get('status') == 'closed' and position.get('accounting_pending') and
                    not position.get('entry_size_variational') and
                    not position.get('entry_size_lighter') and
                    not position.get('pending_funding_venues')):
                position['accounting_pending'] = False
                position['reconciliation_note'] = '旧版零成交记录字段不完整，已从待核账中排除；未发现实际持仓'
                changed = True
                continue
            if position.get('status') != 'closed':
                changed = True
                _settle_flat_position(position, observed_at)
            pending = list(position.get('pending_funding_venues') or [])
            if not pending:
                continue
            settlements = dict(position.get('funding_settlements') or {})
            remaining = []
            for legacy_name in pending:
                # Older position records used variational/lighter for the two
                # legs even when both concrete deployments are Lighter.
                if legacy_name == 'variational':
                    venue = venues[0]
                elif legacy_name == 'lighter' and 'lighter_rh' in venues:
                    venue = venues[1]
                elif legacy_name in venues:
                    venue = legacy_name
                else:
                    remaining.append(legacy_name)
                    continue
                snapshot = _funding_snapshot(funding_readers.get(venue, clients[venue]), position, venue, now_ms)
                snapshot = layer_funding(snapshot, position, state.get('positions', []),
                                         'variational' if legacy_name == 'variational' else 'lighter')
                if snapshot is None:
                    remaining.append(legacy_name)
                    continue
                settlements[legacy_name] = snapshot
                position['realized_pnl_usdc'] = float(position.get('realized_pnl_usdc') or 0) + snapshot['funding_usdc']
                changed = True
            if remaining != pending:
                position['pending_funding_venues'] = remaining
                position['funding_settlements'] = settlements
                position['accounting_pending'] = bool(position.get('pending_fee_venues') or
                    (position.get('entry_size_variational') and position.get('close_price_variational') is None) or
                    (position.get('entry_size_lighter') and position.get('close_price_lighter') is None))
                position['reconciliation_note'] = (
                    '两平台账户、持仓和活动订单已只读核对为空；资金费已自动从 positionFunding 补齐'
                    if not remaining else
                    '两平台账户与活动订单已只读核对为空；剩余资金费结算仍未返回')
        state['halted_reason'] = None
        state['resume_verified'] = False
        state.pop('resume_proof', None)
        state['last_reconciled_at'] = observed_at
        state['last_reconciliation'] = '只读核对两平台账户、持仓和活动订单均为空；未发送订单'
        _atomic_json(state_path, state)
        pending_count = sum(1 for item in state.get('positions', [])
                            if item.get('accounting_pending') or item.get('pending_funding_venues'))
        return {'success': True, 'orders_submitted': False, 'changed': changed,
                'accounts_flat': not any(decimal(p.get('position', 0)) != 0
                    for snapshot in account_snapshots.values() for p in snapshot.get('positions', [])),
                'symbols': symbols, 'pending_accounting_count': pending_count,
                'reconciled_closed_symbols': closed_symbols,
                'message': ('两平台账户已核对空仓；资金费已自动补齐，历史账务已完成，未发送订单。'
                            if pending_count == 0 else
                            '两平台账户已核对空仓；费用或资金费仍未完整返回，继续自动核账，未发送订单。')}
    finally:
        for sdk in reversed(list(funding_sdks.values())):
            try:
                sdk.close()
            except Exception:
                pass
        for public in reversed(list(clients.values())):
            try:
                public.close()
            except Exception:
                pass
