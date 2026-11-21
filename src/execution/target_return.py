"""Round-trip return at a frozen directed BBO target; no venue I/O.

The target uses the same (sell bid - buy ask) / max denominator as history.
Future prices are scenarios, never executable quotes or a profit promise.
"""
import math


def finite(value, name, minimum=None):
    if isinstance(value, bool):
        raise ValueError(name + '无效')
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(name + '未知') from None
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(name + '无效')
    return value


def directed_spread(buy, sell):
    buy, sell = finite(buy, '买价', 1e-100), finite(sell, '卖价', 1e-100)
    return (sell - buy) / max(buy, sell) * 10000


def ratio_to_directed_bps(value):
    """Read legacy ask-denominator observations without rewriting stored rows."""
    value = finite(value, '历史价差')
    return value / (1 + value / 10000) if value > 0 else value


def exit_projection(buy_quote, sell_quote, target_bps, buy_close_slip,
                    sell_close_slip, funding_reserve_bps, funding_reference_price=None):
    """Preserve quote widths and common price level at the historical target.

    buy_quote is the venue where we OPEN LONG. Its future bid closes it.
    sell_quote is the OPEN SHORT venue. Its future ask closes it.
    Close impact/movement is charged once on each actual closing side.
    """
    target = finite(target_bps, '回归目标') / 10000
    if not -1 < target < 1:
        raise ValueError('回归目标超出有效价差范围')
    b_bid, b_ask = (finite(buy_quote[k], '买平台报价', 1e-100) for k in ('bid', 'ask'))
    s_bid, s_ask = (finite(sell_quote[k], '卖平台报价', 1e-100) for k in ('bid', 'ask'))
    if b_bid > b_ask or s_bid > s_ask:
        raise ValueError('买卖报价倒置')
    bf = finite(buy_quote.get('fee_bps'), '买平台费用', 0) / 10000
    sf = finite(sell_quote.get('fee_bps'), '卖平台费用', 0) / 10000
    b_slip = finite(buy_close_slip, '多腿平仓预算', 0) / 10000
    s_slip = finite(sell_close_slip, '空腿平仓预算', 0) / 10000
    funding = finite(funding_reserve_bps, '整轮资金费预算', 0) / 10000
    if max(bf, sf) >= 1 or max(b_slip, s_slip) > .01:
        raise ValueError('回归费用或价格保护无效')
    b_mid, s_mid = (b_bid + b_ask) / 2, (s_bid + s_ask) / 2
    # Solve directed target exactly, including negative BBO targets.
    bbo_ratio = 1 / (1 - target) if target >= 0 else 1 + target
    mid_ratio = bbo_ratio * (b_ask / b_mid) / (s_bid / s_mid)
    target_b_mid = (b_mid + s_mid) / (1 + mid_ratio)
    target_s_mid = (b_mid + s_mid) - target_b_mid
    close_sell = target_b_mid * (b_bid / b_mid) * (1 - b_slip)
    close_buy = target_s_mid * (s_ask / s_mid) * (1 + s_slip)
    fees = close_sell * bf + close_buy * sf
    # Reserve on the larger current/target leg, without assuming funding income.
    funding_reference = max(b_ask, s_bid, close_sell, close_buy,
        finite(funding_reference_price, '资金费预算参考价', 0) if funding_reference_price is not None else 0)
    funding_cost = funding_reference * funding
    return dict(target_bps=float(target_bps), close_sell_price=close_sell,
                close_buy_price=close_buy, close_fees_per_unit=fees,
                funding_cost_per_unit=funding_cost,
                funding_reference_price=funding_reference,
                exit_credit_per_unit=close_sell - close_buy - fees - funding_cost,
                target_buy_ask=target_b_mid * b_ask / b_mid,
                target_sell_bid=target_s_mid * s_bid / s_mid,
                # Numeric inputs only; reuse the existing projection record
                # to explain changing exit estimates without another I/O.
                quote_inputs=dict(buy_bid=b_bid, buy_ask=b_ask,
                    sell_bid=s_bid, sell_ask=s_ask,
                    buy_fee_bps=float(buy_quote['fee_bps']), sell_fee_bps=float(sell_quote['fee_bps']),
                    buy_close_slippage_bps=float(buy_close_slip),
                    sell_close_slippage_bps=float(sell_close_slip),
                    funding_reserve_bps=float(funding_reserve_bps)))


def projected_return(buy, sell, quantity, buy_fee_bps, sell_fee_bps,
                     buy_slippage_bps, sell_slippage_bps, projection):
    buy, sell = finite(buy, '买价', 1e-100), finite(sell, '卖价', 1e-100)
    qty = finite(quantity, '数量', 1e-100)
    bf = finite(buy_fee_bps, '开多费用', 0) / 10000
    sf = finite(sell_fee_bps, '开空费用', 0) / 10000
    bs = finite(buy_slippage_bps, '开多价格预算', 0) / 10000
    ss = finite(sell_slippage_bps, '开空价格预算', 0) / 10000
    if max(bf, sf) >= 1 or max(bs, ss) > .01:
        raise ValueError('入场成本无效')
    protected_buy, protected_sell = buy * (1 + bs), sell * (1 - ss)
    fees = protected_buy * bf + protected_sell * sf
    net_unit = protected_sell - protected_buy - fees + projection['exit_credit_per_unit']
    return dict(net_edge_bps=net_unit / max(buy, sell) * 10000,
                estimated_entry_edge_usdc=net_unit * qty,
                opening_edge_bps=(protected_sell - protected_buy - fees) / max(buy, sell) * 10000,
                opening_fees_usdc=fees * qty)


def completion_return(first, fee, quantity, second, second_fee_bps,
                      second_is_buy, projection):
    first, second = finite(first, '首腿成交价', 1e-100), finite(second, '第二腿价格', 1e-100)
    qty, fee = finite(quantity, '成交数量', 1e-100), finite(fee, '首腿成交费用', 0)
    rate = finite(second_fee_bps, '第二腿费用', 0) / 10000
    if rate >= 1:
        raise ValueError('第二腿费用无效')
    buy, sell = (second, first) if second_is_buy else (first, second)
    net = (sell - buy + projection['exit_credit_per_unit']) * qty - fee - second * qty * rate
    return net, net / (max(buy, sell) * qty) * 10000


def completion_target_limit(first, fee, quantity, reference, second_fee_bps,
                            budget_bps, minimum_bps, cap, second_is_buy, projection,
                            minimum_usdc=0):
    """Solve the return floor, intersecting original price and amount guards."""
    first, qty = finite(first, '首腿成交价', 1e-100), finite(quantity, '数量', 1e-100)
    fee = finite(fee, '首腿费用', 0) / qty
    reference, cap = finite(reference, '原第二腿价格', 1e-100), finite(cap, '金额上限', 1e-100)
    rate = finite(second_fee_bps, '第二腿费用', 0) / 10000
    budget, floor = finite(budget_bps, '价格预算', 0) / 10000, finite(minimum_bps, '净空间底线', 0) / 10000
    if budget > .01 or rate + floor >= 1 or first * qty > cap + 1e-10:
        raise ValueError('补齐回归保护参数无效')
    credit = finite(projection['exit_credit_per_unit'], '目标平仓估算')
    amount_floor = finite(minimum_usdc, '最低平仓净收益', 0) / qty
    at_first = credit - fee - first * (rate + floor)
    if second_is_buy:
        root = ((first + credit - fee) / (1 + rate + floor) if at_first >= 0
                else (first + credit - fee - floor * first) / (1 + rate))
        price_budget = reference * (1 + budget)
        amount_root = (first + credit - fee - amount_floor) / (1 + rate)
        bound = min(root, amount_root, price_budget, cap / qty)
    else:
        root = ((first + fee - credit + floor * first) / (1 - rate) if at_first >= 0
                else (first + fee - credit) / (1 - rate - floor))
        price_budget = reference * (1 - budget)
        amount_root = (first + fee - credit + amount_floor) / (1 - rate)
        bound = max(root, amount_root, price_budget)
    if not math.isfinite(bound) or bound <= 0:
        raise ValueError('目标回归保护价无效')
    return bound, price_budget
