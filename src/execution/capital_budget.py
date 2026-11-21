"""Local entry budgets; no leverage multiplier and no extra account requests."""
import math
from decimal import Decimal


CAPITAL_DEFAULTS = dict(capital_reserve_percent=0., capital_reserve_usdc=0.,
                        capital_allocation_mode='per_leg')


def capital_policy(trading):
    result = {key: trading.get(key, default) for key, default in CAPITAL_DEFAULTS.items()}
    for key, high in (('capital_reserve_percent', 99), ('capital_reserve_usdc', 1000000)):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not 0 <= value <= high):
            raise ValueError('资金预留配置无效：' + key)
        result[key] = float(value)
    if result['capital_allocation_mode'] not in ('per_leg', 'remaining_slots'):
        raise ValueError('资金分配模式无效')
    return result


def available_entry_budget(available, policy):
    """Reserve the larger floor/percentage, then split current free capacity.

    The percentage is explicitly of CURRENT AVAILABLE collateral, not equity
    or a promise of equally sized lifetime groups. A fixed floor is available
    when an account must always keep the same USDC amount. Existing platform
    positions/orders are already reflected in available collateral.
    """
    available = Decimal(str(available))
    normalized = capital_policy(policy)
    slots = policy.get('remaining_slots', 1)
    if (not available.is_finite() or available < 0 or isinstance(slots, bool) or
            not isinstance(slots, int) or slots < 1):
        raise ValueError('账户资金预算依据无效，首腿未提交')
    reserve = max(available * Decimal(str(normalized['capital_reserve_percent'])) / 100,
                  Decimal(str(normalized['capital_reserve_usdc'])))
    divisor = slots if normalized['capital_allocation_mode'] == 'remaining_slots' else 1
    return max(Decimal(0), available-reserve) / divisor
