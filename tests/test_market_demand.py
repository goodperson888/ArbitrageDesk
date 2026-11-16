import unittest
from unittest.mock import Mock, patch

from src.web import app as web
from src.web.market_demand import MarketDemand


class MarketDemandTests(unittest.TestCase):
    def test_subscribers_and_short_requests_release_independently(self):
        now = [100.0]
        demand = MarketDemand(clock=lambda: now[0])
        self.assertEqual(demand.active(), ())
        first, second = demand.acquire('BTC'), demand.acquire('BTC')
        demand.touch(['ETH'], seconds=15)
        demand.release(first)
        self.assertEqual(demand.active(), ('BTC','ETH'))
        now[0] = 116
        self.assertEqual(demand.active(), ('BTC',))
        demand.release(second)
        self.assertEqual(demand.active(), ())

    def test_reference_worker_is_idle_then_only_subscribes_requested_market(self):
        stop = Mock(is_set=Mock(side_effect=[False,False,False,True]))
        feed = Mock()
        with patch.object(web,'monitor_stop',stop), patch.object(web,'v_feed',None), \
             patch.object(web,'market_demand',Mock(active=Mock(side_effect=[(),('BTC',),()]))), \
             patch.object(web,'VariationalPriceFeed',return_value=feed) as factory, \
             patch.object(web,'refresh_prices') as refresh:
            web.reference_worker()
        factory.assert_called_once_with(['BTC'],web.proxy_url)
        refresh.assert_called_once_with(('BTC',))
        feed.start.assert_called_once()
        feed.stop.assert_called_once()

    def test_book_worker_does_not_discover_or_poll_without_consumers(self):
        stop = Mock(is_set=Mock(side_effect=[False,True]))
        with patch.object(web,'monitor_stop',stop), patch.object(web,'l_feed',None), \
             patch.object(web,'market_demand',Mock(active=Mock(return_value=()))), \
             patch.object(web,'lighter_trader',Mock()) as trader, \
             patch.object(web,'LighterBookFeed') as factory:
            web.book_worker()
        trader.get_markets.assert_not_called()
        trader.get_order_book.assert_not_called()
        factory.assert_not_called()
