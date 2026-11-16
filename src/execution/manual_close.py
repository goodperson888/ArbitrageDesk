"""Explicit group exits, queued to the existing coordinator; never sign in Flask."""
import hashlib
import json
import os
import time
from uuid import uuid4

from src.common.market_data import age_seconds, utc_now
from src.execution.live_scope import write_scope_request
from src.execution.position_monitor import read_position_monitor

REASON = '用户双平'
MAX_REQUESTS = 64
MAX_AGE = 300


def identity(row):
    if not isinstance(row, dict):
        row = vars(row)
    values = [row.get(key) for key in ('id', 'symbol', 'open_time', 'variational_venue',
                                       'lighter_venue', 'variational_side', 'lighter_side')]
    values = [value.isoformat() if hasattr(value, 'isoformat') else value for value in values]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def read_queue(path, pid, started_at):
    try:
        with path.open() as stream:
            text = stream.read(65537)
    except FileNotFoundError:
        return []
    if len(text) > 65536:
        raise ValueError('双平请求过多，请先等待已有请求处理')
    data = json.loads(text)
    if data.get('pid') != pid or data.get('worker_started_at') != started_at:
        return []
    rows = data['requests']
    if (not isinstance(rows, list) or len(rows) > MAX_REQUESTS or
            any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) or
                not 1 <= len(item[key]) <= limit for key, limit in
                (('id', 64), ('position_id', 128), ('symbol', 64),
                 ('fingerprint', 64), ('requested_at', 64))) for item in rows)):
        raise ValueError('双平请求队列无效，请刷新后核对')
    return rows


def runtime(console):
    current = console.status()
    if not current.get('running') or type(current.get('pid')) is not int or not console._worker_alive(current):
        raise ValueError('自动监控未运行，请先核对并恢复原仓位监控，再双平')
    path = console._pair_state_path(current.get('venues'))
    monitor = read_position_monitor(path.with_suffix('.monitor.json'), current['pid'])
    if not monitor or monitor.get('heartbeat_stale'):
        raise ValueError('持仓监控心跳未核验，暂不能双平，请先恢复监控')
    if monitor.get('manual_close_version') != 1:
        raise ValueError('当前执行进程尚未加载双平功能；已有仓位继续监控，下次正常启动时生效')
    state = json.loads(path.read_text())
    if monitor.get('stop_reason') or state.get('halted_reason'):
        raise ValueError('存在未知订单或持仓待核对，请先核对账户；不能重复发送平仓')
    return current, path, monitor, state


def eligible(row, persisted):
    if (not persisted or row.get('symbol') != persisted.get('symbol') or
            [row.get('variational_venue'), row.get('lighter_venue')] !=
            [persisted.get('variational_venue'), persisted.get('lighter_venue')]):
        return '本组未由当前进程接管，请先核对并恢复原仓位监控'
    if persisted.get('status') != 'open':
        return '本组正在平仓或待核对，不能重复发送'
    return None


def request_close(console, position_id, symbol, venues):
    if (not isinstance(position_id, str) or not 1 <= len(position_id) <= 128 or
            not isinstance(symbol, str) or not 1 <= len(symbol) <= 64 or
            not isinstance(venues, list) or len(venues) != 2 or
            any(not isinstance(v, str) for v in venues) or venues[0] == venues[1]):
        raise ValueError('双平请求不完整，请刷新本组记录后重试')
    with console.lock:
        current, path, monitor, state = runtime(console)
        matches = [p for p in state.get('positions', []) if p.get('id') == position_id]
        persisted = matches[0] if len(matches) == 1 else None
        error = eligible(dict(symbol=symbol, variational_venue=venues[0], lighter_venue=venues[1]), persisted)
        if error:
            raise ValueError(error)
        queue_path = path.with_suffix('.manual-close.json')
        requests = read_queue(queue_path, current['pid'], monitor['started_at'])
        # The browser may retry a request whose HTTP response was lost.
        previous = persisted.get('manual_close') or {}
        for item in requests:
            age = age_seconds(item.get('requested_at'))
            if (item.get('position_id') == position_id and age is not None and 0 <= age <= MAX_AGE and
                    (previous.get('id') != item.get('id') or previous.get('state') in ('requested', 'waiting_account', 'executing'))):
                return dict(success=True, orders_submitted=False, request_id=item['id'],
                            message='本组双平已排队，请等待双边成交回执，不要重复点击')
        # Keep only commands that can still be executed or deduplicated.
        requests = [item for item in requests if (age_seconds(item.get('requested_at')) is not None and
                    0 <= age_seconds(item.get('requested_at')) <= MAX_AGE)]
        if len(requests) >= MAX_REQUESTS:
            raise ValueError('双平队列已满，请等待已有请求处理后再试')
        request = dict(id=uuid4().hex, position_id=position_id, symbol=symbol,
                       fingerprint=identity(persisted), requested_at=utc_now())
        requests.append(request)
        write_scope_request(queue_path, dict(pid=current['pid'],
            worker_started_at=monitor['started_at'], requests=requests))
    return dict(success=True, orders_submitted=False, request_id=request['id'],
                message='双平已排队：将忽略盈利及收敛门槛，只减本组剩余数量；最终以双边成交回执为准')


def annotate_controls(console, rows):
    if not rows:
        return
    try:
        current, path, monitor, state = runtime(console)
        positions = {p['id']: p for p in state.get('positions', [])}
        requests = read_queue(path.with_suffix('.manual-close.json'), current['pid'], monitor['started_at'])
        error = None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        positions, requests = {}, []
        error = str(exc) if type(exc) is ValueError else '双平状态暂不可读，请刷新后重试'
    for row in rows:
        reason = error or eligible(row, positions.get(row.get('id')))
        previous = (positions.get(row.get('id')) or {}).get('manual_close') or {}
        pending = any(item.get('position_id') == row.get('id') and
                      age_seconds(item.get('requested_at')) is not None and
                      0 <= age_seconds(item.get('requested_at')) <= MAX_AGE and
                      (previous.get('id') != item.get('id') or previous.get('state') in
                       ('requested', 'waiting_account', 'executing')) for item in requests)
        row['manual_close_control'] = dict(available=not reason and not pending,
            pending=pending, reason=reason or ('双平请求待处理，等待成交回执' if pending else '只减本组剩余数量，可亏损退出'))


class ManualCloseController:
    def __init__(self, worker, monitor):
        self.worker, self.monitor = worker, monitor
        self.path = worker.strategy.state_path.with_suffix('.manual-close.json')
        self.stamp, self.next_read, self.pending = None, 0, {}
        self.seen = set()
        worker.strategy.manual_close_version = 1

    def _load(self):
        now = time.monotonic()
        if now < self.next_read:
            return
        self.next_read = now + .25
        try:
            stamp = self.path.stat().st_mtime_ns
            if stamp == self.stamp:
                return
            self.stamp = stamp
            requests = read_queue(self.path, os.getpid(), self.monitor.started_at)
            for item in requests:
                if (isinstance(item, dict) and isinstance(item.get('id'), str) and
                        isinstance(item.get('position_id'), str) and item['id'] not in self.seen):
                    self.pending[item['id']] = item
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return

    def _record(self, position, request, state, message):
        record = dict(id=request['id'], state=state, message=message,
                      requested_at=request.get('requested_at'), updated_at=utc_now())
        if (position.manual_close.get('id'), position.manual_close.get('state')) == (record['id'], state):
            return
        position.manual_close = record
        self.worker.strategy._save()

    def poll(self, account_ready=True):
        strategy = self.worker.strategy
        if not any(p.status != 'closed' for p in strategy.positions):
            return False
        self._load()
        handled = False
        for key, request in list(self.pending.items()):
            position = next((p for p in strategy.positions if p.id == request['position_id']), None)
            if position is None or (position.manual_close.get('id') == key and
                    position.manual_close.get('state') not in ('requested', 'waiting_account')):
                self.pending.pop(key, None)
                self.seen.add(key)
                continue
            age = age_seconds(request.get('requested_at'))
            if (age is None or not 0 <= age <= MAX_AGE or
                    request.get('fingerprint') != identity(position) or
                    request.get('symbol') != position.symbol):
                self.pending.pop(key, None)
                self.seen.add(key)
                continue
            handled = True
            if strategy.halted_reason or position.status != 'open':
                self._record(position, request, 'blocked', '当前订单或持仓状态已变化，未重复发送')
                self.pending.pop(key, None)
                self.seen.add(key)
                continue
            if not account_ready:
                self._record(position, request, 'waiting_account', '等待账户持仓核验，尚未发送双平')
                continue
            # Persist consumption BEFORE calling transports. A lost response,
            # a worker restart or another browser cannot repeat this request.
            self._record(position, request, 'executing', '正在同时只减仓平掉本组双腿')
            self.pending.pop(key, None)
            self.seen.add(key)
            strategy.history.append('manual_close_requested', position_id=position.id,
                                    symbol=position.symbol, request_id=key, reason=REASON)
            self.monitor.closing(position)
            strategy.close_position(position, reason=REASON)
            state = 'closed' if position.status == 'closed' else 'reconcile_required' if strategy.halted_reason else 'blocked'
            self._record(position, request, state, '双腿成交已确认' if state == 'closed' else
                         '双平未完整确认，请核对剩余仓位及订单，禁止重复发送')
            self.monitor.publish()
            if strategy.halted_reason:
                break
        return handled
