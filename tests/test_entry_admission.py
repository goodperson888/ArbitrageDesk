"""Admission waits before threads, while fresh streams bypass optional REST."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusExecutionAdapter
from src.common.discovery import DiscoveryCooldown
from src.common.market_data import utc_now
from src.common.rate_limit import RateLimitGate, RateLimitError, DiscoveryThrottleError
from src.execution.preparation import readonly_preparation
from src.lighter.execution import LighterExecutionAdapter
import test_candidate_preparation as fixtures


class AdmissionSchedulingTests(unittest.TestCase):
    def setUp(self):
        fixtures.CandidateSchedulingTests.setUp(self)

    def test_waiting_candidate_starts_no_thread_and_cached_peer_can_prepare(self):
        self.s.entry_preparation_admission = lambda op: dict(
            wait_seconds=30 if op.symbol == 'A' else 0,
            rest_hosts=('host',) if op.symbol == 'A' else ())
        candidates = [fixtures.opportunity('A'), fixtures.opportunity('B')]
        with self.worker._discovery_session():
            result = self.worker._collect_prepared(candidates, set())
            self.assertEqual(result['opportunity'].symbol, 'B')
            self.assertEqual(self.calls, ['B'])
            self.assertNotIn('A', self.worker._preparation_pending)
            self.s.record_entry_scan_failure.assert_not_called()
            # A stream recovery overrides a previous long REST wait.
            self.s.entry_preparation_admission = lambda op: dict(wait_seconds=0, rest_hosts=())
            result = self.worker._collect_prepared(candidates, {'B'})
            self.assertEqual(result['opportunity'].symbol, 'A')
        self.assertFalse(self.executed)

    def test_same_rest_host_is_serialized_but_cached_peer_runs_concurrently(self):
        release = threading.Event()
        started = threading.Event()
        base = self.s.prepare_entry_candidate
        def prepare(op):
            if op.symbol == 'A':
                started.set()
                release.wait(2)
            return base(op)
        self.s.prepare_entry_candidate = prepare
        self.s.entry_preparation_admission = lambda op: dict(
            wait_seconds=0, rest_hosts=() if op.symbol == 'C' else ('host',))
        candidates = [fixtures.opportunity(s) for s in ('A', 'B', 'C')]
        try:
            with self.worker._discovery_session():
                result = self.worker._collect_prepared(candidates, set())
                self.assertTrue(started.is_set())
                self.assertEqual(result['opportunity'].symbol, 'C')
                self.assertNotIn('B', self.calls)
                self.assertNotIn('B', self.worker._preparation_pending)
                release.set()
                self.worker._preparation_pending['A'][0].result(timeout=1)
                self.worker._collect_prepared(candidates, {'C'})
                self.assertIn('B', self.calls)
        finally:
            release.set()
        self.assertFalse(self.worker._preparation_hosts)

    def test_background_probe_waits_without_starting_a_read_or_recording_failure(self):
        self.s.check_arbitrage_opportunity = Mock(return_value=None)
        self.s.reference_probe_wait_seconds = lambda symbol: 30
        with self.worker._discovery_session():
            self.worker._discover_parallel(wait_budget=0, paced=True)
            self.assertFalse(self.worker._discovery_pending)
            self.s.check_arbitrage_opportunity.assert_not_called()
            self.s.record_entry_scan_failure.assert_not_called()

    def test_equal_prepared_profit_keeps_recency_priority_before_completion_time(self):
        base = self.s.prepare_entry_candidate
        def prepare(op):
            return {**base(op), 'recency_rank': 1 if op.symbol == 'A' else 0}
        self.s.prepare_entry_candidate = prepare
        with self.worker._discovery_session():
            result = self.worker._collect_prepared([fixtures.opportunity('A'), fixtures.opportunity('B')], set())
            self.assertEqual(result['opportunity'].symbol, 'A')


class SharedQuotaReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = RateLimitGate('https://test.invalid', self.tmp.name)

    def test_preflight_gives_candidate_next_slot_without_consuming_or_extending_it(self):
        dashboard = RateLimitGate('https://test.invalid', self.tmp.name)
        with patch('src.common.rate_limit.time.time', return_value=100):
            dashboard.reserve_discovery()
        with patch('src.common.rate_limit.time.time', return_value=101):
            self.assertEqual(self.gate.discovery_delay(priority=True), 1)
            self.assertEqual(self.gate.discovery_delay(priority=True), 1)
            self.assertEqual(dashboard.discovery_delay(), 3)
        with patch('src.common.rate_limit.time.time', return_value=102):
            self.assertEqual(self.gate.discovery_delay(priority=True), 0)
            with self.assertRaises(DiscoveryThrottleError):
                dashboard.reserve_discovery()
            self.gate.reserve_discovery(priority=True)
        with patch('src.common.rate_limit.time.time', return_value=104):
            dashboard.reserve_discovery()

    def test_real_cooldown_is_not_bypassed_by_preflight_or_priority(self):
        self.gate.path.write_text('{"until": 160}')
        with patch('src.common.rate_limit.time.time', return_value=100):
            self.assertEqual(self.gate.discovery_delay(priority=True), 60)
            with self.assertRaises(RateLimitError):
                self.gate.reserve_discovery(priority=True)

    def test_adapter_readiness_is_local_and_stale_streams_need_rest(self):
        for cls in (ArcusExecutionAdapter, LighterExecutionAdapter):
            with self.subTest(adapter=cls.__name__):
                adapter = cls.__new__(cls)
                adapter._discovery_cooldown = DiscoveryCooldown('test')
                adapter._reference_feed = Mock()
                adapter.verified_fee_bps = 0
                adapter._reference_feed.snapshot.return_value = None
                adapter.client = Mock(rate_limit=self.gate)
                adapter.public = Mock(client=Mock(rate_limit=self.gate))
                with patch('src.common.rate_limit.time.time', return_value=100):
                    self.gate.reserve_discovery()
                    self.assertGreater(adapter.discovery_admission('BTC', preparation=True)['wait_seconds'], 0)
                adapter._reference_feed.snapshot.return_value = dict(
                    bid=100, ask=101, bid_size=2, ask_size=2, updated_at=utc_now(),
                    fetched_at=utc_now(), bids=[dict(price=100,size=2)], asks=[dict(price=101,size=2)])
                self.assertEqual(adapter.discovery_admission('BTC', preparation=True),
                                 dict(wait_seconds=0, rest_hosts=()))
                old = (datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat()
                adapter._reference_feed.snapshot.return_value.update(updated_at=old, fetched_at=old)
                self.assertTrue(adapter.discovery_admission('BTC', preparation=True)['rest_hosts'])
                adapter.client.orderbook.assert_not_called()
                adapter.public.get_order_book.assert_not_called()
                # Isolate the next adapter's shared gate.
                self.gate.path.with_suffix('.discovery.json').unlink(missing_ok=True)


class ScopedBookTests(unittest.TestCase):
    def adapter(self):
        a = ArcusExecutionAdapter.__new__(ArcusExecutionAdapter)
        a.cached_reference_quote = Mock(return_value=None)
        a.client = Mock()
        a.client.orderbook.side_effect = lambda *args, **kwargs: dict(
            fetched_at=utc_now(), bids=[dict(price=100,size=2)], asks=[dict(price=101,size=2)])
        return a

    def test_parallel_readers_reuse_one_raw_book_and_do_not_share_mutable_results(self):
        a = self.adapter()
        with readonly_preparation(), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, a.get_sizing_book, 'BTC') for _ in range(2)]
            first, second = [f.result(timeout=1) for f in futures]
            self.assertEqual(a.client.orderbook.call_count, 1)
            first['bids'][0]['price'] = 999
            self.assertEqual(second['bids'][0]['price'], 100)
            self.assertEqual(a.cached_sizing_book('BTC', 1)['bids'][0]['price'], 100)
        a.get_sizing_book('BTC')
        self.assertEqual(a.client.orderbook.call_count, 2, 'execution scope must fetch independently')
        a.client.orderbook.assert_called_with('BTC')

    def test_expired_book_refetches_without_refreshing_the_original_timestamp(self):
        a = self.adapter()
        with readonly_preparation():
            first = a.get_sizing_book('BTC')
            self.assertEqual(a.get_sizing_book('BTC')['fetched_at'], first['fetched_at'])
            self.assertEqual(a.client.orderbook.call_count, 1)
            with patch('src.execution.preparation.age_seconds', return_value=2.01):
                second = a.get_sizing_book('BTC')
            self.assertEqual(a.client.orderbook.call_count, 2)
            self.assertNotEqual(second['fetched_at'], first['fetched_at'])


if __name__ == '__main__':
    unittest.main()
