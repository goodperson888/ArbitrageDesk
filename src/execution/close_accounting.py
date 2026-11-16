"""Bounded settlement GETs; the coordinator alone applies durable accounting."""
import copy
import math
import threading
import time
from concurrent.futures import Future
from datetime import datetime


class ClosedFundingReads:
    def __init__(self):
        self.jobs, self.retry_at = {}, {}

    def poll(self, strategy):
        positions = {p.id: p for p in strategy.positions if p.status == 'closed'}
        changed = False
        for key, (future, opened) in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[key]
            self.retry_at[key] = time.monotonic() + 5
            position = positions.get(key[0])
            leg = key[1]
            if position is None or position.open_time != opened or leg not in position.pending_funding_venues:
                continue
            try:
                settlement = future.result()
                if not isinstance(settlement, dict) or settlement.get('complete') is not True:
                    continue
                amount = settlement.get('funding_usdc')
                if isinstance(amount, bool) or amount is None or not math.isfinite(float(amount)):
                    continue
                settlement = strategy._allocate_layer_funding(settlement, position, leg)
                if settlement is None:
                    continue
                amount = float(settlement['funding_usdc'])
            except Exception:
                continue
            position.funding_settlements[leg] = settlement
            position.realized_pnl_usdc = float(position.realized_pnl_usdc or 0) + amount
            if position.closed_time and position.closed_time.date() == datetime.now().date():
                strategy.daily_pnl += amount
            position.pending_funding_venues.remove(leg)
            position.accounting_pending = bool(position.pending_funding_venues or position.pending_fee_venues)
            if not position.accounting_pending and position.closed_time:
                position.close_accounting_ms = (position.close_execution_ms or 0) + max(0,
                    round((datetime.now() - position.closed_time).total_seconds() * 1000))
            changed = True
        if changed:
            strategy._save()
        active_keys = {(p.id, leg) for p in positions.values() for leg in p.pending_funding_venues}
        self.retry_at = {k: v for k, v in self.retry_at.items() if k in active_keys}
        for position in positions.values():
            if not position.pending_funding_venues:
                continue
            try:
                traders = dict(zip(('variational', 'lighter'), strategy.position_traders(position)))
            except (ValueError, AttributeError):
                continue
            for leg in position.pending_funding_venues:
                key = (position.id, leg)
                if len(self.jobs) >= 2 or key in self.jobs or time.monotonic() < self.retry_at.get(key, 0):
                    continue
                reader = getattr(traders[leg], 'reconcile_funding', None)
                if not callable(reader):
                    continue
                frozen, future = copy.deepcopy(position), Future()
                def read(fn=reader, p=frozen, result=future):
                    try:
                        result.set_result(fn(p.symbol, p.open_time, p.closed_time))
                    except Exception as exc:
                        result.set_exception(exc)
                self.jobs[key] = (future, position.open_time)
                threading.Thread(target=read, name='closed-funding', daemon=True).start()
