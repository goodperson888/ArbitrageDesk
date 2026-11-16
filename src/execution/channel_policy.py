"""Pure spread-channel rules on a fixed A ask / B bid axis, in bps."""
import math
from src.execution.target_return import finite, directed_spread, exit_projection

DEFAULTS = dict(method='quantile', window_seconds=3600, quantile=90., multiplier=2.,
    min_samples=30, min_width_bps=0., max_width_bps=0., lower_bps=-20., upper_bps=20.,
    exit_mode='reversion', reversion_percent=60.)


def channel_policy(config):
    raw = config.get('channel_policy', {}) or {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('通道配置项无效')
    p = dict(DEFAULTS, **raw)
    if p['method'] not in ('quantile', 'bollinger', 'robust', 'fixed'):
        raise ValueError('通道类型无效')
    if p['exit_mode'] not in ('reversion', 'opposite'):
        raise ValueError('通道退出模式无效')
    limits = dict(window_seconds=(900,604800), quantile=(50.1,99.9), multiplier=(.1,10),
        min_samples=(30,100000), min_width_bps=(0,10000), max_width_bps=(0,10000),
        lower_bps=(-9999,9999), upper_bps=(-9999,9999), reversion_percent=(.1,100))
    for key, (low,high) in limits.items():
        p[key] = finite(p[key], '通道参数 '+key)
        if not low <= p[key] <= high:
            raise ValueError('通道参数超出范围: '+key)
    for key in ('window_seconds','min_samples'):
        if int(p[key]) != p[key]:
            raise ValueError('通道窗口和样本数须为整数')
        p[key] = int(p[key])
    if p['lower_bps'] >= p['upper_bps']:
        raise ValueError('通道下边界须小于上边界')
    if p['max_width_bps'] and p['max_width_bps'] < p['min_width_bps']:
        raise ValueError('通道最大宽度不能小于最小宽度')
    if config.get('entry_policy',{}).get('mode') == 'channel':
        if config.get('entry_policy',{}).get('calculation_version') != 2:
            raise ValueError('通道策略需要整轮净收益模型')
        if config.get('position_policy',{}).get('mode','single') != 'single':
            raise ValueError('通道策略使用单开持仓；同一组合结束后才可反向')
    return p


def sample_statistics(rows, quantile):
    """At most one observation per five seconds, independent of push frequency."""
    buckets = {}
    for stamp, value in rows:
        if math.isfinite(value):
            key = int(stamp)//5
            if key not in buckets or stamp >= buckets[key][0]:
                buckets[key] = (stamp, value)
    ordered_rows = sorted(buckets.values())
    values = sorted(v for _,v in ordered_rows)
    if not values:
        return dict(samples=0)
    def q(values, fraction):
        at = (len(values)-1)*fraction
        low = int(at)
        return values[low]+(values[min(low+1,len(values)-1)]-values[low])*(at-low)
    median, mean = q(values,.5), math.fsum(values)/len(values)
    return dict(samples=len(values), center_bps=median, mean_bps=mean,
        stdev_bps=math.sqrt(math.fsum((v-mean)**2 for v in values)/len(values)),
        mad_bps=q(sorted(abs(v-median) for v in values),.5),
        lower_bps=q(values,1-quantile/100), upper_bps=q(values,quantile/100),
        sample_span_seconds=ordered_rows[-1][0]-ordered_rows[0][0],
        last_sample_at=ordered_rows[-1][0])


def channel_bounds(policy, stats):
    p = policy
    if p['method'] == 'fixed':
        lower,upper = p['lower_bps'],p['upper_bps']
        center = (lower+upper)/2
        samples = 0
    else:
        if stats.get('statistics_ready') is False or stats.get('center_ready') is False:
            raise ValueError('通道历史统计正在积累或更新，暂不入场')
        s = stats.get('channel_statistics') or {}
        samples = int(s.get('samples') or 0)
        if samples < p['min_samples'] or float(s.get('sample_span_seconds') or 0) < 870:
            raise ValueError(f'通道有效样本不足或覆盖不足14.5分钟: {samples}/{p["min_samples"]}')
        center = finite(s.get('mean_bps' if p['method']=='bollinger' else 'center_bps'),'通道中心')
        if p['method'] == 'quantile':
            lower,upper = finite(s.get('lower_bps'),'下边界'),finite(s.get('upper_bps'),'上边界')
        else:
            scale = finite(s.get('stdev_bps' if p['method']=='bollinger' else 'mad_bps'),'通道波动',0)
            half = scale*p['multiplier']*(1.4826 if p['method']=='robust' else 1)
            lower,upper = center-half,center+half
    width = upper-lower
    if width < p['min_width_bps']:
        extra = (p['min_width_bps']-width)/2
        lower,upper = lower-extra,upper+extra
        width = upper-lower
    if width <= 1e-9:
        raise ValueError('通道没有有效波动宽度，暂不入场')
    if p['max_width_bps'] and width > p['max_width_bps']:
        raise ValueError(f'通道宽度 {width:.2f} bps 超过允许上限，暂不入场')
    if not -9999 < lower <= center <= upper < 9999:
        raise ValueError('通道边界无效')
    return dict(lower_bps=lower, upper_bps=upper, center_bps=center, width_bps=width,
        samples=samples, method=p['method'], window_seconds=p['window_seconds'])


def channel_context(policy, bounds, a, b, *, upper_direction, symbol, route, axis_venues, require_boundary=True):
    current = directed_spread(a['ask'],b['bid'])
    boundary = bounds['upper_bps' if upper_direction else 'lower_bps']
    at_boundary = current >= boundary if upper_direction else current <= boundary
    if require_boundary and not at_boundary:
        error = ValueError(f'未到通道{"上" if upper_direction else "下"}边界: {current:.2f} / {boundary:.2f} bps')
        error.scan_kind, error.gap_bps = 'boundary', abs(current-boundary)
        raise error
    center = bounds['center_bps']
    if require_boundary and (upper_direction and current <= center or not upper_direction and current >= center):
        raise ValueError('通道入场方向与回归方向不一致')
    target = (bounds['lower_bps' if upper_direction else 'upper_bps']
        if policy['exit_mode']=='opposite' else current+(center-current)*policy['reversion_percent']/100)
    # A reverse B-long/A-short target is NOT simply -target: both venue
    # bid/ask widths must survive the coordinate conversion.
    directed_target = target
    if not upper_direction:
        projection = exit_projection(dict(a,fee_bps=0),dict(b,fee_bps=0),target,0,0,0)
        a_bid = projection['target_buy_ask']*float(a['bid'])/float(a['ask'])
        b_ask = projection['target_sell_bid']*float(b['ask'])/float(b['bid'])
        directed_target = directed_spread(b_ask,a_bid)
    return dict(version=2,symbol=symbol,route=route,target_mode='channel',target_policy_mode='channel',
        target_bps=directed_target,center_bps=None,samples=bounds['samples'],
        window_seconds=policy['window_seconds'],channel=dict(bounds,policy=dict(policy),
            axis_venues=list(axis_venues),side='upper' if upper_direction else 'lower',
            entry_axis_bps=current,exit_axis_bps=target,at_boundary=at_boundary,exit_mode=policy['exit_mode']))
