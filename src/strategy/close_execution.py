"""Parallel reduce-only exits; only the coordinator mutates durable positions."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
import math
import time

from src.common.market_data import age_seconds
from src.execution.orders import direction, fresh_quote
from src.strategy.position_routes import for_position


class CloseRetryReads:
    """Overlap required residual account/quote reads; never place orders."""
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='close-retry-read')
        self.pending = {}
        self.quotes = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def token(position, name, side, size):
        previous = position.close_order_results.get(name, {})
        return (position.id, position.symbol, name, side, size,
                position.close_attempts.get(name), previous.get('client_order_id'))

    def start(self, position, leg, *, quote=False):
        name, trader, side, size = leg
        token = self.token(position, name, side, size)
        # Only request a quote once the coordinator knows a retry is needed.
        # Peer-receipt speculation remains account-only; unknown/zero-pair
        # results must not cause an unnecessary market request.
        quote_reader = getattr(trader, 'get_executable_quote', None)
        if (quote and getattr(trader, 'supports_close_quote_reuse', False) is True
                and callable(quote_reader) and (name not in self.quotes or self.quotes[name][0] != token)):
            self.quotes[name] = (token, self.pool.submit(
                copy_context().run, quote_reader, position.symbol, size))
        if name in self.pending and self.pending[name][0] == token:
            return
        reader = getattr(trader, 'position_snapshot', None)
        if not callable(reader):
            return
        symbol = position.symbol
        def read():
            started = time.monotonic()
            return reader(symbol), started
        self.pending[name] = (token, self.pool.submit(copy_context().run, read))

    def snapshot(self, position, leg):
        name, trader, side, size = leg
        cached = self.pending.pop(name, None)
        if cached and cached[0] == self.token(position, name, side, size):
            snapshot, started = cached[1].result()
            # A slow peer must not turn speculative evidence into a stale
            # authorization. The coordinator still checks exact size/side.
            if 0 <= time.monotonic() - started <= 2:
                return snapshot
        return trader.position_snapshot(position.symbol)

    def take_quote(self, position, leg):
        name, trader, side, size = leg
        cached = self.quotes.pop(name, None)
        if cached is None or cached[0] != self.token(position, name, side, size):
            return None
        return cached[1]

    @staticmethod
    def resolve_quote(pending, symbol, size):
        if pending is None:
            return None
        try:
            quote = pending.result()
            fresh_quote(quote, symbol, size)
            age = age_seconds(quote['updated_at'])
            if age is not None and 0 <= age <= 2:
                return quote
        except Exception:
            # Optional overlap is not authorization. The normal close method
            # obtains another quote if this read failed or became stale.
            pass
        return None

    def quote(self, position, leg):
        return self.resolve_quote(self.take_quote(position, leg), position.symbol, leg[3])


class CloseExecutionMixin:
    @for_position
    def _close_legs(self, position):
        legs = []
        for name, trader, side in (
                ('variational', self.variational_trader, position.variational_side),
                ('lighter', self.lighter_trader, position.lighter_side)):
            size = float(getattr(position, name + '_size'))
            if not math.isfinite(size) or size < 0:
                raise ValueError('本地剩余持仓数量无效，需核对账户')
            if size > 1e-12:
                legs.append((name, trader, side, size))
        return legs

    def _verify_close_retry(self, position, legs, retry_reads=None, *, prepare_quotes=True):
        """Require terminal evidence and a matching fresh account before retry."""
        # Validate every terminal/attempt limit before starting any account GET.
        for name, trader, side, size in legs:
            previous = position.close_order_results.get(name, {})
            if previous.get('confirmed') is not True or previous.get('status') not in (
                    'filled', 'partial', 'canceled', 'rejected'):
                raise ValueError('上一笔平仓终态未确认，禁止重发')
            if position.close_attempts.get(name, 0) >= 1 + self.single_leg_recovery_max_attempts:
                raise ValueError('平仓重试次数已达上限，需核对账户')
            reader = getattr(trader, 'position_snapshot', None)
            if not callable(reader):
                raise ValueError('缺少账户持仓核验，禁止平仓重试')
        if retry_reads is None:
            with CloseRetryReads() as reads:
                return self._verify_close_retry(position, legs, reads, prepare_quotes=prepare_quotes)
        for leg in legs:
            retry_reads.start(position, leg, quote=prepare_quotes)
        for name, trader, side, size in legs:
            snapshot = retry_reads.snapshot(position, (name, trader, side, size))
            qty = float(snapshot['qty'])
            if not math.isfinite(qty) or qty < 0:
                raise ValueError('账户持仓数量无效')
            if snapshot.get('fetched_at') and (age_seconds(snapshot['fetched_at']) is None or
                                              not 0 <= age_seconds(snapshot['fetched_at']) <= 5):
                raise ValueError('账户持仓核验已过期')
            peers = [p for p in self.positions if p.symbol == position.symbol and p.status != 'closed']
            if position not in peers:
                peers.append(position)
            sizes = [float(getattr(p, name + '_size')) for p in peers]
            if any(not math.isfinite(value) or value < 0 for value in sizes):
                raise ValueError('本地剩余持仓数量无效，需核对账户')
            expected = sum(sizes)
            if any(getattr(p, name + '_size') > 1e-12 and
                   self._snapshot_side(getattr(p, name + '_side')) != self._snapshot_side(side) for p in peers):
                raise ValueError('同市场持仓方向不一致')
            if (abs(qty - expected) > max(1e-8, expected * 1e-6) or
                    self._snapshot_side(snapshot.get('side')) != self._snapshot_side(side)):
                raise ValueError('账户持仓与剩余敞口不符，需核对手动成交')

    def _parallel_close_batch(self, position, legs, grid_plan=None, retry_reads=None):
        if grid_plan:
            # Check BOTH before either transport is launched. If both fail
            # to fill, the grid can wait for another profitable exit plan.
            for name, trader, side, size in legs:
                fresh_quote(grid_plan['legs'][name]['quote'], position.symbol, size)
        # Consume proof-bound read results before changing attempt/client ids.
        retry_quotes = {leg[0]: retry_reads.take_quote(position, leg) for leg in legs
                        if retry_reads is not None and not grid_plan}
        jobs = []
        for name, trader, side, size in legs:
            attempt = position.close_attempts.get(name, 0) + 1
            client_id = self._close_client_order_id(position, name)
            position.close_attempts[name] = attempt
            position.close_order_results[name] = dict(
                confirmed=False, status='submitting', client_order_id=client_id,
                requested_size=size, attempt=attempt)
            jobs.append((name, trader, side, size, client_id, attempt))
        self._save()  # Persist every intent before any thread can submit.
        self.history.append('close_batch_started', symbol=position.symbol, position_id=position.id,
                            reason='平仓腿并发只减仓；分别核对终态与实际成交量')

        def submit(job):
            name, trader, side, size, client_id, attempt = job
            if grid_plan:
                plan = grid_plan['legs'][name]
                return trader.place_order(position.symbol, plan['side'], size,
                    price=plan['reference_price'], slippage_bps=plan['slippage_bps'],
                    quote=plan['quote'], reduce_only=True, client_order_id=client_id)
            # Resolve each leg in its own transport worker: a slow peer quote
            # must not hold the other verified residual leg before sending.
            quote = CloseRetryReads.resolve_quote(retry_quotes.get(name), position.symbol, size)
            close_from_quote = getattr(trader, 'close_position_from_quote', None)
            if quote is not None and callable(close_from_quote):
                return close_from_quote(position.symbol, side, size, client_order_id=client_id,
                    quote=quote, reduce_only=True, slippage_bps=self.slippage_tolerance)
            if quote is not None and callable(getattr(trader, 'place_order', None)):
                close_side = 'sell' if direction(side) == 'buy' else 'buy'
                return trader.place_order(position.symbol, close_side, size,
                    price=quote['bid' if close_side == 'sell' else 'ask'],
                    slippage_bps=self.slippage_tolerance, quote=quote,
                    reduce_only=True, client_order_id=client_id)
            return trader.close_position(position.symbol, side, size=size,
                reduce_only=True, client_order_id=client_id, slippage_bps=self.slippage_tolerance)

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {pool.submit(copy_context().run, submit, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    receipt, error = future.result(), None
                except Exception as exc:
                    receipt, error = None, exc
                yield job, receipt, error
                # The coordinator has applied and persisted this receipt by
                # the time the generator resumes. Start required remainder
                # reads before the coordinator's retry-validation stage.
                if retry_reads is not None:
                    has_fills = self._exit_has_fills(position)
                    waiting = any(not peer.done() for peer in futures)
                    for name, trader, side, _, _, _ in jobs:
                        remaining = getattr(position, name + '_size')
                        previous = position.close_order_results.get(name, {})
                        if (remaining > 1e-12 and (waiting or has_fills)
                                and previous.get('confirmed') is True
                                and previous.get('status') in ('filled', 'partial', 'canceled', 'rejected')
                                and position.close_attempts.get(name, 0) < 1 + self.single_leg_recovery_max_attempts):
                            # Quotes are only prefetched after an actual fill
                            # commits this group to residual recovery.
                            retry_reads.start(position, (name, trader, side, remaining), quote=has_fills)

    @for_position
    def _apply_close_receipt(self, position, name, side, requested, receipt):
        filled = self._fill(receipt, requested)
        order_id = receipt.get('order_id')
        previous_ids = position.close_receipt_order_ids.setdefault(name, [])
        if order_id and order_id in previous_ids:
            raise ValueError('重复平仓回执，禁止重复扣减持仓')
        trader = self.variational_trader if name == 'variational' else self.lighter_trader
        self.close_liquidity.record(self._venue_name(trader, name), position.symbol,
                                    'sell' if direction(side) == 'buy' else 'buy', receipt, requested)
        self._record_receipt('close_receipt', position, self._venue_name(
            self.variational_trader if name == 'variational' else self.lighter_trader, name), receipt)
        if order_id and filled:
            previous_ids.append(order_id)
        position.close_order_results[name].update({key: receipt.get(key) for key in (
            'confirmed', 'status', 'filled_size', 'average_price', 'order_id', 'submitted', 'platform_status')})
        setattr(position, name + '_size', max(0, requested - filled))
        if filled:
            already_closed = max(0, float(getattr(position, 'entry_size_' + name) or requested) - requested)
            previous_price = getattr(position, 'close_price_' + name)
            average = float(receipt['average_price'])
            if already_closed and previous_price is not None:
                average = (already_closed * previous_price + filled * average) / (already_closed + filled)
            elif already_closed:
                average = None  # Missing earlier fill evidence remains unknown.
            setattr(position, 'close_price_' + name, average)
            entry = getattr(position, 'entry_price_' + name)
            sign = 1 if side in ('long', 'buy') else -1
            try:
                fees = float(receipt['fees_usdc'])
                if not math.isfinite(fees):
                    raise ValueError('费用缺失')
            except (ValueError, TypeError, KeyError):
                position.accounting_pending = True
                if name not in position.pending_fee_venues:
                    position.pending_fee_venues.append(name)
            else:
                leg_pnl = sign * (float(receipt['average_price']) - entry) * filled - fees
                self.daily_pnl += leg_pnl
                position.realized_pnl_usdc = (position.realized_pnl_usdc or 0.0) + leg_pnl
                try:
                    funding = float(receipt['funding_usdc'])
                    if not math.isfinite(funding):
                        raise ValueError('资金费缺失')
                except (ValueError, TypeError, KeyError):
                    if name not in position.pending_funding_venues:
                        position.pending_funding_venues.append(name)
                    if self.config.get('funding_accounting_mode') != 'defer_settlement':
                        position.accounting_pending = True
                else:
                    self.daily_pnl += funding
                    position.realized_pnl_usdc = (position.realized_pnl_usdc or 0.0) + funding
                    if position.grid_cycle_id:
                        position.funding_settlements[name] = {
                            'funding_usdc': funding, 'complete': True, 'allocation': 'order_receipt'}
        self._save()
        return filled
