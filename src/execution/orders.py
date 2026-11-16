"""Venue-independent validation; prices are protective limits, not fill promises."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import time
from src.common.market_data import age_seconds


def decimal(value, positive=False):
    if isinstance(value, bool):
        raise ValueError('布尔值不是数量')
    result = Decimal(str(value))
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError('数值必须有限且为正')
    return result


def direction(side):
    if side not in ('buy', 'sell', 'long', 'short'):
        raise ValueError('未知买卖方向')
    return 'buy' if side in ('buy', 'long') else 'sell'


class SubmissionWindowExpired(ValueError):
    pass


def check_submit_deadline(deadline):
    if deadline is not None and decimal(deadline, True) <= Decimal(str(time.monotonic())):
        raise SubmissionWindowExpired('补齐对冲提交窗口超时，禁止发送订单')


def fresh_quote(quote, symbol, quantity):
    check_submit_deadline(quote.get('submit_deadline_monotonic'))
    age = age_seconds(quote.get('updated_at'))
    if (quote.get('symbol') != symbol or quote.get('instrument_type') != 'perpetual' or
            decimal(quote.get('size'), True) != decimal(quantity, True) or
            age is None or not 0 <= age <= 5 or quote.get('executable') is not True):
        raise ValueError('报价时效、数量、合约或执行资格不匹配')
    if decimal(quote['bid'], True) > decimal(quote['ask'], True):
        raise ValueError('买卖报价倒置')


def lighter_plan(market, symbol, side, quantity, price, slippage_bps, reduce_only=False):
    side = direction(side)
    if market.get('symbol') != symbol or market.get('market_type') != 'perp' or market.get('status') != 'active':
        raise ValueError('市场不是可交易永续合约')
    amount, reference, slip = decimal(quantity, True), decimal(price, True), decimal(slippage_bps)
    if not 0 <= slip <= 100:
        raise ValueError('滑点限制超出 0–100 bps')
    sd, pd = market['supported_size_decimals'], market['supported_price_decimals']
    if type(sd) is not int or type(pd) is not int or not (0 <= sd <= 12 and 0 <= pd <= 12):
        raise ValueError('市场精度无效')
    scaled = amount * (10 ** sd)
    if scaled != scaled.to_integral_value():
        raise ValueError('数量不符合市场步长，禁止静默改变对冲数量')
    limit = reference * (1 + (slip / 10000 if side == 'buy' else -slip / 10000))
    units = (limit * (10 ** pd)).to_integral_value(rounding=ROUND_FLOOR if side == 'buy' else ROUND_CEILING)
    if int(scaled) >= 2**63 or not 0 < units < 2**32:
        raise ValueError('订单整数参数越界')
    if not reduce_only and (amount < decimal(market['min_base_amount']) or
                           amount * reference < decimal(market['min_quote_amount'])):
        raise ValueError('未满足最小下单数量或金额')
    return {'symbol': symbol, 'side': side, 'size': str(amount), 'market_index': int(market['market_id']),
            'base_amount': int(scaled), 'price': int(units), 'is_ask': side == 'sell',
            'reduce_only': bool(reduce_only), 'limit_price': str(units / (10 ** pd))}


def unknown(reason):
    return {'success': False, 'confirmed': False, 'status': 'unknown', 'error': reason}
