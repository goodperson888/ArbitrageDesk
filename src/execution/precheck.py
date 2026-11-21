"""Single-use account evidence confined to one coordinated entry attempt."""
import time
from contextlib import nullcontext
from contextvars import ContextVar
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from functools import wraps

from .history import TradeHistory
from .telemetry import stage

_expected_positions = ContextVar('grid_expected_positions', default=None)


def account_transaction(confirmed_return=False):
    """Invalidate around signed settings/cancels, including ambiguous errors."""
    def decorate(fn):
        @wraps(fn)
        def call(self, *args, **kwargs):
            with self.account_mutation() as mutation:
                result = fn(self, *args, **kwargs)
                mutation['confirmed'] = confirmed_return or (
                    isinstance(result, dict) and result.get('confirmed') is True)
                return result
        return call
    return decorate


@contextmanager
def expected_entry_positions(evidence):
    """Scope exact existing exposure to one coordinator execution, never a flag."""
    token = _expected_positions.set((TradeHistory.current_execution_id(), evidence))
    try:
        yield
    finally:
        _expected_positions.reset(token)


class EntryPrecheckMixin:
    supports_parallel_discovery = True
    execution_history = None
    precheck_max_age = 2.0

    def start_account_snapshots(self):
        if not callable(getattr(type(self), 'read_entry_account_state', None)):
            return
        from .account_snapshot import AccountSnapshotCache
        cache = getattr(self, '_account_snapshot_cache', None)
        if cache is None or cache.stop_event.is_set():
            previous = cache
            cache = AccountSnapshotCache(self.read_entry_account_state,
                                         self.account_snapshot_identity,
                                         self.account_snapshot_revision)
            if previous is not None:
                with previous.lock:
                    cache.uncertain = previous.uncertain or bool(previous.mutations)
                    cache.retry_at, cache.error = previous.retry_at, previous.error
                    cache.background_ready = previous.background_ready
            self._account_snapshot_cache = cache
        watch = getattr(type(self), 'start_account_watch', None)
        if callable(watch):
            self.start_account_watch()
        cache.start()

    def stop_account_snapshots(self):
        cache = getattr(self, '_account_snapshot_cache', None)
        if cache is not None:
            cache.stop()
        watch = getattr(type(self), 'stop_account_watch', None)
        if callable(watch):
            self.stop_account_watch()

    def account_snapshot_revision(self):
        return None

    def account_mutation(self):
        cache = getattr(self, '_account_snapshot_cache', None)
        return cache.mutation() if cache is not None else nullcontext({'confirmed': False})

    def account_reconciled(self, confirmed):
        cache = getattr(self, '_account_snapshot_cache', None)
        if cache is not None:
            cache.reconciled(confirmed)

    def release_entry_precheck(self):
        self.invalidate_entry_precheck()
        self._entry_capital_policy = None
        cache = getattr(self, '_account_snapshot_cache', None)
        if cache is not None:
            cache.release(TradeHistory.current_execution_id())

    def validate_entry_position(self, symbol, signed_quantity, side):
        actual = Decimal(str(signed_quantity))
        scope = _expected_positions.get()
        expected = None
        if scope and scope[0] and scope[0] == TradeHistory.current_execution_id():
            expected = scope[1].get((id(self), symbol))
        if expected is None:
            if actual != 0:
                raise ValueError('已有账户敞口，禁止叠加')
            return
        expected = Decimal(str(expected))
        if (not actual.is_finite() or not expected.is_finite() or expected == 0 or
                abs(actual - expected) > max(Decimal('0.00000001'), abs(expected) * Decimal('0.000001')) or
                (expected > 0) != (side in ('buy', 'long'))):
            raise ValueError('网格现有持仓与已记录层的合计数量或方向不符')

    def invalidate_entry_precheck(self):
        self._entry_precheck = None

    def entry_capital_balance(self, balance, symbol, side):
        """Reapply the same attempt's budget when fresh account checks run.

        An expired submission proof may fall back to validate_order; that
        path must not silently spend the reserved portion of the account.
        """
        evidence = getattr(self, '_entry_capital_policy', None)
        if evidence is None or evidence[0] != TradeHistory.current_execution_id():
            return Decimal(str(balance))
        normalized = 'buy' if side in ('buy','long') else 'sell'
        if symbol != evidence[1] or normalized != evidence[2]:
            raise ValueError('资金预算与本次订单不匹配，首腿未提交')
        from .capital_budget import available_entry_budget
        return available_entry_budget(balance, evidence[3])

    def precheck_order(self, symbol, quantity, side, *, allow_resize=False,
                       reference_price=None, quantity_step=None, cost_bps=0, capital_policy=None):
        with self.lock:
            self.invalidate_entry_precheck()
            execution_id = TradeHistory.current_execution_id()
            self._entry_capital_policy = ((execution_id, symbol, 'buy' if side in ('buy','long') else 'sell',
                                          dict(capital_policy)) if capital_policy is not None else None)
            if capital_policy is not None and not execution_id:
                raise ValueError('资金预算缺少执行身份，首腿未提交')
            cache = getattr(self, '_account_snapshot_cache', None)
            snapshot = cache.refresh() if cache is not None else None
            state = snapshot[0] if snapshot is not None else None
            checked_at, token = (snapshot[1:] if snapshot is not None else (time.monotonic(), None))
            balance_reader = getattr(type(self), 'entry_available_balance', None)
            if capital_policy is not None and (not allow_resize or not callable(balance_reader)):
                raise ValueError('当前平台不支持资金预算预检，首腿未提交')
            if allow_resize and callable(balance_reader):
                if state is None:
                    state = self.read_entry_account_state()
                balance = Decimal(str(self.entry_available_balance(state)))
                price, step = Decimal(str(reference_price)), Decimal(str(quantity_step))
                costs = Decimal(str(cost_bps))
                if (not balance.is_finite() or balance < 0 or not price.is_finite() or price <= 0 or
                        not step.is_finite() or step <= 0 or not costs.is_finite() or costs < 0):
                    raise ValueError('账户缩量依据无效，首腿未提交')
                if capital_policy is not None:
                    balance = self.entry_capital_balance(balance, symbol, side)
                affordable = balance / (price * (1 + costs / 10000))
                quantity = min(Decimal(str(quantity)),
                               (affordable / step).to_integral_value(rounding=ROUND_DOWN) * step)
                if quantity <= 0:
                    raise ValueError('资金预留后额度不足，首腿未提交' if capital_policy is not None else
                                     '可用保证金不足，缩量后低于数量精度，首腿未提交')
            if state is not None:
                evidence = self.validate_order(symbol, quantity, side=side, account_state=state)
            else:
                evidence = self.validate_order(symbol, quantity, side=side)
            if snapshot is not None:
                cache.reserve(execution_id, token)
            if execution_id and isinstance(evidence, dict):
                balance = self.entry_capital_balance(evidence.get('available_balance'), symbol, side)
                if balance.is_finite() and balance >= 0:
                    self._entry_precheck = (execution_id, symbol, Decimal(str(quantity)),
                                            side, balance, checked_at,
                                            Decimal(str(evidence.get('min_base_amount') or 0)),
                                            Decimal(str(evidence.get('min_quote_amount') or 0)), token)
            return Decimal(str(quantity))

    def resize_entry_precheck(self, symbol, quantity, side):
        """Narrow existing evidence to the same smaller paired quantity, no I/O."""
        proof = self._entry_precheck
        amount = Decimal(str(quantity))
        if (proof is None or proof[0] != TradeHistory.current_execution_id() or
                proof[1] != symbol or proof[3] != side or not 0 < amount <= proof[2]):
            self.invalidate_entry_precheck()
            raise ValueError('缩量账户凭据已变化，首腿未提交')
        self._entry_precheck = (*proof[:2], amount, *proof[3:])

    def consume_entry_precheck(self, symbol, quantity, side, quote, reduce_only=False):
        proof = getattr(self, '_entry_precheck', None)
        self.invalidate_entry_precheck()
        if proof is None or reduce_only:
            return False
        execution_id, checked_symbol, checked_size, checked_side, balance, checked_at, min_size, min_notional, token = proof
        age = time.monotonic() - checked_at
        required = Decimal(str(quantity)) * max(Decimal(str(quote['bid'])), Decimal(str(quote['ask'])))
        cache = getattr(self, '_account_snapshot_cache', None)
        if token is not None and (cache is None or not cache.valid_proof(execution_id, token)):
            return False
        return (execution_id == TradeHistory.current_execution_id() and
                symbol == checked_symbol and Decimal(str(quantity)) == checked_size and
                side == checked_side and 0 <= age <= self.precheck_max_age and
                checked_size >= min_size and
                checked_size * min(Decimal(str(quote['bid'])), Decimal(str(quote['ask']))) >= min_notional and
                required.is_finite() and 0 < required <= balance)

    @contextmanager
    def transport_stage(self, name, symbol):
        history = self.execution_history
        if history is None:
            yield
            return
        history.append('stage_started', symbol=symbol, stage=name,
                       execution_total_ms=TradeHistory.current_elapsed_ms())
        with stage(history, name, symbol):
            yield
