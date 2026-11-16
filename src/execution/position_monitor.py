"""Bounded, read-only position reads; only the worker may act on the results."""
import copy
import json
import math
import os
import tempfile
import threading
import time
from concurrent.futures import Future, wait

from src.common.market_data import age_seconds, utc_now
from src.execution.telemetry import safe_reason


class PositionReadUnavailable(ValueError):
    pass


class PositionMonitor:
    def __init__(self, strategy):
        self.strategy = strategy
        self.path = strategy.state_path.with_suffix('.monitor.json')
        self.traders = dict(variational=strategy.variational_trader, lighter=strategy.lighter_trader)
        self.venue_traders = {trader.venue: trader for trader in self.traders.values()}
        for pair in getattr(strategy, '_route_trader_pairs', ()) or ():
            for trader in pair:
                self.venue_traders[trader.venue] = trader
        self.jobs, self.values, self.errors, self.next_read = {}, {}, {}, {}
        self.generations, self.checks, self.positions = {}, {}, []
        self.exit_stream_reads = {}
        from src.execution.close_accounting import ClosedFundingReads
        self.closed_funding = ClosedFundingReads()
        self._poll_cursor = 0
        self._last_tick_publish = None
        self.closed = False
        self.started_at = utc_now()

    @staticmethod
    def _generation(positions):
        return tuple(sorted((p.id, p.variational_size, p.lighter_size,
                             p.variational_side, p.lighter_side, p.entry_fees_usdc,
                             p.entry_price_variational, p.entry_price_lighter,
                             tuple(p.pending_fee_venues),
                             getattr(p, 'variational_venue', None), getattr(p, 'lighter_venue', None))
                            for p in positions))

    def _position_traders(self, position):
        resolver = getattr(type(self.strategy), 'position_traders', None)
        if callable(resolver):
            try:
                return dict(zip(('variational', 'lighter'), resolver(self.strategy, position)))
            except ValueError as exc:
                raise PositionReadUnavailable(str(exc)) from exc
        names = (getattr(position, 'variational_venue', None),
                 getattr(position, 'lighter_venue', None))
        if names == (None, None):
            # Legacy two-platform rows predate recorded venue identities.
            # A multi-platform row cannot safely inherit a startup pair.
            if getattr(self.strategy, 'multi_route_enabled', False) is True:
                raise PositionReadUnavailable('持仓平台组合缺失，必须先核对原仓位')
            return self.traders
        if any(name not in self.venue_traders for name in names) or names[0] == names[1]:
            raise PositionReadUnavailable('持仓平台组合未匹配，必须先核对原仓位')
        return dict(zip(('variational', 'lighter'), (self.venue_traders[name] for name in names)))

    def _symbol_traders(self, symbol):
        rows = [p for p in self.positions if p.symbol == symbol and p.status != 'closed']
        if not rows:
            return self.traders
        traders = self._position_traders(rows[0])
        if any(any(other[leg] is not trader for leg, trader in traders.items())
               for other in (self._position_traders(p) for p in rows[1:])):
            raise PositionReadUnavailable('同品种持仓平台组合不一致，必须先核对原仓位')
        return traders

    def _submit(self, key, generation, reader):
        kind = key[0]
        limit = {'quote': 4, 'account': 4, 'grid': 2, 'entry': 1}[kind]
        if key in self.jobs or sum(k[0] == kind for k in self.jobs) >= limit:
            return
        future = Future()
        # These threads perform GETs only and never mutate strategy/account
        # state. A slow GET cannot hold up stop or the monitor's main loop.
        def read():
            try:
                future.set_result(reader())
            except Exception as exc:
                future.set_exception(exc)
        self.jobs[key] = (future, generation)
        threading.Thread(target=read, daemon=True, name='position-' + kind).start()
        return True

    def _collect(self):
        for key, (future, generation) in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[key]
            if generation != self.generations.get(key[1]):
                continue
            kind = key[0]
            self.next_read[key] = time.monotonic() + {'quote': .5, 'account': 5, 'grid': 1, 'entry': 5}[kind]
            try:
                value = future.result()
                if kind == 'entry':
                    from src.execution.entry_accounting import apply_entry_evidence
                    position = next((p for p in self.positions if p.id == key[2]), None)
                    if position is not None and apply_entry_evidence(position, value):
                        self.strategy._save()
                        self.generations[key[1]] = self._generation(
                            [p for p in self.positions if p.symbol == key[1] and p.status == 'open'])
                    self.errors.pop(key, None)
                    continue
                if kind == 'quote':
                    self._validate_quote(value)
                    existing = self.values.get(key)
                    if existing and existing[2] == generation and (age_seconds(value['updated_at']) or 0) > (age_seconds(existing[0]['updated_at']) or 0):
                        continue  # An old REST read must not replace a newer native push.
                if kind == 'grid' and isinstance(value, dict):
                    # Refresh before the original quote/submission window runs
                    # out. Collection time must never renew a venue quote.
                    remaining = []
                    for leg in value.get('legs', {}).values():
                        quote = leg.get('quote', {})
                        age = age_seconds(quote.get('updated_at'))
                        if age is not None:
                            remaining.append(2 - age)
                        deadline = quote.get('submit_deadline_monotonic')
                        if isinstance(deadline, (int, float)):
                            remaining.append(deadline - time.monotonic())
                    if remaining:
                        self.next_read[key] = time.monotonic() + min(1, max(.25, min(remaining) - .5))
                self.values[key] = (value, time.monotonic(), generation)
                self.errors.pop(key, None)
            except Exception as exc:
                self.errors[key] = safe_reason(exc)

    @staticmethod
    def _validate_quote(value):
        age = age_seconds(value.get('updated_at'))
        if age is None or not 0 <= age <= 2:
            raise PositionReadUnavailable('持仓报价过期，平仓条件暂无法判断')
        bid, ask = value.get('bid'), value.get('ask')
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and
                   math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
            raise PositionReadUnavailable('持仓盘口无效，平仓条件暂无法判断')
        return value

    def _exit_stream_revision(self, position, traders):
        """Cache-only probe: a changed live book may bypass the REST cadence."""
        revision = []
        for trader in traders.values():
            reader = getattr(type(trader), 'cached_reference_quote', None)
            if not callable(reader):
                return None
            try:
                quote = self._validate_quote(reader(trader, position.symbol))
                if age_seconds(quote['updated_at']) > .75:
                    return None
                sizing_reader = getattr(type(trader), 'cached_sizing_book', None)
                book = (sizing_reader(trader, position.symbol, position.variational_size)
                        if callable(sizing_reader) else quote.get('book'))
                if not isinstance(book, dict) or not book.get('bids') or not book.get('asks'):
                    return None
                book_age = age_seconds(book.get('fetched_at'))
                if book_age is None or not 0 <= book_age <= .75:
                    return None
                revision.append((trader.venue, quote['updated_at'], quote['bid'], quote['ask'], book['fetched_at']))
            except Exception:
                return None
        return tuple(revision)

    def poll(self, positions, *, wait_for_reads=True):
        if self.closed:
            return
        self.closed_funding.poll(self.strategy)
        self.positions = [p for p in positions if p.status != 'closed']
        groups = {}
        for p in self.positions:
            if p.status == 'open':
                groups.setdefault(p.symbol, []).append(p)
        self.generations = {s: self._generation(rows) for s, rows in groups.items()}
        self._collect()
        now = time.monotonic()
        items = list(groups.items())
        if items:
            offset = self._poll_cursor % len(items)
            items = items[offset:] + items[:offset]
            self._poll_cursor += 1
        for symbol, group in items:
            generation = self.generations[symbol]
            try:
                traders = self._symbol_traders(symbol)
            except PositionReadUnavailable as exc:
                for leg in self.traders:
                    for kind in ('quote', 'account'):
                        self.errors[(kind, symbol, leg)] = str(exc)
                continue
            for leg, trader in traders.items():
                for kind, method in (('quote', 'get_position_mark_snapshot'), ('account', 'position_snapshot')):
                    key = (kind, symbol, leg)
                    if kind == 'quote' and callable(getattr(type(trader),'cached_reference_quote',None)):
                        try:
                            native = trader.cached_reference_quote(symbol)
                            if native is not None:
                                self._validate_quote(native)
                                self.values[key] = ({k:native.get(k) for k in
                                    ('bid','ask','updated_at','fee_bps','source')},now,generation)
                                self.errors.pop(key,None)
                                continue
                        except (ValueError,TypeError,KeyError):
                            pass
                    cached = self.values.get(key)
                    if (not cached or cached[2] != generation or now >= self.next_read.get(key, 0)):
                        if now >= self.next_read.get(key, 0) or cached and cached[2] != generation:
                            self._submit(key, generation, lambda t=trader, s=symbol, m=method: getattr(t, m)(s))
            for p in group:
                entry_key = ('entry', symbol, p.id)
                if now >= self.next_read.get(entry_key, 0):
                    from src.execution.entry_accounting import read_entry_evidence
                    frozen = copy.deepcopy(p)
                    self._submit(entry_key, generation, lambda position=frozen: read_entry_evidence(
                        position, self.strategy.state_path.parent, self.strategy.history))
                if not p.grid_cycle_id and self.strategy.close_mode == 'disabled' and not (p.entry_target or {}).get('channel'):
                    continue
                key = ('grid', symbol, p.id)
                revision = self._exit_stream_revision(p, traders)
                previous = self.exit_stream_reads.get(key)
                stream_changed = (revision is not None and
                    (previous is None or now - previous[0] >= .15 and previous[1] != (generation, revision)))
                if now >= self.next_read.get(key, 0) or stream_changed:
                    frozen = copy.deepcopy(p)
                    reader = (self.strategy._grid_exit_estimate if p.grid_cycle_id
                              else self.strategy._single_monitor_exit_estimate)
                    if self._submit(key, generation, lambda position=frozen, read=reader: read(position)):
                        self.exit_stream_reads[key] = (now, (generation, revision))
        self.exit_stream_reads = {key: value for key, value in self.exit_stream_reads.items()
                                  if key[1] in groups and any(p.id == key[2] for p in groups[key[1]])}
        # Fast native snapshots normally finish here. Slow REST is left in
        # flight; all reads remain singleflight and bounded by the caps above.
        pending = [f for key, (f, _) in self.jobs.items() if key[0] in ('quote', 'grid')]
        if pending and wait_for_reads:
            wait(pending, timeout=.03)
        self._collect()
        self.publish_tick()

    def _value(self, key, max_age):
        value = self.values.get(key)
        if (not value or value[2] != self.generations.get(key[1]) or
                time.monotonic() - value[1] > max_age or key in self.errors):
            raise PositionReadUnavailable('持仓' + ('账户待核验' if key[0] == 'account' else '行情待更新，平仓条件暂无法判断'))
        return value[0]

    def quotes(self, position):
        self._position_traders(position)
        return tuple(self._validate_quote(self._value(('quote', position.symbol, leg), 2))
                     for leg in self.traders)

    def prices(self, position):
        return tuple((q['bid'] + q['ask']) / 2 for q in self.quotes(position))

    def accounts(self, symbol):
        self._symbol_traders(symbol)
        values = tuple(self._value(('account', symbol, leg), 8) for leg in self.traders)
        if any(age_seconds(value.get('fetched_at')) is None or
               not 0 <= age_seconds(value.get('fetched_at')) <= 8 for value in values):
            raise PositionReadUnavailable('持仓账户待核验')
        return values

    def refresh_accounts(self, symbol):
        for leg in self.traders:
            key = ('account', symbol, leg)
            self.values.pop(key, None)
            self.errors.pop(key, None)
            self.next_read[key] = 0

    def grid_estimate(self, position):
        value = self._value(('grid', position.symbol, position.id), 2)
        if value is None:
            raise PositionReadUnavailable('网格平仓估算待补盘口或账务')
        for leg in value.get('legs', {}).values():
            age = age_seconds(leg.get('quote', {}).get('updated_at'))
            if age is None or not 0 <= age <= 2:
                raise PositionReadUnavailable('持仓报价过期，平仓条件暂无法判断')
        return value

    def single_estimate(self, position):
        try:
            value = self._value(('grid', position.symbol, position.id), 2)
        except PositionReadUnavailable:
            reason = self.errors.get(('grid', position.symbol, position.id))
            raise PositionReadUnavailable(reason or '平仓净盈利测算中，等待盘口及账务核验')
        if value is None:
            raise PositionReadUnavailable('平仓净盈利测算等待盘口及账务核验')
        return value

    def checked(self, position, reason=None, error=None):
        previous = self.checks.get(position.id, {})
        self.checks[position.id] = {
            **previous,
            'last_attempt_at': utc_now(),
            'last_success_at': previous.get('last_success_at') if error else utc_now(),
            'reason': reason, 'error': str(error) if isinstance(error, PositionReadUnavailable)
            else safe_reason(error) if error else None,
        }

    def closing(self, position):
        self.checks.setdefault(position.id, {})['phase'] = 'closing'
        self.publish()

    @property
    def entry_blocked(self):
        for p in self.positions:
            if p.status == 'closed':
                continue
            try:
                self.accounts(p.symbol)
            except PositionReadUnavailable:
                return True
            check = self.checks.get(p.id, {})
            age = age_seconds(check.get('last_success_at') or p.open_time.astimezone().isoformat())
            if age is None or age > 10:
                return True
        return False

    def publish(self):
        try:
            self._publish()
        except OSError:
            # Telemetry failure cannot prevent an otherwise safe close. The
            # reader treats an absent/stale heartbeat as unavailable.
            pass

    def publish_tick(self):
        # Stream wakeups can run at 20 Hz. Disk/UI telemetry stays at 4 Hz;
        # explicit transitions such as closing/stop still publish immediately.
        now = time.monotonic()
        if self._last_tick_publish is None or now-self._last_tick_publish >= .25:
            self.publish()
            self._last_tick_publish = now

    def _publish(self):
        rows = []
        for p in self.positions:
            if p.status == 'closed':
                continue
            row = {'id': p.id, 'symbol': p.symbol, **self.checks.get(p.id, {}), 'legs': {}}
            try:
                traders = self._position_traders(p)
            except PositionReadUnavailable as exc:
                row.update(status='blocked', error=str(exc), position_status=p.status, account_status='pending')
                rows.append(row)
                continue
            for leg, trader in traders.items():
                key = ('quote', p.symbol, leg)
                cached = self.values.get(key)
                quote = cached[0] if cached and cached[2] == self.generations.get(p.symbol) else {}
                health = trader.reference_health(p.symbol)
                row['legs'][leg] = {'venue': trader.venue, 'source': quote.get('source'),
                    'bid': quote.get('bid'), 'ask': quote.get('ask'), 'fee_bps': quote.get('fee_bps'),
                    'updated_at': quote.get('updated_at'), 'age_seconds': age_seconds(quote.get('updated_at')),
                    'error': self.errors.get(key), 'connection': health}
            try:
                self.accounts(p.symbol)
                row['account_status'] = 'ready'
            except PositionReadUnavailable:
                row['account_status'] = 'pending'
                row['account_errors'] = [self.errors[key] for key in
                    [('account', p.symbol, leg) for leg in self.traders] if key in self.errors]
            row['status'] = ('blocked' if row.get('error') else 'waiting_account'
                             if row['account_status'] != 'ready' else 'monitoring')
            row['position_status'] = p.status
            if not p.grid_cycle_id and p.status == 'open' and (self.strategy.close_mode != 'disabled' or (p.entry_target or {}).get('channel')):
                try:
                    estimate = self.strategy._single_profit_estimate(p)
                except Exception:
                    row['exit_profit_status'] = 'pending'
                    row['exit_profit_error'] = self.errors.get(('grid', p.symbol, p.id)) or '等待新鲜的同数量报价和完整账务'
                else:
                    row['exit_profit_status'] = ('ready' if self.strategy._single_profit_ok(estimate) else 'waiting')
                    row['exit_net_pnl_usdc'] = estimate['net_pnl_usdc']
                    row['exit_reference_net_pnl_usdc'] = estimate.get('reference_net_pnl_usdc')
                    row['exit_movement_cost_usdc'] = estimate.get('movement_cost_usdc')
                    row['exit_closing_fee_usdc'] = estimate.get('closing_fee_usdc')
                    row['exit_estimate_at'] = estimate.get('observed_at')
                    row['exit_minimum_usdc'] = self.strategy.close_min_profit_usdc + self.strategy.close_cost_reserve_usdc
            row['entry_prices'] = [p.entry_price_variational, p.entry_price_lighter]
            row['entry_accounting'] = p.entry_accounting
            rows.append(row)
        payload = {'worker_pid': os.getpid(), 'started_at': self.started_at, 'updated_at': utc_now(),
                   'running': not self.closed, 'entry_paused': self.entry_blocked, 'positions': rows,
                   'stop_reason': self.strategy.halted_reason,
                   'profit_exit_guard_version': 3, 'entry_scan_pipeline_version': 10,
                   'entry_profit_model_version': 2 if self.strategy.target_return_enabled else 1,
                   'entry_budget_policy_version': 2,
                   'channel_policy_version': 1, 'execution_latency_version': 2,
                   'manual_close_version': getattr(self.strategy, 'manual_close_version', 0),
                   'entry_limits': self.strategy.entry_limits_status(),
                   'entry_scan': self.strategy.entry_scan_status()}
        if getattr(self.strategy, 'runtime_scope', None):
            payload['runtime_scope'] = self.strategy.runtime_scope
        diagnostic_storage = getattr(self.strategy.history, 'diagnostic_storage', None)
        if isinstance(diagnostic_storage, dict) and diagnostic_storage:
            payload['history_storage'] = dict(diagnostic_storage)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.path.parent, prefix='.position-monitor-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            os.replace(path, self.path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def close(self):
        self.closed = True
        self.publish()


def read_position_monitor(path, worker_pid=None):
    try:
        if path.stat().st_size > 200000:
            return None
        data = json.loads(path.read_text())
        if worker_pid is None or data.get('worker_pid') != worker_pid:
            return None
        age = age_seconds(data.get('updated_at'))
        data['heartbeat_age_seconds'] = age
        data['heartbeat_stale'] = age is None or age > 3 or not data.get('running')
        data['heartbeat_status'] = ('stopped' if not data.get('running') else
                                    'stale' if data['heartbeat_stale'] else 'live')
        for row in data.get('positions', []):
            row['heartbeat_status'] = data['heartbeat_status']
            row['monitor_stop_reason'] = data.get('stop_reason')
        return data
    except (OSError, ValueError, TypeError):
        return None
