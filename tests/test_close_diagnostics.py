import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.common.market_data import utc_now
from src.execution.close_diagnostics import CloseDiagnostics, DiagnosticWriter, read_close_decisions, quote_snapshot
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position
from src.web.close_diagnostics import attach_close_diagnostics
from test_spread_grid import GridTrader


class SnapshotTrader(GridTrader):
    def __init__(self, price, venue):
        super().__init__(price, venue)
        self.mark_reads = 0

    def get_position_mark_snapshot(self, symbol):
        self.mark_reads += 1
        return dict(bid=self.price - .001, ask=self.price + .001, updated_at=utc_now(),
                    source='test_websocket', account_key='must-not-be-recorded')


class CloseDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.writer = DiagnosticWriter(capacity=8)
        self.addCleanup(self.writer.close)
        self.a, self.b = SnapshotTrader(100, 'lighter'), SnapshotTrader(100, 'arcus')
        self.a.exposure, self.b.exposure = -1, 1
        self.s = ArbitrageStrategy({'mode': 'live', 'state_path': str(self.root/'state.json'),
            'funding_accounting_mode': 'defer_settlement',
            'risk': {'slippage_tolerance': 10, 'execution_price_movement_bps': 2,
                     'max_loss_per_trade': None},
            'close_policy': {'mode': 'convergence', 'target_mode': 'center',
                             'convergence_bps': 2, 'max_hold_seconds': 3600}}, self.a, self.b)
        self.s.close_diagnostics = CloseDiagnostics(self.root/'close_diagnostics.sqlite', self.writer)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)
        self.p = Position('SUI', 'short', 'buy', 1, 1, 100.3, 100, datetime.now(),
            status='open', entry_size_variational=1, entry_size_lighter=1,
            variational_venue='lighter', lighter_venue='arcus', execution_id='entry-test',
            entry_fees_usdc=.01, realized_pnl_usdc=-.01)
        self.s.positions.append(self.p)

    def rows(self):
        self.assertTrue(self.writer.flush(1))
        return read_close_decisions(self.s.close_diagnostics.path, [self.p.id])[self.p.id]

    def test_quote_clock_source_and_sequence_are_retained_but_private_payload_is_not(self):
        q = dict(bid=99, ask=100, updated_at=utc_now(), source='lighter_websocket',
                 received_at=utc_now(), exchange_at=utc_now(), time_basis='exchange',
                 market_id=8, offset=100, nonce=10, account='secret', sizing_book={'raw': 'secret'})
        snapshot = quote_snapshot(q)
        self.assertEqual(snapshot, {k: v for k, v in q.items() if k not in ('account', 'sizing_book')})

    def test_exact_trigger_and_pre_submit_reads_remain_distinct_and_allowlisted(self):
        self.a.price = 100.1
        self.s._route_reversion_stats = lambda *args: dict(center_bps=10, samples=123)
        with patch.object(self.a, 'get_executable_quote', wraps=self.a.get_executable_quote) as quotes:
            self.assertEqual(self.s.close_reason(self.p), '价差收敛')
            self.assertEqual(quotes.call_count, 1, 'capturing the trigger adds no quote request')
        self.assertFalse(self.s.close_diagnostics.path.exists(), 'polling only captures memory')
        self.a.price, self.b.price = 100.01, 100.01
        with patch.object(self.a, 'get_executable_quote', wraps=self.a.get_executable_quote) as quotes:
            self.assertTrue(self.s.close_position(self.p, '价差收敛'))
            self.assertEqual(quotes.call_count, 1, 'diagnostics add no send-time re-quote')
        self.assertEqual((self.a.mark_reads, self.b.mark_reads), (1, 1))
        row = self.rows()[0]
        self.assertEqual(row['execution_id'], 'entry-test')
        trigger, check = row['trigger'], row['pre_submit']
        self.assertEqual(trigger['center_bps'], 10)
        self.assertEqual(trigger['center_samples'], 123)
        self.assertEqual(trigger['effective_target'], 'center')
        self.assertLess(trigger['distance_to_center_bps'], .02)
        self.assertAlmostEqual(trigger['legs']['variational']['mid'], 100.1)
        self.assertAlmostEqual(trigger['legs']['variational']['bid'], 100.099)
        self.assertEqual(check['legs']['variational']['reference_price'], 100.01)
        self.assertTrue(check['allowed'])
        self.assertGreater(check['net_pnl_usdc'], 0)
        self.assertNotIn('must-not-be-recorded', json.dumps(row))
        self.assertNotIn('account_key', json.dumps(row))
        overview = dict(closed_positions=[dict(id=self.p.id, status='closed',
            close_price_variational=100.01, close_price_lighter=100.01, close_spread_bps=0,
            realized_pnl_usdc=.2)])
        attach_close_diagnostics(overview, self.root)
        text = overview['closed_positions'][0]['close_diagnostics']['text']
        self.assertIn('触发盘口 Bid / Ask', text)
        self.assertIn('发送前复核时间', text)
        self.assertIn('实际平仓成交均价', text)

    def test_missing_center_fallback_and_deferred_recheck_are_explicit(self):
        self.s._route_reversion_stats = lambda *args: dict(center_bps=None, samples=0)
        self.assertEqual(self.s.close_reason(self.p), '价差收敛')
        self.b.fee_bps = 100
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        row = self.rows()[0]
        self.assertEqual(row['trigger']['effective_target'], 'zero_fallback_missing_center')
        self.assertIsNone(row['trigger']['center_bps'])
        self.assertFalse(row['pre_submit']['allowed'])
        self.assertLess(row['pre_submit']['net_pnl_usdc'], 0)
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_monitor_capture_reuses_exact_cached_books_without_extra_market_reads(self):
        from src.execution.position_monitor import PositionMonitor
        monitor = PositionMonitor(self.s)
        self.s.position_monitor = monitor
        monitor.generations['SUI'] = monitor._generation([self.p])
        for leg in ('variational', 'lighter'):
            monitor.values[('quote', 'SUI', leg)] = (
                dict(bid=99.99, ask=100.01, updated_at=utc_now(), source='ws'),
                time.monotonic(), monitor.generations['SUI'])
        monitor.single_estimate = lambda p: self.s._single_monitor_exit_estimate(p)
        self.s._route_reversion_stats = lambda *args: dict(center_bps=0, samples=50)
        self.assertEqual(self.s.close_reason(self.p), '价差收敛')
        self.assertEqual((self.a.mark_reads, self.b.mark_reads), (0, 0))
        self.assertEqual(self.s._close_signals[self.p.id]['legs']['lighter']['bid'], 99.99)

    def test_no_signal_is_fabricated_for_direct_close_and_repeated_polling_is_not_logged(self):
        for _ in range(10):
            self.assertEqual(self.s.close_reason(self.p), '价差收敛')
        self.assertEqual(self.writer.queue.unfinished_tasks, 0)
        self.s.close_mode = 'disabled'
        self.assertIsNone(self.s.close_reason(self.p))
        self.assertTrue(self.s.close_position(self.p, '盈利条件'))
        self.assertIsNone(self.rows()[0]['trigger'])

    def test_slow_diagnostic_disk_cannot_block_either_order_transport(self):
        writing, release = threading.Event(), threading.Event()
        def blocked_write(*args):
            writing.set()
            release.wait(2)
        try:
            with patch.object(self.writer, '_write', side_effect=blocked_write):
                self.assertEqual(self.s.close_reason(self.p), '价差收敛')
                started = time.monotonic()
                self.assertTrue(self.s.close_position(self.p, '价差收敛'))
                self.assertLess(time.monotonic()-started, .5)
                self.assertTrue(writing.wait(.5))
                self.assertFalse(release.is_set())
                self.assertEqual(self.p.status, 'closed')
                self.assertEqual(len(self.a.closes), 1)
                self.assertEqual(len(self.b.closes), 1)
                release.set()
                self.assertTrue(self.writer.flush(1))
        finally:
            release.set()

    def test_saturated_queue_and_failed_writer_never_block_or_change_trades(self):
        writing, release = threading.Event(), threading.Event()
        def blocked_write(*args):
            writing.set()
            release.wait(2)
            raise OSError('disk unavailable')
        try:
            with patch.object(self.writer, '_write', side_effect=blocked_write):
                self.s.close_diagnostics.record(position_id='test')
                self.assertTrue(writing.wait(.5))
                started = time.monotonic()
                for _ in range(50):
                    self.s.close_diagnostics.record(position_id='test', account='secret')
                self.assertLess(time.monotonic()-started, .1)
                self.assertGreater(self.writer.dropped, 0)
                self.assertTrue(self.s.close_position(self.p, '价差收敛'))
                release.set()
                self.assertTrue(self.writer.flush(1))
                self.assertGreater(self.writer.failed, 0)
        finally:
            release.set()

    def test_missing_old_evidence_is_not_reconstructed_from_fills(self):
        overview = dict(closed_positions=[dict(id='legacy', status='closed',
            close_price_variational=100, close_price_lighter=100, close_spread_bps=0)])
        attach_close_diagnostics(overview, self.root)
        diagnostic = overview['closed_positions'][0]['close_diagnostics']
        self.assertEqual(diagnostic['decisions'], [])
        self.assertIn('不能用平台成交价倒推', diagnostic['text'])

    def test_max_hold_exit_records_duration_without_profit_or_extra_quote_read(self):
        self.p.open_time = datetime.now() - timedelta(hours=2)
        with patch.object(self.s,'_single_exit_estimate',side_effect=AssertionError('extra quote')):
            reason = self.s.close_reason(self.p)
            self.assertEqual(reason,'最长持仓保护')
            self.assertTrue(self.s.close_position(self.p,reason))
        row = self.rows()[0]
        self.assertGreaterEqual(row['trigger']['held_seconds'],7200)
        self.assertEqual(row['pre_submit']['max_hold_seconds'],3600)
        self.assertTrue(row['pre_submit']['protective_exit'])
        overview = dict(closed_positions=[dict(id=self.p.id,status='closed')])
        attach_close_diagnostics(overview,self.root)
        self.assertIn('风险保护退出，允许亏损平仓',overview['closed_positions'][0]['close_diagnostics']['text'])

    def test_diagnostic_sqlite_lock_is_isolated_from_intents_receipts_and_transports(self):
        self.s.close_diagnostics.record(position_id='seed')
        self.assertTrue(self.writer.flush(1))
        with sqlite3.connect(self.s.close_diagnostics.path) as diagnostic_db:
            diagnostic_db.execute('BEGIN IMMEDIATE')
            self.assertEqual(self.s.close_reason(self.p), '价差收敛')
            started = time.monotonic()
            self.assertTrue(self.s.close_position(self.p, '价差收敛'))
            self.assertLess(time.monotonic()-started, .5)
            receipts = [event for event in self.s.history.recent() if event['event'] == 'close_receipt']
            self.assertEqual(len(receipts), 2, 'durable receipts use their original independent database')


if __name__ == '__main__':
    unittest.main()
