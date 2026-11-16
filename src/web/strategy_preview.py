"""Read-only strategy diagrams. Never constructs a signed adapter."""
import json
import math
import re
from pathlib import Path
from src.api_v2.center_statistics import center_stats_for
from src.execution.channel_policy import channel_policy, channel_bounds


VENUES = {'lighter','lighter_rh','arcus','standx','variational'}


def strategy_preview(history, payload, data_dir):
    symbol = str(payload.get('symbol','')).strip().upper().removesuffix('-PERP')
    if not re.fullmatch(r'[A-Z0-9_]{1,32}',symbol):
        raise ValueError('请选择有效品种')
    venues = payload.get('venues')
    if not isinstance(venues,list) or len(venues)!=2 or len(set(venues))!=2 or any(v not in VENUES for v in venues):
        raise ValueError('预览需选择两个不同平台')
    axis = sorted(venues)
    policy = channel_policy({'channel_policy':payload.get('channel_policy',{})})
    canonical,route = symbol+'-PERP','->'.join(axis)
    stats = center_stats_for(history,canonical,route,policy['window_seconds'],policy['quantile'])
    bounds,error = None,None
    try:
        bounds = channel_bounds(policy,stats)
    except ValueError as exc:
        error = str(exc)
    # A bounded on-demand chart query is independent from the worker's
    # asynchronous full-window statistics. Never fetch exchange history here.
    import time
    from contextlib import closing
    with closing(history._connect()) as db:
        rows = db.execute('SELECT ts,gross_bps,buy_age_seconds,sell_age_seconds FROM observations '
            'WHERE symbol=? AND pair_key=? AND ts>=? ORDER BY ts DESC LIMIT 3600',
            (canonical,route,int(time.time())-policy['window_seconds'])).fetchall()
    valid = [(t,v) for t,v,a,b in reversed(rows) if type(v) in (int,float) and math.isfinite(v)
        and all(type(age) in (int,float) and math.isfinite(age) and 0<=age<=5 for age in (a,b))]
    step = max(1,math.ceil(len(valid)/600))
    points = [dict(timestamp=t,value_bps=v) for t,v in valid[::step]]
    if valid and (not points or points[-1]['timestamp']!=valid[-1][0]):
        points.append(dict(timestamp=valid[-1][0],value_bps=valid[-1][1]))
    positions=[]
    for path in sorted(Path(data_dir).glob('hedge_state*.json'))[:128]:
        if path.name.endswith('.monitor.json'):
            continue
        try:
            saved=json.loads(path.read_text())
            for p in saved.get('positions',[]):
                if (p.get('symbol')==symbol and p.get('status')!='closed' and
                        sorted([p.get('variational_venue',''),p.get('lighter_venue','')])==axis):
                    positions.append({k:p.get(k) for k in ('id','symbol','status','entry_target',
                        'variational_venue','lighter_venue','variational_side','lighter_side','open_time')})
        except (OSError,ValueError,TypeError):
            continue
    positions=list({p['id']:p for p in positions}.values())
    return dict(symbol=symbol,axis_venues=axis,pair_key=route,policy=policy,bounds=bounds,error=error,
        statistics={k:stats.get(k) for k in ('center_bps','samples','center_ready','center_reason','statistics_ready',
            'statistics_age_seconds','channel_statistics')},points=points,positions=positions,
        orders_submitted=False,chart_kind='observed_executable_spread',
        chart_note='历史 A 卖一 / B 买一价差；显示最近最多3600条记录，通道使用完整统计窗口。预览不代表历史可按当前数量成交。')
