"""Pure funding scenarios. Rates are decimal fractions for ONE settlement.

Only explicitly normalized native forecasts are accepted. No I/O happens on
the quote/order path; missing or stale data uses the configured fixed fallback.
"""
import math
import time


def forecast(rate, next_at, observed_at=None, source=None, interval=3600):
    try:
        values = (rate, next_at, interval, time.time() if observed_at is None else observed_at)
        if any(isinstance(v, bool) for v in values):
            return None
        rate, next_at, interval, observed_at = map(float, values)
        if not all(math.isfinite(v) for v in (rate, next_at, interval, observed_at)):
            return None
        if abs(rate) > 1 or interval <= 0 or next_at <= 0:
            return None
        return dict(rate=rate, next_at=next_at, interval_seconds=interval,
                    observed_at=observed_at, source=source)
    except (ValueError, TypeError):
        return None


def funding_scenario(buy, sell, policy, now=None):
    now = time.time() if now is None else now
    fixed = policy.get('funding_reserve_bps')
    result = dict(mode='fixed', budget_bps=fixed, expected_cost_bps=None,
                  extra_bps=None, legs=[], reason='固定整轮资金费预算')
    if policy.get('funding_mode', 'fixed') != 'dynamic':
        return result
    result.update(mode='fallback', reason='资金费预测缺失或过期，使用固定兜底预算')
    horizon = float(policy.get('funding_hold_seconds', 3600))
    extra = float(policy.get('funding_extra_bps', 1))
    if not math.isfinite(horizon) or not 0 < horizon <= 604800 or not math.isfinite(extra) or extra < 0:
        raise ValueError('资金费预测参数无效')
    signed_cost = 0.0
    legs = []
    # Equal base quantities need not have equal notionals across venues.
    # Weight each forecast by its current mid rather than canceling equal
    # rates on two different-priced legs as if the cash amounts matched.
    try:
        mids = [(float(q['bid'])+float(q['ask']))/2 for q in (buy,sell)]
        if not all(math.isfinite(p) and p > 0 for p in mids):
            mids = [1.0,1.0]
    except (KeyError,ValueError,TypeError):
        mids = [1.0,1.0]
    weights = dict(long=mids[0]/max(mids),short=mids[1]/max(mids))
    for label, q, sign in (('long', buy, 1), ('short', sell, -1)):
        f = q.get('funding')
        if not isinstance(f, dict) or any(f.get(k) is None for k in
                ('rate','next_at','observed_at','interval_seconds')):
            return result
        f = forecast(f.get('rate'), f.get('next_at'), f.get('observed_at'),
                     f.get('source'), f.get('interval_seconds'))
        if not f or not 0 <= now - f['observed_at'] <= 120:
            return result
        # Never roll a last-hour prediction into a new settlement silently.
        if not now < f['next_at'] <= now + f['interval_seconds'] + 5:
            return result
        count = (int(math.floor((now + horizon - f['next_at']) / f['interval_seconds'])) + 1
                 if now + horizon >= f['next_at'] else 0)
        cost = sign * f['rate'] * count * 10000 * weights[label]
        signed_cost += cost
        legs.append(dict(side=label, rate_bps=f['rate'] * 10000,
                         settlements=count, cost_bps=cost, notional_weight=weights[label], **f))
    # Net expected receipts are shown, never used to subsidize entry safety.
    expected = max(0.0, signed_cost)
    return dict(mode='dynamic', budget_bps=expected + extra,
                expected_cost_bps=expected, signed_cost_bps=signed_cost,
                extra_bps=extra, horizon_seconds=horizon, legs=legs,
                reason='按方向及预计跨结算次数估算；预期净收入不抵扣入场成本')
