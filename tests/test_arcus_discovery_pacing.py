"""The UI and execution reference fallbacks share one optional REST budget."""
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch

import httpx

from src.api_v2.arcus_adapter import ArcusAdapter
from src.api_v2.models import Instrument
from src.arcus.execution import ArcusClient, ArcusExecutionAdapter
from src.common.rate_limit import DiscoveryThrottleError, RateLimitGate
from src.execution.preparation import readonly_preparation


class ArcusDiscoveryPacingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.client = object.__new__(ArcusClient)
        self.client.base_url, self.client.api_key = 'https://exchange.test', 'test'
        self.client.rate_limit = RateLimitGate(self.client.base_url, temp.name)
        self.client._public_cache = {}
        self.client._public_cache_lock = threading.RLock()
        self.client._public_key_locks = tuple(threading.RLock() for _ in range(32))
        self.client.market = Mock(side_effect=lambda symbol: dict(symbol=symbol+'-USD'))
        self.client.http = Mock()
        payload = dict(bestBid=dict(price='99', size='5'), bestAsk=dict(price='101', size='5'),
                       timestamp=int(datetime.now(timezone.utc).timestamp()*1e6))
        self.response = httpx.Response(200, json=payload,
                                       request=httpx.Request('GET', 'https://exchange.test/bbo'))
        self.client.http.get.return_value = self.response
        self.trader = ArcusExecutionAdapter(self.client, Mock(), 'test', enabled=True, fee_bps=0)
        self.public = ArcusAdapter(base_url='https://exchange.test', proxy_url=None)
        self.addAsyncCleanup(self.public.close)
        self.public.rate_limit = RateLimitGate('https://exchange.test', temp.name)
        self.public._client.get = AsyncMock(return_value=self.response)
        self.public.list_instruments = AsyncMock(return_value=[
            Instrument(venue='arcus', venue_symbol=s+'-USD', canonical_symbol=s+'-PERP',
                       instrument_type='perpetual', base_asset=s, quote_asset='USD') for s in ('BTC', 'SUI')])

    async def test_ui_then_worker_fallback_share_allowance_but_holdings_can_read(self):
        with patch('src.common.rate_limit.time.time', return_value=1000):
            self.assertEqual((await self.public.get_quote('BTC')).bid, 99)
            with self.assertRaises(DiscoveryThrottleError):
                self.trader.get_market_price('SUI')
            self.client.http.get.assert_not_called()
            self.assertEqual(self.trader.get_position_mark_snapshot('SUI')['bid'], 99)
            self.client.http.get.assert_called_once()

    async def test_worker_then_ui_denial_does_not_consume_market_probe_interval(self):
        with patch('src.common.rate_limit.time.time', return_value=1000):
            self.assertEqual(self.trader.get_market_price('BTC'), 100)
            quote = await self.public.get_quote('SUI')
            self.assertIn('共享节流', quote.reason)
            self.assertNotIn('429', quote.reason)
            self.assertNotIn('SUI-PERP', self.public._bbo_rest_at)
            self.public._client.get.assert_not_called()
        with patch('src.common.rate_limit.time.time', return_value=1002.1):
            quote = await self.public.get_quote('SUI')
            self.assertEqual(quote.bid, 99)
            self.public._client.get.assert_awaited_once()

    async def test_fresh_stream_uses_no_rest_even_when_optional_allowance_is_taken(self):
        self.trader._reference_feed = Mock()
        self.trader._reference_feed.snapshot.return_value = dict(bid=99, ask=101)
        with patch('src.common.rate_limit.time.time', return_value=1000):
            self.public.rate_limit.reserve_discovery()
            self.assertEqual(self.trader.get_market_price('BTC'), 100)
        self.client.http.get.assert_not_called()

    async def test_candidate_depth_wins_next_slot_against_public_reference_probe(self):
        with patch('src.common.rate_limit.time.time', return_value=1000):
            await self.public.get_quote('BTC')
        with patch('src.common.rate_limit.time.time', return_value=1001), readonly_preparation():
            with self.assertRaises(DiscoveryThrottleError):
                self.client._get('/v1/l2Orderbook/SUI-USD', discovery=True)
        with patch('src.common.rate_limit.time.time', return_value=1002):
            quote = await self.public.get_quote('SUI')
            self.assertIn('共享节流', quote.reason)
            self.public._client.get.assert_awaited_once()
            with readonly_preparation():
                self.client._get('/v1/l2Orderbook/SUI-USD', discovery=True)
            self.client.http.get.assert_called_once()
