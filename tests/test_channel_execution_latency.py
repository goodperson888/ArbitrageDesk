"""Real orders remain guarded while optional GETs leave the send path."""
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.common.live_feed import SocketFeed
from src.common.market_data import utc_now
from src.execution.close_accounting import ClosedFundingReads
from src.execution.journal import OrderJournal
from src.execution.position_monitor import PositionMonitor, PositionReadUnavailable
from src.execution.worker import ExecutionWorker
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position
from test_spread_grid import GridTrader


class ExecutionLatencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.a, self.b = GridTrader(100, 'lighter'), GridTrader(100, 'arcus')
        self.s = ArbitrageStrategy({'state_path': str(self.root/'state.json'),
            'mode':'live', 'funding_accounting_mode':'defer_settlement',
            'risk':{'max_loss_per_trade':None},
            'close_policy':{'mode':'convergence','target_mode':'zero'}}, self.a,self.b)
        self.p = Position('SUI','short','buy',1,1,100.5,100,datetime.now(),
            entry_size_variational=1,entry_size_lighter=1,status='open',
            variational_venue='lighter',lighter_venue='arcus',realized_pnl_usdc=0)
        self.a.exposure,self.b.exposure=-1,1
        self.s.positions=[self.p]
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service,self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush,1)

    def test_final_funding_guard_never_waits_on_background_network(self):
        entered,release=threading.Event(),threading.Event()
        def hold():
            with self.s._exit_funding_lock:
                entered.set(); release.wait(2)
        thread=threading.Thread(target=hold); thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started=time.monotonic()
            with self.assertRaises(PositionReadUnavailable):
                self.s._single_exit_funding(self.p,allow_refresh=False)
            self.assertLess(time.monotonic()-started,.1)
        finally:
            release.set(); thread.join(1)

    def test_missing_funding_defers_without_get_and_complete_cache_is_reused(self):
        with patch.object(self.a,'reconcile_funding',wraps=self.a.reconcile_funding) as read:
            with self.assertRaises(PositionReadUnavailable):
                self.s._single_exit_funding(self.p,allow_refresh=False)
            read.assert_not_called()
            self.s._single_exit_funding(self.p)
            self.s._single_exit_funding(self.p,allow_refresh=False)
            self.assertEqual(read.call_count,1)

    def test_old_rest_completion_cannot_replace_newer_native_quote(self):
        monitor=PositionMonitor(self.s)
        generation=monitor._generation([self.p]); monitor.generations['SUI']=generation
        key=('quote','SUI','variational')
        current=dict(bid=100,ask=101,updated_at=utc_now())
        monitor.values[key]=(current,time.monotonic(),generation)
        future=Future();future.set_result(dict(bid=90,ask=91,
            updated_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()))
        monitor.jobs[key]=(future,generation);monitor._collect()
        self.assertEqual(monitor.values[key][0],current)

    def test_stream_tick_does_not_multiply_disk_telemetry_and_stop_is_immediate(self):
        monitor=PositionMonitor(self.s)
        with patch.object(monitor,'_publish') as publish, \
                patch('src.execution.position_monitor.time.monotonic',return_value=100):
            for _ in range(20):monitor.publish_tick()
            self.assertEqual(publish.call_count,1)
            monitor.close()
            self.assertEqual(publish.call_count,2)

    def test_quote_wakeups_are_symbol_scoped_and_unregister_on_flat(self):
        feed=SocketFeed('wss://fixture.invalid');event=threading.Event()
        feed.watch_quotes('SUI',event);feed._quote_changed('BTC')
        self.assertFalse(event.is_set())
        feed._quote_changed('SUI');self.assertTrue(event.is_set())
        feed.unwatch_quotes('SUI',event);event.clear();feed._quote_changed('SUI')
        self.assertFalse(event.is_set())
        self.a._reference_feed=feed
        worker=ExecutionWorker(self.s,[]);worker.stop_event.wait=Mock(return_value=False)
        worker.quote_event.wait=Mock(return_value=True)
        worker._wait_cycle();self.assertIn((feed,'SUI'),worker._quote_watches)
        self.p.status='closed';worker._wait_cycle()
        self.assertEqual(worker._quote_watches,set());self.assertEqual(feed._quote_watchers,{})

    def test_slow_settlement_is_bounded_and_only_coordinator_applies_once(self):
        release=threading.Event()
        def slow(*args):
            release.wait(2);return {'complete':True,'funding_usdc':.02}
        self.p.status='closed';self.p.closed_time=datetime.now()
        self.p.pending_funding_venues=['variational','lighter'];self.p.accounting_pending=True
        reads=ClosedFundingReads()
        with patch.object(self.a,'reconcile_funding',side_effect=slow), \
                patch.object(self.b,'reconcile_funding',side_effect=slow):
            try:
                started=time.monotonic();reads.poll(self.s);reads.poll(self.s)
                self.assertLess(time.monotonic()-started,.1)
                self.assertEqual(len(reads.jobs),2)
                self.assertEqual(self.p.realized_pnl_usdc,0)
                futures=[value[0] for value in reads.jobs.values()]
                release.set()
                for f in futures:f.result(timeout=1)
                self.assertEqual(self.p.realized_pnl_usdc,0,'GET threads cannot mutate trading state')
                reads.poll(self.s);reads.poll(self.s)
                self.assertAlmostEqual(self.p.realized_pnl_usdc,.04)
                self.assertEqual(self.p.pending_funding_venues,[])
                self.assertFalse(self.p.accounting_pending)
            finally:release.set()

    def test_close_receipts_do_not_wait_for_post_close_settlement(self):
        # Prewarm the final profit gate; the following GET must only be needed
        # for optional accounting after both reduce-only receipts.
        self.s._single_exit_estimate(self.p)
        self.s.position_monitor=PositionMonitor(self.s)
        with patch.object(self.a,'reconcile_funding',side_effect=AssertionError('slow GET on send path')), \
                patch.object(self.b,'reconcile_funding',side_effect=AssertionError('slow GET on send path')):
            self.assertTrue(self.s.close_position(self.p,'价差收敛'))
        self.assertEqual(self.p.status,'closed')
        self.assertEqual(len(self.a.closes),1);self.assertEqual(len(self.b.closes),1)
        self.assertIsNotNone(self.p.close_execution_ms)

    def test_durable_journal_closes_connections_and_reader_does_not_block(self):
        journal=OrderJournal(self.root/'orders.sqlite')
        opened=[];original=journal.connect
        def connect():
            db=original();opened.append(db);return db
        with closing(original()) as reader, patch.object(journal,'connect',side_effect=connect):
            reader.execute('BEGIN');reader.execute('SELECT * FROM orders').fetchall()
            row,_=journal.reserve('lighter','test','one',{'size':1})
            journal.finish(row['seq'],{'confirmed':True,'filled_size':1,'fees_usdc':0})
            self.assertEqual(journal.get(row['seq'])['state'],'terminal')
            self.assertEqual(reader.execute('SELECT * FROM orders').fetchall(),[])
        import sqlite3
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
