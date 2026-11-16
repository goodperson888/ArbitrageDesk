"""Stream coverage is scanned locally; slow probes cannot delay ready edges."""
import tempfile
import threading
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.worker import ExecutionWorker
from src.lighter.execution import LighterExecutionAdapter
from src.strategy.arbitrage_strategy import ArbitrageStrategy
from test_execution_latency import NativeTrader


class CachedStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = NativeTrader(100, 'lighter'), NativeTrader(101, 'arcus')
        config = dict(mode='live', state_path=str(Path(self.tmp.name)/'state.json'),
            trading=dict(position_size=12, max_positions=1), spread_threshold=dict(min=5, max=500),
            entry_policy=dict(calculation_version=2, funding_reserve_bps=0),
            close_policy=dict(target_mode='zero'), risk=dict(execution_price_movement_bps=0),
            wash_trading=dict(trade_interval_min=0, max_trades_per_day=100))
        self.s = ArbitrageStrategy(config, self.a, self.b)
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)
        for trader in (self.a, self.b):
            trader.cached_reference_quote = Mock(side_effect=lambda symbol, t=trader: dict(
                bid=t.price, ask=t.price, fee_bps=0, bid_size=100, ask_size=100, updated_at=utc_now()))
            trader.get_market_price = Mock(side_effect=AssertionError('reference network read'))

    def test_all_cached_symbols_are_evaluated_without_reference_reads_threads_or_stage_writes(self):
        symbols = tuple('COIN'+str(i) for i in range(120))
        with patch('src.strategy.arbitrage_strategy.ThreadPoolExecutor') as pool, \
                patch.object(self.s.history, 'append') as append:
            candidates, missing = self.s.cached_discovery_opportunities(symbols)
        self.assertEqual({c.symbol for c in candidates}, set(symbols))
        self.assertEqual(missing, ())
        pool.assert_not_called()
        append.assert_not_called()
        self.assertEqual(self.a.cached_reference_quote.call_count, 120)
        self.assertEqual(self.b.cached_reference_quote.call_count, 120)
        self.assertEqual(self.a.sends+self.b.sends, [])
        self.assertEqual(self.s.entry_scan_status()['cached_scan']['cached_symbols'], 120)

    def test_missing_stale_future_or_crossed_quotes_are_left_to_fallback(self):
        for bad in (None, dict(bid=101, ask=100, updated_at=utc_now()),
                    dict(bid=100, ask=100, updated_at=(datetime.now(timezone.utc)-timedelta(seconds=3)).isoformat()),
                    dict(bid=100, ask=100, updated_at=(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()),
                    dict(bid=float('nan'), ask=100, updated_at=utc_now())):
            with self.subTest(bad=bad):
                self.a.cached_reference_quote.return_value = bad
                self.a.cached_reference_quote.side_effect = None
                candidates, missing = self.s.cached_discovery_opportunities(('BTC',))
                self.assertEqual(candidates, [])
                self.assertEqual(missing, ('BTC',))
        self.a.get_market_price.assert_not_called()
        self.b.get_market_price.assert_not_called()

    def test_unprofitable_fresh_books_do_not_need_network_probes(self):
        self.b.price = self.a.price
        candidates, missing = self.s.cached_discovery_opportunities(('BTC', 'ETH'))
        self.assertEqual((candidates, missing), ([], ()))

    def test_signed_target_and_reverse_route_match_regular_discovery_logic(self):
        self.s.close_target_mode = 'center'
        for centers in ((-150, 150), (150, -150)):
            self.s._target_policy_stats = lambda symbol, direction: dict(samples=100,
                center_bps=centers[direction == 'sell_variational'])
            candidates, missing = self.s.cached_discovery_opportunities(('BTC',))
            quotes = [t.cached_reference_quote('BTC') for t in (self.a, self.b)]
            expected = self.s._opportunity_from_references('BTC', 100, 101, quotes)
            self.assertEqual(missing, ())
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].spread_direction, expected.spread_direction)
            self.assertAlmostEqual(candidates[0].return_preview['net_bps'], expected.return_preview['net_bps'])

    def test_live_blocker_still_prevents_cached_candidates_and_fallback(self):
        self.s.halted_reason = 'requires reconciliation'
        self.assertEqual(self.s.cached_discovery_opportunities(('BTC',)), ([], ()))
        self.a.cached_reference_quote.assert_not_called()

    def test_lighter_cache_read_never_retries_stream_catalog_startup(self):
        adapter = LighterExecutionAdapter.__new__(LighterExecutionAdapter)
        adapter._reference_feed = None
        adapter._ensure_reference_feed = Mock(side_effect=AssertionError('catalog REST'))
        self.assertIsNone(adapter.cached_reference_quote('BTC'))
        adapter._ensure_reference_feed.assert_not_called()

    def test_bbo_spread_not_midpoint_and_round_counts_reset_without_io(self):
        self.a.cached_reference_quote.side_effect = None
        self.b.cached_reference_quote.side_effect = None
        self.a.cached_reference_quote.return_value = dict(bid=99, ask=101, fee_bps=0,
            bid_size=100, ask_size=100, updated_at=utc_now())
        self.b.cached_reference_quote.return_value = dict(bid=100.5, ask=102, fee_bps=0,
            bid_size=100, ask_size=100, updated_at=utc_now())
        # Midpoints suggest positive gross; neither actual route can enter
        # above zero, so the cached scan must reject both without REST.
        for _ in range(2):
            self.assertEqual(self.s.cached_discovery_opportunities(('BTC',)), ([], ()))
            d = self.s.entry_scan_status()['cached_scan']['diagnostics']
            self.assertEqual(d['directions'], 2)
            self.assertEqual(d['reasons'], {'net_return': 2})
        self.a.get_market_price.assert_not_called()
        self.b.get_market_price.assert_not_called()

    def test_channel_boundary_gap_is_visible_before_profit_validation(self):
        self.s.entry_mode = 'channel'
        self.s.config['entry_policy']['mode'] = 'channel'
        self.s.config['channel_policy'] = dict(method='fixed', lower_bps=-200, upper_bps=200)
        self.s.cached_discovery_opportunities(('BTC',))
        d = self.s.entry_scan_status()['cached_scan']['diagnostics']
        self.assertEqual(d['reasons'], {'boundary': 2})
        self.assertGreater(d['closest']['boundary']['gap_bps'], 0)


class CachedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fast = SimpleNamespace(symbol='FAST', spread_bps=10)
        self.strategy = SimpleNamespace(parallel_discovery=True, spread_threshold_min=0,
            spread_threshold_max=500, cached_discovery_opportunities=Mock(),
            check_arbitrage_opportunity=Mock())
        self.worker = ExecutionWorker(self.strategy, ('FAST', 'SLOW'), kill_path=Path(self.tmp.name)/'STOP')

    def test_ready_cached_candidate_does_not_wait_for_background_probe(self):
        release = threading.Event()
        self.strategy.cached_discovery_opportunities.return_value = ([self.fast], ('SLOW',))
        self.strategy.check_arbitrage_opportunity.side_effect = lambda symbol: release.wait(2) and None
        try:
            with self.worker._discovery_session(), patch('src.execution.worker.wait') as wait, \
                    patch('src.execution.worker.time.monotonic', return_value=100) as clock:
                candidates = self.worker._candidate_opportunities()
                self.assertEqual(candidates, [self.fast])
                wait.assert_not_called()
                self.strategy.check_arbitrage_opportunity.assert_not_called()
                clock.return_value = 110
                self.assertEqual(self.worker._candidate_opportunities(), [self.fast])
                wait.assert_not_called()
                self.assertEqual(set(self.worker._discovery_pending), {'SLOW'})
                release.set()
                self.worker._discovery_pending['SLOW'][0].result(timeout=1)
        finally:
            release.set()
        self.strategy.check_arbitrage_opportunity.assert_called_once_with('SLOW')

    def test_full_coverage_creates_no_discovery_pool_or_probes(self):
        self.strategy.cached_discovery_opportunities.return_value = ([self.fast], ())
        with self.worker._discovery_session():
            self.assertEqual(self.worker._candidate_opportunities(), [self.fast])
            self.assertIsNone(self.worker._discovery_pool)
        self.strategy.check_arbitrage_opportunity.assert_not_called()

    def test_multi_route_probe_reads_in_background_but_evaluates_on_main_thread(self):
        thread_ids = {}
        candidate = self.fast
        class Strategy(SimpleNamespace):
            multi_route_enabled = True
            def probe_reference_prices(self, symbol):
                thread_ids['probe'] = threading.get_ident()
                return {'symbol': symbol, 'routes': []}
            def complete_reference_probe(self, probe):
                thread_ids['complete'] = threading.get_ident()
                return candidate
        self.worker.strategy = Strategy(check_arbitrage_opportunity=Mock(
            side_effect=AssertionError('background route mutation')))
        with self.worker._discovery_session():
            result = self.worker._discover_parallel(('FAST',), wait_budget=.2, paced=True)
        self.assertEqual(result, [candidate])
        self.assertNotEqual(thread_ids['probe'], threading.get_ident())
        self.assertEqual(thread_ids['complete'], threading.get_ident())

    def test_stop_or_kill_prevents_even_cached_discovery(self):
        for kill in (False, True):
            with self.subTest(kill=kill):
                if kill:
                    self.worker.stop_event.clear()
                    self.worker.kill_path.touch()
                else:
                    self.worker.stop()
                self.assertEqual(self.worker._candidate_opportunities(), [])
        self.strategy.cached_discovery_opportunities.assert_not_called()

    def test_stream_recovery_discards_older_probe_even_if_it_looks_better(self):
        stale = SimpleNamespace(symbol='FAST', spread_bps=400)
        future = Future()
        future.set_result((stale, 0))
        self.worker._discovery_pending['FAST'] = (future, 0)
        self.strategy.cached_discovery_opportunities.return_value = ([self.fast], ())
        with self.worker._discovery_session():
            self.assertEqual(self.worker._candidate_opportunities(), [self.fast])
            self.assertEqual(self.worker._discovery_pending, {})

    def test_missing_scope_remains_fair_bounded_and_does_not_duplicate_running_reads(self):
        release = threading.Event()
        self.worker.symbols = tuple('M'+str(i) for i in range(30))
        self.strategy.cached_discovery_opportunities.return_value = ([self.fast], self.worker.symbols)
        self.strategy.check_arbitrage_opportunity.side_effect = lambda symbol: release.wait(2) and None
        try:
            with self.worker._discovery_session(), \
                    patch('src.execution.worker.time.monotonic', return_value=100) as clock:
                self.worker._candidate_opportunities()
                self.assertEqual(self.worker._discovery_pending, {})
                for now in (110, 120, 130, 140):
                    clock.return_value = now
                    self.worker._candidate_opportunities()
                    self.assertLessEqual(len(self.worker._discovery_pending), 4)
                self.assertEqual(set(self.worker._discovery_pending), {'M0', 'M1', 'M2', 'M3'})
                for _ in range(3):
                    self.worker._candidate_opportunities()
                self.assertEqual(len(self.worker._discovery_pending), 4)
                release.set()
                for future, _ in list(self.worker._discovery_pending.values()):
                    future.result(timeout=1)
                clock.return_value = 150
                self.worker._candidate_opportunities()
                self.assertIn('M4', self.worker._discovery_pending)
        finally:
            release.set()
        calls = [call.args[0] for call in self.strategy.check_arbitrage_opportunity.call_args_list]
        self.assertEqual(len(calls), len(set(calls)))
