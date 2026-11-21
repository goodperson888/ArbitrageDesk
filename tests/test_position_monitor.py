import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import httpx
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.position_monitor import PositionMonitor, PositionReadUnavailable, read_position_monitor
from src.execution.worker import ExecutionWorker
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position


class Reader:
    def __init__(self, venue):
        self.venue, self.calls = venue, 0
        self.account_gate = None

    def get_position_mark_snapshot(self, symbol):
        self.calls += 1
        return {'bid': 99., 'ask': 101., 'updated_at': utc_now(),
                'source': self.venue + '_websocket', 'fee_bps': 0.}

    def position_snapshot(self, symbol):
        if self.account_gate:
            self.account_gate.wait(2)
        return {'qty': 1, 'side': 'long' if self.venue == 'lighter' else 'short', 'fetched_at': utc_now()}

    def reference_health(self, symbol):
        return {'connected': True, 'quote_status': 'fresh'}

    def get_executable_quote(self, symbol, quantity):
        return dict(symbol=symbol, size=quantity, bid=100, ask=100, bid_vwap=100, ask_vwap=100,
                    updated_at=utc_now(), executable=True, instrument_type='perpetual', fee_bps=0)

    def reconcile_funding(self, *args):
        return {'complete': True, 'funding_usdc': 0}


class PositionMonitorTests(unittest.TestCase):
    def test_pool_exhaustion_has_bounded_fast_recovery_and_clears_diagnostics(self):
        self.monitor.positions = [self.p]
        generation = self.monitor._generation([self.p])
        self.monitor.generations['SUI'] = generation
        key = ('account', 'SUI', 'lighter')
        for i, delay in enumerate((1, 2, 4, 5)):
            failed = Future()
            failed.set_exception(httpx.PoolTimeout('secret signed URL must not appear'))
            self.monitor.jobs[key] = failed, generation
            with patch('src.execution.position_monitor.time.monotonic', return_value=100+i):
                self.monitor._collect()
                health = self.monitor._account_health('SUI', 'lighter')
            self.assertEqual(self.monitor.next_read[key], 100+i+delay)
            self.assertEqual(health['blocked_for_seconds'], i)
            self.assertIn('PoolTimeout', health['error'])
            self.assertNotIn('secret', health['error'])
        ready = Future()
        ready.set_result(dict(qty=1, fetched_at=utc_now()))
        self.monitor.jobs[key] = ready, generation
        self.monitor._collect()
        self.assertEqual(self.monitor._account_health('SUI', 'lighter')['status'], 'ready')
        self.assertNotIn(key, self.monitor.error_since)

    def test_passive_monitor_reader_does_not_replace_foreground_position_reader(self):
        with patch.object(Reader, 'monitor_position_snapshot', return_value={}, create=True) as passive, \
                patch.object(self.monitor, '_submit') as submit:
            self.monitor.poll([self.p], wait_for_reads=False)
            callbacks = [call.args[2] for call in submit.call_args_list if call.args[0][0] == 'account']
            self.assertEqual(len(callbacks), 2)
            for reader in callbacks:
                reader()
        self.assertEqual(passive.call_count, 2)

    def test_pending_account_diagnostics_name_venue_and_do_not_extend_stale_evidence(self):
        self.monitor.positions = [self.p]
        stamp = '2000-01-01T00:00:00Z'
        self.seed('account', 'lighter', dict(qty=1, fetched_at=stamp))
        self.monitor.errors[('account', 'SUI', 'lighter')] = '等待可用连接超时（PoolTimeout）'
        self.monitor.publish()
        row = json.loads(self.monitor.path.read_text())['positions'][0]
        self.assertIn('Arcus：', row['account_errors'][0])
        self.assertEqual(row['legs']['lighter']['account']['last_success_at'], stamp)
        self.assertEqual(row['legs']['lighter']['account']['status'], 'pending')
        with self.assertRaises(PositionReadUnavailable):
            self.monitor.accounts('SUI')

    def test_expired_read_is_discarded_and_does_not_spawn_duplicate_or_overwrite_recovery(self):
        self.monitor.positions=[self.p]
        generation=self.monitor._generation([self.p])
        self.monitor.generations['SUI']=generation
        key=('account','SUI','variational')
        future=Future(); self.monitor.jobs[key]=(future,generation)
        with patch('src.execution.position_monitor.time.monotonic',return_value=100):
            self.monitor.job_started[key]=91
            self.monitor._collect()
            self.assertNotIn(key,self.monitor.jobs)
            self.assertIn(future,self.monitor.expired_jobs)
            self.assertEqual(self.monitor.next_read[key],105)
            self.assertIn('超时',self.monitor.errors[key])
            with patch('src.execution.position_monitor.threading.Thread.start') as start:
                self.monitor._submit(key,generation,lambda:{})
                start.assert_not_called()
        future.set_result({'qty':999})
        self.monitor._collect()
        self.assertNotIn(key,self.monitor.values)
        fresh=Future(); fresh.set_result(dict(qty=1,fetched_at=utc_now()))
        self.monitor.jobs[key]=(fresh,generation); self.monitor._collect()
        self.seed('account','lighter',dict(qty=1,fetched_at=utc_now()))
        self.monitor.checked(self.p)
        self.assertFalse(self.monitor.entry_blocked)

    def test_read_concurrency_counts_running_expired_tasks_and_respects_rate_limit(self):
        generation=self.monitor._generation([self.p]); self.monitor.generations['SUI']=generation
        self.monitor.expired_jobs={Future():('account','OTHER',str(i)) for i in range(3)}
        self.monitor.jobs[('account','SUI','variational')]=(Future(),generation)
        with patch('src.execution.position_monitor.threading.Thread.start') as start:
            self.monitor._submit(('account','SUI','lighter'),generation,lambda:{})
            start.assert_not_called()
        from src.common.rate_limit import RateLimitError
        future=Future(); future.set_exception(RateLimitError(93))
        key=('account','SUI','variational'); self.monitor.jobs[key]=(future,generation)
        with patch('src.execution.position_monitor.time.monotonic',return_value=100):
            self.monitor._collect()
            self.assertEqual(self.monitor.next_read[key],193)
        fresh=Future(); fresh.set_result(dict(qty=1,fetched_at=utc_now()))
        self.monitor.jobs[key]=(fresh,generation); self.monitor._collect()
        self.assertNotIn(key,self.monitor.errors)
        self.assertNotIn(key,self.monitor.read_failures)

    def test_transient_quote_and_exit_failure_preserves_fast_close_retry_cadence(self):
        generation=self.monitor._generation([self.p]);self.monitor.generations['SUI']=generation
        for kind,leg,delay in (('quote','variational',.5),('grid',self.p.id,1)):
            for i in range(4):
                future=Future();future.set_exception(ValueError('报价过期'))
                key=(kind,'SUI',leg);self.monitor.jobs[key]=(future,generation)
                with patch('src.execution.position_monitor.time.monotonic',return_value=100):
                    self.monitor._collect()
                self.assertEqual(self.monitor.next_read[key],100+delay)

    def test_exit_refresh_is_scheduled_before_original_quote_deadline(self):
        generation = self.monitor._generation([self.p])
        self.monitor.generations['SUI'] = generation
        future = Future()
        deadline = time.monotonic() + .6
        quote = dict(updated_at=(datetime.now(timezone.utc)-timedelta(seconds=1.4)).isoformat(),
                     submit_deadline_monotonic=deadline)
        future.set_result({'legs': {'variational': {'quote': quote}, 'lighter': {'quote': dict(quote)}}})
        key = ('grid', 'SUI', self.p.id)
        self.monitor.jobs[key] = (future, generation)
        self.monitor._collect()
        self.assertLess(self.monitor.next_read[key]-time.monotonic(), .35)
        self.assertEqual(self.monitor.values[key][0]['legs']['variational']['quote']['submit_deadline_monotonic'], deadline)

    def test_changed_fresh_stream_refreshes_early_but_unchanged_or_missing_stream_does_not(self):
        key = ('grid', 'SUI', self.p.id)
        generation = self.monitor._generation([self.p])
        self.monitor.next_read[key] = time.monotonic() + 10
        self.monitor.exit_stream_reads[key] = (time.monotonic()-.3, (generation, 'old'))
        stamp = utc_now()
        cached = lambda trader, symbol: dict(bid=100, ask=101, updated_at=stamp,
            book=dict(fetched_at=stamp,bids=[dict(price=100,size=10)],asks=[dict(price=101,size=10)]))
        with patch.object(Reader, 'cached_reference_quote', cached, create=True), \
                patch.object(self.monitor, '_submit', return_value=True) as submit, \
                patch.object(self.monitor, 'publish'):
            self.monitor.poll([self.p])
            self.assertEqual(sum(call.args[0] == key for call in submit.call_args_list), 1)
            submit.reset_mock()
            self.monitor.exit_stream_reads[key] = (time.monotonic()-.3, self.monitor.exit_stream_reads[key][1])
            self.monitor.poll([self.p])
            self.assertFalse(any(call.args[0] == key for call in submit.call_args_list))
        with patch.object(self.monitor, '_submit') as submit, patch.object(self.monitor, 'publish'):
            self.monitor.poll([self.p])
            self.assertFalse(any(call.args[0] == key for call in submit.call_args_list))

    def test_stream_probe_keeps_singleflight_and_does_not_replace_inflight_revision(self):
        key = ('grid', 'SUI', self.p.id)
        generation = self.monitor._generation([self.p])
        self.monitor.jobs[key] = (Future(), generation)
        previous = (time.monotonic()-.3, (generation, 'old'))
        self.monitor.exit_stream_reads[key] = previous
        stamp = utc_now()
        cached = lambda trader, symbol: dict(bid=100, ask=101, updated_at=stamp,
            book=dict(fetched_at=stamp,bids=[dict(price=100,size=10)],asks=[dict(price=101,size=10)]))
        with patch.object(Reader, 'cached_reference_quote', cached, create=True), \
                patch.object(self.s, '_single_monitor_exit_estimate') as read, \
                patch.object(self.monitor, 'publish'):
            self.monitor.poll([self.p])
        read.assert_not_called()
        self.assertEqual(self.monitor.exit_stream_reads[key], previous)

    def test_bbo_without_fresh_sized_book_cannot_accelerate_rest_reads(self):
        key = ('grid', 'SUI', self.p.id)
        self.monitor.next_read[key] = time.monotonic() + 10
        stamp = utc_now()
        for book in (None, dict(fetched_at='2000-01-01T00:00:00Z',bids=[1],asks=[1])):
            cached = lambda trader, symbol: dict(bid=100,ask=101,updated_at=stamp,book=book)
            with self.subTest(book=book), \
                    patch.object(Reader, 'cached_reference_quote', cached, create=True), \
                    patch.object(self.monitor, '_submit') as submit, patch.object(self.monitor, 'publish'):
                self.monitor.poll([self.p])
                self.assertFalse(any(call.args[0] == key for call in submit.call_args_list))

    def test_corrected_cost_invalidates_inflight_old_profit_estimate(self):
        self.p.variational_venue, self.p.lighter_venue = 'lighter', 'arcus'
        generation = self.monitor._generation([self.p])
        self.monitor.positions = [self.p]
        self.monitor.generations['SUI'] = generation
        correction, old_profit = Future(), Future()
        self.monitor.jobs[('entry', 'SUI', self.p.id)] = (correction, generation)
        self.monitor.jobs[('grid', 'SUI', self.p.id)] = (old_profit, generation)
        correction.set_result({'lighter': {'venue':'arcus', 'price':99.9,
            'fees_usdc':None, 'source':'platform_order_history', 'order_ids':['order']}})
        old_profit.set_result({'net_pnl_usdc': 1})
        with patch.object(self.s, '_save') as save:
            self.monitor._collect()
        self.assertEqual(self.p.entry_price_lighter, 99.9)
        self.assertNotIn(('grid', 'SUI', self.p.id), self.monitor.values)
        save.assert_called_once()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.b = Reader('lighter'), Reader('arcus')
        self.s = ArbitrageStrategy({'state_path': str(self.root / 'state.json')}, self.a, self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)
        self.p = Position('SUI', 'long', 'sell', 1, 1, 100, 100, datetime.now(),
                          id='one', status='open', entry_size_variational=1, entry_size_lighter=1)
        self.s.positions = [self.p]
        self.monitor = PositionMonitor(self.s)
        self.s.position_monitor = self.monitor
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)

    def seed(self, kind, leg, value):
        self.monitor.generations['SUI'] = self.monitor._generation([self.p])
        self.monitor.values[(kind, 'SUI', leg)] = (
            value, time.monotonic(), self.monitor.generations['SUI'])

    def test_unknown_close_keeps_quote_and_account_reads_without_exit_estimates(self):
        self.p.status = 'reconcile_required'
        self.s.halted_reason = '平仓回执需核对：TimeoutError'
        with patch.object(self.monitor, '_submit') as submit:
            self.monitor.poll([self.p], wait_for_reads=False)
        self.assertEqual({call.args[0] for call in submit.call_args_list}, {
            ('quote', 'SUI', 'variational'), ('quote', 'SUI', 'lighter'),
            ('account', 'SUI', 'variational'), ('account', 'SUI', 'lighter')})
        payload = json.loads(self.monitor.path.read_text())
        self.assertTrue(payload['running'])
        self.assertTrue(payload['read_only'])
        self.assertTrue(payload['entry_paused'])
        self.assertEqual(payload['stop_reason'], self.s.halted_reason)
        self.assertEqual(payload['positions'][0]['status'], 'blocked')
        self.assertEqual(payload['positions'][0]['position_status'], 'reconcile_required')

    def test_pending_position_without_halt_does_not_start_background_order_reads(self):
        self.p.status = 'reconcile_required'
        with patch.object(self.monitor, '_submit') as submit:
            self.monitor.poll([self.p], wait_for_reads=False)
        submit.assert_not_called()

    def test_slow_accounts_do_not_block_quote_checks_or_allow_close(self):
        gate = threading.Event()
        self.a.account_gate = self.b.account_gate = gate
        self.s.live_blockers = Mock(return_value=[])
        self.s.close_reason = Mock(wraps=self.s.close_reason)
        self.s.close_position = Mock()
        self.s._save = Mock()
        worker = ExecutionWorker(self.s, ['OTHER'], self.root/'lock', self.root/'STOP', live=True)
        worker.stop_event.wait = Mock(return_value=False)
        started = time.monotonic()
        try:
            with patch.object(worker, '_attempt_candidates') as discover:
                worker.run(max_cycles=2)
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(self.s.close_reason.call_count, 2)
            self.s.close_position.assert_not_called()
            discover.assert_not_called()
            self.assertIsNotNone(self.monitor.checks['one']['last_success_at'])
            payload = read_position_monitor(self.monitor.path, os.getpid())
            self.assertEqual(payload['positions'][0]['account_status'], 'pending')
            self.assertTrue(payload['entry_paused'])
            started = time.monotonic()
            self.monitor.close()
            self.assertLess(time.monotonic() - started, .1)
        finally:
            gate.set()

    def test_late_account_result_cannot_be_used_after_position_quantity_changes(self):
        generation = self.monitor._generation([self.p])
        future = Future()
        self.monitor.generations['SUI'] = generation
        self.monitor.jobs[('account', 'SUI', 'variational')] = (future, generation)
        self.p.variational_size = .5
        self.monitor.generations['SUI'] = self.monitor._generation([self.p])
        future.set_result({'qty': 1, 'side': 'long', 'fetched_at': utc_now()})
        self.monitor._collect()
        self.assertNotIn(('account', 'SUI', 'variational'), self.monitor.values)
        with self.assertRaises(PositionReadUnavailable):
            self.monitor.accounts('SUI')

    def test_expired_quotes_are_rejected_even_if_read_completed_recently(self):
        quote = self.a.get_position_mark_snapshot('SUI')
        for leg in ('variational', 'lighter'):
            self.seed('quote', leg, quote)
        self.assertEqual(self.monitor.prices(self.p), (100, 100))
        quote['updated_at'] = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
        with self.assertRaisesRegex(PositionReadUnavailable, '过期'):
            self.monitor.prices(self.p)
        self.assertIsNone(self.s.close_reason(self.p))
        self.assertIn('过期', self.monitor.checks['one']['error'])

    def test_failed_check_preserves_last_success_and_stale_account_is_rejected(self):
        self.monitor.checked(self.p)
        success = self.monitor.checks['one']['last_success_at']
        self.monitor.checked(self.p, error=PositionReadUnavailable('持仓报价过期，平仓条件暂无法判断'))
        self.assertEqual(self.monitor.checks['one']['last_success_at'], success)
        for leg in ('variational', 'lighter'):
            self.seed('account', leg, {'qty': 1, 'fetched_at': '2000-01-01T00:00:00Z'})
        with self.assertRaises(PositionReadUnavailable):
            self.monitor.accounts('SUI')

    def test_heartbeat_is_bound_to_worker_and_stale_state_is_visible(self):
        self.monitor.poll([])
        self.assertIsNone(read_position_monitor(self.monitor.path, os.getpid() + 1))
        payload = json.loads(self.monitor.path.read_text())
        payload['updated_at'] = '2000-01-01T00:00:00Z'
        self.monitor.path.write_text(json.dumps(payload))
        self.assertTrue(read_position_monitor(self.monitor.path, os.getpid())['heartbeat_stale'])
        self.monitor.close()
        self.assertTrue(read_position_monitor(self.monitor.path, os.getpid())['heartbeat_stale'])

    def test_repeated_polls_keep_only_one_inflight_read_per_key(self):
        gate = threading.Event()
        self.a.account_gate = self.b.account_gate = gate
        try:
            for _ in range(4):
                self.monitor.poll([self.p])
            self.assertEqual(sum(key[0] == 'account' for key in self.monitor.jobs), 2)
            self.assertLessEqual(self.a.calls, 1)
            self.assertLessEqual(self.b.calls, 1)
        finally:
            gate.set()

    def test_three_platform_monitor_reads_recorded_hedge_instead_of_startup_pair(self):
        rh = Reader('lighter_rh')
        rh.position_snapshot = Mock(return_value={'qty': 0, 'side': None, 'fetched_at': utc_now()})
        self.s.configure_routes([(self.a, rh), (self.a, self.b), (rh, self.b)])
        self.p.variational_venue, self.p.lighter_venue = 'lighter', 'arcus'
        # Reproduce runtime startup before the winning route is known.
        self.s.activate_route(0)
        monitor = PositionMonitor(self.s)
        self.s.position_monitor = monitor
        self.s.activate_route(1)
        for _ in range(3):
            monitor.poll([self.p])
        first, second = monitor.accounts('SUI')
        self.assertEqual((first['qty'], second['qty']), (1, 1))
        self.assertEqual(self.s.reconcile_open_positions(), 'ok')
        self.assertIsNone(self.s.halted_reason)
        rh.position_snapshot.assert_not_called()
        payload = json.loads(monitor.path.read_text())
        self.assertEqual(payload['positions'][0]['legs']['lighter']['venue'], 'arcus')

    def test_route_change_invalidates_old_symbol_cache_and_late_reads(self):
        rh = Reader('lighter_rh')
        self.s.configure_routes([(self.a, self.b), (self.a, rh)])
        self.p.variational_venue, self.p.lighter_venue = 'lighter', 'arcus'
        monitor = PositionMonitor(self.s)
        monitor.positions = [self.p]
        generation = monitor._generation([self.p])
        monitor.generations['SUI'] = generation
        future = Future()
        monitor.jobs[('account', 'SUI', 'lighter')] = (future, generation)
        self.p.lighter_venue = 'lighter_rh'
        monitor.generations['SUI'] = monitor._generation([self.p])
        future.set_result({'qty': 1, 'side': 'short', 'fetched_at': utc_now()})
        monitor._collect()
        self.assertNotIn(('account', 'SUI', 'lighter'), monitor.values)
        with self.assertRaises(PositionReadUnavailable):
            monitor.accounts('SUI')

    def test_unmatched_or_missing_multi_platform_identity_never_reads_startup_pair(self):
        rh = Reader('lighter_rh')
        self.s.configure_routes([(self.a, self.b), (self.a, rh)])
        for names in ((None, None), ('lighter', 'unknown')):
            self.p.variational_venue, self.p.lighter_venue = names
            monitor = PositionMonitor(self.s)
            monitor.poll([self.p])
            self.assertEqual(monitor.jobs, {})
            with self.assertRaises(PositionReadUnavailable):
                monitor.accounts('SUI')
            self.assertTrue(monitor.entry_blocked)
            self.assertEqual(json.loads(monitor.path.read_text())['positions'][0]['status'], 'blocked')

    def test_slow_exit_funding_does_not_block_quote_poll_or_stop(self):
        gate = threading.Event()
        def estimate(position, **kwargs):
            gate.wait(2)
            return None
        try:
            with patch.object(self.s, '_single_exit_estimate', side_effect=estimate) as read:
                started = time.monotonic()
                self.monitor.poll([self.p])
                self.monitor.poll([self.p])
                self.assertLess(time.monotonic()-started, .3)
                self.assertEqual(read.call_count, 1)
                self.assertEqual(self.monitor.prices(self.p), (100, 100))
                self.monitor.close()
                self.assertLess(time.monotonic()-started, .3)
        finally:
            gate.set()

    def test_reconcile_failure_cannot_turn_into_success_on_next_throttled_tick(self):
        self.s.position_monitor = None
        self.a.position_snapshot = Mock(side_effect=RuntimeError('offline'))
        self.assertEqual(self.s.reconcile_open_positions(), 'unavailable')
        self.assertEqual(self.s.reconcile_open_positions(), 'unavailable')
        self.a.position_snapshot.assert_called_once()

    def test_account_failure_with_locked_diagnostics_keeps_monitor_alive(self):
        self.s.history.prepare_indexes()
        self.s.live_blockers = Mock(return_value=[])
        self.s.close_reason = Mock(return_value='价差收敛')
        self.s.close_position = Mock()
        self.monitor.poll = Mock()
        self.monitor.accounts = Mock(side_effect=PositionReadUnavailable('account pending'))
        worker = ExecutionWorker(self.s, ['SUI'], self.root/'lock', self.root/'STOP', live=True)
        worker._wait_cycle = Mock()
        lock = sqlite3.connect(self.s.history.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertLogs('src.execution.history', level='WARNING'), \
                    patch.object(worker, '_attempt_candidates') as discover:
                result = worker.run(max_cycles=2)
            self.assertLess(time.monotonic()-started, .5)
            self.assertEqual(result['positions'], 1)
            self.assertEqual(self.monitor.accounts.call_count, 2)
            self.s.close_position.assert_not_called()
            discover.assert_not_called()
            self.assertEqual(self.p.status, 'open')
            self.assertIsNone(self.s.halted_reason)
        finally:
            lock.rollback()
            lock.close()
        self.monitor.accounts.side_effect = None
        self.monitor.accounts.return_value = (
            dict(qty=1, side='long', fetched_at=utc_now()),
            dict(qty=1, side='short', fetched_at=utc_now()))
        self.assertEqual(self.s.reconcile_open_positions(), 'ok')
        self.monitor.accounts.side_effect = PositionReadUnavailable('account pending again')
        self.assertEqual(self.s.reconcile_open_positions(), 'unavailable')
        self.assertEqual(self.s.history.recent()[0]['event'], 'position_reconcile_failed')

    def test_recent_confirmed_entry_waits_for_lagging_account_without_halting(self):
        self.s._entry_position_settling[self.p.id] = time.monotonic()+5
        self.seed('account','variational',dict(qty=0,side=None,fetched_at=utc_now()))
        self.seed('account','lighter',dict(qty=1,side='short',fetched_at=utc_now()))
        self.assertEqual(self.s.reconcile_open_positions(), 'unavailable')
        self.assertEqual(self.p.status,'open')
        self.assertIsNone(self.s.halted_reason)
        self.assertNotIn(('account','SUI','variational'),self.monitor.values)
        self.seed('account','variational',dict(qty=1,side='long',fetched_at=utc_now()))
        self.seed('account','lighter',dict(qty=1,side='short',fetched_at=utc_now()))
        self.assertEqual(self.s.reconcile_open_positions(),'ok')
        self.assertNotIn(self.p.id,self.s._entry_position_settling)

    def test_persistent_mismatch_after_entry_window_remains_fail_closed(self):
        self.s._entry_position_settling[self.p.id] = time.monotonic()-1
        self.seed('account','variational',dict(qty=0,side=None,fetched_at=utc_now()))
        self.seed('account','lighter',dict(qty=1,side='short',fetched_at=utc_now()))
        self.assertEqual(self.s.reconcile_open_positions(),'halted')
        self.assertEqual(self.p.status,'reconcile_required')

    def test_opposite_or_excess_exposure_is_not_treated_as_entry_lag(self):
        self.s._entry_position_settling[self.p.id] = time.monotonic()+5
        self.seed('account','variational',dict(qty=1,side='short',fetched_at=utc_now()))
        self.seed('account','lighter',dict(qty=1,side='short',fetched_at=utc_now()))
        self.assertEqual(self.s.reconcile_open_positions(),'halted')
        self.assertEqual(self.p.status,'reconcile_required')

    def test_stopped_monitor_is_distinguished_from_missing_heartbeat(self):
        self.s.halted_reason='检测到账户持仓与程序记录不一致；必须先核对两边账户'
        self.monitor.poll([self.p])
        self.monitor.close()
        value=read_position_monitor(self.monitor.path,os.getpid())
        self.assertEqual(value['heartbeat_status'],'stopped')
        self.assertIn('持仓与程序记录不一致',value['stop_reason'])

    def test_single_validation_close_loop_also_polls_priority_monitor(self):
        self.s.reconcile_open_positions = Mock(return_value='ok')
        self.s.close_reason = Mock(return_value='价差收敛')
        self.s.close_position = Mock(side_effect=lambda p, **kw: setattr(p, 'status', 'closed'))
        worker = ExecutionWorker(self.s, ['SUI'], self.root/'lock', self.root/'STOP', live=True)
        with patch.object(self.monitor, 'poll', wraps=self.monitor.poll) as poll:
            self.assertTrue(worker._finish_single_cycle())
        poll.assert_called_once()
        self.s.close_position.assert_called_once_with(self.p, reason='价差收敛')

    def test_grid_estimate_cannot_renew_a_quote_that_expired_during_funding_read(self):
        self.seed('grid', self.p.id, {'net_pnl_usdc': 1, 'legs': {
            'variational': {'quote': {'updated_at': '2000-01-01T00:00:00Z'}}}})
        with self.assertRaisesRegex(PositionReadUnavailable, '过期'):
            self.monitor.grid_estimate(self.p)

    def test_closing_position_is_never_reported_as_no_position(self):
        self.monitor.poll([self.p])
        self.monitor.closing(self.p)
        self.p.status = 'closing'
        self.monitor.publish()
        data = read_position_monitor(self.monitor.path, os.getpid())
        self.assertEqual(data['positions'][0]['phase'], 'closing')
        self.assertEqual(data['positions'][0]['position_status'], 'closing')
