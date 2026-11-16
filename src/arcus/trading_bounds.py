"""Local off-hours fill checks against Arcus's cached market boundaries."""
from src.arcus.pricing import PriceGridError
from src.execution.orders import decimal, direction


def validate_trading_quote(market, side, quote):
    bounds = market.get('trading_bounds')
    if bounds is None:
        return  # Compatibility with catalogs that do not publish this field.
    try:
        lower, upper = bounds['lower'], bounds['upper']
        if lower is None and upper is None:
            return  # Regular-hours / 24-hour market: no active off-hours band.
        lower, upper = decimal(lower, True), decimal(upper, True)
        if lower >= upper:
            raise ValueError('invalid interval')
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise PriceGridError('Arcus 交易价格边界无效，等待更新') from None
    buy = direction(side) == 'buy'
    prices = [quote['ask' if buy else 'bid']]
    # Both the best and worst fill must be strictly inside the band. A limit
    # inside the band alone cannot make an out-of-band resting BBO executable.
    book = quote.get('sizing_book')
    if isinstance(book, dict):
        levels = book.get('asks' if buy else 'bids')
        if isinstance(levels, list) and levels:
            prices.append(levels[0]['price'])
    if any(not lower < decimal(price, True) < upper for price in prices):
        raise PriceGridError('Arcus 可成交报价触及盘外交易边界，禁止提交')
