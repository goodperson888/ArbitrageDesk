"""Memory-only scan summaries; never fetch a quote or persist a scan event."""
import math


def rejection_kind(reason):
    if any(word in reason for word in ('样本', '历史统计', '中心', '覆盖不足')):
        return 'statistics'
    if any(word in reason for word in ('边界', '触边', '回归方向')):
        return 'boundary'
    if any(word in reason for word in ('最小', '最低订单', 'minimum', '精度', '数量')):
        return 'quantity'
    return 'other'


def skipped(scan, kind, symbol, route, *, gap=None, net=None, floor=None, reason=None):
    if scan is None:
        return
    reasons = scan['reasons']
    reasons[kind] = reasons.get(kind, 0) + 1
    if type(gap) not in (int, float) or not math.isfinite(gap) or gap < 0:
        return
    candidate = dict(symbol=symbol, route=route, kind=kind, gap_bps=gap,
                     net_bps=net, minimum_bps=floor, reason=reason)
    # Profit gaps and boundary gaps measure different conditions. Keep a
    # separate nearest candidate for each instead of comparing unlike gaps.
    previous = scan['closest'].get(kind)
    if previous is None or gap < previous['gap_bps']:
        scan['closest'][kind] = candidate
