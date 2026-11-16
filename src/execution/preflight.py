"""Pure order-budget checks; never raises the user's cap to a venue minimum."""
from decimal import ROUND_CEILING
from src.execution.orders import decimal
from src.common.market_data import utc_now


def budget_check(symbol, cap, market, reference_price):
    cap = decimal(cap, True)
    price = decimal(reference_price, True)
    if symbol not in ('ETH', 'BTC') or market.get('symbol') != symbol or market.get('market_type') != 'perp' or market.get('status') != 'active':
        raise ValueError('不是支持的活跃永续市场')
    sd = market['supported_size_decimals']
    if type(sd) is not int or not 0 <= sd <= 12:
        raise ValueError('市场数量精度无效')
    tick = decimal(10) ** -sd
    minimum_quote = decimal(market['min_quote_amount'], True)
    minimum_base = decimal(market['min_base_amount'], True)
    quantity = (max(minimum_base, minimum_quote / price) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    estimate = quantity * price
    reasons = []
    if cap < minimum_quote:
        reasons.append(f'Lighter 最低开仓名义金额 {float(minimum_quote):g} USDC，高于每腿上限 {float(cap):g} USDC')
    elif estimate > cap:
        reasons.append('按当前参考价及数量步长取整后超过上限，不提高金额')
    return {'symbol': symbol, 'cap_usdc': float(cap), 'minimum_quote_usdc': float(minimum_quote),
            'minimum_base': float(minimum_base), 'size_step': str(tick),
            'minimum_quantity_at_reference': str(quantity), 'reference_price': float(price),
            'estimated_minimum_notional': float(estimate), 'budget_allowed': not reasons,
            'blockers': reasons, 'checked_at': utc_now(), 'orders_submitted': False,
            'note': '仅 Lighter 最低金额检查；不是双平台可成交报价，不改变保证金或杠杆。'}
