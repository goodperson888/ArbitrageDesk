"""Quote-side comparisons; never label reference spreads as executable profits."""
from src.common.market_data import age_seconds, number


def estimate_entry_cost(comparison, mark_price=None, mark_updated_at=None):
    """Omni: zero trading commission; quoted slippage is relative to mark.

    Half-spread and midpoint deviation are a decomposition of quoted slippage,
    not two fees to charge on top of a bid/ask price.
    """
    bid, ask, quantity = (comparison.get(k) for k in
                          ('variational_bid', 'variational_ask', 'base_quantity'))
    result = {'available': False, 'fee_bps': 0, 'fee_source': 'Omni 官方费用说明',
              'fee_source_url': 'https://docs.variational.io/omni/trading/fees',
              'reference_only': True, 'executable': False}
    if not bid or not ask or not quantity or bid > ask:
        return result
    mid = (bid + ask) / 2
    half_spread_cost = quantity * (ask - bid) / 2
    mark = number(mark_price, True) if mark_price is not None else None
    mark_age = age_seconds(mark_updated_at)
    quote_age = comparison.get('variational_quote_age')
    aligned = (mark_age is not None and quote_age is not None and
               0 <= mark_age <= 10 and 0 <= quote_age <= 10 and abs(mark_age - quote_age) <= 3)
    def side(price, direction):
        deviation = quantity * (mid - mark) * direction if mark else None
        quoted_cost = half_spread_cost + deviation if mark else None
        return {'quote_notional_usdc': quantity * price,
                'spread_cost_usdc': half_spread_cost, 'midpoint_deviation_usdc': deviation,
                'quoted_cost_usdc': quoted_cost,
                'quoted_slippage_bps': (price / mark - 1) * direction * 10000 if mark else None}
    result.update(available=True, quantity=quantity, midpoint=mid, mark_price=mark,
                  full_spread_bps=(ask - bid) / mid * 10000,
                  half_spread_bps=(ask - bid) / mid * 5000,
                  unchanged_roundtrip_spread_usdc=2 * half_spread_cost,
                  buy=side(ask, 1), sell=side(bid, -1),
                  quote_age_seconds=quote_age, mark_age_seconds=mark_age,
                  aligned=aligned, actual_slippage_bps=None)
    return result


def history_point(row):
    c = row['comparison']
    point = {'timestamp': row['timestamp'], 'mark_spread_bps': row.get('spread_bps'),
             'quote_source': c['quote_source'], 'reference_only': True,
             'variational_quote_age': c['variational_quote_age'],
             'lighter_quote_age': c['lighter_quote_age']}
    va, la = c['variational_quote_age'], c['lighter_quote_age']
    reference_valid = va is not None and 0 <= va <= 600 and la is not None and 0 <= la <= 10
    for key in ('buy_variational_sell_lighter_bps', 'buy_lighter_sell_variational_bps'):
        point[key] = c[key] if c['fresh'] else None
        # Cache observations are a separate series, never backfilled into fresh quotes.
        point['reference_' + key] = c[key] if reference_valid else None
    return point


def depth_price(levels, quantity):
    remaining, cost, available, worst = quantity, 0., 0., None
    for level in levels:
        size, price = number(level['size']), number(level['price'], True)
        if size < 0:
            raise ValueError('negative depth')
        available += size
        take = min(remaining, size)
        cost += take * price
        if take > 0:
            worst = price
        remaining -= take
    complete = remaining <= max(1e-12, quantity * 1e-10)
    return {'vwap': cost / quantity if complete and quantity > 0 else None,
            'sufficient': complete, 'available_size': available, 'worst_price': worst if complete else None}


def _depth_impact_bps(top_price, vwap, side):
    """Return adverse VWAP impact versus the best visible order-book price."""
    if not all(type(value) in (int, float) for value in (top_price, vwap)):
        return None
    if top_price <= 0 or vwap <= 0:
        return None
    if side == 'buy':
        return (vwap / top_price - 1) * 10000
    if side == 'sell':
        return (1 - vwap / top_price) * 10000
    return None


def _page_slippage_bps(quote, side):
    page = quote.get('page_slippage') or {}
    if page.get('side') != side:
        return None
    value = page.get('estimated_bps')
    return value if type(value) in (int, float) else None


def compare(row, book, notional=1000):
    v = row.get('details', {}).get('variational', {})
    l = row.get('details', {}).get('lighter', {})
    vb, va = v.get('bid'), v.get('ask')
    lb = book['bids'][0]['price'] if book and book.get('bids') else None
    la = book['asks'][0]['price'] if book and book.get('asks') else None
    quantity = v.get('quote_size')
    quantity_source = '网页参考报价基础数量' if quantity else '按目标金额估算基础数量'
    if not quantity and va:
        quantity = notional / va
    buy = depth_price(book.get('asks', []), quantity) if book and quantity else {}
    sell = depth_price(book.get('bids', []), quantity) if book and quantity else {}
    v_age = age_seconds(v.get('quote_updated_at'))
    l_age = age_seconds(book.get('fetched_at')) if book else None
    fresh = (v_age is not None and 0 <= v_age <= 10 and l_age is not None and 0 <= l_age <= 10)
    public = v.get('quote_source') != 'variational_browser_quote'
    reasons = []
    if not vb or not va: reasons.append('Variational 当前无双边报价')
    if not lb or not la: reasons.append('Lighter 当前无双边盘口')
    if not fresh: reasons.append('报价过期或缺少时间戳')
    if public: reasons.append('Variational 为缓存参考报价，非当前数量 RFQ')
    if public and v.get('quote_notional_usdc') and notional != v['quote_notional_usdc']:
        reasons.append('测算金额与 Variational 参考报价档位不同，仅为估算')
    if row.get('instrument_mismatch'): reasons.append('Swap 与永续合约不同')
    if quantity and (not buy.get('sufficient') or not sell.get('sufficient')):
        reasons.append('已获取的 Lighter 深度不足以覆盖目标数量')
    def spread(sell_price, buy_price):
        return (sell_price / buy_price - 1) * 10000 if sell_price and buy_price else None
    lighter_buy_slippage = _depth_impact_bps(la, buy.get('vwap'), 'buy')
    lighter_sell_slippage = _depth_impact_bps(lb, sell.get('vwap'), 'sell')
    slippage_directions = {
        'a': {
            'variational_bps': _page_slippage_bps(v, 'buy'),
            'lighter_bps': lighter_sell_slippage,
            'variational_source': 'variational_page_estimate' if _page_slippage_bps(v, 'buy') is not None else None,
            'lighter_source': 'lighter_orderbook_vwap' if lighter_sell_slippage is not None else None,
        },
        'b': {
            'variational_bps': _page_slippage_bps(v, 'sell'),
            'lighter_bps': lighter_buy_slippage,
            'variational_source': 'variational_page_estimate' if _page_slippage_bps(v, 'sell') is not None else None,
            'lighter_source': 'lighter_orderbook_vwap' if lighter_buy_slippage is not None else None,
        },
    }
    for direction in slippage_directions.values():
        known = [value for value in (direction['variational_bps'], direction['lighter_bps'])
                 if type(value) in (int, float)]
        direction['observed_bps'] = sum(known) if len(known) == 2 else None
        direction['known_legs'] = len(known)
    return {'variational_bid': vb, 'variational_ask': va, 'lighter_bid': lb, 'lighter_ask': la,
            'buy_variational_sell_lighter_bps': spread(lb, va),
            'buy_lighter_sell_variational_bps': spread(vb, la),
            'depth_buy_variational_sell_lighter_bps': spread(sell.get('vwap'), va),
            'depth_buy_lighter_sell_variational_bps': spread(vb, buy.get('vwap')),
            'lighter_buy_vwap': buy.get('vwap'), 'lighter_sell_vwap': sell.get('vwap'),
            'base_quantity': quantity, 'quantity_source': quantity_source, 'notional': notional,
            'reference_only': True, 'executable': False, 'fresh': fresh,
            'quote_source': v.get('quote_source', 'variational_public_api'),
            'variational_quote_age': v_age, 'lighter_quote_age': l_age,
            'slippage': {'directions': slippage_directions,
                         'note': 'V 为官网当前 RFQ 页面预计值；L 为同数量盘口 VWAP 相对首档的观测冲击；均不是最终成交滑点'},
            'reasons': reasons, 'note': '毛价差未扣手续费、资金费和滑点；页面参考报价也不保证成交'}
