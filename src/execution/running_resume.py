"""Resume a draining worker using its existing, fresh reconciliation evidence."""
import json

from src.common.market_data import age_seconds
from src.execution.position_monitor import read_position_monitor


def stop_generation(path):
    stamp = path.stat()
    return f'{stamp.st_ino}:{stamp.st_mtime_ns}'


def resume_control(console, execution):
    """Local reads only; never refresh accounts or change execution state."""
    result = {'available': False, 'reason': '当前没有可恢复的运行中停止请求'}
    if not execution.get('running') or not execution.get('stop_requested'):
        return result
    if execution.get('mode') != 'continuous_monitor' or execution.get('close_only'):
        result['reason'] = '本次为只平仓或单次验证；请在本次结束后重新检查并启动'
        return result
    if (type(execution.get('pid')) is not int or execution['pid'] <= 0 or
            not isinstance(execution.get('started_at'), str) or not execution['started_at']):
        result['reason'] = '无法确认当前工作进程，请等待状态更新'
        return result
    venues = execution.get('venues')
    if (not isinstance(venues, list) or len(venues) < 2 or
            any(v not in ('lighter', 'lighter_rh', 'arcus', 'standx', 'variational') for v in venues)):
        result['reason'] = '无法确认原运行范围'
        return result
    path = console._pair_state_path(venues)
    monitor = read_position_monitor(path.with_suffix('.monitor.json'), execution['pid'])
    if (not monitor or monitor.get('heartbeat_stale') or
            monitor.get('entry_paused') is not False or monitor.get('stop_reason')):
        result['reason'] = '持仓监控或账户核验暂未就绪，请等待更新后重试'
        return result
    if (monitor.get('entry_limits') or {}).get('version') != 1:
        result['reason'] = '当前工作进程不支持动态持仓上限，请在本次结束后启动新版'
        return result
    try:
        state = json.loads(path.read_text())
        positions = state['positions']
        active = [p for p in positions if p['status'] != 'closed']
        rows = monitor['positions']
        if (state.get('halted_reason') or not active or
                any(p['status'] != 'open' for p in active) or
                len(rows) != len(active) or
                len({p['id'] for p in active}) != len(active) or
                {p['id'] for p in active} != {row['id'] for row in rows}):
            result['reason'] = '原仓位正在平仓、待核对或已结束；请等待核对完成'
            return result
        for row in rows:
            age = age_seconds(row.get('last_success_at'))
            if (row.get('position_status') != 'open' or row.get('account_status') != 'ready' or
                    row.get('status') != 'monitoring' or row.get('error') or
                    age is None or not 0 <= age <= 10):
                result['reason'] = '原仓位或账户状态尚未核对就绪，请等待更新后重试'
                return result
        result.update(available=True, reason='', pid=execution['pid'],
                      started_at=execution['started_at'],
                      stop_generation=stop_generation(console.stop_path))
    except (OSError, ValueError, KeyError, TypeError):
        result['reason'] = '原仓位或停止标记正在更新，请稍后重试'
    return result
