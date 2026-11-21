"""Diagnostic replay only. Never used to admit, size, or close an order."""
import math
import statistics


HORIZON_SECONDS = 86400
MAX_GAP_SECONDS = 120


def replay_quality(rows, bounds, *, now, target_mode='center', reversion_percent=100,
                   band_bps=2, opposite=False):
    """Replay current frozen bounds; do not imply a walk-forward backtest.

    A side is armed only by an observed inside-to-outside crossing. An
    excursion counts once until it returns, expires, or loses continuous
    observations. Unfinished/gapped observations never enter the rate's
    denominator. This measures a sampled spread target, not net profit.
    """
    rows = [(float(t), float(v)) for t, v in rows
            if math.isfinite(t) and math.isfinite(v) and t <= now]
    result = {}
    lower, upper, center = (bounds[k] for k in ('lower_bps', 'upper_bps', 'center_bps'))
    for side, boundary, other, sign in (('upper', upper, lower, 1), ('lower', lower, upper, -1)):
        events, durations = [], []
        counts = dict(entered=0, returned=0, timed_out=0, pending=0, interrupted=0)
        active, previous = None, None

        def finish(status, stamp):
            nonlocal active
            counts[status] += 1
            event = dict(active, status=status, end_timestamp=stamp,
                         duration_seconds=max(0, stamp-active['start_timestamp']))
            if status == 'returned':
                durations.append(event['duration_seconds'])
            events.append(event)
            active = None

        for stamp, value in rows:
            gap = previous is not None and stamp-previous[0] > MAX_GAP_SECONDS
            if gap and active:
                finish('interrupted', previous[0])
            if active:
                elapsed = stamp-active['start_timestamp']
                if elapsed > HORIZON_SECONDS:
                    finish('timed_out', active['start_timestamp']+HORIZON_SECONDS)
                elif sign*(value-active['target_bps']) <= 0:
                    finish('returned', stamp)
                elif elapsed == HORIZON_SECONDS:
                    finish('timed_out', stamp)
            outside = sign*(value-boundary) >= 0
            crossed = (previous is not None and not gap
                       and sign*(previous[1]-boundary) < 0 and outside)
            if active is None and crossed:
                targets = [center] if target_mode == 'center' else [0.] if target_mode == 'zero' else [center, 0.]
                if opposite:
                    target = other
                else:
                    favorable = [t for t in targets if sign*(value-t) > band_bps]
                    target = (min(favorable, key=lambda t: abs(value-t)) if favorable else None)
                    if target is not None:
                        target = value+(target-value)*reversion_percent/100
                        if reversion_percent == 100:
                            target += sign*band_bps
                if target is not None and sign*(value-target) > 1e-9:
                    active = dict(start_timestamp=stamp, entry_bps=value, target_bps=target)
                    counts['entered'] += 1
            previous = (stamp, value)
        if active:
            # No quote after the deadline is not evidence of non-return.
            status = 'interrupted' if now-previous[0] > MAX_GAP_SECONDS else 'pending'
            finish(status, previous[0])
        resolved = counts['returned']+counts['timed_out']
        result[side] = dict(counts, resolved=resolved,
            return_rate=counts['returned']/resolved if resolved else None,
            median_return_seconds=statistics.median(durations) if durations else None,
            recent_events=list(reversed(events[-10:])))
    return result


def quality_snapshot(history, symbol, route, seconds, target_mode, min_deviation_bps, policy, stats):
    """Only called by the existing background statistics worker."""
    import time
    from src.execution.channel_policy import channel_bounds
    from src.execution.target_return import ratio_to_directed_bps

    now = int(time.time())
    observation_window = min(int(seconds), 7*86400)
    prepared = history.prepared_samples(symbol, route, observation_window)
    rows = [(stamp, ratio_to_directed_bps(value)) for stamp, value in prepared['rows']]
    bounds, reason = None, None
    try:
        if policy:
            bounds = channel_bounds(policy, stats)
        elif stats.get('center_ready'):
            center = stats['center_bps']
            bounds = dict(center_bps=center,
                lower_bps=min(stats['p10_bps'], center-max(2., min_deviation_bps)),
                upper_bps=max(stats['p90_bps'], center+max(2., min_deviation_bps)))
        else:
            reason = stats.get('center_reason') or '历史样本不足'
    except ValueError as exc:
        reason = str(exc)
    sample_span = rows[-1][0]-rows[0][0] if rows else 0
    ready = bounds is not None and len(rows) >= 30 and sample_span >= 870
    result = dict(symbol=symbol, pair_key=route, seconds=observation_window,
        requested_seconds=seconds, horizon_seconds=HORIZON_SECONDS,
        max_gap_seconds=MAX_GAP_SECONDS, diagnostic_only=True,
        replay_basis='current_frozen_bounds', quality_ready=ready,
        quality_reason=reason if reason else None if ready else '有效样本不足30个或覆盖不足14.5分钟',
        center_bps=stats.get('center_bps'), samples=len(rows), sample_span_seconds=sample_span,
        raw_samples=prepared['raw_samples'], invalid_quote_samples=prepared['invalid_quote_samples'],
        anomaly_count=prepared['anomaly_count'], last_sample_at=rows[-1][0] if rows else None,
        center_drift_bps=stats.get('center_drift_bps'), center_status=stats.get('center_status'),
        recent_centers=stats.get('recent_centers'), bounds=bounds, policy=policy,
        target_mode=target_mode, sides=None,
        note='按当前冻结边界回看最近最多7天的30秒报价；存在回看偏差，不是成交胜率。'
             '观察期限24小时，缺失报价和仍在等待不计入回归比例；触及价差目标不代表扣费后盈利。')
    if ready:
        result['sides'] = replay_quality(rows, bounds, now=now,
            target_mode='center' if policy else target_mode,
            reversion_percent=policy['reversion_percent'] if policy and policy['exit_mode'] != 'opposite' else 100,
            band_bps=0 if policy else 2,
            opposite=bool(policy and policy['exit_mode'] == 'opposite'))
    return result
