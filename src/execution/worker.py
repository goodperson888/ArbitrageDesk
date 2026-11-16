"""One executor process; default dry run. Stopping never claims to flatten."""
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from pathlib import Path
from src.execution.journal import execution_lease


class _ReferenceProbe:
    def __init__(self, data):
        self.data = data


class ExecutionWorker:
    def __init__(self, strategy, symbols, lock_path='data/execution.lock',
                 kill_path='data/STOP_EXECUTION', live=False, interval=1,
                 target_notional=0, candidate_fallback=True,
                 single_position_cycle=True, gross_spread_min_bps=0,
                 gross_spread_max_bps=0, close_only=False):
        self.strategy, self.symbols = strategy, tuple(symbols)
        self.lock_path, self.kill_path = lock_path, Path(kill_path)
        self.live, self.interval = live, max(1, interval)
        try:
            target = float(target_notional or 0)
        except (TypeError, ValueError):
            target = 0.0
        self.target_notional = target if math.isfinite(target) and target > 0 else 0.0
        self.target_filled_notional = 0.0
        self.base_position_size = getattr(strategy, 'position_size', None)
        self.candidate_fallback = bool(candidate_fallback)
        self.single_position_cycle = bool(single_position_cycle)
        self.strategy.runtime_single_position_cycle = self.single_position_cycle
        self.close_only = bool(close_only)
        self.gross_spread_min_bps = self._bound_spread(gross_spread_min_bps, 0.0)
        self.gross_spread_max_bps = self._bound_spread(gross_spread_max_bps, None)
        self.stop_event = threading.Event()
        self.quote_event = threading.Event()
        self._quote_watches = set()
        self._discovery_pool = None
        self._discovery_pending = {}
        self._discovery_offset = 0
        self._discovery_retry_at = {}
        self._discovery_failures = {}
        self._discovery_next_launch_at = 0.0
        self._discovery_priority_probe_at = None
        self.discovery_budget = 1.0
        self.discovery_max_age = 2.0
        self._preparation_pool = None
        self._preparation_pending = {}
        self._preparation_hosts = {}
        self._preparation_ready = {}
        self._preparation_retry_at = {}
        self.preparation_budget = .25
        self._continuous_limit_updates = False
        self.scope_controller = None
        self.manual_close_controller = None

    @contextmanager
    def _continuous_limits(self):
        self._continuous_limit_updates = True
        try:
            yield
        finally:
            self._continuous_limit_updates = False

    def _saved_limits_block_entry(self):
        if self._continuous_limit_updates:
            self._reload_entry_configuration()
            if self.target_notional:
                self.strategy.position_size = min(float(self.base_position_size),
                    max(0, self.target_notional - self.target_filled_notional))
        capacity = getattr(self.strategy, 'entry_capacity_reason', None)
        return callable(capacity) and isinstance(capacity(), str)

    @contextmanager
    def _discovery_session(self):
        account_adapters = []
        previous_watch = getattr(self.strategy, 'entry_wait_callback', None)
        try:
            self.strategy.entry_wait_callback = self._monitor_during_entry_wait
            if self.live and not self.close_only:
                traders = []
                configured_pairs = getattr(self.strategy, '_route_trader_pairs', None)
                # Test doubles and older strategy implementations may expose
                # this attribute as a truthy Mock/sentinel.  Only a concrete
                # sequence is a route list; otherwise retain the historical
                # two-adapter fallback.
                pairs = (configured_pairs if isinstance(configured_pairs, (list, tuple))
                         else [(
                             getattr(self.strategy, 'variational_trader', None),
                             getattr(self.strategy, 'lighter_trader', None))])
                for pair in pairs:
                    for trader in pair:
                        if trader is not None and not any(trader is item for item in traders):
                            traders.append(trader)
                for trader in traders:
                    if callable(getattr(type(trader), 'start_account_snapshots', None)):
                        account_adapters.append(trader)
                        trader.start_account_snapshots()
                        cache = getattr(trader, '_account_snapshot_cache', None)
                        if cache is not None:
                            cache.background_ready = lambda: not any(
                                p.status != 'closed' for p in self._positions())
            yield
        finally:
            self.strategy.entry_wait_callback = previous_watch
            for feed, symbol in self._quote_watches:
                feed.unwatch_quotes(symbol, self.quote_event)
            self._quote_watches.clear()
            for trader in account_adapters:
                try:
                    trader.stop_account_snapshots()
                except Exception:
                    pass  # Continue closing peer readers and discovery pools.
            if self._preparation_pool is not None:
                self._preparation_pool.shutdown(wait=False, cancel_futures=True)
                self._preparation_pool = None
            self._preparation_pending.clear()
            self._preparation_hosts.clear()
            self._preparation_ready.clear()
            self._preparation_retry_at.clear()
            if self._discovery_pool is not None:
                self._discovery_pool.shutdown(wait=False, cancel_futures=True)
                self._discovery_pool = None
            self._discovery_pending.clear()
            self._discovery_retry_at.clear()
            self._discovery_failures.clear()
            self._discovery_next_launch_at = 0.0
            self._discovery_priority_probe_at = None

    def _discover_parallel(self, symbols=None, wait_budget=None, candidate_priority=False, paced=False):
        symbols = self.symbols if symbols is None else tuple(symbols)
        allowed = set(symbols)
        # A recovered stream supersedes an older probe, including a late
        # completion. Cancel queued work; let running reads finish once.
        for symbol, (future, _) in list(self._discovery_pending.items()):
            if symbol not in allowed and (future.done() or future.cancel()):
                del self._discovery_pending[symbol]
        for symbol in set(self._discovery_retry_at) - allowed:
            self._discovery_retry_at.pop(symbol, None)
            self._discovery_failures.pop(symbol, None)
        # Missing markets get a recovery probe even when cached candidates
        # persist, but cannot consume the first candidate's next REST slot.
        if candidate_priority:
            if self._discovery_priority_probe_at is None:
                self._discovery_priority_probe_at = time.monotonic() + 10
        else:
            self._discovery_priority_probe_at = None
        if not symbols:
            return []
        if self._discovery_pool is None:
            self._discovery_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='entry-discovery')
        deadline = time.monotonic() + (self.discovery_budget if wait_budget is None else wait_budget)
        attempted = set(self._discovery_pending)
        candidates = []
        def discover(symbol):
            try:
                if (getattr(self.strategy, 'multi_route_enabled', False) is True and
                        callable(getattr(type(self.strategy), 'probe_reference_prices', None))):
                    result = _ReferenceProbe(self.strategy.probe_reference_prices(symbol))
                else:
                    result = self.strategy.check_arbitrage_opportunity(symbol)
                return result, time.monotonic(), None
            except Exception as exc:
                recorder = getattr(self.strategy, 'record_entry_scan_failure', None)
                if callable(recorder):
                    recorder(exc)
                return None, time.monotonic(), exc
        while not self.stop_event.is_set() and not self.kill_path.exists():
            if self._entry_scan_backoff():
                break
            for symbol, (future, started) in list(self._discovery_pending.items()):
                if future.done():
                    del self._discovery_pending[symbol]
                    result, completed, *failure = future.result()
                    exc = failure[0] if failure else None
                    delay = 2.0
                    if exc is not None:
                        wait_seconds = max(float(getattr(exc, 'wait_seconds', 0)),
                                           float(getattr(exc, 'retry_after_seconds', 0)))
                        if wait_seconds:
                            # Expected local pacing/market cooldowns are not
                            # consecutive transport failures.
                            delay = wait_seconds
                        else:
                            failures = min(5, self._discovery_failures.get(symbol, 0) + 1)
                            self._discovery_failures[symbol] = failures
                            delay = min(60, 5 * 2 ** (failures - 1))
                    else:
                        self._discovery_failures.pop(symbol, None)
                    if symbol in allowed:
                        self._discovery_retry_at[symbol] = completed + delay
                    if symbol in allowed and result is not None and time.monotonic() - started <= self.discovery_max_age:
                        if isinstance(result, _ReferenceProbe):
                            result = self.strategy.complete_reference_probe(result.data)
                        if result is not None:
                            candidates.append(result)
            for _ in range(len(symbols)):
                if len(self._discovery_pending) >= 4:
                    break
                now = time.monotonic()
                if paced and (now < self._discovery_next_launch_at or
                        (candidate_priority and now < self._discovery_priority_probe_at)):
                    break
                self._discovery_offset %= len(symbols)
                symbol = symbols[self._discovery_offset]
                self._discovery_offset = (self._discovery_offset + 1) % len(symbols)
                if symbol in attempted or now < self._discovery_retry_at.get(symbol, 0):
                    continue
                attempted.add(symbol)
                readiness = getattr(self.strategy, 'reference_probe_wait_seconds', None)
                delay = readiness(symbol) if callable(readiness) else 0
                if isinstance(delay, (int, float)) and delay > 0:
                    self._discovery_retry_at[symbol] = now + delay
                    continue
                self._discovery_pending[symbol] = (
                    self._discovery_pool.submit(discover, symbol), time.monotonic())
                # Coordinate probes before launching threads, rather than
                # repeatedly discovering the shared REST limit by exception.
                if paced:
                    self._discovery_next_launch_at = now + 2
                    if candidate_priority:
                        self._discovery_priority_probe_at = now + 10
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._discovery_pending:
                break
            wait([item[0] for item in self._discovery_pending.values()],
                 timeout=min(remaining, .05), return_when=FIRST_COMPLETED)
        return candidates

    @staticmethod
    def _bound_spread(value, default):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if default is None and number == 0:
            return None
        return number if math.isfinite(number) and number >= 0 else default

    def stop(self):
        self.stop_event.set()
        self.quote_event.set()

    def _positions(self):
        """Return a stable position snapshot for real and test adapters."""
        try:
            return list(self.strategy.positions)
        except (AttributeError, TypeError):
            return []

    def _wait_cycle(self):
        """Do not add a whole interval AFTER the work has already taken it.

        Fresh native updates wake holdings at up to twenty passes a second;
        quiet feeds use a quarter-second fallback. Monitor singleflight,
        account/funding caches and REST pacing still govern actual requests.
        """
        active = any(p.status != 'closed' for p in self._positions())
        target = min(self.interval, .25) if active else self.interval
        started = getattr(self, '_cycle_started', None)
        elapsed = max(0, time.monotonic()-started) if started is not None else 0
        desired = set()
        resolver = getattr(type(self.strategy), 'position_traders', None)
        for position in self._positions():
            if position.status != 'open' or not callable(resolver):
                continue
            try:
                for trader in resolver(self.strategy, position):
                    feed = getattr(trader, '_reference_feed', None)
                    if hasattr(feed, 'by_symbol'):
                        feed = feed.by_symbol.get(position.symbol)
                    if callable(getattr(type(feed), 'watch_quotes', None)):
                        desired.add((feed, position.symbol))
            except ValueError:
                continue
        for feed, symbol in self._quote_watches - desired:
            feed.unwatch_quotes(symbol, self.quote_event)
        for feed, symbol in desired - self._quote_watches:
            feed.watch_quotes(symbol, self.quote_event)
        self._quote_watches = desired
        if not desired:
            self.stop_event.wait(max(.05, target-elapsed))
            return
        # Coalesce bursts and cap coordinator work at 20 passes/sec. Quiet or
        # disconnected feeds keep the existing quarter-second fallback.
        self.stop_event.wait(.05)
        if not self.stop_event.is_set():
            self.quote_event.wait(max(0, target-elapsed-.05))
        self.quote_event.clear()

    def _monitor_during_entry_wait(self, entering_symbol):
        """Coordinator-only reads; never interleave another signed action.

        One entry is still being confirmed. Finish its hedge first; keep all
        other groups' quotes, profit checks and heartbeat current meanwhile.
        The normal cycle freshly revalidates any exit before sending it.
        """
        monitor = getattr(self.strategy, 'position_monitor', None)
        if monitor is None:
            return
        try:
            monitor.poll(self._positions(), wait_for_reads=False)
            for position in self._positions():
                if position.status == 'open' and position.symbol != entering_symbol:
                    self.strategy.close_reason(position)
            monitor.publish_tick()
        except Exception as exc:
            # Losing optional telemetry cannot abandon a submitted order or
            # replace its receipt with an exception from a monitoring read.
            now = time.monotonic()
            if now - getattr(self, '_entry_watch_error_at', -10) >= 5:
                self._entry_watch_error_at = now
                self.strategy.history.append_diagnostic('entry_wait_monitor_failed',
                    symbol=entering_symbol, reason=type(exc).__name__)

    def _candidate_opportunities(self):
        """Choose the strongest current discovery edge across the scan scope.

        The strategy still re-quotes, sizes and checks net costs immediately
        before submission; this ranking only decides which market gets that
        expensive execution attempt first.
        """
        if self.stop_event.is_set() or self.kill_path.exists() or self._entry_scan_backoff():
            return []
        candidates = []
        symbols = self.symbols
        if getattr(self.strategy, 'parallel_discovery', False) is True:
            cached_scan = getattr(self.strategy, 'cached_discovery_opportunities', None)
            if callable(cached_scan):
                candidates, missing = cached_scan(self.symbols)
                if missing == self.symbols and not candidates:
                    candidates = self._discover_parallel(paced=True)
                else:
                    # Ready cached candidates never wait for a slow/missing
                    # market. Only missing symbols consume probe slots.
                    candidates.extend(self._discover_parallel(missing, wait_budget=0 if candidates else None,
                                                              candidate_priority=bool(candidates), paced=True))
            else:
                candidates = self._discover_parallel()
            if self.stop_event.is_set() or self.kill_path.exists() or self._entry_scan_backoff():
                return []
            symbols = ()
        for symbol in symbols:
            try:
                opportunity = self.strategy.check_arbitrage_opportunity(symbol)
            except Exception as exc:
                # Public reference reads are a safe, pre-submit stage.  A
                # transient HTTP/API failure for one symbol must not kill a
                # wait-once or continuous worker; the next symbol/cycle can
                # still be evaluated.  The strategy stage already records a
                # sanitized failure event for diagnostics.
                recorder = getattr(self.strategy, 'record_entry_scan_failure', None)
                if callable(recorder):
                    recorder(exc)
                if self._entry_scan_backoff():
                    break
                continue
            if opportunity:
                candidates.append(opportunity)
        def score(item):
            try:
                return float(getattr(item, 'spread_bps', 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        def rank(item):
            scorer = getattr(self.strategy, 'reversion_rank', None)
            if callable(scorer):
                try:
                    activate = getattr(self.strategy, 'activate_route_for_opportunity', None)
                    current = getattr(self.strategy, '_active_route_index', None)
                    if callable(activate):
                        activate(item)
                    value = scorer(item)
                    if current is not None:
                        restore = getattr(self.strategy, 'activate_route', None)
                        if callable(restore):
                            restore(current)
                    if isinstance(value, tuple) and len(value) >= 3:
                        return value
                except (TypeError, ValueError, ArithmeticError, RuntimeError):
                    if current is not None:
                        restore = getattr(self.strategy, 'activate_route', None)
                        if callable(restore):
                            restore(current)
                    pass
            return (1, score(item), 0.0, 0, None)
        # A very large reference spread is intentionally outside the
        # execution guard (it is often stale or a symbol/contract mismatch).
        # Do not let that outlier win the ranking forever while a smaller,
        # executable opportunity is available in the same scan.
        ceiling = getattr(self.strategy, 'spread_threshold_max', None)
        try:
            upper = self.gross_spread_max_bps if self.gross_spread_max_bps is not None else float(ceiling)
        except (TypeError, ValueError):
            upper = None
        minimum_fn = getattr(self.strategy, 'execution_min_bps', None)
        def candidate_minimum(item):
            fallback = getattr(self.strategy, 'spread_threshold_min', 0) or 0
            try:
                direction = ('sell_variational'
                             if getattr(item, 'spread_direction', '') == 'variational_higher'
                             else 'buy_variational')
                symbol = getattr(item, 'symbol', None)
                if callable(minimum_fn) and symbol:
                    value = minimum_fn(symbol, direction)
                    if isinstance(value, (int, float)):
                        return float(value)
            except (AttributeError, TypeError, ValueError):
                pass
            try:
                return float(fallback) if isinstance(fallback, (int, float)) else 0.0
            except (TypeError, ValueError):
                return 0.0
        if getattr(self.strategy, 'target_return_enabled', False) is True:
            # Negative opening spreads can have positive return to a signed
            # historical target. The same-size round-trip gate remains final.
            candidates = [item for item in candidates
                          if abs(score(item)) >= self.gross_spread_min_bps and
                          (self.gross_spread_max_bps is None or abs(score(item)) <= self.gross_spread_max_bps)]
        else:
            candidates = [item for item in candidates
                          if score(item) >= max(self.gross_spread_min_bps, candidate_minimum(item)) and
                          (upper is None or score(item) <= upper)]
        return sorted(candidates, key=rank, reverse=True)

    def _best_opportunity(self):
        return next(iter(self._candidate_opportunities()), None)

    def _entry_notional(self):
        """Return the confirmed notional of the newest open position."""
        open_positions = [p for p in self.strategy.positions if p.status != 'closed']
        if not open_positions:
            return 0.0
        position = max(open_positions, key=lambda item: item.open_time)
        values = []
        for size, price in ((position.variational_size, position.entry_price_variational),
                            (position.lighter_size, position.entry_price_lighter)):
            try:
                value = abs(float(size)) * abs(float(price))
                if value > 0:
                    values.append(value)
            except (TypeError, ValueError):
                pass
        return min(values) if values else 0.0

    def _attempt_candidates(self):
        """Try ranked opportunities until one submits or all safe attempts fail.

        A fallback is allowed only when the previous attempt was rejected before
        any order submission.  An ambiguous or partial submission always stops
        the worker to prevent duplicate exposure.
        """
        if self.target_notional:
            remaining = self.target_notional - self.target_filled_notional
            if remaining <= 1e-9:
                return False, None, 0, 0
            try:
                self.strategy.position_size = min(float(self.base_position_size), remaining)
            except (TypeError, ValueError):
                pass
        candidates = self._candidate_opportunities()
        if self._saved_limits_block_entry():
            return False, None, 0, len(candidates)
        if getattr(self.strategy, 'parallel_preparation', False) is True:
            return self._attempt_prepared_candidates(candidates)
        for index, opportunity in enumerate(candidates):
            if self.stop_event.is_set() or self.kill_path.exists() or self._entry_scan_backoff():
                break
            if self._saved_limits_block_entry():
                break
            try:
                activate = getattr(self.strategy, 'activate_route_for_opportunity', None)
                if callable(activate):
                    activate(opportunity)
                result = self.strategy.execute_arbitrage(opportunity)
            except Exception:
                raise
            submitted = getattr(self.strategy, 'last_execution_submitted', False) is True
            if result or submitted:
                return result, opportunity, index + 1, len(candidates)
            if self._entry_scan_backoff():
                break
            if not self.candidate_fallback:
                break
            if index + 1 < len(candidates):
                self.strategy.history.append(
                    'candidate_fallback', symbol=opportunity.symbol,
                    reason='候选未提交订单，切换下一条合格机会',
                    next_symbol=candidates[index + 1].symbol,
                    candidate_rank=index + 1, candidate_total=len(candidates))
        return False, None, 0, len(candidates)

    def _reload_entry_configuration(self):
        """Only the coordinator applies saves, never an in-flight order thread."""
        reload_config = getattr(self.strategy, 'reload_runtime_config', None)
        if not callable(reload_config):
            return
        before = getattr(self.strategy, 'max_positions', None)
        try:
            changed = reload_config()
        except (OSError, ValueError, TypeError):
            return
        if changed is not True:
            return
        # Saving a new group limit is an explicit request to use that limit.
        # Migrate the old single-cycle launch flag so it cannot silently keep
        # a 1 -> 2 edit from opening another group.
        if before != getattr(self.strategy, 'max_positions', None):
            self.single_position_cycle = False
        self.strategy.runtime_single_position_cycle = self.single_position_cycle
        self.base_position_size = self.strategy.position_size
        self._preparation_ready.clear()
        self._preparation_retry_at.clear()

    def _collect_prepared(self, candidates, attempted):
        """Bounded read-only preparation; pending requests survive scan ticks."""
        if not self.candidate_fallback:
            candidates = candidates[:1]
        if self._preparation_pool is None:
            self._preparation_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='entry-prepare')
        deadline = time.monotonic() + self.preparation_budget
        key = self.strategy.entry_preparation_key()
        allowed = {op.symbol for op in candidates}
        for symbol, (due, retry_key) in list(self._preparation_retry_at.items()):
            if symbol not in allowed or retry_key != key or time.monotonic() >= due:
                del self._preparation_retry_at[symbol]
            else:
                # A recovered native stream need not wait for an old REST
                # admission delay. This hook reads local snapshots only.
                readiness = getattr(self.strategy, 'entry_preparation_admission', None)
                op = next((op for op in candidates if op.symbol == symbol), None)
                admission = readiness(op) if callable(readiness) and op is not None else None
                if isinstance(admission, dict) and not admission.get('rest_hosts') and not admission.get('wait_seconds'):
                    del self._preparation_retry_at[symbol]
        for symbol, item in list(self._preparation_ready.items()):
            if symbol not in allowed or symbol in attempted or item['key'] != key or time.monotonic()-item['prepared_at'] > 1:
                del self._preparation_ready[symbol]
        submitted = set()
        while not self.stop_event.is_set() and not self.kill_path.exists() and not self._entry_scan_backoff():
            for symbol, (future, started) in list(self._preparation_pending.items()):
                if not future.done():
                    continue
                del self._preparation_pending[symbol]
                self._preparation_hosts.pop(symbol, None)
                try:
                    item = future.result()
                except Exception as exc:
                    self.strategy.record_entry_scan_failure(exc)
                    attempted.add(symbol)
                    continue
                if item.get('error'):
                    attempted.add(symbol)
                    retry = item.get('retry_after_seconds', 0)
                    if retry > 0 and symbol in allowed and item['key'] == key:
                        self._preparation_retry_at[symbol] = (item['prepared_at'] + retry, key)
                elif (symbol in allowed and symbol not in attempted and item['key'] == key
                      and time.monotonic()-started <= 2 and time.monotonic()-item['prepared_at'] <= 1):
                    self._preparation_ready[symbol] = item
            limit = 3 if self.candidate_fallback else 1
            for op in candidates:
                if len(self._preparation_pending) >= limit:
                    break
                if (op.symbol in attempted or op.symbol in submitted or op.symbol in self._preparation_ready
                        or op.symbol in self._preparation_pending or op.symbol in self._preparation_retry_at):
                    continue
                readiness = getattr(self.strategy, 'entry_preparation_admission', None)
                admission = readiness(op) if callable(readiness) else None
                hosts = set(admission.get('rest_hosts', ())) if isinstance(admission, dict) else set()
                busy = set().union(*self._preparation_hosts.values()) if self._preparation_hosts else set()
                if hosts & busy:
                    continue
                delay = admission.get('wait_seconds', 0) if isinstance(admission, dict) else 0
                if delay > 0:
                    self._preparation_retry_at[op.symbol] = (time.monotonic() + delay, key)
                    continue
                submitted.add(op.symbol)
                self._preparation_hosts[op.symbol] = hosts
                self._preparation_pending[op.symbol] = (
                    self._preparation_pool.submit(self.strategy.prepare_entry_candidate, op), time.monotonic())
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self._preparation_pending:
                break
            wait([future for future, _ in self._preparation_pending.values()],
                 timeout=min(remaining, .025), return_when=FIRST_COMPLETED)
        if self.stop_event.is_set() or self.kill_path.exists() or self._entry_scan_backoff():
            return None
        if not self._preparation_ready:
            return None
        best = max(self._preparation_ready.values(), key=lambda item: (
            item['plan'][0]['estimated_entry_edge_usdc'], item['plan'][0]['net_edge_bps'],
            item.get('recency_rank', 0), item['prepared_at']))
        del self._preparation_ready[best['opportunity'].symbol]
        return best

    def _attempt_prepared_candidates(self, candidates):
        attempted, tries, total = set(), 0, len(candidates)
        # Only account checks/submission are serial. A rejection always
        # refreshes discovery/ranking instead of walking an old candidate list.
        for _ in range(3):
            prepared = self._collect_prepared(candidates, attempted)
            if prepared is None:
                break
            opportunity = prepared['opportunity']
            if self._saved_limits_block_entry():
                break
            attempted.add(opportunity.symbol)
            tries += 1
            result = self.strategy.execute_prepared_candidate(prepared)
            if result or getattr(self.strategy, 'last_execution_submitted', False) is True:
                return result, opportunity, tries, total
            if not self.candidate_fallback or self._entry_scan_backoff() or self.stop_event.is_set() or self.kill_path.exists():
                break
            budget = self.discovery_budget
            try:
                self.discovery_budget = min(budget, .1)
                candidates = [op for op in self._candidate_opportunities() if op.symbol not in attempted]
            finally:
                self.discovery_budget = budget
            total = max(total, len(candidates))
        return False, None, 0, total

    def _target_reached(self):
        return bool(self.target_notional and
                    self.target_filled_notional + 1e-9 >= self.target_notional)

    def _entry_scan_backoff(self):
        getter = getattr(self.strategy, 'entry_scan_backoff_seconds', None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter()))
        except (TypeError, ValueError):
            return 0

    def _result_note(self, base):
        if self.target_notional:
            return (base + '；累计确认入场 %.2f / 目标 %.2f USDC' %
                    (self.target_filled_notional, self.target_notional))
        return base

    def _stop_requires_reconciliation(self):
        """Exit only at a cycle boundary; never interrupt a synchronous send."""
        if not self.kill_path.exists():
            return False
        pending = [p for p in self._positions() if p.status not in ('open', 'closed')]
        if not pending and not self.strategy.halted_reason:
            return False
        self.strategy.history.append(
            'execution_stop_requires_reconcile',
            reason='执行进程停止；未核对持仓和订单保留，不能据此认定已平仓',
            position_ids=[getattr(p, 'id', None) for p in pending])
        return True

    def _finish_single_cycle(self):
        """Manage the one newly opened hedge until it is flat.

        A stop request only disables future entries. It must not strand the
        hedge this single validation already opened; the strategy's maximum
        holding-time protection remains the final safety exit.
        """
        while not self.stop_event.is_set():
            monitor = getattr(self.strategy, 'position_monitor', None)
            if monitor is not None:
                monitor.poll(self._positions())
            account_ready = True
            reconcile = getattr(self.strategy, 'reconcile_open_positions', None)
            if callable(reconcile):
                state = reconcile()
                if state == 'halted':
                    return False
                if state == 'unavailable':
                    account_ready = False
            if self.manual_close_controller is not None:
                self.manual_close_controller.poll(account_ready=account_ready)
            active = [position for position in self.strategy.positions
                      if position.status != 'closed']
            if not active:
                return True
            for position in list(active):
                if position.status != 'open':
                    return False
                reason = self.strategy.close_reason(position)
                if reason and account_ready:
                    if monitor is not None:
                        monitor.closing(position)
                    self.strategy.close_position(position, reason=reason)
                    if position.status == 'reconcile_required':
                        return False
            if monitor is not None:
                monitor.publish_tick()
            if not any(position.status != 'closed' for position in self.strategy.positions):
                return True
            self._wait_cycle()
        return False

    def run_once(self):
        """Open one hedge, wait for its close condition, then finish."""
        if self.close_only:
            return {'attempted': False, 'reason': '仅管理已有仓位模式不执行开仓验证，请启动只平仓监控'}
        if not self.live:
            return {'submitted': False, 'mode': 'dry_run', 'blockers': self.strategy.live_blockers()}
        blockers = self.strategy.live_blockers()
        if blockers:
            raise ValueError('；'.join(blockers))
        with execution_lease(self.lock_path), self._discovery_session():
            if self.kill_path.exists() or self.strategy.halted_reason:
                return {'attempted': False, 'reason': '停机标志或未核对状态'}
            if any(p.status != 'closed' for p in self.strategy.positions):
                raise ValueError('单次验收要求先核对已有持仓，不自动处理旧仓位')
            symbol = self.symbols[0] if len(self.symbols) == 1 else 'ALL'
            self.strategy.history.append('single_attempt_started', symbol=symbol,
                                         cap_usdc=self.strategy.position_size)
            try:
                result, opportunity, rank, total = self._attempt_candidates()
                if opportunity:
                    if result:
                        self.target_filled_notional += self._entry_notional()
                        closed = self._finish_single_cycle()
                        return {'attempted': True, 'result': closed,
                                'entry_result': True, 'closed': closed,
                                'candidate_rank': rank, 'candidate_total': total,
                                'note': self._result_note(
                                    '单次验证已完成一轮开仓与平仓' if closed else
                                    '单次验证已开仓但平仓尚未确认；请核对当前持仓')}
                    return {'attempted': True, 'result': result,
                            'candidate_rank': rank, 'candidate_total': total,
                            'note': self._result_note('本轮未确认双腿成交；未进入平仓阶段')}
                self.strategy.history.append('single_attempt_skipped', symbol=symbol, reason='无符合条件的机会')
                return {'attempted': False, 'reason': '无符合条件的机会，未发送订单'}
            except Exception:
                self.strategy.history.append('single_attempt_stopped', symbol=symbol,
                                             reason='执行异常，检查订单意图、成交回执与账户；不自动重试')
                raise

    def run_until_opportunity(self, max_cycles=None):
        """Wait for one opportunity, complete its hedge, then close it."""
        if self.close_only:
            return {'attempted': False, 'reason': '仅管理已有仓位模式不执行开仓验证，请启动只平仓监控'}
        if not self.live:
            return {'submitted': False, 'mode': 'dry_run', 'blockers': self.strategy.live_blockers()}
        blockers = self.strategy.live_blockers()
        if blockers:
            raise ValueError('；'.join(blockers))
        with execution_lease(self.lock_path), self._discovery_session():
            if any(p.status != 'closed' for p in self.strategy.positions):
                raise ValueError('自动单次验收要求先核对已有持仓，不自动处理旧仓位')
            cycles = 0
            symbol = self.symbols[0] if len(self.symbols) == 1 else 'ALL'
            self.strategy.history.append('wait_once_started', symbol=symbol,
                                         cap_usdc=self.strategy.position_size)
            while not self.stop_event.is_set():
                if self.kill_path.exists() or self.strategy.halted_reason:
                    return {'attempted': False, 'stopped': True,
                            'reason': '停机标志、停止请求或未核对状态；未发送订单'}
                try:
                    result, opportunity, rank, total = self._attempt_candidates()
                except (ValueError, RuntimeError):
                    result, opportunity, rank, total = False, None, 0, 0
                if opportunity:
                    try:
                        if result or getattr(self.strategy, 'last_execution_submitted', False) is True:
                            if result:
                                self.target_filled_notional += self._entry_notional()
                                closed = self._finish_single_cycle()
                                return {'attempted': True, 'result': closed,
                                        'entry_result': True, 'closed': closed,
                                        'candidate_rank': rank, 'candidate_total': total,
                                        'note': self._result_note(
                                            '单次验证已完成一轮开仓与平仓' if closed else
                                            '单次验证已开仓但平仓尚未确认；请核对当前持仓')}
                            return {'attempted': True, 'result': result,
                                    'candidate_rank': rank, 'candidate_total': total,
                                    'note': self._result_note('本轮未确认双腿成交；未进入平仓阶段')}
                        self.strategy.history.append('wait_once_skipped', symbol=symbol,
                                                     reason='本轮报价未达到可执行净价差；未提交订单，继续等待')
                    except Exception:
                        self.strategy.history.append('wait_once_stopped', symbol=symbol,
                            reason='执行异常，检查订单意图、成交回执与账户；不自动重试')
                        raise
                elif total:
                    self.strategy.history.append('wait_once_skipped', symbol=symbol,
                                                 reason='本轮报价未达到可执行净价差；未提交订单，继续等待')
                else:
                    # A candidate count of zero includes symbols rejected by
                    # the historical-reversion gate.  Record a heartbeat so
                    # the dashboard can distinguish deliberate waiting from
                    # a stalled worker.
                    self.strategy.history.append(
                        'wait_once_skipped', symbol=symbol,
                        reason='当前没有通过机会筛选的候选；未提交订单，继续等待')
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                self._wait_cycle()
        return {'attempted': False, 'stopped': True,
                'reason': self._result_note('等待结束时没有符合条件的机会，未发送订单')}

    def run(self, max_cycles=None):
        if not self.live:
            return {'submitted': False, 'mode': 'dry_run', 'blockers': self.strategy.live_blockers()}
        blockers = self.strategy.live_blockers()
        if blockers:
            raise RuntimeError('；'.join(blockers))
        with execution_lease(self.lock_path), self._discovery_session(), self._continuous_limits():
            cycles = 0
            while not self.stop_event.is_set():
                self._cycle_started = time.monotonic()
                if self._stop_requires_reconciliation():
                    break
                recover = getattr(self.strategy, 'recover_single_leg_positions', None)
                # A stop request is graceful: it blocks new entries, while
                # an existing hedge remains under the normal close monitor.
                # A strategy halt is different and must exit immediately for
                # reconciliation.
                if self.strategy.halted_reason:
                    recovery_state = recover() if callable(recover) else 'blocked'
                    if recovery_state in ('recovered', 'waiting'):
                        cycles += 1
                        if max_cycles is not None and cycles >= max_cycles:
                            break
                        self._wait_cycle()
                        continue
                    break
                # Existing positions are managed before discovering new entries.
                monitor = getattr(self.strategy, 'position_monitor', None)
                if monitor is not None:
                    monitor.poll(self._positions())
                account_ready = True
                reconcile = getattr(self.strategy, 'reconcile_open_positions', None)
                if callable(reconcile):
                    state = reconcile()
                    if state == 'halted':
                        recovery_state = recover() if callable(recover) else 'blocked'
                        if recovery_state in ('recovered', 'waiting'):
                            cycles += 1
                            if max_cycles is not None and cycles >= max_cycles:
                                break
                            self._wait_cycle()
                            continue
                        break
                    if state == 'unavailable':
                        account_ready = False
                manual_activity = False
                if self.manual_close_controller is not None:
                    manual_activity = self.manual_close_controller.poll(account_ready=account_ready)
                closed_any = manual_activity
                for position in self._positions():
                    if self.stop_event.is_set() or self.strategy.halted_reason:
                        break
                    reason = self.strategy.close_reason(position) if position.status == 'open' else None
                    if reason and account_ready:
                        if monitor is not None:
                            monitor.closing(position)
                        self.strategy.close_position(position, reason=reason)
                        closed_any = True
                # Apply saved entry limits even while draining or waiting on
                # accounts. This never clears a stop or changes open hedges.
                self._reload_entry_configuration()
                if self.scope_controller is not None:
                    self.scope_controller.poll()
                if monitor is not None:
                    monitor.publish_tick()
                if not account_ready:
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    self._wait_cycle()
                    continue
                active = any(p.status != 'closed' for p in self._positions())
                if self.kill_path.exists():
                    if self._stop_requires_reconciliation():
                        break
                    if not active:
                        self.strategy.history.append(
                            'execution_stop_completed',
                            reason='已停止新开仓；已有仓位已确认结束')
                        break
                    # Do not scan or submit another entry after a stop.  The
                    # next cycle only re-checks the still-open hedge.
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    self._wait_cycle()
                    continue
                if self._target_reached():
                    if not active:
                        self.strategy.history.append('target_reached',
                            target_notional=self.target_notional,
                            filled_notional=self.target_filled_notional,
                            reason='已达到目标累计入场金额，停止新开仓')
                        break
                    # Keep managing the already-open hedge until its normal
                    # close/reconciliation path completes; target completion
                    # must never strand an open position.
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    self._wait_cycle()
                    continue
                # In single-position mode a close and a new entry never happen
                # in the same cycle.  The next scan starts from fresh quotes.
                grid_mode = getattr(self.strategy, 'grid_enabled', False) is True
                can_open = (not self.close_only and (not active or not self.single_position_cycle or grid_mode))
                capacity = getattr(self.strategy, 'entry_capacity_reason', None)
                if callable(capacity) and isinstance(capacity(), str):
                    can_open = False
                if monitor is not None and monitor.entry_blocked:
                    can_open = False
                backoff = self._entry_scan_backoff()
                if can_open and not manual_activity and not (closed_any and (self.single_position_cycle or grid_mode)) and not backoff:
                    result, opportunity, rank, total = self._attempt_candidates()
                    if result:
                        self.target_filled_notional += self._entry_notional()
                        self.strategy.history.append('entry_cycle',
                            symbol=opportunity.symbol, candidate_rank=rank,
                            candidate_total=total,
                            filled_notional=self.target_filled_notional,
                            target_notional=self.target_notional or None,
                            single_position_cycle=self.single_position_cycle)
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                # Account-capacity failures use a bounded backoff to avoid
                # repeatedly scanning every symbol; active positions are
                # still checked on the normal short interval above.
                self._wait_cycle()
        stop_requested = self.kill_path.exists()
        active = any(p.status != 'closed' for p in self._positions())
        return {'stopped': True, 'positions': self.strategy.get_strategy_status()['positions'],
                'filled_notional': self.target_filled_notional,
                'target_notional': self.target_notional or None,
                'close_only': self.close_only,
                'note': self._result_note(
                    ('执行进程已停止；仓位或订单仍需核对，停止保护已保留' if active or self.strategy.halted_reason else
                     '已请求停止全局实盘；新开仓已停止，已有仓位已完成平仓') if stop_requested else
                    '停止执行不等于已平仓；未确认订单和持仓必须核对')}
