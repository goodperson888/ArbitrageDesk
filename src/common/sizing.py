"""Conservative equal-base sizing against visible depth, never a fill promise."""
from decimal import Decimal, ROUND_DOWN
from src.execution.orders import decimal


def book_utilization_percent(value=80):
    try:
        result = decimal(value, True)
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError('盘口使用率必须在 0.1–100% 之间') from exc
    if not Decimal('0.1') <= result <= 100:
        raise ValueError('盘口使用率必须在 0.1–100% 之间')
    return float(result)


def paired_depth_capacity(first, second, direction, impact_bps, level_allowed):
    """Common base depth on the actual opening sides, before amount limits.

    Walk matched depth segments once. Prices already include depth impact;
    the predicate checks fees/profit without charging that impact again.
    Never treat an RFQ quantity or the requested order size as book capacity.
    """
    if direction not in ('buy_variational', 'sell_variational'):
        raise ValueError('未知开仓方向')
    buy_book, sell_book = ((first, second) if direction == 'buy_variational'
                           else (second, first))
    impact = decimal(impact_bps) / 10000
    if impact < 0:
        return 0.0

    def levels(book, side):
        rows = sorted(((decimal(row['price'], True), decimal(row['size']))
                       for row in book.get(side, [])), reverse=side == 'bids')
        if any(size < 0 for _, size in rows):
            raise ValueError('盘口数量无效')
        rows = [(price, size) for price, size in rows if size]
        if not rows:
            return []
        top = rows[0][0]
        return [(price, size) for price, size in rows
                if (price <= top * (1 + impact) if side == 'asks'
                    else price >= top * (1 - impact))]

    buys, sells = levels(buy_book, 'asks'), levels(sell_book, 'bids')
    i = j = 0
    total = Decimal(0)
    while i < len(buys) and j < len(sells):
        buy, buy_size = buys[i]
        sell, sell_size = sells[j]
        if not level_allowed(float(buy), float(sell)):
            break
        take = min(buy_size, sell_size)
        total += take
        buys[i], sells[j] = (buy, buy_size - take), (sell, sell_size - take)
        if buys[i][1] == 0:
            i += 1
        if sells[j][1] == 0:
            j += 1
    return float(total)


def smaller_book_quantities(books, quantity, size_decimals, limit=5):
    """Bounded local alternatives at actual depth breaks, largest first.

    Include the smallest break even on a large book. Never invent depth or
    issue another quote request. The caller must fully validate each size.
    """
    step = Decimal(1).scaleb(-size_decimals)
    current = decimal(quantity, True)
    candidates = set()
    for book in books:
        for side in ('bids', 'asks'):
            levels = sorted(((decimal(row['price'], True), decimal(row['size']))
                             for row in book.get(side, [])), reverse=side == 'bids')
            total = Decimal(0)
            for _, size in levels:
                if size < 0:
                    raise ValueError('盘口数量无效')
                total += size
                q = total.quantize(step, rounding=ROUND_DOWN)
                if 0 < q < current:
                    candidates.add(q)
    ordered = sorted(candidates, reverse=True)
    if len(ordered) > limit:
        ordered = [ordered[round(i * (len(ordered)-1)/(limit-1))] for i in range(limit)]
    return [float(q) for q in ordered]


def allocate(v_quote, book, cap_usdc, size_decimals, min_bps, max_bps,
             slippage_bps=0, lighter_fee_bps=None, max_quantity=None, edge_model=None,
             direction_quantity_limits=None):
    cap = decimal(cap_usdc, True)
    if type(size_decimals) is not int or not 0 <= size_decimals <= 12:
        raise ValueError('数量精度尚未确认')
    step = Decimal(1).scaleb(-size_decimals)
    low, high = decimal(min_bps), decimal(max_bps)
    vf, lf = decimal(v_quote['fee_bps']) / 10000, decimal(lighter_fee_bps) / 10000
    if not (0 <= low <= high and 0 <= vf < 1 and 0 <= lf < 1):
        raise ValueError('费用或阈值无效')

    def leg_slippage(direction, side):
        if isinstance(slippage_bps, dict):
            value = slippage_bps.get(direction, {}).get(side)
        else:
            value = slippage_bps
        slip = decimal(value) / 10000
        if not 0 <= slip <= Decimal('.01'):
            raise ValueError('滑点预算无效')
        return slip
    vb, va = decimal(v_quote['bid'], True), decimal(v_quote['ask'], True)
    if vb > va:
        raise ValueError('买卖报价倒置')
    q_limit = decimal(max_quantity, True) if max_quantity is not None else cap / max(vb, va)
    plans = []
    for direction, side, vp in [('buy_variational', 'bids', va), ('sell_variational', 'asks', vb)]:
        direction_limit = (min(q_limit, decimal(direction_quantity_limits[direction]))
                           if direction_quantity_limits is not None else q_limit)
        buy_slip = leg_slippage(direction, 'buy')
        sell_slip = leg_slippage(direction, 'sell')
        levels = [(decimal(x['price'], True), decimal(x['size'])) for x in book.get(side, [])]
        if any(size < 0 for _, size in levels):
            raise ValueError('盘口数量无效')
        levels.sort(reverse=side == 'bids')
        total, cost, worst = Decimal(0), Decimal(0), None
        for price, size in levels:
            if not size:
                continue
            buy, sell = (vp, price) if side == 'bids' else (price, vp)
            bf, sf = (vf, lf) if side == 'bids' else (lf, vf)
            protected_buy, protected_sell = buy * (1 + buy_slip), sell * (1 - sell_slip)
            margin = (protected_sell * (1 - sf) - protected_buy * (1 + bf)) / max(buy, sell) * 10000
            if edge_model is not None:
                margin = decimal(edge_model(direction, buy, sell, Decimal(1))['net_edge_bps'])
            # A zero or negative protected edge is never an entry candidate,
            # even when the configured minimum is set to zero.  The other
            # direction is evaluated separately and may still be profitable.
            if margin <= 0 or margin < low:
                break  # Do not consume a marginal level below the target.
            # Cap each entire leg at its worst observed price + protection budget.
            capacity = min(direction_limit, cap / (max(vp, price) * (1 + buy_slip)))
            take = min(size, max(Decimal(0), capacity - total))
            total += take
            cost += take * price
            if take:
                worst = price
            if take < size:
                break
        quantity = (total / step).to_integral_value(rounding=ROUND_DOWN) * step
        if not quantity:
            plans.append({'direction': direction, 'available': False, 'reason': '金额上限、数量精度或净价差以内没有可用深度'})
            continue
        remaining, cost = quantity, Decimal(0)
        for price, size in levels:
            take = min(remaining, size)
            if take:
                worst = price
            cost += take * price
            remaining -= take
            if not remaining:
                break
        vwap = cost / quantity
        buy, sell = (vp, vwap) if side == 'bids' else (vwap, vp)
        bf, sf = (vf, lf) if side == 'bids' else (lf, vf)
        net = quantity * (sell * (1 - sell_slip) * (1 - sf) - buy * (1 + buy_slip) * (1 + bf))
        edge = net / (quantity * max(buy, sell)) * 10000
        model_result = edge_model(direction, buy, sell, quantity) if edge_model is not None else None
        if model_result is not None:
            edge = decimal(model_result['net_edge_bps'])
            net = decimal(model_result['estimated_entry_edge_usdc'])
        valid = edge > 0 and low <= edge <= high
        plans.append({'direction': direction, 'available': valid,
                      'reason': '' if valid else '整体净价差超过设置的最大阈值',
                      'quantity': float(quantity), 'variational_price': float(vp),
                      'lighter_vwap': float(vwap), 'lighter_worst_price': float(worst),
                      'variational_notional': float(quantity * vp), 'lighter_notional': float(cost),
                      'protected_notional': float(quantity * max(vp, worst) * (1 + buy_slip)),
                      'buy_slippage_bps': float(buy_slip * 10000),
                      'sell_slippage_bps': float(sell_slip * 10000),
                      'slippage_per_leg_bps': float(max(buy_slip, sell_slip) * 10000),
                      'slippage_budget_bps': float((buy_slip + sell_slip) * 10000),
                      'net_edge_bps': float(edge), 'estimated_entry_edge_usdc': float(net)})
    return plans
