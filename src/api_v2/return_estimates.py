"""Round-trip estimates over already-fetched public snapshots only."""
from functools import lru_cache
import json
import yaml
from src.execution.return_preview import preview_policy, round_trip_preview
from src.execution.window_risk import ExecutionWindowRisk


@lru_cache(maxsize=8)
def _read_policy(path, mtime):
    with open(path) as handle:
        config = yaml.safe_load(handle) or {}
    strategy = config.get('strategy', {})
    result = preview_policy(strategy)
    result['_entry_mode'] = strategy.get('entry_policy',{}).get('mode')
    result['_channel_policy'] = strategy.get('channel_policy',{})
    return result


def display_policy(root):
    try:
        path = root / 'config/config.yaml'
        policy = dict(_read_policy(str(path), path.stat().st_mtime_ns))
        paths = sorted((root/'data').glob('hedge_state_*.json'))[:128]
        signatures = tuple((str(p), p.stat().st_mtime_ns) for p in paths
                           if not p.name.endswith('.monitor.json'))
        policy['_route_risk'] = _read_route_risk(signatures)
        return policy
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        # Missing settings never imply free funding or zero execution costs.
        return {}


@lru_cache(maxsize=4)
def _read_route_risk(signatures):
    routes = {}
    for path, stamp in sorted(signatures, key=lambda item:item[1]):
        try:
            with open(path) as handle:
                routes.update(json.load(handle).get('execution_window_risk', {}))
        except (OSError, ValueError, TypeError):
            continue
    return routes


def attach_return_previews(rows, history, policy, target_mode='center', window=604800):
    from .center_statistics import center_stats_for
    result = []
    for row in rows:
        stats = center_stats_for(history, row.canonical_symbol,
            history.pair_key(row.buy_venue, row.sell_venue), window)
        inputs = row.return_inputs
        channel,p,channel_error = None,None,None
        if policy.get('_entry_mode') == 'channel':
            from src.execution.channel_policy import channel_policy,channel_bounds,channel_context,channel_stats_for
            try:
                p = channel_policy({'channel_policy':policy.get('_channel_policy',{})})
                axis = sorted([row.buy_venue,row.sell_venue])
                stats = (channel_stats_for(history,row.canonical_symbol,'->'.join(axis),p) if p['method']=='touches' else
                         center_stats_for(history,row.canonical_symbol,'->'.join(axis),p['window_seconds'],p['quantile']))
                a,b = ((inputs.get('buy',{}),inputs.get('sell',{})) if row.buy_venue==axis[0]
                    else (inputs.get('sell',{}),inputs.get('buy',{})))
                channel = channel_context(p,channel_bounds(p,stats),a,b,
                    upper_direction=row.buy_venue==axis[0],symbol=row.canonical_symbol,
                    route=row.buy_venue+'->'+row.sell_venue,axis_venues=axis,require_boundary=False)
            except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
                channel_error = str(exc)
        effective = {k:v for k,v in policy.items() if not k.startswith('_')}
        key = row.canonical_symbol.split('-',1)[0].upper()+'|'+row.buy_venue+'->'+row.sell_venue
        evidence = ExecutionWindowRisk({key:policy.get('_route_risk', {}).get(key, {})}).evidence(key)
        if effective.get('movement_bps') is not None:
            base = min(float(effective['movement_bps']), float(effective.get('slippage_tolerance', 10)))
            effective.update(movement_bps=base, movement_evidence=evidence)
        preview = round_trip_preview(inputs.get('buy', {}), inputs.get('sell', {}), inputs.get('quantity'),
            target_mode, stats.get('center_bps'), stats.get('samples'), effective,
            target_override=channel['target_bps'] if channel else None)
        if policy.get('_entry_mode') == 'channel':
            preview['target_mode'] = 'channel'
            preview['channel_policy'] = p or policy.get('_channel_policy',{})
            if channel_error:
                preview.update(net_bps=None,profit_usdc=None,meets_floor=False,reason=channel_error,
                    target_bps=None,target_mode='channel')
            elif channel:
                preview['channel'] = channel['channel']
                if not channel['channel']['at_boundary']:
                    preview.update(meets_floor=False,reason='通道边界尚未触及；净收益为回归情景估算')
        preview['window_seconds'] = p['window_seconds'] if p else window
        preview['policy'] = effective
        preview['center_statistics'] = {k:stats.get(k) for k in ('center_bps','samples','raw_samples',
            'invalid_quote_samples','sample_span_seconds','last_sample_at','center_ready','center_status',
            'center_reason','recent_centers','center_drift_bps','statistics_age_seconds')}
        if policy.get('_entry_mode') != 'channel' and target_mode != 'zero' and stats.get('center_ready') is False:
            preview.update(net_bps=None, profit_usdc=None, meets_floor=False,
                           reason=stats.get('center_reason') or '历史中心尚未就绪')
        if row.buy_stale or row.sell_stale or any(age is None or age > 5 for age in (row.buy_age_seconds, row.sell_age_seconds)):
            preview.update(net_bps=None, profit_usdc=None, meets_floor=False, reason='双平台报价超过实盘估算时效，等待新盘口')
        target = preview.get('target_bps')
        result.append(row.model_copy(update={
            'return_preview': preview,
            'reversion_target_bps': target,
            'favorable_deviation_bps': preview.get('gross_bps', row.gross_bps)-target if target is not None else None,
            'estimated_reversion_profit_usdc': preview.get('profit_usdc'),
            'reversion_ready': target is not None and (bool(channel) if policy.get('_entry_mode') == 'channel'
                else target_mode == 'zero' or stats.get('center_ready', True)),
        }))
    return result
