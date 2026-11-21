"""Public WebSockets with bounded reconnects and explicit health state."""
import json
import math
import threading
import time
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import urlsplit
import websocket
from src.common.market_data import number, utc_now, age_seconds, cached_quote_age, cached_quote_time, MarketDataError, normalize_quote_time
from src.common.quote_clock import quote_clock
from src.common.discovery import ArcusSubscriptionError, arcus_subscription_batches, check_arcus_subscription_error


class SocketFeed:
    def __init__(self, url, proxy_url=None):
        self.url = url
        self.proxy_url = proxy_url
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.socket = None
        self.connected = False
        self.error = None
        self.last_message_at = None
        self.last_connected_at = None
        self.last_disconnect_at = None
        self.last_disconnect_reason = None
        self.message_count = 0
        self.timeout_count = 0
        self.reconnects = 0
        self.blocked = False
        self._quote_watchers = {}

    def watch_quotes(self, symbol, event):
        with self.lock:
            self._quote_watchers.setdefault(symbol, set()).add(event)

    def unwatch_quotes(self, symbol, event):
        with self.lock:
            events = self._quote_watchers.get(symbol, set())
            events.discard(event)
            if not events:
                self._quote_watchers.pop(symbol, None)

    def _quote_changed(self, symbol):
        # Only wake existing holders. No calculation, disk or network here.
        with self.lock:
            watchers = tuple(self._quote_watchers.get(symbol, ()))
        for event in watchers:
            event.set()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.connected = False
        if self.socket:
            self.socket.close()

    def quote_health(self, symbol):
        with self.lock:
            stamp = self.quote_timestamp(symbol)
            age = self.quote_age(symbol)
            return {**self.health(), 'quote_updated_at': stamp, 'quote_age_seconds': age,
                    'quote_status': 'fresh' if self.connected and age is not None and 0 <= age <= 2
                    else 'stale' if stamp else 'missing'}

    def quote_timestamp(self, symbol):
        return None

    def quote_age(self, symbol):
        return age_seconds(self.quote_timestamp(symbol))

    def health(self):
        with self.lock:
            return {'connected': self.connected, 'error': self.error, 'blocked': self.blocked,
                    'last_message_at': self.last_message_at, 'last_connected_at': self.last_connected_at,
                    'last_disconnect_at': self.last_disconnect_at,
                    'last_disconnect_reason': self.last_disconnect_reason,
                    'messages': self.message_count, 'timeouts': self.timeout_count,
                    'reconnects': self.reconnects, 'quote_clock': quote_clock.status()}

    def _run(self):
        delay = 1
        while not self.stop_event.is_set():
            try:
                options = {}
                if self.proxy_url:
                    proxy = urlsplit(self.proxy_url)
                    options = {'http_proxy_host': proxy.hostname, 'http_proxy_port': proxy.port or 7890,
                               'proxy_type': 'http'}
                ws = websocket.create_connection(self.url, timeout=15, **options)
                self.socket = ws
                with self.lock:
                    self.connected, self.error = True, None
                    self.last_connected_at = utc_now()
                    self.reset()
                self.subscribe(ws)
                last_ping = time.monotonic()
                while not self.stop_event.is_set():
                    # Busy books may never hit recv's idle timeout. Keep the
                    # transport alive even while messages arrive continuously.
                    if time.monotonic() - last_ping >= 30:
                        ws.ping()
                        last_ping = time.monotonic()
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        with self.lock:
                            self.timeout_count += 1
                        # A socket that accepts ping sends can still be dead.
                        # Reconnect after a bounded absence of all data frames.
                        silence = age_seconds(self.last_message_at or self.last_connected_at)
                        connected_age = age_seconds(self.last_connected_at)
                        if silence is not None and silence > 45 and connected_age is not None and connected_age > 45:
                            raise TimeoutError('no websocket data for 45 seconds')
                        ws.ping()
                        last_ping = time.monotonic()
                        continue
                    if not raw:
                        raise ConnectionError('connection closed')
                    if len(raw) > 4_000_000:
                        raise ValueError('oversized frame')
                    message = json.loads(raw)
                    if message.get('type') == 'ping':
                        ws.send(json.dumps({'type': 'pong'}))
                    with self.lock:
                        self.on_message(message)
                        self.last_message_at = utc_now()
                        self.message_count += 1
                    delay = 1
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.reset()
                    self.last_disconnect_at = utc_now()
                    self.last_disconnect_reason = type(exc).__name__+': '+str(exc)[:160]
                    # Respect explicit exchange access restrictions; do not reconnect around them.
                    if 'restricted jurisdiction' in str(exc).lower():
                        self.blocked = True
                        self.error = '平台拒绝当前网络地区的 WebSocket；已使用公开 REST 降级'
                    elif isinstance(exc, ArcusSubscriptionError):
                        self.error = str(exc)
                    else:
                        self.error = 'WebSocket 断开: ' + type(exc).__name__
            finally:
                if self.socket:
                    self.socket.close()
                self.socket = None
            if self.blocked or self.stop_event.wait(delay):
                break
            self.reconnects += 1
            delay = min(delay * 2, 30)


class VariationalPriceFeed(SocketFeed):
    """Observed website protocol; mark/index prices, never bid/ask RFQs."""
    def __init__(self, symbols, proxy_url=None):
        super().__init__('wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices', proxy_url)
        self.symbols = [s for s in symbols if s in ('BTC', 'ETH')]
        self.prices = {}

    def reset(self):
        self.prices.clear()

    def subscribe(self, ws):
        instruments = [{'underlying': s, 'instrument_type': 'perpetual_future',
                        'settlement_asset': 'USDC', 'funding_interval_s': 3600} for s in self.symbols]
        ws.send(json.dumps({'action': 'subscribe', 'instruments': instruments}))

    def on_message(self, message):
        channel = message.get('channel', '')
        for symbol in self.symbols:
            if channel != f'instrument_price:P-{symbol}-USDC-3600':
                continue
            p = message.get('pricing', {})
            price = number(p.get('price'), True)
            timestamp = p.get('timestamp')
            age = age_seconds(timestamp)
            if age is None or age < 0 or age > 30:
                return
            previous = self.prices.get(symbol)
            if previous and timestamp <= previous['mark_updated_at']:
                return
            self.prices[symbol] = {'mark_price': price,
                'index_price': number(p.get('underlying_price'), True),
                'mark_updated_at': timestamp, 'received_at': utc_now(),
                'mark_source': 'variational_websocket'}

    def snapshot(self, symbol):
        with self.lock:
            p = self.prices.get(symbol)
            if not self.connected or not p:
                return None
            age = age_seconds(p['mark_updated_at'])
            return dict(p) if age is not None and 0 <= age <= 15 else None


class ArcusBboFeed(SocketFeed):
    """Public BBO for discovery only; execution still reads sized REST depth."""
    def __init__(self, markets, proxy_url=None, ws_url='wss://api.arcus.xyz/v1/ws'):
        super().__init__(ws_url, proxy_url)
        self.markets = dict(markets)
        self.symbol_by_market = {str(m).upper():s for s,m in self.markets.items()}
        self.quotes = {}
        self.unavailable = set()
        self.funding = {}

    def reset(self):
        self.quotes.clear()
        self.unavailable.clear()
        self.funding.clear()

    def discovery_issue(self, symbol):
        with self.lock:
            return 'unavailable' if self.connected and symbol in self.unavailable else None

    def quote_timestamp(self, symbol):
        return self.quotes.get(symbol, {}).get('updated_at')

    def quote_age(self, symbol):
        quote = self.quotes.get(symbol, {})
        return cached_quote_age(age_seconds(quote.get('updated_at')), quote.get('_observed_monotonic'))

    def subscribe(self, ws):
        if len(self.markets) * 2 > 100:
            raise ArcusSubscriptionError('Arcus 订阅范围需要拆分到多条行情连接')
        for market in self.markets.values():
            ws.send(json.dumps({'type': 'subscribe', 'channel': 'bbo', 'id': market}))
            ws.send(json.dumps({'type': 'subscribe', 'channel': 'predictedFunding', 'id': market}))

    def on_message(self, message):
        from src.common.discovery import arcus_unavailable_market
        if message.get('channel') == 'predictedFunding':
            from src.execution.funding_forecast import forecast
            data = message.get('contents') or {}
            if not isinstance(data, dict):
                return
            market = data.get('market') or message.get('id')
            symbol = self.symbol_by_market.get(str(market).upper())
            if symbol:
                now = time.time()
                value = forecast(data.get('rate1h'), (math.floor(now/3600)+1)*3600,
                                 now, 'arcus_predictedFunding')
                if value:
                    self.funding[symbol] = value
            return
        unavailable = arcus_unavailable_market(message)
        if unavailable:
            for symbol, market in self.markets.items():
                if str(market).upper() == unavailable:
                    self.quotes.pop(symbol, None)
                    self.unavailable.add(symbol)
            return
        check_arcus_subscription_error(message)
        if message.get('channel') != 'bbo':
            return
        symbol = self.symbol_by_market.get(str(message.get('id')).upper())
        if symbol is None:
            return
        contents = message.get('contents') or {}
        try:
            bid, ask = (number(contents[key]['price'], True) for key in ('bestBid', 'bestAsk'))
            if bid > ask:
                raise ValueError('crossed BBO')
            stamp = contents.get('timestamp')
            received = datetime.now(timezone.utc)
            received_mono = time.monotonic()
            observed = (datetime.fromtimestamp(float(stamp) / 1e6, timezone.utc)
                        if stamp is not None else received)
            effective = (normalize_quote_time(observed, received, received_monotonic=received_mono)
                         if stamp is not None else received)
            age = age_seconds(effective.isoformat())
            if age is None or not 0 <= age <= 2:
                self.quotes.pop(symbol, None)
                return
            self.quotes[symbol] = {'bid': bid, 'ask': ask, 'updated_at': effective.isoformat(),
                                   'bid_size': contents['bestBid'].get('size'),
                                   'ask_size': contents['bestAsk'].get('size'),
                                   'exchange_updated_at': observed.isoformat(),
                                   'received_at': received.isoformat(),
                                   '_observed_monotonic': received_mono-(received-effective).total_seconds(),
                                   'source': 'arcus_websocket'}
            self.unavailable.discard(symbol)
            self._quote_changed(symbol)
        except (KeyError, TypeError, ValueError, OverflowError, MarketDataError):
            self.quotes.pop(symbol, None)

    def snapshot(self, symbol):
        with self.lock:
            quote = self.quotes.get(symbol)
            age = self.quote_age(symbol) if quote else None
            return dict({k:v for k,v in quote.items() if not k.startswith('_')},
                        updated_at=cached_quote_time(quote['updated_at'],quote.get('_observed_monotonic')),
                        funding=self.funding.get(symbol)) if self.connected and age is not None and 0 <= age <= 2 else None


class ArcusFeedGroup:
    """Two subscriptions per market; partition without dropping any market."""
    def __init__(self, markets, proxy_url=None, ws_url='wss://api.arcus.xyz/v1/ws'):
        self.feeds = [ArcusBboFeed(dict(batch), proxy_url, ws_url)
                      for batch in arcus_subscription_batches(markets.items(), 2)]
        self.by_symbol = {symbol: feed for feed in self.feeds for symbol in feed.markets}

    def start(self):
        for feed in self.feeds:
            feed.start()

    def add_markets(self, markets):
        missing = {s:m for s,m in markets.items() if s not in self.by_symbol}
        # Keep all existing sockets/quotes. New markets get bounded sockets.
        added = [ArcusBboFeed(dict(batch), self.feeds[0].proxy_url if self.feeds else None,
                             self.feeds[0].url if self.feeds else 'wss://api.arcus.xyz/v1/ws')
                 for batch in arcus_subscription_batches(missing.items(), 2)]
        for feed in added:
            self.by_symbol.update({symbol:feed for symbol in feed.markets})
            self.feeds.append(feed)
            feed.start()

    def stop(self):
        for feed in self.feeds:
            feed.stop()

    def snapshot(self, symbol):
        feed = self.by_symbol.get(symbol)
        return feed.snapshot(symbol) if feed else None

    def discovery_issue(self, symbol):
        feed = self.by_symbol.get(symbol)
        return feed.discovery_issue(symbol) if feed else None

    def quote_health(self, symbol):
        feed = self.by_symbol.get(symbol)
        return feed.quote_health(symbol) if feed else dict(connected=False, quote_status='missing')

    def health(self):
        states = [feed.health() for feed in self.feeds]
        connected = sum(state['connected'] for state in states)
        return dict(connected=bool(states) and connected == len(states),
            connections=len(states), connected_connections=connected,
            subscriptions=[len(feed.markets)*2 for feed in self.feeds],
            error=next((state['error'] for state in states if state.get('error')), None))


class LighterBookFeed(SocketFeed):
    """Native Lighter order-book stream for either deployment."""
    def __init__(self, markets, proxy_url=None, ws_url=None):
        super().__init__(ws_url or 'wss://mainnet.zklighter.elliot.ai/stream', proxy_url)
        self.markets = markets  # {symbol: market_id}
        self.symbol_by_market = {str(i):s for s,i in markets.items()}
        self.books = {}
        self.funding = {}

    def reset(self):
        self.books.clear()
        self.funding.clear()

    def quote_timestamp(self, symbol):
        book = self.books.get(symbol, {})
        received = book.get('received_at')
        observed = book.get('updated_at')
        if observed and received:
            return min(observed, received)
        return received

    def quote_age(self, symbol):
        return cached_quote_age(age_seconds(self.quote_timestamp(symbol)), self.books.get(symbol, {}).get('_observed_monotonic'))

    def discovery_issue(self, symbol):
        with self.lock:
            book = self.books.get(symbol)
            if self.connected and book is not None and (not book['bids'] or not book['asks']):
                return 'empty'
        return None

    def subscribe(self, ws):
        ws.send(json.dumps({'type': 'subscribe', 'channel': 'market_stats/all'}))
        with self.lock:
            markets = tuple(self.markets.values())
        for market_id in markets:
            ws.send(json.dumps({'type': 'subscribe', 'channel': f'order_book/{market_id}'}))

    def add_markets(self, markets):
        with self.lock:
            added = {s:m for s,m in markets.items() if s not in self.markets}
            self.markets.update(added)
            self.symbol_by_market.update({str(m):s for s,m in added.items()})
            socket = self.socket if self.connected else None
        if socket is not None:
            for market_id in added.values():
                socket.send(json.dumps({'type':'subscribe','channel':f'order_book/{market_id}'}))

    def on_message(self, message):
        kind = message.get('type')
        if kind in ('subscribed/market_stats', 'update/market_stats'):
            from src.execution.funding_forecast import forecast
            data = message.get('market_stats') or {}
            rows = [data] if 'market_id' in data else list(data.values())
            now = time.time()
            try:
                observed_at = datetime.fromtimestamp(number(message['timestamp'], True)/1000, timezone.utc)
                received = datetime.now(timezone.utc)
                observed = normalize_quote_time(observed_at, received, received_monotonic=time.monotonic()).timestamp()
            except (KeyError, ValueError, TypeError, OverflowError, OSError, MarketDataError):
                return
            if not 0 <= now-observed <= 120:
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = self.symbol_by_market.get(str(row.get('market_id')))
                try:
                    # Native market_stats rates are hourly percentages;
                    # funding_timestamp is the LAST settlement in ms.
                    value = forecast(float(row['current_funding_rate'])/100,
                                     float(row['funding_timestamp'])/1000+3600,
                                     observed, 'lighter_market_stats')
                except (KeyError, ValueError, TypeError):
                    value = None
                if symbol and value:
                    self.funding[symbol] = value
            return
        if kind not in ('subscribed/order_book', 'update/order_book'):
            return
        market_id = str(message.get('channel', '')).split(':')[-1]
        symbol = self.symbol_by_market.get(market_id)
        if symbol is None:
            return
        update = message.get('order_book', {})
        offset = update.get('offset')
        nonce = update.get('nonce')
        if not isinstance(offset, int) or not isinstance(nonce, int):
            raise ValueError('missing orderbook offset')
        if kind == 'subscribed/order_book':
            book = {'bids': {}, 'asks': {}, 'offset': offset, 'nonce': nonce}
        else:
            if symbol not in self.books:
                raise ValueError('delta before snapshot')
            book = self.books[symbol]
            if offset <= book['offset']:
                return
            # Observed official stream chains begin_nonce to the prior nonce.
            # Offsets are monotonic but can legitimately skip values.
            begin = update.get('begin_nonce')
            if begin != book['nonce'] or nonce <= book['nonce']:
                raise ValueError('orderbook sequence gap')
        received = datetime.now(timezone.utc)
        received_mono = time.monotonic()
        exchange_at = book.get('exchange_at')
        effective_at = book.get('updated_at') if exchange_at else received.isoformat()
        observed_mono = book.get('_observed_monotonic') if exchange_at else received_mono
        time_basis = 'exchange_last_observed' if exchange_at else 'receipt_only'
        if 'timestamp' in message:
            # Native stream timestamps are milliseconds. Receipt time alone
            # must not make a delayed exchange frame look new.
            try:
                observed = datetime.fromtimestamp(number(message['timestamp'], True) / 1000, timezone.utc)
                effective = normalize_quote_time(observed, received, received_monotonic=received_mono)
                effective_at = effective.isoformat()
                observed_mono = received_mono-(received-effective).total_seconds()
            except (ValueError, OverflowError, OSError, MarketDataError) as exc:
                raise ValueError('invalid orderbook timestamp') from exc
            exchange_at = observed.isoformat()
            time_basis = 'exchange'
        # Some frames may omit the optional exchange clock. Preserve the
        # previous observed age instead of resetting it to local receipt time.
        # Sequence continuity, rather than server clock monotonicity, orders deltas.
        for side in ('bids', 'asks'):
            for row in update.get(side, []):
                p = Decimal(str(row['price']))
                size = Decimal(str(row['size']))
                number(p, True); number(size)
                if size < 0:
                    raise ValueError('negative book size')
                if size == 0:
                    book[side].pop(p, None)
                else:
                    book[side][p] = size
        if book['bids'] and book['asks'] and max(book['bids']) > min(book['asks']):
            raise ValueError('crossed orderbook')
        book.update(offset=offset, nonce=nonce, received_at=received.isoformat(),
                    exchange_at=exchange_at, updated_at=effective_at, time_basis=time_basis,
                    _observed_monotonic=observed_mono)
        self.books[symbol] = book
        self._quote_changed(symbol)

    def snapshot(self, symbol):
        with self.lock:
            b = self.books.get(symbol)
            if not self.connected or not b:
                return None
            stamp = cached_quote_time(self.quote_timestamp(symbol), b.get('_observed_monotonic'))
            age = self.quote_age(symbol)
            if age is None or not 0 <= age <= 10:
                return None
            result = {'symbol': symbol, 'market_id': self.markets[symbol], 'source': 'lighter_websocket',
                      'fetched_at': stamp, 'received_at': b['received_at'], 'exchange_at': b.get('exchange_at'),
                      'time_basis': b['time_basis'],
                      'offset': b['offset'], 'nonce': b['nonce'], 'demo': False,
                      'funding': self.funding.get(symbol)}
            for side in ('bids', 'asks'):
                prices = sorted(b[side], reverse=side == 'bids')[:50]
                result[side] = [{'price': float(p), 'size': float(b[side][p])} for p in prices]
            return result
