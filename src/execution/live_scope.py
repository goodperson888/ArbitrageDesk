"""Prepare entry scope changes off the coordinator; apply only between hedges."""
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

from src.common.market_data import age_seconds, utc_now


SIGNED_SCOPE_VENUES = ('lighter', 'lighter_rh', 'arcus')


def validate_scope(venues, symbols):
    if (not isinstance(venues, list) or not 2 <= len(venues) <= 3 or
            any(not isinstance(v, str) for v in venues) or
            len(set(venues)) != len(venues) or any(v not in SIGNED_SCOPE_VENUES for v in venues)):
        raise ValueError('带仓切换目前支持 Lighter 主网、Lighter RH、Arcus，至少选择两个不同平台')
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 2000 or
            any(not isinstance(s, str) or not s.strip() or len(s) > 64 for s in symbols)):
        raise ValueError('新范围没有有效共同品种，请等待目录返回后重试')
    return list(venues), list(dict.fromkeys(s.strip() for s in symbols))


def write_scope_request(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.entry-scope-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(payload, out, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LiveScopeController:
    def __init__(self, worker, monitor, venues, prepare, dispose=lambda value: None):
        self.worker, self.monitor = worker, monitor
        self.prepare, self.dispose = prepare, dispose
        self.path = worker.strategy.state_path.with_suffix('.scope-request.json')
        self.stamp, self.desired, self.future = None, None, None
        self.pool = None
        self.closed = False
        self.status = dict(version=1, state='applied', entry_venues=list(venues),
                           symbols=list(worker.symbols), worker_started_at=monitor.started_at,
                           managed_venues=list(venues), updated_at=utc_now())
        worker.strategy.runtime_scope = self.status

    def _read_request(self):
        try:
            stamp = self.path.stat().st_mtime_ns
            if stamp == self.stamp:
                return
            self.stamp = stamp
            if self.path.stat().st_size > 131072:
                return
            request = json.loads(self.path.read_text())
            age = age_seconds(request.get('requested_at'))
            if (request.get('pid') != os.getpid() or
                    request.get('worker_started_at') != self.monitor.started_at or
                    age is None or not 0 <= age <= 300):
                return
            venues, symbols = validate_scope(request.get('venues'), request.get('symbols'))
            self.desired = {**request, 'venues':venues, 'symbols':symbols}
            self.status.update(state='preparing', request_id=request['id'],
                               requested_venues=venues,
                               message='后台准备新平台与行情；已有仓位继续监控', updated_at=utc_now())
        except (OSError, ValueError, TypeError, KeyError):
            return

    def poll(self):
        """Never wait for network reads, authentication, feeds or an entry receipt."""
        if self.closed:
            return False
        self._read_request()
        if self.future is not None:
            request, future = self.future
            if not future.done():
                return False
            if any(not item[0].done() for item in self.worker._preparation_pending.values()):
                return False
            self.future = None
            try:
                prepared = future.result()
                request_age = age_seconds(request.get('requested_at'))
                if not self.desired or request['id'] != self.desired['id']:
                    self.dispose(prepared)
                elif request_age is None or not 0 <= request_age <= 300:
                    self.dispose(prepared)
                    raise ScopePreparationError('范围准备已过期，请重新应用参与平台')
                elif self.worker.strategy.halted_reason or any(
                        p.status not in ('open', 'closed') for p in self.worker._positions()):
                    self.dispose(prepared)
                    raise ValueError('已有订单或持仓待核对，未切换新仓范围')
                else:
                    self._apply(request, prepared)
                    return True
            except Exception as exc:
                if self.desired and request['id'] == self.desired['id']:
                    # Never expose SDK payloads or credentials through status.
                    reason = str(exc) if isinstance(exc, ScopePreparationError) else '新平台或账户核验未通过，请核对连接与持仓后重试'
                    self.status.update(state='failed', message=reason, updated_at=utc_now())
                    self.desired = None
        if self.desired is not None and self.future is None:
            if self.pool is None:
                self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='entry-scope-prepare')
            request = self.desired
            self.future = request, self.pool.submit(self.prepare, request)
        return False

    def _apply(self, request, prepared):
        strategy = self.worker.strategy
        adapters = prepared['adapters']
        previous = (list(strategy._route_trader_pairs), strategy._entry_route_indices,
                    strategy._active_route_index, strategy.parallel_discovery,
                    self.worker.symbols, dict(self.status), dict(self.monitor.venue_traders),
                    strategy.variational_trader, strategy.lighter_trader)
        strategy.set_entry_routes(list(combinations(adapters, 2)))
        self.worker.symbols = tuple(request['symbols'])
        self.monitor.venue_traders.update({trader.venue:trader for trader in adapters})
        for name in ('_discovery_pending', '_preparation_pending'):
            pending = getattr(self.worker, name)
            for item in pending.values():
                item[0].cancel()
            pending.clear()
        for name in ('_discovery_retry_at', '_discovery_failures', '_preparation_ready',
                     '_preparation_retry_at', '_preparation_hosts'):
            getattr(self.worker, name).clear()
        self.worker._discovery_offset = 0
        self.worker._discovery_next_launch_at = 0
        managed = list(dict.fromkeys(self.status['managed_venues'] + request['venues']))
        self.status.update(state='applied', entry_venues=request['venues'], symbols=request['symbols'],
                           managed_venues=managed, message='后续新仓范围已更新；旧仓仍在原平台管理',
                           updated_at=utc_now())
        try:
            strategy._save()
        except Exception:
            pairs, allowed, active, parallel, symbols, status, traders, first, second = previous
            strategy._route_trader_pairs, strategy._entry_route_indices = pairs, allowed
            strategy.parallel_discovery = parallel
            strategy._active_route_index = active
            strategy.variational_trader, strategy.lighter_trader = first, second
            self.worker.symbols = symbols
            self.status.clear(); self.status.update(status)
            self.monitor.venue_traders.clear(); self.monitor.venue_traders.update(traders)
            self.dispose(prepared)
            raise
        if callable(prepared.get('commit')):
            prepared['commit']()
        self.desired = None
        try:
            strategy.history.append('entry_scope_applied', venues=request['venues'],
                                    symbols=request['symbols'], request_id=request['id'])
        except Exception:
            pass  # Audit delivery cannot invalidate an already persisted scope.

    def close(self):
        self.closed = True
        if self.future:
            _, future = self.future
            if not future.cancel():
                def cleanup(done):
                    try:
                        self.dispose(done.result())
                    except Exception:
                        pass
                future.add_done_callback(cleanup)
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)


class ScopePreparationError(ValueError):
    """Fixed, safe messages suitable for the local UI."""
