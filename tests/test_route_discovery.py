"""A third venue's catalog/failure cannot erase healthy pairs or change the active route."""
import threading
import unittest
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusExecutionAdapter
from src.common.market_data import utc_now
import test_cached_discovery as cached_tests
from test_execution_latency import NativeTrader


class MultiRouteDiscoveryTests(unittest.TestCase):
    setUp = cached_tests.CachedStrategyTests.setUp

    def routes(self):
        self.b.venue = 'lighter_rh'
        self.c = NativeTrader(102, 'arcus')
        self.c.cached_reference_quote = Mock(return_value=dict(
            bid=102, ask=102, fee_bps=0, bid_size=100, ask_size=100, updated_at=utc_now()))
        for trader in (self.a, self.b, self.c):
            trader.get_market_price = Mock(return_value=trader.price)
        self.s.configure_routes([(self.a, self.b), (self.a, self.c), (self.b, self.c)])

    def test_missing_arcus_market_is_not_read_and_does_not_discard_lighter_pair(self):
        self.routes()
        self.c.discovery_supported_symbols = frozenset({'BTC'})
        probe = self.s.probe_reference_prices('TRX')
        self.assertEqual([row[0] for row in probe['routes']], [0])
        self.c.get_market_price.assert_not_called()
        result = self.s.complete_reference_probe(probe)
        self.assertIsNotNone(result)
        self.assertEqual(result.route_index, 0)
        candidates, missing = self.s.cached_discovery_opportunities(('TRX',))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(missing, ())
        self.c.cached_reference_quote.assert_not_called()

    def test_reads_each_venue_once_and_never_changes_foreground_pair_in_background(self):
        self.routes()
        self.s.activate_route(1)
        self.s.activate_route = Mock(wraps=self.s.activate_route)
        result = []
        thread = threading.Thread(target=lambda: result.append(self.s.probe_reference_prices('BTC')))
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.s.activate_route.assert_not_called()
        self.assertEqual(self.s._active_route_index, 1)
        self.assertIs(self.s.variational_trader, self.a)
        self.assertIs(self.s.lighter_trader, self.c)
        self.assertEqual(len(result[0]['routes']), 3)
        for trader in (self.a, self.b, self.c):
            trader.get_market_price.assert_called_once_with('BTC')
        self.assertIsNotNone(self.s.complete_reference_probe(result[0]))
        self.assertEqual(self.s._active_route_index, 1)

    def test_failed_venue_leaves_other_pair_usable(self):
        self.routes()
        self.c.get_market_price.side_effect = ValueError('Arcus BBO 缺失')
        probe = self.s.probe_reference_prices('BTC')
        self.assertEqual(len(probe['routes']), 1)
        self.assertIsNotNone(self.s.complete_reference_probe(probe))
        self.b.get_market_price.side_effect = ValueError('missing')
        with self.assertRaises(ValueError):
            self.s.probe_reference_prices('BTC')

    def test_missing_third_feed_gets_probe_even_when_first_pair_has_fresh_quotes(self):
        self.routes()
        self.c.cached_reference_quote.return_value = None
        _, missing = self.s.cached_discovery_opportunities(('BTC',))
        self.assertEqual(missing, ('BTC',))

    def test_unknown_catalog_remains_recoverable_but_halt_does_not_read(self):
        self.routes()
        self.assertEqual(len(self.s.probe_reference_prices('BTC')['routes']), 3)
        self.s.halted_reason = 'requires reconciliation'
        for trader in (self.a, self.b, self.c):
            trader.get_market_price.reset_mock()
        self.assertIsNone(self.s.check_arbitrage_opportunity('BTC'))
        for trader in (self.a, self.b, self.c):
            trader.get_market_price.assert_not_called()


class ArcusFeedScopeTests(unittest.TestCase):
    def adapter(self):
        client = Mock(proxy_url=None, ws_url='wss://exchange.test/ws')
        client.list_markets.return_value = {
            'BTC': dict(symbol='BTC-USD', status='active'),
            'OLD': dict(symbol='OLD-USD', status='closed')}
        return ArcusExecutionAdapter(client, Mock(), 'test')

    def test_union_scope_subscribes_supported_markets_and_ignores_missing_or_closed(self):
        adapter = self.adapter()
        with patch('src.common.live_feed.ArcusFeedGroup') as factory:
            adapter.start_reference_feed(['TRX', 'BTC', 'BMNR', 'OLD'])
            adapter.start_reference_feed(['TRX', 'BTC', 'BMNR', 'OLD'])
        factory.assert_called_once_with({'BTC': 'BTC-USD'}, None, 'wss://exchange.test/ws')
        self.assertEqual(adapter.discovery_supported_symbols, frozenset({'BTC'}))
        adapter.client.market.assert_not_called()
        adapter.close_reference_feed()
        factory.return_value.stop.assert_called_once()

    def test_transient_catalog_failure_retries_after_delay_and_recovers_without_restart(self):
        adapter = self.adapter()
        adapter.client.list_markets.side_effect = [ConnectionError('offline'),
                                                  {'BTC': dict(symbol='BTC-USD', status='active')}]
        with patch('src.arcus.execution.time.monotonic', return_value=100) as now, \
                patch('src.common.live_feed.ArcusFeedGroup') as factory:
            factory.return_value.snapshot.return_value = dict(bid=99, ask=101)
            adapter.start_reference_feed(['BTC', 'TRX'])
            self.assertIsNone(adapter._reference_feed)
            adapter.client.bbo.return_value = dict(bid=99, ask=101)
            adapter.get_market_price('BTC')
            adapter.client.list_markets.assert_called_once()
            now.return_value = 130
            self.assertEqual(adapter.get_market_price('BTC'), 100)
            self.assertEqual(adapter.client.list_markets.call_count, 2)
            factory.return_value.start.assert_called_once()
            adapter.close_reference_feed()
            now.return_value = 200
            adapter._ensure_reference_feed()
            self.assertEqual(adapter.client.list_markets.call_count, 2)
