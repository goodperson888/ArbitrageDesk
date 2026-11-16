"""Offline regression tests: optional discovery never gates position/exit reads."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.common.discovery import DiscoveryCooldown, DiscoveryCooldownError, EmptyBookError
from src.common.live_feed import ArcusBboFeed, LighterBookFeed
from src.common.market_data import MarketDataError, utc_now
from src.common.rate_limit import DiscoveryThrottleError, RateLimitError
from src.execution.telemetry import safe_reason
from src.arcus.execution import ArcusExecutionAdapter
from src.lighter.execution import LighterExecutionAdapter
from src.lighter.api_trader import LighterApiTrader
from src.api_v2.arcus_adapter import ArcusAdapter
from src.api_v2.adapters import SyncVenueAdapter
from src.api_v2.models import CatalogMode, Instrument, VenueCapability


def unavailable(symbol):
    return {'type': 'error', 'message': f"Market '{symbol}-USD' is not available"}


def bbo(symbol):
    return {'channel': 'bbo', 'id': symbol + '-USD', 'contents': {
        'bestBid': {'price': '99', 'size': '10'}, 'bestAsk': {'price': '101', 'size': '10'},
        'timestamp': int(datetime.now(timezone.utc).timestamp() * 1e6)}}


def lighter_feed(empty=True, age=0):
    feed = LighterBookFeed({'BOT': 1})
    feed.connected = True
    feed.on_message({'type': 'subscribed/order_book', 'channel': 'order_book:1', 'order_book': {
        'offset': 10, 'nonce': 10, 'bids': [] if empty else [{'price': '99', 'size': '10'}],
        'asks': [] if empty else [{'price': '101', 'size': '10'}]}})
    feed.books['BOT']['received_at'] = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    return feed


def status_error(status):
    response = httpx.Response(status, request=httpx.Request('GET', 'https://exchange.test/bbo'))
    return httpx.HTTPStatusError('private response must stay hidden', request=response.request, response=response)


class DiscoveryBackoffTests(unittest.TestCase):
    def test_bounded_exponential_probes_are_per_symbol_and_never_sleep(self):
        gate = DiscoveryCooldown('Arcus')
        now = 100
        for delay in (30, 60, 120, 240, 300, 300):
            with patch('src.common.discovery.time.monotonic', return_value=now):
                gate.failed('AVAX', 'unavailable')
                with self.assertRaises(DiscoveryCooldownError) as error:
                    gate.check('AVAX')
                self.assertEqual(error.exception.wait_seconds, delay)
                self.assertNotIn('429', safe_reason(error.exception))
                gate.check('SUI')
            now += delay
            with patch('src.common.discovery.time.monotonic', return_value=now):
                gate.check('AVAX')
        gate.fresh_stream('AVAX')
        gate.check('AVAX')

    def test_same_old_ws_error_cannot_rearm_after_successful_rest_probe(self):
        gate = DiscoveryCooldown('Arcus')
        with patch('src.common.discovery.time.monotonic', return_value=100):
            gate.observe('AVAX', 'unavailable')
        with patch('src.common.discovery.time.monotonic', return_value=130):
            gate.observe('AVAX', 'unavailable')
            gate.check('AVAX')
            gate.recovered('AVAX')
            gate.observe('AVAX', 'unavailable')
            gate.check('AVAX')
            gate.fresh_stream('AVAX')
            gate.observe('AVAX', 'unavailable')
            with self.assertRaises(DiscoveryCooldownError):
                gate.check('AVAX')

    def test_arcus_unavailable_error_invalidates_only_named_market_and_fresh_ws_recovers(self):
        feed = ArcusBboFeed({'AVAX': 'AVAX-USD', 'SUI': 'SUI-USD'})
        feed.connected = True
        feed.on_message(bbo('AVAX'))
        feed.on_message(bbo('SUI'))
        feed.on_message(unavailable('AVAX'))
        self.assertIsNone(feed.snapshot('AVAX'))
        self.assertEqual(feed.discovery_issue('AVAX'), 'unavailable')
        self.assertIsNotNone(feed.snapshot('SUI'))
        feed.on_message(unavailable('UNKNOWN'))
        feed.on_message(bbo('AVAX'))
        self.assertIsNone(feed.discovery_issue('AVAX'))
        self.assertIsNotNone(feed.snapshot('AVAX'))
        feed.reset()
        self.assertIsNone(feed.discovery_issue('AVAX'))

    def test_arcus_404_is_not_retried_each_scan_and_does_not_gate_holdings_or_depth(self):
        client = Mock(api_key='test')
        adapter = ArcusExecutionAdapter(client, Mock(), 'test', enabled=True, fee_bps=0)
        client.bbo.side_effect = status_error(404)
        with patch('src.common.discovery.time.monotonic', return_value=100):
            for _ in range(5):
                with self.assertRaises(DiscoveryCooldownError):
                    adapter.get_market_price('AVAX')
            self.assertEqual(client.bbo.call_count, 1)
            client.bbo.side_effect = None
            client.bbo.return_value = {'bid': 99, 'ask': 101, 'updated_at': utc_now()}
            self.assertEqual(adapter.get_market_price('SUI'), 100)
            self.assertEqual(adapter.get_position_mark_snapshot('AVAX')['bid'], 99)
            adapter.get_sizing_book('AVAX')
            client.orderbook.assert_called_once_with('AVAX')
        with patch('src.common.discovery.time.monotonic', return_value=130):
            self.assertEqual(adapter.get_market_price('AVAX'), 100)
        client.submit.assert_not_called()

    def test_arcus_ws_unavailable_suppresses_rest_but_recovery_bypasses_cooldown(self):
        client = Mock(api_key='test')
        adapter = ArcusExecutionAdapter(client, Mock(), 'test', enabled=True, fee_bps=0)
        feed = ArcusBboFeed({'AVAX': 'AVAX-USD'})
        adapter._reference_feed = feed
        feed.connected = True
        feed.on_message(unavailable('AVAX'))
        for _ in range(5):
            with self.assertRaises(DiscoveryCooldownError):
                adapter.get_market_price('AVAX')
        client.bbo.assert_not_called()
        feed.on_message(bbo('AVAX'))
        self.assertEqual(adapter.get_market_price('AVAX'), 100)
        client.bbo.assert_not_called()

    def test_lighter_empty_ws_survives_snapshot_expiry_without_repeated_rest(self):
        public = Mock(config={'api': {}})
        public.get_market_price.side_effect = EmptyBookError('empty')
        adapter = LighterExecutionAdapter(public, Mock(), Mock(), 7)
        adapter._reference_feed = lighter_feed(age=20)
        with patch('src.common.discovery.time.monotonic', return_value=100):
            for _ in range(5):
                with self.assertRaises(DiscoveryCooldownError):
                    adapter.get_market_price('BOT')
            public.get_market_price.assert_not_called()
        for now, calls in ((130, 1), (159, 1), (160, 2), (219, 2)):
            with patch('src.common.discovery.time.monotonic', return_value=now):
                with self.assertRaises(DiscoveryCooldownError):
                    adapter.get_market_price('BOT')
            self.assertEqual(public.get_market_price.call_count, calls)
        adapter._reference_feed = lighter_feed(empty=False)
        self.assertEqual(adapter.get_market_price('BOT'), 100)
        self.assertEqual(public.get_market_price.call_count, 2)

    def test_lighter_slow_ws_discovery_does_not_relax_position_or_execution_freshness(self):
        public = Mock(config={'api': {}})
        adapter = LighterExecutionAdapter(public, Mock(), Mock(), 7)
        adapter._reference_feed = lighter_feed(empty=False, age=8)
        self.assertEqual(adapter.get_market_price('BOT'), 100)
        public.get_market_price.assert_not_called()
        fresh = lighter_feed(empty=False).snapshot('BOT')
        public.get_order_book.return_value = fresh
        self.assertEqual(adapter.get_position_mark_snapshot('BOT')['bid'], 99)
        public.get_order_book.assert_called_once_with('BOT')
        old = lighter_feed(empty=False, age=8).snapshot('BOT')
        public.get_order_book.return_value = old
        with self.assertRaisesRegex(ValueError, '盘口过期'):
            adapter.get_executable_quote('BOT', 1)
        adapter._discovery_cooldown.failed('BOT', 'empty')
        public.get_order_book.return_value = fresh
        self.assertEqual(adapter.get_position_mark_snapshot('BOT')['ask'], 101)
        adapter.bridge.submit.assert_not_called()

    def test_lighter_rest_errors_preserve_actual_cause_instead_of_becoming_empty_book(self):
        public = LighterApiTrader(session=Mock())
        public.get_snapshot = Mock(return_value={})
        public.get_order_book = Mock()
        for error in (DiscoveryThrottleError(2), RateLimitError(30), MarketDataError('行情请求失败: HTTP 405')):
            public.get_order_book.side_effect = error
            with self.assertRaises(type(error)) as actual:
                public.get_market_price('BOT', discovery=True)
            self.assertIs(actual.exception, error)
        public.get_order_book.side_effect = None
        public.get_order_book.return_value = {'bids': [], 'asks': []}
        with self.assertRaises(EmptyBookError):
            public.get_market_price('BOT', discovery=True)


class PublicDiscoveryBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def arcus(self):
        adapter = ArcusAdapter()
        self.addAsyncCleanup(adapter.close)
        instruments = [Instrument(venue='arcus', venue_symbol=s + '-USD', canonical_symbol=s + '-PERP',
            instrument_type='perpetual', base_asset=s, quote_asset='USD') for s in ('AVAX', 'SUI')]
        adapter.list_instruments = AsyncMock(return_value=instruments)
        adapter._market_by_id = {str(i): row for i, row in enumerate(instruments)}
        return adapter

    async def test_arcus_public_404_backs_off_and_healthy_symbol_and_ws_recovery_continue(self):
        adapter = await self.arcus()
        adapter._get = AsyncMock(side_effect=status_error(404))
        for _ in range(5):
            quote = await adapter.get_quote('AVAX')
            self.assertIsNone(quote.bid)
            self.assertTrue(quote.stale)
            self.assertIn('探测冷却', quote.reason)
        self.assertEqual(adapter._get.await_count, 1)
        adapter._get.side_effect = None
        adapter._get.return_value = bbo('SUI')['contents']
        self.assertEqual((await adapter.get_quote('SUI')).bid, 99)
        adapter._handle_ws_message(bbo('AVAX'))
        self.assertEqual((await adapter.get_quote('AVAX')).source, 'arcus_websocket')
        self.assertEqual(adapter._get.await_count, 2)

    async def test_arcus_public_ws_rejection_does_not_spend_a_rest_request(self):
        adapter = await self.arcus()
        adapter._get = AsyncMock()
        adapter._handle_ws_message(unavailable('AVAX'))
        quote = await adapter.get_quote('AVAX')
        self.assertTrue(quote.stale)
        self.assertIn('市场暂不可用', quote.reason)
        adapter._get.assert_not_awaited()

    async def test_arcus_catalog_rate_limit_preserves_platform_cooldown_message(self):
        adapter = await self.arcus()
        adapter.list_instruments.side_effect = RateLimitError(93)
        quote = await adapter.get_quote('AVAX')
        self.assertTrue(quote.stale)
        self.assertIn('HTTP 429', quote.reason)
        self.assertIn('93 秒', quote.reason)

    async def test_arcus_no_first_bbo_obeys_15_second_transient_retry_interval(self):
        adapter = await self.arcus()
        adapter._get = AsyncMock(side_effect=status_error(503))
        with patch('src.api_v2.arcus_adapter.time.monotonic', return_value=100):
            for _ in range(4):
                await adapter.get_quote('AVAX')
        self.assertEqual(adapter._get.await_count, 1)
        with patch('src.api_v2.arcus_adapter.time.monotonic', return_value=115):
            await adapter.get_quote('AVAX')
        self.assertEqual(adapter._get.await_count, 2)

    async def test_lighter_public_empty_ws_and_rest_cannot_appear_as_fresh_quotes(self):
        client = Mock(config={'api': {}})
        client.get_snapshot.return_value = {'exchange_symbol': 'BOT', 'fetched_at': utc_now()}
        adapter = SyncVenueAdapter(client, 'lighter', 'Lighter', ['BOT'], VenueCapability(
            market_data=['test'], catalog_mode=CatalogMode.full, executable_quotes=False, order_submission=False))
        adapter._live_feed = lighter_feed(age=20)
        for _ in range(4):
            quote = await adapter.get_quote('BOT')
            self.assertTrue(quote.stale)
            self.assertIsNone(quote.bid)
            self.assertIn('盘口为空', quote.reason)
        client.get_order_book.assert_not_called()
        client.get_snapshot.assert_not_called()
        adapter._live_feed = lighter_feed(empty=False)
        # A fresh WS snapshot wins immediately over the cached failure.
        self.assertEqual((await adapter.get_quote('BOT')).bid, 99)
        client.get_order_book.assert_not_called()
        adapter._live_feed = None
        client.get_order_book.return_value = {'bids': [], 'asks': [], 'fetched_at': utc_now()}
        # Test a second symbol so the prior cached response cannot mask REST emptiness.
        quote = await adapter.get_quote('BE')
        self.assertTrue(quote.stale)
        self.assertIn('盘口为空', quote.reason)
