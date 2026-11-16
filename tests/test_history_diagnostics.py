"""Optional account-read diagnostics must not stop a position monitor."""
import sqlite3
import tempfile
import time
import unittest
from src.execution.telemetry import stage
from pathlib import Path
from unittest.mock import patch

from src.execution.history import TradeHistory


class HistoryDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'events.sqlite'
        self.history = TradeHistory(self.path)
        self.history.prepare_indexes()

    def test_locked_database_skips_diagnostic_without_waiting_for_lock(self):
        lock = sqlite3.connect(self.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertLogs('src.execution.history', level='WARNING') as logs:
                written = self.history.append_diagnostic('position_reconcile_failed',
                    symbol='BMNR', position_id='p', reason='PositionReadUnavailable')
            self.assertFalse(written)
            self.assertLess(time.monotonic() - started, .25)
            self.assertIn('sqlite_busy', logs.output[0])
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(self.history.recent(), [])
        self.assertTrue(self.history.append_diagnostic('position_reconcile_failed',
            symbol='BMNR', position_id='p', reason='PositionReadUnavailable'))
        self.assertEqual(self.history.recent()[0]['detail']['symbol'], 'BMNR')

    def test_missing_diagnostic_store_does_not_create_or_migrate_database(self):
        absent = self.path.parent / 'absent.sqlite'
        with self.assertLogs('src.execution.history', level='WARNING'):
            self.assertFalse(TradeHistory(absent).append_diagnostic(
                'position_reconcile_failed', reason='PositionReadUnavailable'))
        self.assertFalse(absent.exists())

    def test_wal_reader_does_not_block_diagnostic_or_durable_receipt(self):
        reader = sqlite3.connect(self.path)
        try:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM events').fetchall()
            self.assertTrue(self.history.append_diagnostic('position_reconcile_failed',
                symbol='BMNR', reason='PositionReadUnavailable'))
            self.history.append('close_receipt', symbol='BMNR', filled_size=1)
            self.assertEqual(reader.execute('SELECT * FROM events').fetchall(), [])
            self.assertEqual(len(self.history.recent()), 2)
        finally:
            reader.rollback()
            reader.close()
        self.assertEqual(self.history.recent()[0]['event'], 'close_receipt')

    def test_unclassified_storage_error_does_not_expose_exception_message(self):
        with patch('src.execution.history.sqlite3.connect',
                   side_effect=sqlite3.OperationalError('private request body')):
            with self.assertLogs('src.execution.history', level='WARNING') as logs:
                self.assertFalse(self.history.append_diagnostic('position_reconcile_failed'))
        self.assertNotIn('private request body', logs.output[0])

    def test_trade_events_cannot_use_optional_path(self):
        for event in ('entry_receipt', 'close_receipt', 'position_closed', 'order_intent'):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.history.append_diagnostic(event, symbol='BMNR')

    def test_normal_trade_writes_still_raise_storage_errors(self):
        for event in ('entry_receipt', 'close_receipt'):
            with self.subTest(event=event), patch.object(self.history, '_append_rows',
                    side_effect=sqlite3.OperationalError('database is locked')):
                with self.assertRaises(sqlite3.OperationalError):
                    self.history.append(event, symbol='BMNR', filled_size=1)

    def test_optional_record_retains_execution_identity_and_filters_fields(self):
        with self.history.execution_scope('execution-one'):
            self.assertTrue(self.history.append_diagnostic('position_reconcile_failed',
                symbol='BMNR', reason='PositionReadUnavailable', private_key='secret'))
        detail = self.history.recent()[0]['detail']
        self.assertEqual(detail['execution_id'], 'execution-one')
        self.assertNotIn('private_key', detail)

    def test_candidate_and_timing_logs_skip_locked_database_without_waiting(self):
        lock = sqlite3.connect(self.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertLogs('src.execution.history',level='WARNING') as logs:
                for event in ('blocked','quote_evaluation','stage_started','stage_completed'):
                    self.assertFalse(self.history.append(event,symbol='GME',reason='未达门槛'))
                with self.assertRaisesRegex(ValueError,'original validation'):
                    with stage(self.history,'生成双腿计划','GME'):
                        raise ValueError('original validation')
            self.assertLess(time.monotonic()-started,.25)
            self.assertEqual(len(logs.output),1,'repeated busy logs must not flood stderr')
            self.assertEqual(self.history.diagnostic_storage['skipped_rows'],5)
            self.assertEqual(self.history.diagnostic_storage['last_error'],'sqlite_busy')
        finally:
            lock.rollback(); lock.close()
        self.history.append('entry_receipt',symbol='GME',filled_size=1)
        self.assertEqual(self.history.recent()[0]['event'],'entry_receipt')

    def test_buffered_preparation_skips_busy_commit_but_never_drops_receipts(self):
        lock = sqlite3.connect(self.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            with self.assertLogs('src.execution.history',level='WARNING'):
                with self.history.buffered():
                    self.history.append('quote_evaluation',symbol='GME')
                    self.history.append('blocked',symbol='GME')
            with patch.object(self.history,'_append_rows',side_effect=sqlite3.OperationalError('critical failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    with self.history.buffered():
                        self.history.append('close_receipt',symbol='BMNR',filled_size=1)
        finally:
            lock.rollback(); lock.close()

    def test_write_and_read_connections_are_closed_without_gc(self):
        connections=[]
        original = sqlite3.connect
        class Tracked(sqlite3.Connection):
            closed = False
            def close(self):
                self.closed=True
                return super().close()
        def connect(*args,**kwargs):
            db=original(*args,**kwargs,factory=Tracked);connections.append(db);return db
        with patch('src.execution.history.sqlite3.connect',side_effect=connect):
            self.history.append('entry_receipt',symbol='GME',filled_size=1)
            self.history.append('blocked',symbol='GME')
            self.history.recent(); self.history.count()
        self.assertEqual(len(connections),4)
        self.assertTrue(all(db.closed for db in connections))


if __name__ == '__main__':
    unittest.main()
