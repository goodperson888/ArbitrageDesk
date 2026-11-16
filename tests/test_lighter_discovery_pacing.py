import unittest
from unittest.mock import Mock

from src.api_v2.adapters import SyncVenueAdapter
from src.api_v2.models import CatalogMode, VenueCapability
from src.common.market_data import utc_now
from src.common.rate_limit import DiscoveryThrottleError


class LighterDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(config={'api': {}})
        self.client.get_markets.return_value = ({'BTC': {'market_id': 1}}, utc_now())
        self.client.get_snapshot.return_value = {'exchange_symbol': 'BTC', 'fetched_at': utc_now()}
        self.adapter = SyncVenueAdapter(self.client, 'lighter', 'Lighter', ['BTC'],
            VenueCapability(market_data=['test'], catalog_mode=CatalogMode.full,
                            executable_quotes=False, order_submission=False))

    async def test_scanner_fallback_is_paced_and_explains_local_wait(self):
        self.client.get_order_book.side_effect = DiscoveryThrottleError(2)
        quote = await self.adapter.get_quote('BTC')
        self.client.get_order_book.assert_called_once_with('BTC', discovery=True)
        self.assertIn('共享节流', quote.reason)
        self.assertNotIn('429', quote.reason)
        self.assertIsNone(quote.bid)
        self.assertIsNone(quote.ask)

    async def test_native_book_remains_available_without_consuming_rest_allowance(self):
        self.adapter._live_feed = Mock()
        self.adapter._live_feed.snapshot.return_value = {'fetched_at': utc_now(),
            'bids': [{'price': 99, 'size': 3}], 'asks': [{'price': 101, 'size': 3}]}
        quote = await self.adapter.get_quote('BTC')
        self.assertEqual((quote.bid, quote.ask), (99, 101))
        self.client.get_snapshot.assert_not_called()
        self.client.get_order_book.assert_not_called()
