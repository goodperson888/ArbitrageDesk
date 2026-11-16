"""Read-only retries yield to ready candidates and never authorize an order."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.common.discovery import DiscoveryCooldownError
from src.common.rate_limit import DiscoveryThrottleError
from src.execution.worker import ExecutionWorker
import test_candidate_preparation as preparation_tests

opportunity = preparation_tests.opportunity


class ProbeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.strategy = SimpleNamespace(check_arbitrage_opportunity=Mock(return_value=None),
                                        record_entry_scan_failure=Mock())
        self.worker = ExecutionWorker(self.strategy, ('A', 'B', 'C'),
                                      kill_path=Path(self.tmp.name)/'STOP')

    def finish_reads(self):
        for future, _ in list(self.worker._discovery_pending.values()):
            future.result(timeout=1)

    def test_missing_markets_rotate_without_launching_a_burst_each_tick(self):
        with self.worker._discovery_session(), \
                patch('src.execution.worker.time.monotonic', return_value=100) as clock:
            for now, expected in ((100, ['A']), (100.5, ['A']), (102, ['A', 'B']),
                                  (102.5, ['A', 'B']), (104, ['A', 'B', 'C'])):
                clock.return_value = now
                self.worker._discover_parallel(wait_budget=0, paced=True)
                self.finish_reads()
                self.assertEqual([c.args[0] for c in self.strategy.check_arbitrage_opportunity.call_args_list], expected)

    def test_known_market_cooldown_creates_no_more_threads_or_failures_until_due(self):
        self.strategy.check_arbitrage_opportunity.side_effect = DiscoveryCooldownError('Arcus', 'empty', 30)
        with self.worker._discovery_session(), \
                patch('src.execution.worker.time.monotonic', return_value=100) as clock:
            self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.finish_reads()
            for now in (101, 102, 110, 129.9):
                clock.return_value = now
                self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.strategy.check_arbitrage_opportunity.assert_called_once_with('A')
            self.strategy.record_entry_scan_failure.assert_called_once()
            clock.return_value = 130
            self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.finish_reads()
            self.assertEqual(self.strategy.check_arbitrage_opportunity.call_count, 2)

    def test_repeated_local_pacing_does_not_turn_into_a_transport_failure_backoff(self):
        self.strategy.check_arbitrage_opportunity.side_effect = DiscoveryThrottleError(2)
        with self.worker._discovery_session(), \
                patch('src.execution.worker.time.monotonic', return_value=100) as clock:
            for now in (100, 101, 102, 103, 104, 105, 106, 107):
                clock.return_value = now
                self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
                self.finish_reads()
            self.assertEqual(self.strategy.check_arbitrage_opportunity.call_count, 4)

    def test_stream_recovery_bypasses_old_probe_cooldown(self):
        self.worker._discovery_retry_at['A'] = time.monotonic() + 300
        self.worker._discovery_failures['A'] = 5
        fast = SimpleNamespace(symbol='A', spread_bps=20)
        self.strategy.parallel_discovery = True
        self.strategy.cached_discovery_opportunities = lambda symbols: ([fast], ())
        with self.worker._discovery_session():
            self.assertEqual(self.worker._candidate_opportunities(), [fast])
            self.assertFalse(self.worker._discovery_retry_at)
            self.assertFalse(self.worker._discovery_failures)
        self.strategy.check_arbitrage_opportunity.assert_not_called()

    def test_unexpected_transport_failures_back_off_per_market(self):
        self.strategy.check_arbitrage_opportunity.side_effect = RuntimeError('transport')
        with self.worker._discovery_session(), \
                patch('src.execution.worker.time.monotonic', return_value=100) as clock:
            self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.finish_reads()
            clock.return_value = 101
            self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.assertEqual(self.worker._discovery_retry_at['A'], 105)
            clock.return_value = 105
            self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.finish_reads()
            clock.return_value = 106
            self.worker._discover_parallel(('A',), wait_budget=0, paced=True)
            self.assertEqual(self.worker._discovery_retry_at['A'], 115)


class PreparationRetryTests(unittest.TestCase):
    def setUp(self):
        preparation_tests.CandidateSchedulingTests.setUp(self)

    def test_pacing_one_candidate_does_not_block_another_or_retry_each_tick(self):
        base = self.s.prepare_entry_candidate
        def prepare(op):
            if op.symbol == 'A':
                self.calls.append('A')
                return dict(opportunity=op, error=True, retry_after_seconds=30,
                            key=1, prepared_at=time.monotonic())
            return base(op)
        self.s.prepare_entry_candidate = prepare
        with self.worker._discovery_session():
            candidates = [opportunity('A'), opportunity('B')]
            item = self.worker._collect_prepared(candidates, set())
            self.assertEqual(item['opportunity'].symbol, 'B')
            for _ in range(3):
                self.assertIsNone(self.worker._collect_prepared(candidates, {'B'}))
            self.assertEqual(self.calls.count('A'), 1)
            self.assertFalse(self.executed)
            self.worker._preparation_retry_at['A'] = (time.monotonic() - 1, 1)
            self.worker._collect_prepared(candidates, {'B'})
            self.assertEqual(self.calls.count('A'), 2)

    def test_changed_policy_does_not_keep_old_throttle_delay(self):
        self.worker._preparation_retry_at['A'] = (time.monotonic() + 30, 0)
        with self.worker._discovery_session():
            self.assertIsNotNone(self.worker._collect_prepared([opportunity('A')], set()))
            self.assertEqual(self.calls, ['A'])


class PreparationRetryMetadataTests(unittest.TestCase):
    def setUp(self):
        preparation_tests.PreparedPlanTests.setUp(self)

    def test_real_preparation_failure_returns_retry_without_account_reads_or_orders(self):
        with patch.object(self.s, 'plan_entry', side_effect=DiscoveryThrottleError(2)):
            item = self.s.prepare_entry_candidate(self.op)
        self.assertTrue(item['error'])
        self.assertEqual(item['retry_after_seconds'], 2)
        self.assertEqual(self.a.reads + self.b.reads, 0)
        self.assertFalse(self.a.sends + self.b.sends)

    def test_below_floor_roundtrip_is_not_reported_as_a_depth_shortage(self):
        self.b.price = self.a.price
        item = self.s.prepare_entry_candidate(self.op)
        self.assertTrue(item['error'])
        rejected = [event for event in self.s.history.recent() if event['event'] == 'blocked']
        self.assertEqual(rejected[0]['detail']['reason'], '本次数量的预计整轮净收益未达入场门槛')
        self.assertEqual(self.a.reads + self.b.reads, 0)
        self.assertFalse(self.a.sends + self.b.sends)
