"""Arcus placement tiers and inward limit rounding; signing uses the base tick."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

from src.execution.orders import decimal, direction


class PriceGridError(ValueError):
    """A local pre-send grid failure, proving no order was transmitted."""


def price_bands(tick_size, tick_tiers=None):
    try:
        base = decimal(tick_size, True)
        if tick_tiers is None:
            return [(Decimal(0), None, base)]
        if not isinstance(tick_tiers, list) or not tick_tiers:
            raise ValueError('missing tiers')
        bands, lower = [], Decimal(0)
        for index, tier in enumerate(tick_tiers):
            if not isinstance(tier, dict):
                raise ValueError('invalid tier')
            tick = decimal(tier.get('tick'), True)
            if tick % base:
                raise ValueError('tier cannot be signed in base units')
            raw_upper = tier.get('upToPrice')
            upper = decimal(raw_upper, True) if raw_upper is not None else None
            if upper is None:
                if index != len(tick_tiers) - 1:
                    raise ValueError('unbounded tier must be last')
            elif upper <= lower or index == len(tick_tiers) - 1:
                raise ValueError('unordered or incomplete tiers')
            bands.append((lower, upper, tick))
            lower = upper
        return bands
    except (ValueError, TypeError, ArithmeticError):
        raise PriceGridError('Arcus 价格档位规则无效') from None


def placement_tick(price, tick_size, tick_tiers=None):
    value = decimal(price, True)
    for lower, upper, tick in price_bands(tick_size, tick_tiers):
        # Official upToPrice is EXCLUSIVE: the boundary belongs to the next band.
        if value >= lower and (upper is None or value < upper):
            return tick
    raise PriceGridError('Arcus 价格档位规则无效')


def validate_placement_price(price, tick_size, tick_tiers=None):
    value = decimal(price, True)
    tick = placement_tick(value, tick_size, tick_tiers)
    if value % tick or value % decimal(tick_size, True):
        raise PriceGridError('Arcus 订单价格不符合当前价格档位步长')


def protective_limit(price, side, slippage_bps, tick_size, tick_tiers=None):
    """Choose the closest legal price inside the original adverse-price bound.

    Search each band, because rounding at a boundary may move into a band
    with a different grid. Never change quantity or widen the given budget.
    """
    reference, slip = decimal(price, True), decimal(slippage_bps)
    if not 0 <= slip <= 100:
        raise ValueError('Arcus 滑点保护超出 0–100 bps')
    buy = direction(side) == 'buy'
    bound = reference * (1 + (slip if buy else -slip) / 10000)
    candidates = []
    for lower, upper, tick in price_bands(tick_size, tick_tiers):
        if buy:
            units = (bound / tick).to_integral_value(rounding=ROUND_FLOOR)
            if upper is not None:
                # Largest multiple strictly below this band's exclusive upper bound.
                units = min(units, (upper / tick).to_integral_value(rounding=ROUND_CEILING) - 1)
        else:
            units = (max(bound, lower) / tick).to_integral_value(rounding=ROUND_CEILING)
        candidate = units * tick
        if candidate > 0 and candidate >= lower and (upper is None or candidate < upper):
            candidates.append(candidate)
    if not candidates:
        raise PriceGridError('Arcus 保护限价无效')
    result = max(candidates) if buy else min(candidates)
    validate_placement_price(result, tick_size, tick_tiers)
    return format(result.normalize(), 'f')
