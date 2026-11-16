"""Entry latency improvements retain fresh-account and fresh-quote guards."""
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.history import TradeHistory
from src.execution.journal import OrderJournal
from src.execution.precheck import EntryPrecheckMixin
from src.execution.orders import direction
from src.execution.worker import ExecutionWorker
from src.lighter.execution import LighterExecutionAdapter
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = TradeHistory(Path(self.tmp.name) / 'events.sqlite')
        self.adapter = EntryPrecheckMixin()
        self.adapter.lock = threading.RLock()
        self.adapter.validate_order = Mock(return_value={'available_balance': '120'})
        self.quote = {'bid': 99, 'ask': 100}

    def test_account_evidence_is_single_use_and_scoped_to_same_execution(self):
        with self.history.execution_scope('a'):
            self.adapter.precheck_order('BTC', 1, 'buy')
            self.assertTrue(self.adapter.consume_entry_precheck('BTC', 1, 'buy', self.quote))
            self.assertFalse(self.adapter.consume_entry_precheck('BTC', 1, 'buy', self.quote))
            self.adapter.precheck_order('BTC', 1, 'buy')
        with self.history.execution_scope('b'):
            self.assertFalse(self.adapter.consume_entry_precheck('BTC', 1, 'buy', self.quote))

    def test_changed_order_expired_proof_and_insufficient_balance_force_account_read(self):
        for symbol, size, side, quote, reduce in (
                ('ETH', 1, 'buy', self.quote, False),
                ('BTC', .5, 'buy', self.quote, False),
                ('BTC', 1, 'sell', self.quote, False),
                ('BTC', 1, 'buy', {'bid': 120, 'ask': 121}, False),
                ('BTC', 1, 'buy', self.quote, True)):
            with self.subTest(symbol=symbol, size=size, side=side, reduce=reduce):
                with self.history.execution_scope('a'):
                    self.adapter.precheck_order('BTC', 1, 'buy')
                    self.assertFalse(self.adapter.consume_entry_precheck(symbol, size, side, quote, reduce))
        with self.history.execution_scope('a'):
            self.adapter.precheck_order('BTC', 1, 'buy')
            self.adapter.precheck_max_age = -1
            self.assertFalse(self.adapter.consume_entry_precheck('BTC', 1, 'buy', self.quote))

    def test_failed_account_check_cannot_leave_an_earlier_proof(self):
        with self.history.execution_scope('a'):
            self.adapter.precheck_order('BTC', 1, 'buy')
            self.adapter.validate_order.side_effect = ValueError('new exposure')
            with self.assertRaises(ValueError):
                self.adapter.precheck_order('BTC', 1, 'buy')
            self.assertFalse(self.adapter.consume_entry_precheck('BTC', 1, 'buy', self.quote))

    def test_final_quote_still_has_to_meet_checked_minimum_notional(self):
        self.adapter.validate_order.return_value = {'available_balance': '120', 'min_quote_amount': '90'}
        with self.history.execution_scope('a'):
            self.adapter.precheck_order('BTC', 1, 'buy')
            self.assertFalse(self.adapter.consume_entry_precheck('BTC', 1, 'buy', {'bid': 89, 'ask': 90}))

    def test_transport_events_separate_start_completion_and_keep_execution_id(self):
        self.adapter.execution_history = self.history
        with self.history.execution_scope('a'):
            with self.adapter.transport_stage('Lighter 签名发送', 'BTC'):
                pass
        events = list(reversed(self.history.recent()))
        self.assertEqual([e['event'] for e in events], ['stage_started', 'stage_completed'])
        self.assertTrue(all(e['detail']['execution_id'] == 'a' for e in events))


class DiscoveryTests(unittest.TestCase):
    def test_slow_market_does_not_hold_fast_candidates_or_duplicate_pending_reads(self):
        release = threading.Event()
        calls = []
        def check(symbol):
            calls.append(symbol)
            if symbol == 'SLOW':
                release.wait(2)
            return SimpleNamespace(symbol=symbol, spread_bps=10)
        strategy = SimpleNamespace(parallel_discovery=True, check_arbitrage_opportunity=check,
                                   spread_threshold_min=0, spread_threshold_max=100)
        with tempfile.TemporaryDirectory() as tmp:
            worker = ExecutionWorker(strategy, ['SLOW', 'FAST', 'FAST2'], kill_path=Path(tmp) / 'STOP')
            worker.discovery_budget = .08
            try:
                with worker._discovery_session():
                    first = worker._candidate_opportunities()
                    self.assertEqual({p.symbol for p in first}, {'FAST', 'FAST2'})
                    self.assertFalse(release.is_set())
                    worker._candidate_opportunities()
                    self.assertEqual(calls.count('SLOW'), 1)
                    self.assertLessEqual(len(worker._discovery_pending), 4)
            finally:
                release.set()

    def test_late_discovery_result_is_discarded_and_stop_returns_without_waiting(self):
        release = threading.Event()
        def check(symbol):
            release.wait(2)
            return SimpleNamespace(symbol=symbol, spread_bps=10)
        strategy = SimpleNamespace(parallel_discovery=True, check_arbitrage_opportunity=check,
                                   spread_threshold_min=0, spread_threshold_max=100)
        with tempfile.TemporaryDirectory() as tmp:
            worker = ExecutionWorker(strategy, ['SLOW'], kill_path=Path(tmp) / 'STOP')
            worker.discovery_budget = .03
            worker.discovery_max_age = .01
            try:
                with worker._discovery_session():
                    self.assertEqual(worker._candidate_opportunities(), [])
                    release.set()
                    next(iter(worker._discovery_pending.values()))[0].result(timeout=1)
                    self.assertEqual(worker._candidate_opportunities(), [])
                    worker.stop()
                    self.assertEqual(worker._candidate_opportunities(), [])
            finally:
                release.set()


class NativeTrader(EntryPrecheckMixin):
    live_ready = True
    def __init__(self, price, venue):
        self.price, self.venue = price, venue
        self.lock = threading.RLock()
        self.reads, self.sends, self.quote_reads = 0, [], 0
    def size_decimals(self, symbol): return 3
    def get_market_price(self, symbol): return self.price
    def get_sizing_book(self, symbol):
        return {'fetched_at': utc_now(), 'bids': [{'price': self.price, 'size': 100}],
                'asks': [{'price': self.price, 'size': 100}]}
    def get_executable_quote(self, symbol, quantity):
        self.quote_reads += 1
        return dict(symbol=symbol, size=quantity, bid=self.price, ask=self.price,
                    bid_vwap=self.price, ask_vwap=self.price, updated_at=utc_now(),
                    executable=True, instrument_type='perpetual', fee_bps=0)
    def validate_order(self, symbol, quantity, reduce_only=False, side=None):
        self.reads += 1
        return {'available_balance': '10000'}
    def validate_quote_order(self, *args): return True
    def place_order(self, symbol, side, size, price, slippage_bps, client_order_id, quote):
        if not self.consume_entry_precheck(symbol, size, direction(side), quote):
            self.validate_order(symbol, size, side=side)
        self.sends.append((symbol, side, size, price))
        return dict(success=True, confirmed=True, status='filled', filled_size=size,
                    average_price=price, order_id=client_order_id, fees_usdc=0, funding_usdc=0)


class FinalQuoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first, self.second = NativeTrader(102, 'lighter'), NativeTrader(100, 'arcus')
        config = {'mode': 'live', 'state_path': str(Path(self.tmp.name) / 'state.json'),
                  'trading': {'position_size': 102.02, 'max_positions': 1},
                  'spread_threshold': {'min': 1, 'max': 500}, 'risk': {'slippage_tolerance': 1},
                  'wash_trading': {'trade_interval_min': 0, 'max_trades_per_day': 10}}
        self.strategy = ArbitrageStrategy(config, self.first, self.second)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.strategy.market_history)
        self.opportunity = ArbitrageOpportunity('BTC', 102, 100, 198,
                                               'variational_higher', 2, datetime.now())

    def test_account_precheck_then_final_quote_retains_one_read_per_account(self):
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual((self.first.reads, self.second.reads), (1, 1))
        self.assertGreaterEqual(self.first.quote_reads, 2)
        stages = [e['detail'].get('stage') for e in self.strategy.history.recent()]
        self.assertIn('提交前最终报价', stages)

    def test_arcus_band_failure_blocks_before_either_leg_is_sent(self):
        from src.arcus.execution import ArcusExecutionAdapter
        client = Mock(api_key='verified')
        client.market.return_value = dict(tick_size='.01',
            trading_bounds={'lower':'90','upper':'100'})
        arcus = ArcusExecutionAdapter(client,None,'test',enabled=True,fee_bps=0)
        self.second.validate_quote_order = arcus.validate_quote_order
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.first.sends + self.second.sends, [])
        self.assertEqual(self.strategy.positions, [])
        client.submit.assert_not_called()

    def test_opportunity_disappearing_during_account_checks_never_submits(self):
        check = self.first.precheck_order
        def changed(*args):
            check(*args)
            self.first.price = 99
        with patch.object(self.first, 'precheck_order', side_effect=changed):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.first.sends + self.second.sends, [])
        self.assertEqual(self.strategy.positions, [])

    def test_final_quote_cannot_silently_change_prechecked_size(self):
        check = self.first.precheck_order
        def changed(*args):
            check(*args)
            self.first.price = 104
        with patch.object(self.first, 'precheck_order', side_effect=changed):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.first.sends + self.second.sends, [])

    def test_verify_only_does_not_wait_for_informational_leverage_query(self):
        self.strategy.target_leverage = 5
        with patch.object(self.strategy, '_ensure_common_leverage',
                          side_effect=AssertionError('not on entry path')) as leverage:
            self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        leverage.assert_not_called()
        self.assertEqual((self.first.reads, self.second.reads), (1, 1))

    def test_explicit_common_leverage_is_still_checked(self):
        self.strategy.target_leverage = 5
        self.strategy.leverage_mode = 'enforce_common'
        with patch.object(self.strategy, '_ensure_common_leverage',
                          side_effect=ValueError('统一杠杆未确认')) as leverage:
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        leverage.assert_called_once_with('BTC')
        self.assertEqual(self.first.sends + self.second.sends, [])


class LighterFinalAccountGuardTests(unittest.TestCase):
    def test_quote_expiring_in_last_account_read_is_rejected_before_send(self):
        public, bridge = Mock(), Mock()
        public.get_market.return_value = (dict(symbol='BTC', market_type='perp', status='active',
            market_id=1, supported_size_decimals=3,
            supported_price_decimals=2, min_base_amount='.001', min_quote_amount='1'), utc_now())
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LighterExecutionAdapter(public, bridge, OrderJournal(Path(tmp) / 'orders.sqlite'),
                                              7, enabled=True, poll_seconds=0)
            adapter.verified_fee_bps = 0
            quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                         executable=True, instrument_type='perpetual')
            with patch.object(adapter, 'validate_order') as validate, \
                    patch('src.execution.orders.age_seconds', side_effect=[0, 6]):
                result = adapter.place_order('BTC', 'buy', 1, 100, 0, 'id-1', quote)
            self.assertEqual(result['status'], 'rejected')
            validate.assert_called_once()
            bridge.call.assert_not_called()


class NativeBookTests(unittest.TestCase):
    def setUp(self):
        self.public = Mock(config={'api': {'ws_url': 'wss://api.rh.lighter.xyz/stream', 'proxy_url': None}})
        self.adapter = LighterExecutionAdapter(self.public, Mock(), None, 7, enabled=True, venue='lighter_rh')
        self.adapter.verified_fee_bps = 0
        self.book = {'symbol': 'BTC', 'market_id': 1, 'fetched_at': utc_now(),
                     'bids': [{'price': 99, 'size': 3}], 'asks': [{'price': 101, 'size': 3}]}

    def test_fresh_native_book_avoids_rest_and_preserves_source_timestamp(self):
        self.adapter._reference_feed = Mock()
        self.adapter._reference_feed.snapshot.return_value = self.book
        self.assertEqual(self.adapter.get_market_price('BTC'), 100)
        self.assertIs(self.adapter.get_sizing_book('BTC'), self.book)
        self.assertEqual(self.adapter.get_executable_quote('BTC', 1)['updated_at'], self.book['fetched_at'])
        self.public.get_order_book.assert_not_called()
        self.public.get_market_price.assert_not_called()
        with self.assertRaisesRegex(ValueError, '深度不足'):
            self.adapter.get_executable_quote('BTC', 4)

    def test_position_read_bypasses_discovery_allowance_and_rejects_stale_rest(self):
        self.public.get_order_book.return_value = self.book
        quote = self.adapter.get_position_mark_snapshot('BTC')
        self.public.get_order_book.assert_called_once_with('BTC')
        self.assertEqual((quote['bid'], quote['ask']), (99, 101))
        self.public.get_order_book.return_value = {**self.book, 'fetched_at': '2000-01-01T00:00:00Z'}
        with self.assertRaisesRegex(ValueError, '过期'):
            self.adapter.get_position_mark_snapshot('BTC')

    def test_expired_missing_and_empty_native_book_fall_back_to_rest(self):
        self.adapter._reference_feed = Mock()
        for book, age in ((self.book, 11), (None, 0), ({**self.book, 'asks': []}, 0)):
            with self.subTest(book=book, age=age):
                self.adapter._reference_feed.snapshot.return_value = book
                with patch('src.lighter.execution.age_seconds', return_value=age):
                    self.assertIs(self.adapter.get_sizing_book('BTC'), self.public.get_order_book.return_value)
                    self.assertIs(self.adapter.get_market_price('BTC'), self.public.get_market_price.return_value)

    def test_three_second_book_is_discovery_only_and_sized_execution_still_refreshes(self):
        self.adapter._reference_feed = Mock()
        self.adapter._reference_feed.snapshot.return_value = self.book
        with patch('src.lighter.execution.age_seconds', return_value=3):
            self.assertEqual(self.adapter.get_market_price('BTC'), 100)
            self.assertIs(self.adapter.get_sizing_book('BTC'), self.public.get_order_book.return_value)
        self.public.get_market_price.assert_not_called()

    def test_start_uses_selected_deployment_and_symbols_and_close_stops_feed(self):
        self.public.get_markets.return_value = ({'BTC': {'market_id': 1}, 'ETH': {'market_id': 2}}, utc_now())
        with patch('src.common.live_feed.LighterBookFeed') as factory:
            self.adapter.start_reference_feed(['BTC'])
            self.adapter.start_reference_feed(['BTC'])
        factory.assert_called_once_with({'BTC': 1}, None, ws_url='wss://api.rh.lighter.xyz/stream')
        factory.return_value.start.assert_called_once()
        self.adapter.close_reference_feed()
        factory.return_value.stop.assert_called_once()
        self.assertIsNone(self.adapter._reference_feed)

    def test_start_during_cooldown_recovers_once_without_retrying_each_symbol(self):
        from src.common.rate_limit import RateLimitError
        self.public.get_markets.side_effect = [RateLimitError(93),
            ({'BTC': {'market_id': 1}}, utc_now())]
        with patch('src.lighter.execution.time.monotonic', return_value=100) as now, \
                patch('src.common.live_feed.LighterBookFeed') as factory:
            factory.return_value.snapshot.return_value = self.book
            self.adapter.start_reference_feed(['BTC'])
            self.assertIsNone(self.adapter._reference_feed)
            now.return_value = 192
            for _ in range(5):
                self.adapter._fresh_reference_book('BTC')
            self.assertEqual(self.public.get_markets.call_count, 1)
            now.return_value = 193
            self.assertIs(self.adapter._fresh_reference_book('BTC'), self.book)
            self.adapter._fresh_reference_book('BTC')
            self.assertEqual(self.public.get_markets.call_count, 2)
            factory.return_value.start.assert_called_once()
            self.adapter.close_reference_feed()
            now.return_value = 1000
            self.adapter._fresh_reference_book('BTC')
            self.assertEqual(self.public.get_markets.call_count, 2)
            factory.return_value.stop.assert_called_once()

    def test_other_startup_failures_are_throttled_and_discovery_only_is_paced(self):
        self.public.get_markets.side_effect = ConnectionError('offline')
        with patch('src.lighter.execution.time.monotonic', return_value=100):
            self.adapter.start_reference_feed(['BTC'])
            for _ in range(5):
                self.adapter.get_market_price('BTC')
            self.public.get_markets.assert_called_once()
        self.public.get_market_price.assert_called_with('BTC', discovery=True)
        self.adapter.get_sizing_book('BTC')
        self.public.get_order_book.assert_called_with('BTC')

    def test_factory_passes_each_deployments_own_websocket_url(self):
        from src.execution.platform_clients import build_lighter_session, LighterCredentials
        with patch('src.execution.platform_clients.SDKLoop'), \
                patch('src.execution.platform_clients.LighterApiTrader') as public:
            for venue, url in (('lighter', 'wss://mainnet.zklighter.elliot.ai/stream'),
                               ('lighter_rh', 'wss://api.rh.lighter.xyz/stream')):
                build_lighter_session(venue, LighterCredentials(1, 2, 'test'))
                self.assertEqual(public.call_args.args[0]['api']['ws_url'], url)


class AdapterPrecheckIntegrationTests(unittest.TestCase):
    def test_lighter_actual_submit_consumes_one_precheck_with_normalized_short_side(self):
        public, bridge = Mock(), Mock()
        public.get_market.return_value = (dict(symbol='BTC', market_type='perp', status='active',
            market_id=1, supported_size_decimals=3,
            supported_price_decimals=2, min_base_amount='.001', min_quote_amount='1'), utc_now())
        with tempfile.TemporaryDirectory() as tmp:
            history = TradeHistory(Path(tmp) / 'events.sqlite')
            adapter = LighterExecutionAdapter(public, bridge, OrderJournal(Path(tmp) / 'orders.sqlite'),
                                              7, enabled=True)
            adapter.verified_fee_bps = 0
            adapter.validate_order = Mock(return_value={'available_balance': '120'})
            quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                         executable=True, instrument_type='perpetual')
            with history.execution_scope('one'), patch.object(adapter, '_reconcile',
                    return_value={'confirmed': True, 'filled_size': 0, 'status': 'canceled'}):
                adapter.precheck_order('BTC', 1, 'sell')
                result = adapter.place_order('BTC', 'short', 1, 99, 0, 'one', quote)
            self.assertEqual(result['status'], 'canceled')
            adapter.validate_order.assert_called_once_with('BTC', 1, side='sell')
            self.assertEqual(bridge.call.call_args.args[0], 'submit')

    def test_arcus_actual_submit_consumes_one_precheck_with_normalized_long_side(self):
        from src.arcus.execution import ArcusExecutionAdapter
        client = Mock(api_key='test')
        client.market.return_value = {'tick_size': '0.01'}
        client.submit.return_value = {'accepted': False, 'error': 'test rejection'}
        with tempfile.TemporaryDirectory() as tmp:
            history = TradeHistory(Path(tmp) / 'events.sqlite')
            adapter = ArcusExecutionAdapter(client, OrderJournal(Path(tmp) / 'orders.sqlite'),
                                            'test', enabled=True, fee_bps=0)
            adapter.validate_order = Mock(return_value={'available_balance': '120'})
            quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                         executable=True, instrument_type='perpetual')
            with history.execution_scope('one'):
                adapter.precheck_order('BTC', 1, 'buy')
                result = adapter.place_order('BTC', 'long', 1, 100, 0, 'one', quote)
            self.assertEqual(result['status'], 'rejected')
            adapter.validate_order.assert_called_once_with('BTC', 1, side='buy')
            client.submit.assert_called_once()
