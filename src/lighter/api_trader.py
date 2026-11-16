"""Lighter mainnet public REST client. Never uses the former invalid HMAC API."""
from src.common.market_data import PublicClient, MarketDataError, number
from src.common.discovery import EmptyBookError
from decimal import Decimal


class LighterApiTrader:
    live_ready = False
    live_blocker = 'Lighter 需要官方 SDK 签名、真实 API 私钥及成交核对；旧 HMAC 下单已禁用'

    def __init__(self, config=None, api_key='', api_secret='', account_index=None, session=None):
        self.config = config or {}
        api = self.config.get('api', {})
        self.base_url = api.get('base_url', 'https://mainnet.zklighter.elliot.ai')
        if self.base_url.rstrip('/') == 'https://api.lighter.xyz':
            self.base_url = 'https://mainnet.zklighter.elliot.ai'
        self.account_index = account_index
        self.client = PublicClient(self.base_url, api.get('cache_seconds', 10), session,
                                   api.get('proxy_url'), api.get('request_timeout', 45))

    def get_markets(self):
        # Lighter's current public catalog endpoint is ``orderBooks``.  The
        # former ``orderBookDetails`` route is still referenced in older
        # examples, but the mainnet CloudFront/WAF currently answers it with
        # HTTP 405.  Accept the legacy response key as well so injected test
        # clients and older deployments remain compatible.
        data, fetched_at = self.client.get('/api/v1/orderBooks')
        items = data.get('order_books') or data.get('order_book_details')
        if not isinstance(items, list) or not items:
            raise MarketDataError('Lighter order_books 缺失')
        return {row['symbol']: row for row in items
                if row.get('market_type') == 'perp'}, fetched_at

    def get_market(self, symbol):
        markets, fetched_at = self.get_markets()
        if symbol not in markets:
            raise MarketDataError(f'Lighter 未上线永续市场 {symbol}')
        return markets[symbol], fetched_at

    def get_market_risk(self, symbol):
        """The per-market details endpoint retains IMF fields absent from the catalog."""
        market, _ = self.get_market(symbol)
        data, fetched_at = self.client.get('/api/v1/orderBookDetails',
                                          {'market_id': market['market_id']}, ttl=300)
        rows = [row for row in data.get('order_book_details', [])
                if str(row.get('market_id')) == str(market['market_id'])
                and row.get('symbol') == symbol]
        if len(rows) != 1:
            raise MarketDataError('Lighter 市场保证金详情缺失或合约不匹配')
        return rows[0], fetched_at

    def get_snapshot(self, symbol):
        row, fetched_at = self.get_market(symbol)
        # ``orderBooks`` is the current public catalog endpoint and no longer
        # includes the mark/last-trade fields that the legacy details route
        # returned.  They are useful metadata but are not required to build a
        # quote: the executable side prices come from orderBookOrders below.
        mark_raw = row.get('mark_price')
        last_raw = row.get('last_trade_price')
        return {'symbol': symbol, 'exchange_symbol': symbol, 'source': 'lighter_public_api',
                'market_id': row['market_id'], 'instrument_type': 'perpetual',
                'mark_price': number(mark_raw, True) if mark_raw not in (None, '') else None,
                'last_trade_price': number(last_raw, True) if last_raw not in (None, '') else None,
                'fetched_at': fetched_at, 'mark_updated_at': None,
                'status': row.get('status'), 'executable': False,
                'size_decimals': row.get('supported_size_decimals'),
                'price_decimals': row.get('supported_price_decimals'),
                'min_base_amount': row.get('min_base_amount'),
                'min_quote_amount': row.get('min_quote_amount')}

    def get_market_price(self, symbol, discovery=False):
        snapshot = self.get_snapshot(symbol)
        for key in ('mark_price', 'last_trade_price'):
            value = snapshot.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        # The current orderBooks catalog no longer includes mark/last prices.
        # Use a read-only top-of-book midpoint as the reference price so the
        # scanner can discover candidates.  Execution still re-quotes both
        # executable sides, depth, fees and slippage in plan_entry().
        # Preserve HTTP/transport errors so discovery can distinguish an
        # empty book from a rejected request and apply the correct backoff.
        book = self.get_order_book(symbol, discovery=discovery)
        bids, asks = book.get('bids') or [], book.get('asks') or []
        if not bids or not asks:
            raise EmptyBookError('Lighter 盘口为空或缺少一侧')
        bid, ask = number(bids[0]['price'], True), number(asks[0]['price'], True)
        if bid > ask:
            raise MarketDataError('Lighter 盘口买卖价交叉')
        return (bid + ask) / 2.0

    def get_order_book(self, symbol, discovery=False):
        market, _ = self.get_market(symbol)
        data, fetched_at = self.client.get('/api/v1/orderBookOrders',
                                          {'market_id': market['market_id'], 'limit': 100}, ttl=2,
                                          discovery=discovery)
        result = {'symbol': symbol, 'market_id': market['market_id'],
                  'source': 'lighter_public_api', 'fetched_at': fetched_at, 'demo': False,
                  'received_at': fetched_at, 'time_basis': 'receipt_only'}
        for side in ('bids', 'asks'):
            if not isinstance(data.get(side), list):
                raise MarketDataError(f'Lighter {side} 缺失')
            levels = {}
            for order in data[side]:
                price = number(order.get('price'), True)
                size = number(order.get('remaining_base_amount'))
                if size > 0:
                    levels[price] = levels.get(price, Decimal(0)) + Decimal(str(order['remaining_base_amount']))
            result[side] = [{'price': p, 'size': float(levels[p])}
                            for p in sorted(levels, reverse=side == 'bids')]
        return result

    def get_account_info(self):
        if self.account_index is None:
            raise MarketDataError('未配置 Lighter 账户索引')
        data, _ = self.client.get('/api/v1/account',
                                  {'by': 'index', 'value': self.account_index})
        if not data.get('accounts'):
            raise MarketDataError('未找到 Lighter 账户')
        return data

    def get_positions(self):
        return self.get_account_info()['accounts'][0].get('positions', [])

    def get_balance(self):
        return self.get_account_info()['accounts'][0]

    def get_position_funding(self, symbol, start_timestamp_ms, end_timestamp_ms,
                             limit=100):
        """Read settled position-funding rows for one account and market.

        Lighter exposes this as a read-only account endpoint.  Keep the raw
        rows bounded and return a completion flag so callers never treat a
        truncated page as a zero-funding result.
        """
        market, _ = self.get_market(symbol)
        params = {
            'account_index': self.account_index,
            'limit': max(1, min(int(limit), 100)),
            'market_ids': str(market['market_id']),
            'start_timestamp': int(start_timestamp_ms),
            'end_timestamp': int(end_timestamp_ms),
        }
        rows = []
        cursor = None
        for _ in range(20):
            if cursor:
                params['cursor'] = cursor
            data, fetched_at = self.client.get('/api/v1/positionFunding', params, ttl=0)
            page = data.get('position_fundings')
            if not isinstance(page, list):
                raise MarketDataError('Lighter positionFunding 数据缺失')
            rows.extend(row for row in page if isinstance(row, dict))
            cursor = data.get('next_cursor')
            if not cursor:
                return {'symbol': symbol, 'market_id': int(market['market_id']),
                        'rows': rows, 'complete': True, 'fetched_at': fetched_at}
        raise MarketDataError('Lighter positionFunding 分页未完整读取')

    def place_order(self, *args, **kwargs):
        return {'success': False, 'status': 'rejected', 'filled_size': 0,
                'error': self.live_blocker}

    close_position = place_order
    cancel_order = place_order
    cancel_all_orders = place_order

    def close(self):
        self.client.close()
