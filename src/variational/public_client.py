"""Official Omni read-only API, independent of browser/wallet state.

https://docs.variational.io/technical-documentation/api
Public quotes may be cached upstream for 600 seconds; they are not live RFQs.
"""
from src.common.market_data import PublicClient, MarketDataError, number, age_seconds


class VariationalPublicClient:
    live_ready = False
    live_blocker = 'Variational 官方交易 API 尚未开放；网页成交数量与回执尚未验证'

    def __init__(self, config=None, wallet_address='', session=None):
        self.config = config or {}
        self.wallet_address = wallet_address
        api = self.config.get('api', {})
        self.client = PublicClient(
            api.get('base_url', 'https://omni-client-api.prod.ap-northeast-1.variational.io'),
            api.get('cache_seconds', 10), session, api.get('proxy_url'))
        self.aliases = {'US100': 'US100S', **self.config.get('symbol_aliases', {})}

    def get_markets(self):
        data, fetched_at = self.client.get('/metadata/stats')
        listings = data.get('listings')
        if not isinstance(listings, list) or not listings:
            raise MarketDataError('Variational listings 缺失')
        return {item['ticker']: item for item in listings if isinstance(item, dict) and item.get('ticker')}, fetched_at

    def get_snapshot(self, symbol):
        markets, fetched_at = self.get_markets()
        ticker = self.aliases.get(symbol, symbol)
        if ticker not in markets:
            raise MarketDataError(f'Variational 未上线 {ticker}')
        listing = markets[ticker]
        quotes = listing.get('quotes') or {}
        quote = quotes.get('size_1k') or {}
        bid = number(quote['bid'], True) if quote.get('bid') is not None else None
        ask = number(quote['ask'], True) if quote.get('ask') is not None else None
        if bid is not None and ask is not None and bid > ask:
            raise MarketDataError('Variational bid 大于 ask')
        updated_at = quotes.get('updated_at')
        age = age_seconds(updated_at)
        swap = listing.get('name', '').startswith('Swap on ')
        return {
            'symbol': symbol, 'exchange_symbol': ticker, 'source': 'variational_public_api',
            'instrument_type': 'swap' if swap else 'perpetual',
            'mark_price': number(listing.get('mark_price'), True),
            'bid': bid, 'ask': ask, 'quote_notional_usdc': 1000 if quote else None,
            'quote_updated_at': updated_at, 'quote_age_seconds': age,
            'quote_stale': age is None or age < 0 or age > 30,
            'fetched_at': fetched_at, 'mark_updated_at': None, 'executable': False,
            'note': ('Swap 参考价；交易时间、融资费用与永续不同' if swap else
                     '公开参考行情；报价可能缓存 600 秒，不是可执行 RFQ'),
        }

    def get_market_price(self, symbol):
        return self.get_snapshot(symbol)['mark_price']

    def place_order(self, *args, **kwargs):
        return {'success': False, 'status': 'rejected', 'filled_size': 0,
                'error': self.live_blocker}

    close_position = place_order

    def close(self):
        self.client.close()
