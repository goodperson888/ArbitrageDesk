"""Finite execution budgets; entry edge is not a realized profit estimate."""
from dataclasses import dataclass
from .orders import decimal


@dataclass(frozen=True)
class HedgePolicy:
    price_movement_bps: float
    max_delay_seconds: float
    rollback_limit: int
    cooldown_seconds: float

    @classmethod
    def from_config(cls, risk):
        def bounded(key, default, low, high):
            value = decimal(risk.get(key, default))
            if not low <= value <= high:
                raise ValueError('对冲保护配置越界：' + key)
            return float(value)
        movement = bounded('execution_price_movement_bps', 2, 0, 100)
        delay = bounded('hedge_max_delay_seconds', 5, .25, 30)
        limit = bounded('route_rollback_limit', 2, 1, 10)
        if not limit.is_integer():
            raise ValueError('路线回补次数必须为整数')
        cooldown = bounded('route_rollback_cooldown_seconds', 900, 60, 86400)
        return cls(movement, delay, int(limit), cooldown)


def completion_limit(first_price, first_fee, quantity, second_reference,
                     second_fee_bps, budget_bps, minimum_net_bps, cap, second_is_buy):
    """Intersect the original price budget, actual-fill edge and amount cap."""
    first, qty = decimal(first_price, True), decimal(quantity, True)
    reference, cap = decimal(second_reference, True), decimal(cap, True)
    fee = decimal(first_fee)
    rate, budget = decimal(second_fee_bps) / 10000, decimal(budget_bps) / 10000
    floor = decimal(minimum_net_bps) / 10000
    if fee < 0 or not 0 <= rate < 1 or not 0 <= budget <= .01 or not 0 <= floor < 1:
        raise ValueError('对冲费用或价格保护无效')
    if first * qty > cap:
        raise ValueError('首腿成交超过金额上限')
    if second_is_buy:
        price_budget = reference * (1 + budget)
        edge_budget = (first * (1 - floor) - fee / qty) / (1 + rate)
        limit = min(price_budget, edge_budget, cap / qty)
    else:
        if 1 - rate - floor <= 0:
            raise ValueError('对冲净价差保护无效')
        price_budget = reference * (1 - budget)
        edge_budget = (first + fee / qty) / (1 - rate - floor)
        limit = max(price_budget, edge_budget)
    if limit <= 0:
        raise ValueError('对冲保护价无效')
    return float(limit), float(price_budget)


def completion_edge(first_price, first_fee, quantity, limit, second_fee_bps, second_is_buy):
    first, qty, second = decimal(first_price, True), decimal(quantity, True), decimal(limit, True)
    rate = decimal(second_fee_bps) / 10000
    buy, sell = (second, first) if second_is_buy else (first, second)
    edge = (sell - buy) * qty - decimal(first_fee) - second * qty * rate
    return float(edge), float(edge / (max(buy, sell) * qty) * 10000)


def unwind_estimate(first_price, first_fee, quantity, quote, first_was_sell, budget_bps):
    """Indicative immediate flattening PnL, separately labelled from hedge edge."""
    qty, first = decimal(quantity, True), decimal(first_price, True)
    fee = decimal(quote['fee_bps']) / 10000
    budget = decimal(budget_bps) / 10000
    if not 0 <= fee < 1 or not 0 <= budget <= .01:
        raise ValueError('回补费用或价格保护无效')
    price = decimal(quote['ask' if first_was_sell else 'bid'], True)
    price *= 1 + budget if first_was_sell else 1 - budget
    gross = (first - price) * qty if first_was_sell else (price - first) * qty
    return float(gross - decimal(first_fee) - price * qty * fee)
