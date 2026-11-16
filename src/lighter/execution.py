"""Official SDK IOC submission and reconciliation, not wired into the web monitor.

No credentials, raw SDK messages, or signed transactions are persisted or logged.
The transport uses the SDK's nonce manager; ambiguous failures never resend.
"""
import asyncio
import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from src.common.market_data import utc_now, age_seconds
from src.common.comparison import depth_price
from src.execution.orders import (decimal, direction, fresh_quote, lighter_plan, unknown,
                                  check_submit_deadline, SubmissionWindowExpired)
from src.execution.precheck import EntryPrecheckMixin, account_transaction
from src.execution.preparation import preparation_book
from src.common.rate_limit import RateLimitGate
from src.common.discovery import DiscoveryCooldown, EmptyBookError


def as_dict(value):
    return value.to_dict() if hasattr(value, 'to_dict') else value


class OfficialSDKTransport:
    def __init__(self, sdk, account_index, key_index):
        self.sdk, self.account_index, self.key_index = sdk, int(account_index), int(key_index)
        self.order_stream = None

    @classmethod
    def from_credentials(cls, account_index, key_index, private_key,
                         url='https://mainnet.zklighter.elliot.ai', proxy_url=None,
                         chain_id=None):
        # Construct inside an event loop. Monitor installs need not import the SDK.
        import lighter
        kwargs = dict(url=url, account_index=int(account_index),
                      api_private_keys={int(key_index): private_key})
        # Robinhood Lighter is a separate deployment.  Its L2 signing domain
        # is not the EVM network chain id (4663); the official SDK expects the
        # deployment-specific domain id 466324.
        if chain_id is not None:
            kwargs['chain_id'] = int(chain_id)
        sdk = lighter.SignerClient(**kwargs)
        if proxy_url:
            sdk.api_client.rest_client.proxy = proxy_url
        # The native signer CheckClient performs its own direct HTTP request
        # and ignores the Python REST client's proxy. Credential identity and
        # fee evidence were already verified through the proxied read-only
        # path before this live session is allowed to start. Keep the direct
        # check when no proxy is configured; with a proxy, authenticated REST
        # reconciliation is the check that can actually reach the deployment.
        if not proxy_url and sdk.check_client() is not None:
            raise RuntimeError('Lighter 签名客户端校验失败')
        transport = cls(sdk, account_index, key_index)
        transport.rate_limit = RateLimitGate(url)
        from .order_stream import LighterOrderStream, stream_url
        transport.order_stream = LighterOrderStream(stream_url(url), account_index, transport.auth,
            lambda order, plan, account, index: terminal_receipt(order, plan, account, index, []) is not None,
            proxy_url=proxy_url)
        transport.order_stream.start()
        return transport

    def auth(self):
        token, error = self.sdk.create_auth_token_with_expiry(api_key_index=self.key_index)
        if error or not token:
            raise RuntimeError('无法创建账户查询授权')
        return token

    async def submit(self, plan, client_index):
        check_submit_deadline(plan.get('submit_deadline_monotonic'))
        # Arm before send: a terminal push can arrive before HTTP acceptance.
        # Neither arming nor an acceptance response constitutes a fill.
        if self.order_stream is not None:
            self.order_stream.arm(plan, client_index)
        _, response, error = await self.sdk.create_order(
            market_index=plan['market_index'], client_order_index=client_index,
            base_amount=plan['base_amount'], price=plan['price'], is_ask=plan['is_ask'],
            order_type=self.sdk.ORDER_TYPE_LIMIT,
            time_in_force=self.sdk.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only=plan['reduce_only'], order_expiry=self.sdk.DEFAULT_IOC_EXPIRY)
        # Acceptance is not execution. Even an SDK error is reconciled before deciding.
        data = as_dict(response) if response else {}
        return {'accepted': not error and data.get('code') == 200}

    async def update_leverage(self, market_index, margin_mode, leverage):
        """Set the account's per-market leverage before a new position.

        Lighter does not carry leverage in ``create_order``.  It is a separate
        signed account transaction, so the execution path must verify the
        resulting market setting before submitting an order.
        """
        _, response, error = await self.sdk.update_leverage(
            market_index=int(market_index), margin_mode=int(margin_mode),
            leverage=float(leverage), api_key_index=self.key_index)
        data = as_dict(response) if response else {}
        return {'accepted': error is None and data.get('code', 200) == 200,
                'error': str(error) if error else None, 'response': data}

    async def find_order(self, market_id, client_index):
        stream = self.order_stream
        if stream is None:
            return await self._find_order_rest(market_id, client_index)
        row = stream.order(market_id, client_index)
        if row is not None:
            return row
        # Give a healthy subscription a short head start, avoiding history
        # requests when the terminal push is already on its way. Disconnected
        # streams add no grace delay. REST and push then race, so a slow REST
        # response cannot hold up complete execution evidence from the socket.
        if stream.ready:
            try:
                row = await asyncio.wait_for(stream.wait_order(market_id, client_index), .075)
                if row is not None:
                    return row
            except asyncio.TimeoutError:
                pass
        rest = asyncio.create_task(self._find_order_rest(market_id, client_index))
        pushed = asyncio.create_task(stream.wait_order(market_id, client_index))
        try:
            done, _ = await asyncio.wait((rest, pushed), return_when=asyncio.FIRST_COMPLETED)
            if pushed in done and pushed.result() is not None:
                return pushed.result()
            if rest in done:
                try:
                    recovered = rest.result()
                    status = recovered.get('status', '') if recovered else ''
                    if (not recovered or (status != 'filled' and not status.startswith('canceled'))) and not pushed.done():
                        # History can lag the stream or briefly show an open
                        # IOC. Keep listening instead of sleeping a poll cycle.
                        try:
                            row = await asyncio.wait_for(pushed, .25)
                            if row is not None:
                                return row
                        except asyncio.TimeoutError:
                            pass
                    return recovered
                except Exception:
                    # REST cooldown must not discard an imminent valid push.
                    try:
                        row = await asyncio.wait_for(pushed, .25)
                        if row is not None:
                            return row
                    except asyncio.TimeoutError:
                        pass
                    raise
            return await rest
        finally:
            for task in (rest, pushed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(rest, pushed, return_exceptions=True)

    async def _find_order_rest(self, market_id, client_index):
        gate = getattr(self, 'rate_limit', None)
        if isinstance(gate, RateLimitGate):
            gate.check()
        try:
            return await self._read_order_history(market_id, client_index)
        except Exception as exc:
            # Record a 429 even when a concurrent push wins the race. Keep the
            # shared REST cooldown out of the independent stream fast path.
            if isinstance(gate, RateLimitGate) and getattr(exc, 'status', None) == 429:
                gate.reject(SimpleNamespace(status_code=429, headers=getattr(exc, 'headers', None) or {}))
            raise

    async def _read_order_history(self, market_id, client_index):
        token = self.auth()
        # IOC orders normally land in inactive history. Query that first and
        # only fall back to the active endpoint for a still-open order; this
        # removes one round trip from the common filled path.
        cursor = None
        for _ in range(10):
            args = {'authorization': token, 'account_index': self.account_index,
                    'market_id': market_id, 'limit': 100, '_request_timeout': 5.0}
            if cursor:
                args['cursor'] = cursor
            page = as_dict(await self.sdk.order_api.account_inactive_orders(**args))
            for order in page.get('orders', []):
                if order.get('client_order_index') == client_index:
                    return order
            cursor = page.get('next_cursor')
            if not cursor:
                break
        active = as_dict(await self.sdk.order_api.account_active_orders(
            authorization=token, account_index=self.account_index, market_id=market_id, _request_timeout=5.0))
        for order in active.get('orders', []):
            if order.get('client_order_index') == client_index:
                return order
        return None

    async def active_orders(self, market_id=None):
        data = as_dict(await self.sdk.order_api.account_active_orders(
            authorization=self.auth(), account_index=self.account_index,
            market_id=market_id, _request_timeout=5.0))
        if data.get('code') != 200 or not isinstance(data.get('orders'), list):
            raise RuntimeError('未确认活动订单')
        return data['orders']

    async def inactive_orders(self, market_id, limit=100, max_pages=10):
        """Read the authenticated account order history for one market.

        This is an explicit audit path, separate from the hot execution loop.
        Keep pagination bounded so a large exchange history cannot stall the
        dashboard or turn a refresh click into an unbounded download.
        """
        result, cursor = [], None
        token = self.auth()
        page_limit = max(1, min(int(limit), 100))
        for _ in range(max(1, min(int(max_pages), 20))):
            args = dict(limit=page_limit, market_id=int(market_id), account_index=self.account_index,
                        authorization=token, _request_timeout=5.0)
            if cursor:
                args['cursor'] = cursor
            page = as_dict(await self.sdk.order_api.account_inactive_orders(**args))
            if page.get('code', 200) != 200 or not isinstance(page.get('orders'), list):
                raise RuntimeError('历史订单分页未返回完整数据')
            result.extend(page['orders'])
            cursor = page.get('next_cursor')
            if not cursor:
                return result
        raise RuntimeError('历史订单分页未完整读取')

    async def trades(self, market_id, order_index):
        result, cursor = [], None
        token = self.auth()
        for _ in range(20):
            args = dict(sort_by='timestamp', sort_dir='desc', limit=100, market_id=market_id,
                        order_index=order_index, account_index=self.account_index,
                        authorization=token, _request_timeout=5.0)
            if cursor:
                args['cursor'] = cursor
            page = as_dict(await self.sdk.order_api.trades(**args))
            result.extend(page.get('trades', []))
            cursor = page.get('next_cursor')
            if not cursor:
                return result
        raise RuntimeError('成交分页未完整读取')

    async def position_funding(self, market_id, start_timestamp_ms, end_timestamp_ms):
        """Read authenticated funding settlements for one account/market."""
        account_api = getattr(self.sdk, 'account_api', None)
        if account_api is None:
            import lighter
            account_api = lighter.AccountApi(self.sdk.api_client)
        result, cursor = [], None
        for _ in range(20):
            args = dict(account_index=self.account_index, limit=100,
                        authorization=self.auth(), market_ids=[int(market_id)],
                        start_timestamp=int(start_timestamp_ms),
                        end_timestamp=int(end_timestamp_ms), _request_timeout=5.0)
            if cursor:
                args['cursor'] = cursor
            page = as_dict(await account_api.position_funding(**args))
            if page.get('code', 200) != 200 or not isinstance(page.get('position_fundings'), list):
                raise RuntimeError('资金费分页未返回完整数据')
            result.extend(page['position_fundings'])
            cursor = page.get('next_cursor')
            if not cursor:
                return result
        raise RuntimeError('资金费分页未完整读取')

    async def cancel(self, market_id, order_index):
        await self.sdk.cancel_order(market_index=market_id, order_index=order_index)

    async def close(self):
        try:
            if self.order_stream is not None:
                await self.order_stream.close()
        finally:
            await self.sdk.close()


class SDKLoop:
    """One loop/thread for the SDK session and serial nonce allocation."""
    def __init__(self, transport_factory):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        async def create():
            return transport_factory()
        future = asyncio.run_coroutine_threadsafe(create(), self.loop)
        try:
            self.transport = future.result(30)
        except Exception:
            future.cancel()
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            self.loop.close()
            raise

    def call(self, name, *args):
        gate = getattr(self.transport, 'rate_limit', None)
        if isinstance(gate, RateLimitGate) and name in (
                'active_orders', 'inactive_orders', 'trades', 'position_funding'):
            gate.check()
        future = asyncio.run_coroutine_threadsafe(getattr(self.transport, name)(*args), self.loop)
        try:
            return future.result(30)
        except TimeoutError:
            future.cancel()
            raise
        except Exception as exc:
            if isinstance(gate, RateLimitGate) and getattr(exc, 'status', None) == 429:
                gate.reject(SimpleNamespace(status_code=429, headers=getattr(exc, 'headers', None) or {}))
            raise

    def close(self):
        try:
            self.call('close')
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            self.loop.close()


def terminal_receipt(order, plan, account_index, client_index, trades):
    if (order.get('owner_account_index') != account_index or order.get('market_index') != plan['market_index'] or
            order.get('client_order_index') != client_index or order.get('is_ask') != plan['is_ask'] or
            bool(order.get('reduce_only')) != plan['reduce_only'] or
            decimal(order['initial_base_amount']) != decimal(plan['size'])):
        raise ValueError('订单身份与提交意图不匹配')
    status = order.get('status', '')
    if status != 'filled' and not status.startswith('canceled'):
        return None
    filled = decimal(order['filled_base_amount'])
    if not 0 <= filled <= decimal(plan['size']):
        raise ValueError('成交数量越界')
    if status == 'filled' and filled != decimal(plan['size']):
        raise ValueError('完全成交状态与数量矛盾')
    total = decimal(order['filled_quote_amount'])
    if (filled == 0 and total != 0) or (filled > 0 and total <= 0):
        raise ValueError('成交金额无效')
    price = total / filled if filled else None
    if price and ((plan['is_ask'] and price < decimal(plan['limit_price'])) or
                  (not plan['is_ask'] and price > decimal(plan['limit_price']))):
        raise ValueError('成交价越过保护限价，需核对')
    # Fee integer units differ from quote currency. Never invent a conversion.
    # Zero is provable only when every matching fill explicitly has zero fees.
    fees, seen, trade_size = 0.0, set(), decimal(0)
    fee_fields_observed = False
    for trade in trades:
        key = trade.get('trade_id')
        if key in seen:
            continue
        seen.add(key)
        side = 'ask' if plan['is_ask'] else 'bid'
        if (trade.get(side+'_id') != order['order_index'] or
                trade.get(side+'_account_id') != account_index or trade.get('market_id') != plan['market_index']):
            continue
        trade_size += decimal(trade['size'], True)
        maker = bool(trade['is_maker_ask']) == plan['is_ask']
        for field in (('maker_fee', 'integrator_maker_fee') if maker else ('taker_fee', 'integrator_taker_fee')):
            if field in trade:
                fee_fields_observed = True
            if trade.get(field) != 0:
                fees = None
    if trade_size != filled:
        fees = None
    return {'success': filled > 0, 'confirmed': True,
            'status': 'filled' if filled == decimal(plan['size']) else 'partial' if filled else 'canceled',
            'filled_size': float(filled), 'average_price': float(price) if price else None,
            'order_id': str(order['order_id']), 'fees_usdc': fees,
            'fee_fields_observed': fee_fields_observed,
            'fees_verified': fees is not None, 'funding_usdc': None,
            'note': '非零费用单位与本次平仓资金费未验收时保留未知，不按零处理'}


class LighterExecutionAdapter(EntryPrecheckMixin):
    supports_grid_additions = True
    supports_close_quote_reuse = True
    live_blocker = 'Lighter 执行代码已实现；需签名账户、费用和账户核对验收后启用'

    def __init__(self, public, bridge, journal, account_index, enabled=False, poll_seconds=8,
                 venue='lighter'):
        self.public, self.bridge, self.journal = public, bridge, journal
        self.account_index, self.enabled, self.poll_seconds = int(account_index), enabled, poll_seconds
        self.venue = venue
        self.lock = threading.RLock()
        # A leverage setting is account-level configuration, not an order
        # field.  Reuse a confirmed setting during one worker session instead
        # of doing an account read and signed transaction for every order.
        self._leverage_cache = {}
        self._leverage_cache_seconds = 300
        self._leverage_info_cache = {}
        self._leverage_info_cache_seconds = 30
        self._reference_feed = None
        self._account_feed = None
        self._reference_symbols = ()
        self._reference_retry_at = 0
        self._reference_lock = threading.Lock()
        self.discovery_supported_symbols = None
        self._discovery_cooldown = DiscoveryCooldown('Lighter RH' if venue == 'lighter_rh' else 'Lighter')

    def start_reference_feed(self, symbols):
        """Warm a public stream only when an execution worker is started."""
        self._reference_symbols = tuple(symbols)
        self._ensure_reference_feed()

    def _ensure_reference_feed(self):
        # A startup catalog request may be blocked by another process's 429.
        # Retry on later reads, without turning every symbol into a REST retry.
        if (self._reference_feed is not None or not self._reference_symbols or
                time.monotonic() < self._reference_retry_at):
            return
        api = self.public.config.get('api', {})
        if not api.get('ws_url') or not self._reference_lock.acquire(blocking=False):
            return
        try:
            if (self._reference_feed is not None or not self._reference_symbols or
                    time.monotonic() < self._reference_retry_at):
                return
            self._reference_retry_at = time.monotonic() + 30
            markets, _ = self.public.get_markets()
            self.discovery_supported_symbols = frozenset(markets)
            mapping = {symbol: markets[symbol]['market_id'] for symbol in self._reference_symbols
                       if symbol in markets and markets[symbol].get('market_id') is not None}
            if mapping:
                from src.common.live_feed import LighterBookFeed
                feed = LighterBookFeed(mapping, api.get('proxy_url'), ws_url=api['ws_url'])
                feed.start()
                self._reference_feed = feed
        except Exception as exc:
            self._reference_retry_at = time.monotonic() + max(5, getattr(exc, 'retry_after_seconds', 30))
        finally:
            self._reference_lock.release()

    def expand_reference_feed(self, symbols):
        requested = tuple(dict.fromkeys(self._reference_symbols + tuple(symbols)))
        if requested == self._reference_symbols and self._reference_feed is not None:
            return
        self.start_reference_feed(requested)
        if self._reference_feed is not None:
            markets, _ = self.public.get_markets()
            self.discovery_supported_symbols = frozenset(markets)
            self._reference_feed.add_markets({s:markets[s]['market_id'] for s in requested if s in markets})

    def close_reference_feed(self):
        with self._reference_lock:
            self._reference_symbols = ()
            self._reference_retry_at = 0
            if self._reference_feed is not None:
                self._reference_feed.stop()
                self._reference_feed = None

    def _fresh_reference_book(self, symbol, max_age_seconds=2, *, ensure_feed=True):
        if ensure_feed:
            self._ensure_reference_feed()
        book = self._reference_feed.snapshot(symbol) if self._reference_feed is not None else None
        if book is not None:
            age = age_seconds(book.get('fetched_at'))
            if age is not None and 0 <= age <= max_age_seconds and book.get('bids') and book.get('asks'):
                return book
        return None

    @property
    def live_ready(self):
        return bool(self.enabled and self.bridge is not None and hasattr(self, 'verified_fee_bps'))

    def size_decimals(self, symbol):
        return self.public.get_market(symbol)[0]['supported_size_decimals']

    def get_market_price(self, symbol):
        # Discovery only: the native book retains its actual timestamp.
        # Holding reads below keep the 2s limit; sized execution re-quotes.
        book = self._fresh_reference_book(symbol, max_age_seconds=10)
        if book is not None:
            self._discovery_cooldown.fresh_stream(symbol)
            return (float(book['bids'][0]['price']) + float(book['asks'][0]['price'])) / 2
        issue = self._reference_feed.discovery_issue(symbol) if self._reference_feed is not None else None
        if issue == 'empty':
            self._discovery_cooldown.observe(symbol, issue)
        self._discovery_cooldown.check(symbol)
        try:
            price = self.public.get_market_price(symbol, discovery=True)
        except EmptyBookError:
            self._discovery_cooldown.failed(symbol, 'empty')
            self._discovery_cooldown.check(symbol)
            raise
        self._discovery_cooldown.recovered(symbol)
        return price

    def reference_health(self, symbol):
        if self._reference_feed is not None:
            return self._reference_feed.quote_health(symbol)
        return {'connected': False, 'quote_status': 'missing',
                'retry_seconds': max(0, self._reference_retry_at - time.monotonic())}

    def get_position_mark_snapshot(self, symbol):
        """Priority holding read; optional discovery pacing never applies."""
        book = self._fresh_reference_book(symbol) or self.public.get_order_book(symbol)
        from src.execution.position_monitor import PositionMonitor
        result = {'bid': float(book['bids'][0]['price']), 'ask': float(book['asks'][0]['price']),
                  'updated_at': book['fetched_at'], 'source': book.get('source', 'lighter_public_api'),
                  'fee_bps': getattr(self, 'verified_fee_bps', None),
                  **{key: book[key] for key in ('received_at', 'exchange_at', 'time_basis',
                      'market_id', 'offset', 'nonce') if key in book}}
        PositionMonitor._validate_quote(result)
        return result

    def cached_reference_quote(self, symbol):
        """Use the strict fresh WS book without starting a REST request."""
        book = self._fresh_reference_book(symbol, ensure_feed=False)
        if book is None:
            return None
        return dict(bid=float(book['bids'][0]['price']), ask=float(book['asks'][0]['price']),
                    updated_at=book['fetched_at'], fee_bps=getattr(self, 'verified_fee_bps', None), book=book,
                    funding=book.get('funding'), **{key: book[key] for key in (
                        'source', 'received_at', 'exchange_at', 'time_basis', 'market_id',
                        'offset', 'nonce') if key in book})

    def history_orders(self, symbol, limit=100, max_pages=10):
        """Return authenticated inactive orders for an explicit audit call."""
        market, _ = self.public.get_market(symbol)
        return self.bridge.call('inactive_orders', market['market_id'], limit, max_pages)

    def reconcile_funding(self, symbol, start_time, end_time):
        """Read and total settled funding for a closed position.

        This is deliberately read-only.  An incomplete response stays
        unknown so the coordinator keeps the accounting-pending state.
        """
        market, _ = self.public.get_market(symbol)
        start_ms, end_ms = int(start_time.timestamp() * 1000), int(end_time.timestamp() * 1000)
        rows = None
        try:
            result = self.bridge.call('position_funding', int(market['market_id']), start_ms, end_ms)
            if isinstance(result, list):
                rows = result
        except Exception:
            rows = None
        if rows is None and hasattr(self.public, 'get_position_funding'):
            result = self.public.get_position_funding(symbol, start_ms, end_ms)
            if result.get('complete') is not True:
                return None
            rows = result.get('rows', [])
            fetched_at = result.get('fetched_at')
        elif rows is not None:
            fetched_at = utc_now()
        else:
            return None
        total = decimal('0')
        safe_rows = []
        for row in rows:
            if not isinstance(row, dict) or row.get('change') is None:
                return None
            total += decimal(row['change'])
            safe_rows.append({key: row.get(key) for key in
                         ('timestamp', 'market_id', 'funding_id', 'change', 'rate',
                          'position_size', 'position_side')})
        return {'complete': True, 'venue': self.venue, 'symbol': symbol,
                'market_id': int(market['market_id']), 'funding_usdc': float(total),
                'rows': safe_rows, 'observed_at': fetched_at}

    def order_constraints(self, symbol):
        """Return current venue minimums used by the pair planner."""
        market, _ = self.public.get_market(symbol)
        return {
            'min_base_amount': market.get('min_base_amount'),
            'min_quote_amount': market.get('min_quote_amount'),
        }

    def discovery_admission(self, symbol, *, preparation=False):
        """Inspect the native stream and shared REST quota without reading REST."""
        if self._fresh_reference_book(symbol, max_age_seconds=2 if preparation else 10,
                                      ensure_feed=False) is not None:
            return {'wait_seconds': 0, 'rest_hosts': ()}
        gate = self.public.client.rate_limit
        delay = gate.discovery_delay(priority=preparation)
        if not preparation:
            delay = max(delay, self._discovery_cooldown.remaining(symbol))
        return {'wait_seconds': delay, 'rest_hosts': (str(gate.path),)}

    @preparation_book
    def get_sizing_book(self, symbol):
        from src.execution.preparation import is_preparing
        if is_preparing():
            return self._fresh_reference_book(symbol) or self.public.get_order_book(symbol, discovery=True)
        return self._fresh_reference_book(symbol) or self.public.get_order_book(symbol)

    def leverage_info(self, symbol, force=False):
        """Read market limits and this account's current market setting."""
        cached = self._leverage_info_cache.get(symbol)
        if not force and cached and time.monotonic() - cached['verified_at'] <= self._leverage_info_cache_seconds:
            return dict(cached['info'])
        def imf_bps(value, account=False):
            # Market metadata uses basis points (2000 = 20%), while the
            # account snapshot returns a percentage string ("20.00").
            if value in (None, ''):
                return None
            number = decimal(value, True)
            result = number * 100 if account else number
            if result <= 0 or result > 10000:
                raise ValueError('Lighter 初始保证金设置无效')
            return result

        market, _ = self.public.get_market(symbol)
        # ``active_only=false`` is required here.  Lighter's account endpoint
        # also stores leverage/margin settings for markets with no live
        # position; the default response may omit those rows, leaving the
        # strategy with no IMF and a Decimal conversion failure at the final
        # pre-submit check.
        data, fetched_at = self.public.client.get('/api/v1/account',
            {'by': 'index', 'value': self.account_index, 'active_only': False}, ttl=0 if force else 1)
        matches = [a for a in data.get('accounts', [])
                   if str(a.get('account_index')) == str(self.account_index)]
        if len(matches) != 1:
            raise ValueError('账户身份未确认')
        rows = [p for p in matches[0].get('positions', [])
                if str(p.get('market_id')) == str(market['market_id'])]
        if len(rows) > 1:
            raise ValueError('Lighter 账户市场设置重复，需核对')
        row = rows[0] if len(rows) == 1 else None
        minimum_imf = imf_bps(market.get('min_initial_margin_fraction'))
        default_imf = imf_bps(market.get('default_initial_margin_fraction'))
        account_imf = imf_bps(row.get('initial_margin_fraction'), account=True) if row else None
        if default_imf is None or minimum_imf is None:
            # An explicitly returned account setting is sufficient for a
            # conservative read-only check. An untouched market needs the
            # verified market default, never the configured target leverage.
            if account_imf is None:
                risk, _ = self.public.get_market_risk(symbol)
                default_imf = imf_bps(risk.get('default_initial_margin_fraction'))
                minimum_imf = imf_bps(risk.get('min_initial_margin_fraction'))
        # The current orderBooks catalog omits market IMF/default fields, but
        # the account settings row still contains the effective IMF even with
        # no open position.  Use it as the current/default setting and as a
        # conservative maximum when the market minimum is unavailable.
        current_imf = account_imf or default_imf
        if row is not None and account_imf is None:
            raise ValueError(f'Lighter {symbol} 账户已有市场记录但保证金设置缺失')
        if current_imf is None:
            raise ValueError(f'Lighter {symbol} 未返回账户杠杆或初始保证金设置')
        minimum_imf = minimum_imf or current_imf
        default_imf = default_imf or current_imf
        if current_imf < minimum_imf:
            raise ValueError('Lighter 初始保证金设置无效')
        current_leverage = 10000 / float(current_imf)
        max_leverage = 10000 / float(minimum_imf)
        result = {
            'venue': self.venue, 'symbol': symbol, 'market_id': int(market['market_id']),
            'current_leverage': current_leverage, 'current_imf_bps': float(current_imf),
            'default_leverage': 10000 / float(default_imf),
            'max_leverage': max_leverage, 'margin_mode': int(row.get('margin_mode', 0)) if row else 0,
            'setting_source': 'account' if account_imf else 'market_default_no_account_row',
            'fetched_at': fetched_at,
        }
        self._leverage_info_cache[symbol] = {'verified_at': time.monotonic(), 'info': result}
        return dict(result)

    @account_transaction(confirmed_return=True)
    def ensure_leverage(self, symbol, target_leverage):
        """Make the signed account setting match the requested common leverage."""
        target = float(target_leverage)
        if not math.isfinite(target) or target < 1:
            raise ValueError('统一杠杆必须为不小于 1 的有限数值')
        cache_key = (symbol, target)
        cached = self._leverage_cache.get(cache_key)
        if cached and time.monotonic() - cached['verified_at'] <= self._leverage_cache_seconds:
            return {**cached['info'], 'changed': False, 'target_leverage': target,
                    'cache_hit': True}
        info = self.leverage_info(symbol)
        if target > info['max_leverage'] + 1e-9:
            raise ValueError(f"{self.venue} {symbol} 最高支持 {info['max_leverage']:g} 倍，不能设置 {target:g} 倍")
        if abs(info['current_leverage'] - target) <= 1e-9:
            result = {**info, 'changed': False, 'target_leverage': target, 'cache_hit': False}
            self._leverage_cache[cache_key] = {'verified_at': time.monotonic(), 'info': result}
            return result
        self.invalidate_entry_precheck()
        result = self.bridge.call('update_leverage', info['market_id'], info['margin_mode'], target)
        if not isinstance(result, dict) or not result.get('accepted'):
            # Keep the signed API's rejection detail.  Previously this was
            # collapsed to a generic ValueError, making an account/API-key or
            # deployment rejection look identical to a quote failure.
            detail = result.get('error') if isinstance(result, dict) else None
            response = result.get('response') if isinstance(result, dict) else None
            if not detail and isinstance(response, dict):
                detail = response.get('message') or response.get('error') or response.get('code')
            raise ValueError(f"{self.venue} 杠杆设置失败: {detail or '未返回成功回执'}")
        deadline = time.monotonic() + 8
        self._leverage_info_cache.pop(symbol, None)
        while time.monotonic() < deadline:
            time.sleep(.25)
            refreshed = self.leverage_info(symbol, force=True)
            if abs(refreshed['current_leverage'] - target) <= 1e-9:
                result = {**refreshed, 'changed': True, 'target_leverage': target, 'cache_hit': False}
                self._leverage_cache[cache_key] = {'verified_at': time.monotonic(), 'info': result}
                return result
        raise ValueError(f"{self.venue} 杠杆设置后未确认")

    def get_executable_quote(self, symbol, quantity):
        book = self.get_sizing_book(symbol)
        return self.quote_from_sizing_book(symbol, quantity, book)

    def quote_from_sizing_book(self, symbol, quantity, book):
        """Build a size-bound quote locally; freshness and depth remain required."""
        age = age_seconds(book['fetched_at'])
        if age is None or not 0 <= age <= 5:
            raise ValueError('盘口过期')
        buy, sell = depth_price(book['asks'], float(quantity)), depth_price(book['bids'], float(quantity))
        if not buy['sufficient'] or not sell['sufficient']:
            raise ValueError('可见深度不足')
        # A fee schedule is account-specific; no default of zero for execution.
        if not hasattr(self, 'verified_fee_bps'):
            raise ValueError('尚未核验当前账户费率')
        # Limits must reach the last consumed level, not stop at the average.
        # The coordinator uses these conservative prices for the final profit check.
        return dict(symbol=symbol, size=quantity, bid=sell['worst_price'], ask=buy['worst_price'],
                    bid_vwap=sell['vwap'], ask_vwap=buy['vwap'],
                    updated_at=book['fetched_at'], executable=self.live_ready,
                    instrument_type='perpetual', fee_bps=self.verified_fee_bps,
                    funding=book.get('funding'), sizing_book=book,
                    **{key: book[key] for key in ('source', 'market_id', 'offset', 'nonce',
                        'received_at', 'exchange_at', 'time_basis') if key in book})

    def validate_quote_order(self, symbol, quantity, side, quote, slippage_bps):
        market, _ = self.public.get_market(symbol)
        return lighter_plan(market, symbol, side, quantity, quote['ask' if side == 'buy' else 'bid'], slippage_bps)

    def account_snapshot_identity(self):
        return self.venue, self.account_index, self.public.base_url, id(self.bridge)

    def start_account_watch(self):
        api = self.public.config.get('api', {})
        if self._account_feed is None and api.get('ws_url'):
            from .account_feed import LighterAccountFeed
            self._account_feed = LighterAccountFeed(self.account_index, api['ws_url'], api.get('proxy_url'))
            self._account_feed.start()

    def stop_account_watch(self):
        if self._account_feed is not None:
            self._account_feed.stop()
            self._account_feed = None

    def account_snapshot_revision(self):
        return self._account_feed.token() if self._account_feed is not None else None

    @staticmethod
    def entry_available_balance(state):
        return state['account']['available_balance']

    def read_entry_account_state(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            account_future = pool.submit(self.public.client.get, '/api/v1/account',
                {'by': 'index', 'value': self.account_index}, 0)
            # Official SDK: market_id=None returns orders for ALL markets.
            orders_future = pool.submit(self.bridge.call, 'active_orders', None)
            data, _ = account_future.result()
            orders = orders_future.result()
        matches = [a for a in data.get('accounts', [])
                   if str(a.get('account_index')) == str(self.account_index)]
        if len(matches) != 1 or not isinstance(matches[0].get('positions'), list):
            raise ValueError('Lighter 全账户身份或持仓未确认')
        account = matches[0]
        balance = decimal(account['available_balance'])
        if not balance.is_finite() or balance < 0:
            raise ValueError('Lighter 可用余额无效')
        for position in account['positions']:
            if not isinstance(position, dict) or position.get('market_id') is None:
                raise ValueError('Lighter 持仓市场未确认')
            amount = decimal(position['position'])
            if not amount.is_finite():
                raise ValueError('Lighter 持仓数量无效')
        if not isinstance(orders, list) or any(
                not isinstance(o, dict) or o.get('market_index') is None or
                str(o.get('owner_account_index')) != str(self.account_index) for o in orders):
            raise ValueError('Lighter 全账户活动订单未确认')
        return {'account': account, 'orders': orders}

    def validate_order(self, symbol, quantity, reduce_only=False, side=None, account_state=None):
        market, _ = self.public.get_market(symbol)
        if not reduce_only:
            market_price = decimal(self.get_market_price(symbol), True)
            quantity_decimal = decimal(quantity, True)
            notional = quantity_decimal * market_price
            min_base = decimal(market['min_base_amount'], True)
            min_quote = decimal(market['min_quote_amount'], True)
            if quantity_decimal < min_base or notional < min_quote:
                raise ValueError(
                    f'Lighter 最小下单条件不满足: 数量 {quantity_decimal:g} < {min_base:g} '
                    f'或名义金额 {notional:.6f} < {min_quote:g}')
        # These are independent read-only checks. Fetch the fresh account
        # snapshot and active orders together; both still complete before a
        # submission is allowed.
        if reduce_only and account_state is None:
            cache = getattr(self, '_account_snapshot_cache', None)
            cached = cache.get() if cache is not None else None
            if cached is not None:
                account_state = cached[0]
        if account_state is None:
            with ThreadPoolExecutor(max_workers=2) as pool:
                account_future = pool.submit(
                    self.public.client.get, '/api/v1/account',
                    {'by': 'index', 'value': self.account_index}, 0)
                active_future = pool.submit(self.bridge.call, 'active_orders', market['market_id'])
                data, _ = account_future.result()
                active_orders = active_future.result()
            matches = [a for a in data.get('accounts', []) if a.get('account_index') == self.account_index]
            if len(matches) != 1:
                raise ValueError('账户身份未确认')
            account = matches[0]
        else:
            account = account_state['account']
            active_orders = [o for o in account_state['orders']
                             if str(o['market_index']) == str(market['market_id'])]
        positions = [p for p in account.get('positions', []) if str(p.get('market_id')) == str(market['market_id'])
                     and decimal(p.get('position', '0')) != 0]
        if active_orders:
            raise ValueError('当前市场有活动订单，需先核对')
        if reduce_only:
            if len(positions) != 1 or abs(decimal(positions[0]['position'])) < decimal(quantity):
                raise ValueError('只减仓数量超过真实持仓')
            close_side = 'sell' if int(positions[0]['sign']) > 0 else 'buy'
            if direction(side) != close_side:
                raise ValueError('平仓方向与持仓不符')
        else:
            if len(positions) > 1:
                raise ValueError('同市场多条持仓记录，禁止叠加')
            signed = (abs(decimal(positions[0]['position'])) *
                      (1 if int(positions[0]['sign']) > 0 else -1)) if positions else 0
            self.validate_entry_position(symbol, signed, side)
            # Conservative 1x collateral requirement until leverage/margin verification.
            if decimal(account['available_balance']) < quantity_decimal * market_price:
                raise ValueError('可用保证金不足以覆盖保守的一倍名义金额')
            return {'available_balance': str(account['available_balance']),
                    'min_base_amount': str(min_base), 'min_quote_amount': str(min_quote)}

    def assert_flat(self, symbol):
        market, _ = self.public.get_market(symbol)
        data, at = self.public.client.get('/api/v1/account',
            {'by': 'index', 'value': self.account_index}, ttl=0)
        age = age_seconds(at)
        matches = [a for a in data.get('accounts', []) if a.get('account_index') == self.account_index]
        if len(matches) != 1 or age is None or not 0 <= age <= 5:
            raise ValueError('Lighter 平仓后账户快照缺失或过期')
        positions = matches[0].get('positions')
        if (not isinstance(positions, list) or
                any(decimal(p['position']) != 0 for p in positions) or
                self.bridge.call('active_orders', market['market_id'])):
            raise ValueError('Lighter 当前账户未确认空仓且无挂单')
        return True

    def position_snapshot(self, symbol):
        """Return a fresh, signed-account position snapshot for reconciliation.

        This is deliberately separate from ``assert_flat``: the coordinator
        needs to distinguish a manually closed leg from a still-open or
        mismatched leg before it sends any reduce-only order.
        """
        market, _ = self.public.get_market(symbol)
        data, fetched_at = self.public.client.get(
            '/api/v1/account', {'by': 'index', 'value': self.account_index}, ttl=0)
        matches = [account for account in data.get('accounts', [])
                   if account.get('account_index') == self.account_index]
        if len(matches) != 1:
            raise ValueError('Lighter 账户身份未确认')
        positions = [row for row in matches[0].get('positions', [])
                     if row.get('market_id') == market['market_id']]
        non_zero = []
        for row in positions:
            try:
                raw = decimal(row.get('position', '0'))
            except (TypeError, ValueError):
                raise ValueError('Lighter 持仓数量缺失') from None
            if raw == 0:
                continue
            try:
                sign = int(row.get('sign'))
            except (TypeError, ValueError):
                sign = 1 if raw > 0 else -1
            non_zero.append({'qty': float(abs(raw)),
                             'side': 'long' if sign >= 0 else 'short',
                             'raw': row})
        if len(non_zero) > 1:
            raise ValueError('Lighter 当前市场持仓快照不明确')
        result = non_zero[0] if non_zero else {'qty': 0.0, 'side': None, 'raw': None}
        result['fetched_at'] = fetched_at
        return result

    def place_order(self, symbol, side, size, price, slippage_bps, client_order_id, quote, reduce_only=False):
        if not self.live_ready:
            return {'success': False, 'confirmed': True, 'status': 'rejected', 'filled_size': 0, 'error': self.live_blocker}
        # These checks happen before journal reservation and before the signed
        # submit call. A quote can age out while the other leg is being
        # checked; expose that deterministic local rejection so the
        # coordinator can safely move to another candidate.
        try:
            fresh_quote(quote, symbol, size)
        except ValueError as exc:
            return {'success': False, 'confirmed': True, 'status': 'rejected',
                    'filled_size': 0, 'error': '提交前报价校验失败，未发送订单：' + str(exc)}
        with self.lock:
            normalized_side = direction(side)
            if decimal(price) != decimal(quote['ask' if normalized_side == 'buy' else 'bid']):
                raise ValueError('价格与本次报价不符')
            market, _ = self.public.get_market(symbol)
            try:
                plan = lighter_plan(market, symbol, side, size, price, slippage_bps, reduce_only)
            except ValueError as exc:
                return {'success': False, 'confirmed': True, 'status': 'rejected',
                        'filled_size': 0, 'error': '提交前订单参数校验失败，未发送订单：' + str(exc)}
            row, new = self.journal.reserve(self.venue, self.account_index, client_order_id, plan)
            if not new:
                return json.loads(row['result']) if row['result'] else unknown('提交意图已存在，禁止重发')
            index = int.from_bytes(hashlib.sha256((str(self.account_index)+':'+client_order_id).encode()).digest()[:6], 'big')
            try:
                if not self.consume_entry_precheck(symbol, size, normalized_side, quote, reduce_only):
                    self.validate_order(symbol, size, reduce_only, side)
                fresh_quote(quote, symbol, size)
            except Exception:
                result = {'success': False, 'confirmed': True, 'status': 'rejected', 'filled_size': 0,
                          'error': '提交前账户检查失败，未发送订单'}
                self.journal.finish(row['seq'], result)
                return result
            with self.account_mutation() as mutation:
                try:
                    if quote.get('submit_deadline_monotonic') is not None:
                        plan = dict(plan, submit_deadline_monotonic=quote['submit_deadline_monotonic'])
                    with self.transport_stage('Lighter 签名发送', symbol):
                        self.bridge.call('submit', plan, index)
                except SubmissionWindowExpired as exc:
                    result = {'success': False, 'confirmed': True, 'status': 'rejected',
                              'filled_size': 0, 'submitted': False, 'error': str(exc)}
                    mutation['confirmed'] = True
                    self.journal.finish(row['seq'], result)
                    return result
                except Exception:
                    pass  # Timeout may have landed; query, never submit again.
                with self.transport_stage('Lighter 成交回执查询', symbol):
                    result = self._reconcile(plan, index)
                mutation['confirmed'] = result.get('confirmed') is True
                self.journal.finish(row['seq'], result)
                return result

    def _reconcile(self, plan, index):
        deadline = time.monotonic() + self.poll_seconds
        while True:
            try:
                order = self.bridge.call('find_order', plan['market_index'], index)
                if order:
                    # Filled quantity/price are authenticated order-history
                    # facts. For our IOC orders, a verified zero taker fee
                    # makes an additional trades GET unnecessary. The same
                    # full identity and limit checks run before returning.
                    receipt = terminal_receipt(order, plan, self.account_index, index, [])
                    if receipt and (receipt['filled_size'] == 0 or getattr(self, 'verified_fee_bps', None) == 0):
                        return {**receipt, 'fees_usdc': 0.0, 'fees_verified': True,
                                'fee_basis': '账户已核验 Taker 0 bps；IOC 订单终态核验' if receipt['filled_size'] else '零成交终态'}
                    if receipt is None:
                        if time.monotonic() >= deadline:
                            return unknown('等待订单终态超时，需核对，未重发')
                        time.sleep(.25)
                        continue
                    trades = self.bridge.call('trades', plan['market_index'], order['order_index'])
                    receipt = terminal_receipt(order, plan, self.account_index, index, trades)
                    if receipt:
                        # The public trades endpoint currently omits the
                        # taker/maker fee fields on some deployments.  A
                        # freshly verified account-level zero-taker-fee proof
                        # is sufficient to account this fill as zero; without
                        # that proof the conservative unknown state remains.
                        if (receipt.get('fees_usdc') is None and
                                receipt.get('fee_fields_observed') is False and
                                getattr(self, 'verified_fee_bps', None) == 0):
                            receipt = {**receipt, 'fees_usdc': 0.0,
                                       'fees_verified': True,
                                       'fee_basis': '当前账户已核验 Taker 0 bps；成交接口未返回费用字段'}
                        return receipt
            except Exception:
                return unknown('订单或费用查询未通过核对')
            if time.monotonic() >= deadline:
                return unknown('等待订单终态超时，需核对，未重发')
            time.sleep(.25)

    def reconcile_order(self, sequence):
        """Read-only recovery by durable intent, including after a process restart."""
        row = self.journal.get(sequence)
        if not row or row['venue'] != self.venue or row['account'] != str(self.account_index):
            raise ValueError('日志订单不属于本账户')
        plan = json.loads(row['intent'])
        index = int.from_bytes(hashlib.sha256((str(self.account_index)+':'+row['client_id']).encode()).digest()[:6], 'big')
        with self.lock:
            result = self._reconcile(plan, index)
            self.journal.finish(sequence, result)
            self.account_reconciled(result.get('confirmed') is True)
        return result

    @account_transaction()
    def cancel_order(self, sequence):
        """Cancel only a known journal-owned order; never cancel-all the account."""
        if not self.live_ready:
            raise ValueError(self.live_blocker)
        row = self.journal.get(sequence)
        if not row or row['venue'] != self.venue or row['account'] != str(self.account_index):
            raise ValueError('无法取消其他账户或未记录的订单')
        if row['state'] == 'terminal':
            return json.loads(row['result'])
        plan = json.loads(row['intent'])
        index = int.from_bytes(hashlib.sha256((str(self.account_index)+':'+row['client_id']).encode()).digest()[:6], 'big')
        with self.lock:
            observed = self.bridge.call('find_order', plan['market_index'], index)
            if not observed:
                return unknown('未找到具体订单，不发送取消')
            # Validates account, market, side, size and reduce-only even if still open.
            existing = terminal_receipt(observed, plan, self.account_index, index, [])
            if existing is None:
                try:
                    self.bridge.call('cancel', plan['market_index'], observed['order_index'])
                except Exception:
                    pass
            result = self._reconcile(plan, index)
            self.journal.finish(sequence, result)
            return result

    def close_position(self, symbol, side, size, client_order_id, reduce_only=True, slippage_bps=10):
        if reduce_only is not True:
            raise ValueError('平仓必须只减仓')
        opposite = 'sell' if direction(side) == 'buy' else 'buy'
        quote = self.get_executable_quote(symbol, size)
        return self.place_order(symbol, opposite, size, quote['ask' if opposite == 'buy' else 'bid'],
                                slippage_bps, client_order_id, quote, reduce_only=True)
