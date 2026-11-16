"""Parallel reads never imply parallel order submission or stale plan reuse."""
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.execution.worker import ExecutionWorker
from src.execution.preparation import is_preparing, readonly_preparation
from src.execution.history import TradeHistory
from src.common.market_data import utc_now
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_execution_latency import NativeTrader


def opportunity(symbol, edge=10):
    return SimpleNamespace(symbol=symbol, edge=edge)


class CandidateSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.executed = [], []
        self.s = SimpleNamespace(parallel_preparation=True, position_size=12,
            entry_preparation_key=lambda: 1, last_execution_submitted=False,
            record_entry_scan_failure=Mock())
        def prepare(op):
            self.calls.append(op.symbol)
            return dict(opportunity=op, plan=({'estimated_entry_edge_usdc': op.edge, 'net_edge_bps': op.edge},),
                        key=1, prepared_at=time.monotonic())
        self.s.prepare_entry_candidate = prepare
        self.s.execute_prepared_candidate = lambda item: self.executed.append(item['opportunity'].symbol) or True
        self.worker = ExecutionWorker(self.s, ['A','B','C'], kill_path=Path(self.tmp.name)/'STOP')
        self.worker.preparation_budget = .08

    def test_parallel_reads_choose_best_completed_return_and_submit_once_on_coordinator(self):
        lock = threading.Lock()
        active, peak = [0], [0]
        base = self.s.prepare_entry_candidate
        def prepare(op):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(.015)
            try:
                return base(op)
            finally:
                with lock: active[0] -= 1
        self.s.prepare_entry_candidate = prepare
        coordinator = threading.get_ident()
        def execute(item):
            self.assertEqual(threading.get_ident(), coordinator)
            self.executed.append(item['opportunity'].symbol)
            return True
        self.s.execute_prepared_candidate = execute
        self.worker._candidate_opportunities = Mock(return_value=[opportunity('A',1),opportunity('B',3),opportunity('C',2)])
        with self.worker._discovery_session():
            self.assertTrue(self.worker._attempt_candidates()[0])
        self.assertEqual(self.executed, ['B'])
        self.assertEqual(peak[0], 3)
        self.assertCountEqual(self.calls, ['A','B','C'])

    def test_slow_request_is_not_duplicated_and_does_not_block_fast_candidate(self):
        release = threading.Event()
        base = self.s.prepare_entry_candidate
        def prepare(op):
            if op.symbol == 'A':
                self.calls.append('A-start')
                release.wait(2)
            return base(op)
        self.s.prepare_entry_candidate = prepare
        candidates = [opportunity('A'),opportunity('B')]
        try:
            with self.worker._discovery_session():
                item = self.worker._collect_prepared(candidates, set())
                self.assertEqual(item['opportunity'].symbol, 'B')
                self.worker._collect_prepared(candidates, {'B'})
                self.assertEqual(self.calls.count('A-start'), 1)
        finally:
            release.set()

    def test_rejection_refreshes_ranking_and_cannot_submit_an_old_second_choice(self):
        self.worker._candidate_opportunities = Mock(side_effect=[
            [opportunity('A',4),opportunity('B',2)], [opportunity('NEW',8)]])
        def execute(item):
            self.executed.append(item['opportunity'].symbol)
            return len(self.executed) > 1
        self.s.execute_prepared_candidate = execute
        with self.worker._discovery_session():
            self.assertTrue(self.worker._attempt_candidates()[0])
        self.assertEqual(self.executed, ['A','NEW'])
        self.assertEqual(self.worker._candidate_opportunities.call_count, 2)

    def test_ambiguous_submission_and_stop_both_prevent_fallback(self):
        self.worker._candidate_opportunities = Mock(return_value=[opportunity('A'),opportunity('B')])
        def execute(item):
            self.executed.append(item['opportunity'].symbol)
            self.s.last_execution_submitted = True
            return False
        self.s.execute_prepared_candidate = execute
        with self.worker._discovery_session():
            self.worker._attempt_candidates()
            self.assertEqual(len(self.executed), 1)
            self.worker.stop()
            self.assertIsNone(self.worker._collect_prepared([opportunity('C')], set()))
        self.assertNotIn('C', self.calls)

    def test_disabling_fallback_does_not_prepare_or_execute_a_second_symbol(self):
        self.worker.candidate_fallback=False
        def failed(op):
            self.calls.append(op.symbol)
            return dict(opportunity=op,error=True,key=1,prepared_at=time.monotonic())
        self.s.prepare_entry_candidate=failed
        self.worker._candidate_opportunities=Mock(return_value=[opportunity('A'),opportunity('B')])
        with self.worker._discovery_session():
            self.assertFalse(self.worker._attempt_candidates()[0])
        self.assertEqual(self.calls,['A'])
        self.assertFalse(self.executed)


class PreparedPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = NativeTrader(100, 'lighter'), NativeTrader(101, 'arcus')
        cfg = dict(mode='live',state_path=str(Path(self.tmp.name)/'state.json'),
            trading=dict(position_size=12,max_positions=1), spread_threshold=dict(min=5,max=500),
            entry_policy=dict(calculation_version=2,funding_reserve_bps=0),
            close_policy=dict(target_mode='zero'), risk=dict(execution_price_movement_bps=0),
            wash_trading=dict(trade_interval_min=0,max_trades_per_day=100))
        self.s = ArbitrageStrategy(cfg,self.a,self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)
        self.op = ArbitrageOpportunity('BTC',100,101,99,'lighter_higher',0,datetime.now())

    def test_preparation_reads_only_and_scope_and_diagnostics_do_not_leak(self):
        item = self.s.prepare_entry_candidate(self.op)
        self.assertFalse(item.get('error'))
        self.assertFalse(self.a.sends+self.b.sends)
        self.assertEqual(self.a.reads+self.b.reads,0)
        self.assertFalse(is_preparing())
        self.assertIsNone(TradeHistory.current_execution_id())
        self.assertIsNotNone(self.s._prepared_plan(self.op,item))
        events = self.s.history.recent()
        ids = {e['detail'].get('execution_id') for e in events}
        self.assertEqual(len(ids),1)
        self.assertNotIn(None,ids)

    def test_age_policy_and_exposure_changes_invalidate_prepared_plan(self):
        item = self.s.prepare_entry_candidate(self.op)
        with patch('src.strategy.arbitrage_strategy.time.monotonic',return_value=item['prepared_at']+1.01):
            self.assertIsNone(self.s._prepared_plan(self.op,item))
        self.s.position_size = 11
        self.assertIsNone(self.s._prepared_plan(self.op,item))

    def test_prepared_initial_quote_does_not_remove_final_requote(self):
        item = self.s.prepare_entry_candidate(self.op)
        self.a.price,self.b.price = 101,100
        self.assertFalse(self.s.execute_prepared_candidate(item))
        self.assertFalse(self.a.sends+self.b.sends)

    def test_buffer_is_flushed_on_failure_with_original_timestamp_and_allowlist(self):
        with self.assertRaises(ValueError):
            with self.s.history.buffered():
                self.s.history.append('test',symbol='BTC',secret='must not persist')
                self.assertEqual(self.s.history.count(),0)
                raise ValueError('test')
        row = self.s.history.recent()[0]
        self.assertEqual(row['detail'],{'symbol':'BTC'})


class PreparationTransportTests(unittest.TestCase):
    def test_arcus_fresh_ws_sizing_needs_no_rest_and_required_sizing_keeps_depth_read(self):
        from src.arcus.execution import ArcusExecutionAdapter
        adapter=ArcusExecutionAdapter.__new__(ArcusExecutionAdapter)
        adapter.client=Mock()
        adapter.verified_fee_bps=4.5
        adapter._reference_feed=Mock()
        adapter._reference_feed.snapshot.return_value=dict(bid=100,ask=101,bid_size='2',ask_size='3',updated_at=utc_now())
        with readonly_preparation():
            book=adapter.get_sizing_book('BTC')
        self.assertEqual(book['bids'][0]['size'],2)
        adapter.client.orderbook.assert_not_called()
        adapter.get_sizing_book('BTC')
        adapter.client.orderbook.assert_called_once_with('BTC')

    def test_missing_ws_preparation_fallback_is_paced_but_required_reads_are_not_optional(self):
        from src.lighter.execution import LighterExecutionAdapter
        from src.arcus.execution import ArcusExecutionAdapter
        lighter=LighterExecutionAdapter.__new__(LighterExecutionAdapter)
        lighter._fresh_reference_book=Mock(return_value=None)
        lighter.public=Mock()
        arcus=ArcusExecutionAdapter.__new__(ArcusExecutionAdapter)
        arcus.cached_reference_quote=Mock(return_value=None)
        arcus.client=Mock()
        with readonly_preparation():
            lighter.get_sizing_book('BTC');arcus.get_sizing_book('BTC')
        lighter.public.get_order_book.assert_called_once_with('BTC',discovery=True)
        arcus.client.orderbook.assert_called_once_with('BTC',discovery=True)

    def test_arcus_cache_hits_do_not_consume_optional_rest_allowance(self):
        import httpx
        from src.arcus.execution import ArcusClient
        client=ArcusClient.__new__(ArcusClient)
        client._public_cache_lock=threading.RLock();client._public_cache={}
        client._public_key_locks=tuple(threading.RLock() for _ in range(32))
        client.rate_limit=Mock();client.api_key='unit-test';client.base_url='https://test.invalid'
        client.http=Mock()
        client.http.get.return_value=httpx.Response(200,json={'value':1},request=httpx.Request('GET',client.base_url))
        client._get('/book',ttl=.2,discovery=True)
        client._get('/book',ttl=.2,discovery=True)
        self.assertEqual(client.http.get.call_count,1)
        client.rate_limit.reserve_discovery.assert_called_once()
        client._get('/account')
        self.assertEqual(client.rate_limit.reserve_discovery.call_count,1)
