"""Read-only entry telemetry from the matching worker, never the web strategy."""
import math
import re
from datetime import datetime, timedelta, timezone

from src.common.market_data import age_seconds


def annotate_entry_scan(execution, events):
    if not execution.get('running'):
        return
    monitor = execution.get('position_monitor')
    if not monitor or monitor.get('heartbeat_stale'):
        return
    scan = dict(monitor.get('entry_scan') or {})
    now = datetime.now(timezone.utc)
    # Older workers did not publish backoff state. Their timestamped 429
    # receipt still gives an accurate deadline beyond the banner's 30s window.
    if 'entry_scan' not in monitor:
        deadlines = []
        for event in events:
            reason = (event.get('detail') or {}).get('reason') or ''
            match = re.search(r'HTTP 429；暂停 REST 请求 (\d+) 秒', reason)
            if event.get('event') not in ('stage_failed', 'blocked') or not match:
                continue
            try:
                stamp = datetime.fromisoformat(event['timestamp'])
                if stamp.tzinfo is not None:
                    deadlines.append(stamp + timedelta(seconds=int(match[1])))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        until = max(deadlines, default=now)
        if until > now:
            scan.update(backoff_until=until.isoformat(), backoff_reason='平台接口限流，暂停新开仓扫描；已有仓位继续监控')
    try:
        until = datetime.fromisoformat(scan.get('backoff_until') or '')
        remaining = max(0, math.ceil((until-now).total_seconds())) if until.tzinfo else 0
    except (TypeError, ValueError):
        remaining = 0
    scan['backoff_remaining_seconds'] = remaining
    if not remaining:
        scan.update(backoff_until=None, backoff_reason=None)
    execution.update(entry_scan=scan, entry_scan_backoff_until=scan.get('backoff_until'),
                     entry_scan_backoff_reason=scan.get('backoff_reason'))
    # Position management, shutdown and unresolved orders keep their own
    # higher-priority messages; an entry scan must never claim they are idle.
    event = execution.get('latest_event') or {}
    if (execution.get('close_only') or execution.get('stop_requested') or monitor.get('positions')
            or execution.get('recovery_blocked') or execution.get('tracked_position_count')
            or event.get('event') in ('accounting_pending', 'reconcile_required', 'planned',
                                      'entry_receipt', 'hedged', 'entry_cycle')):
        return
    if remaining:
        execution.update(phase='新开仓扫描限流等待',
            note=f'平台 REST 限流，新开仓扫描暂停，约 {remaining} 秒后重新检查；已有仓位继续监控')
    else:
        cached = scan.get('cached_scan') or {}
        cached_age = age_seconds(cached.get('updated_at'))
        coverage = ''
        if cached_age is not None and 0 <= cached_age <= 10:
            coverage = (f"；本轮长连接缓存覆盖 {cached.get('cached_symbols', 0)}/{cached.get('scope', 0)} 个品种"
                        + (f"，其余 {cached['missing_symbols']} 个后台限速补查" if cached.get('missing_symbols') else ''))
        errors = [state['error'] for state in (scan.get('reference_feeds') or {}).values()
                  if isinstance(state, dict) and state.get('error')]
        if errors:
            coverage += '；行情连接异常：' + '；'.join(dict.fromkeys(errors))
        diagnostics = cached.get('diagnostics')
        if diagnostics and cached_age is not None and 0 <= cached_age <= 10:
            labels = dict(boundary='未触边', net_return='回归收益不足', quotes='报价缺失或过期',
                          statistics='统计未就绪', quantity='数量不达标', route_filter='路线筛选或冷却',
                          other='其他条件', ready='收益达标待实盘复核', needs_validation='需数量核验')
            counts = ' · '.join(f"{labels.get(k,k)} {v}" for k,v in diagnostics.get('reasons',{}).items() if v)
            note = f"本轮 {diagnostics.get('directions',0)} 个方向：{counts or '等待报价'}"
            closest = diagnostics.get('closest',{})
            for kind, label in (('net_return','收益门槛'),('boundary','通道边界')):
                item = closest.get(kind) or {}
                gap = item.get('gap_bps')
                if type(gap) in (int,float) and math.isfinite(gap) and 0 <= gap < 100000:
                    note += f"；距{label}最近 {item.get('symbol','')}（{item.get('route','')}）差 {gap:.2f} bps"
            sized = scan.get('last_sized_rejection') or {}
            sized_age = age_seconds(sized.get('updated_at'))
            if sized_age is not None and 0 <= sized_age <= 30:
                note += f"；最近数量核验 {sized.get('symbol','')}：{sized.get('reason','未通过')}（另计，非本轮缓存统计）"
            execution.update(phase='扫描入场条件', note=note+coverage)
            return
        last = scan.get('last_preview_skip') or {}
        age = age_seconds(last.get('updated_at'))
        if age is not None and 0 <= age <= 10:
            execution.update(phase='等待入场门槛', note=(
                f"新鲜盘口预判未达入场门槛；已跳过 {scan.get('preview_skipped', 0)} 个不达标方向（累计），继续扫描" + coverage))
        else:
            execution.update(phase='扫描双平台报价',
                note='正在扫描双平台报价；候选达到收益门槛后进入数量、账户和提交前核验' + coverage)
