"""Redistribute existing close movement allowance; no I/O or new risk budget."""
import math
import threading
import time


class CloseLiquidity:
    def __init__(self):
        self._rows = {}
        self._lock = threading.Lock()

    def record(self, venue, symbol, side, receipt, requested):
        if (receipt.get('confirmed') is not True or requested <= 0 or
                receipt.get('status') not in ('filled', 'partial', 'canceled')):
            return
        ratio = float(receipt.get('filled_size', 0)) / requested
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            return
        key, now = (venue, symbol, side), time.monotonic()
        with self._lock:
            if key not in self._rows and len(self._rows) >= 256:
                self._rows.pop(next(iter(self._rows)))
            self._rows[key] = tuple(row for row in self._rows.get(key, ())[-7:]
                                    if 0 <= now - row[0] <= 900) + ((now, ratio),)

    def weight(self, venue, symbol, side, quote, size):
        now = time.monotonic()
        with self._lock:
            rows = self._rows.get((venue, symbol, side), ())
        ratios = [ratio for at, ratio in rows if 0 <= now - at <= 900]
        recent = 1 + (1 - sum(ratios) / len(ratios) if ratios else 0)
        # Reuse the quantity-bound book. Missing or malformed depth is neutral,
        # and must never trigger another market request during exit planning.
        try:
            levels = quote['sizing_book']['asks' if side == 'buy' else 'bids']
            available = float(levels[0]['size'])
            depth = 1 + min(1, size / available) if available > 0 else 1
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            depth = 1
        return recent * depth if math.isfinite(depth) else recent


def allocate_close_movement(legs, ceiling):
    """Keep the original fee-adjusted money budget and at least half per leg.

    Depth impact is required independently and is never transferred. Only the
    existing extra movement allowance moves toward the less liquid leg. The
    receiving leg remains inside its configured slippage ceiling.
    """
    names = tuple(legs)
    if len(names) != 2:
        return
    a, b = (legs[name] for name in names)
    if abs(a['liquidity_weight'] - b['liquidity_weight']) <= 1e-12:
        return
    cost = lambda leg: (leg['reference_price'] * float(leg['quote']['size']) / 10000 *
                        (1 + (1 if leg['side'] == 'buy' else -1) * leg['fee_bps'] / 10000))
    ca, cb = cost(a), cost(b)
    ma, mb = a['movement_bps'], b['movement_bps']
    if min(ca, cb, ma, mb) <= 0:
        return
    budget = ca * ma + cb * mb
    weight = a['liquidity_weight'] + b['liquidity_weight']
    # Clamp the transfer from both sides, so neither leg loses more than half
    # its original movement protection or exceeds its individual ceiling.
    lower = max(ca * ma / 2, budget - cb * min(1.5 * mb, ceiling - b['impact_bps']))
    upper = min(ca * min(1.5 * ma, ceiling - a['impact_bps']), budget - cb * mb / 2)
    if lower > upper or not all(math.isfinite(v) for v in (budget, weight, lower, upper)):
        return
    money_a = min(upper, max(lower, budget * a['liquidity_weight'] / weight))
    movements = (money_a / ca, (budget - money_a) / cb)
    for leg, movement in zip((a, b), movements):
        leg['slippage_bps'] = min(ceiling, leg['impact_bps'] + movement)
        leg['bound'] = leg['reference_price'] * (
            1 + (1 if leg['side'] == 'buy' else -1) * leg['slippage_bps'] / 10000)
