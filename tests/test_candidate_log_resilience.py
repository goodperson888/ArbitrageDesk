"""Rejected candidates cannot strand a held hedge because SQLite is busy."""
import sqlite3
import unittest
from unittest.mock import patch

import test_multi_position_execution as portfolio


class CandidateLogResilienceTests(unittest.TestCase):
    setUp = portfolio.MultiPositionTests.setUp
    opportunity = portfolio.MultiPositionTests.opportunity
    enter = portfolio.MultiPositionTests.enter
    worker = portfolio.MultiPositionTests.worker

    def test_rejected_next_entry_cannot_exit_worker_with_existing_position(self):
        held = self.enter('BTC')
        worker = self.worker()
        self.a.price = self.b.price = 100
        before = (len(self.a.sends),len(self.b.sends))
        self.s.history._diagnostic_warned_at = None
        database = sqlite3.connect(self.s.history.path)
        try:
            database.execute('BEGIN EXCLUSIVE')
            with self.assertLogs('src.execution.history',level='WARNING'), \
                    patch.object(worker,'_candidate_opportunities',return_value=[self.opportunity('ETH')]), \
                    patch.object(self.s,'close_reason',return_value=None) as monitored, \
                    patch.object(self.s,'reconcile_open_positions',return_value='ok'), \
                    patch.object(worker.stop_event,'wait'):
                result = worker.run(max_cycles=2)
            self.assertIsInstance(result,dict)
            self.assertEqual(monitored.call_count,2,'the held position is still monitored next cycle')
            self.assertEqual(held.status,'open')
            self.assertIsNone(self.s.halted_reason)
            self.assertEqual((len(self.a.sends),len(self.b.sends)),before)
            self.assertEqual(self.s.history.diagnostic_storage['last_error'],'sqlite_busy')
        finally:
            database.rollback(); database.close()

    def test_storage_io_error_during_rejection_is_redacted_and_nonfatal(self):
        self.s.history.prepare_indexes()
        self.a.price = self.b.price = 100
        with patch('src.execution.history.sqlite3.connect',side_effect=sqlite3.OperationalError('disk I/O error')), \
                self.assertLogs('src.execution.history',level='WARNING'):
            self.assertFalse(self.s.execute_arbitrage(self.opportunity('ETH')))
        self.assertIsNone(self.s.halted_reason)
        self.assertFalse(self.a.sends or self.b.sends)
        self.assertEqual(self.s.history.diagnostic_storage['last_error'],'sqlite_io')
