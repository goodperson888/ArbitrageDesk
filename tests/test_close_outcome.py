import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.execution.history import TradeHistory
from src.web.close_diagnostics import attach_close_diagnostics
from src.web.close_outcome import close_outcome, read_close_events, CLOSE_EVENTS_SQL, CLOSE_VERSION_SQL
from src.web.history_cache import ProjectionCache, enrich_history


class CloseOutcomeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.history = TradeHistory(self.root/'hedge_events.sqlite')
        self.history.prepare_indexes()
        self.row = dict(id='vvv', status='closed', hedge_status='paired',
            variational_venue='lighter', lighter_venue='lighter_rh',
            entry_size_variational=.59, entry_size_lighter=.59,
            close_price_variational=20.2644, close_price_lighter=20.276,
            realized_pnl_usdc=-.014809, close_reason='价差收敛且达到盈利')
        self.decisions = [dict(pre_submit=dict(allowed=True, net_pnl_usdc=.007027431,
            legs=dict(lighter=dict(close_side='buy', bound=20.2100412))))]

    def events(self):
        return read_close_events(self.history.path, ['vvv']).get('vvv', [])

    def add_receipt(self, venue, order_id, status, qty, price=None, **extra):
        self.history.append('close_receipt', position_id='vvv', venue=venue, order_id=order_id,
                            confirmed=True, status=status, filled_size=qty, average_price=price, **extra)

    def test_original_profitable_trigger_and_residual_loss_are_both_explicit(self):
        self.add_receipt('lighter_rh', 'zero', 'canceled', 0)
        self.add_receipt('lighter', 'core', 'filled', .59, 20.2644)
        self.history.append('close_residual_retry', position_id='vvv')
        self.add_receipt('lighter_rh', 'retry', 'filled', .59, 20.276)
        outcome = close_outcome(self.row, self.decisions, self.events())
        self.assertEqual(outcome['summary'], '补平后亏损')
        self.assertIn('0.007027431 USDC', outcome['text'])
        self.assertIn('-0.014809 USDC', outcome['text'])
        self.assertIn('平仓记录1：canceled；成交 0', outcome['text'])
        self.assertIn('平仓记录2：filled；成交 0.59 @ 20.276', outcome['text'])
        self.assertIn('成交损益影响 -0.038915692 USDC', outcome['text'])
        self.assertIn('补平不再等待整组盈利', outcome['text'])
        self.assertNotIn('行情过期', outcome['text'], 'cancellation alone does not prove stale market data')
        self.assertEqual(self.row['close_reason'], '价差收敛且达到盈利')

    def test_unknown_receipts_and_duplicate_events_do_not_manufacture_retry(self):
        self.add_receipt('lighter_rh', 'one', 'filled', .59, 20.276)
        self.add_receipt('lighter_rh', 'one', 'filled', .59, 20.276)
        self.history.append('close_receipt', position_id='vvv', venue='lighter_rh',
                            order_id='unknown', confirmed=False, status='unknown')
        outcome = close_outcome(self.row, self.decisions, self.events())
        self.assertFalse(outcome['residual_retry'])
        self.assertEqual(outcome['summary'], '成交后收益转负')
        self.assertNotIn('平仓记录2', outcome['text'])

    def test_legacy_evidence_does_not_invent_trigger_and_positive_retry_is_not_a_loss(self):
        self.add_receipt('lighter_rh', 'zero', 'canceled', 0)
        self.history.append('close_residual_retry', position_id='vvv')
        self.add_receipt('lighter_rh', 'retry', 'filled', .59, 20.276)
        book = dict(closed_positions=[dict(self.row)])
        attach_close_diagnostics(book, self.root)
        diagnostic = book['closed_positions'][0]['close_diagnostics']
        self.assertIn('不能用平台成交价倒推', diagnostic['text'])
        self.assertEqual(diagnostic['outcome']['summary'], '补平后亏损')
        self.assertIsNone(diagnostic['outcome']['estimated_net_pnl_usdc'])
        outcome = close_outcome(dict(self.row, realized_pnl_usdc=.01), [], self.events())
        self.assertEqual(outcome['summary'], '含残余补平')
        outcome = close_outcome(dict(self.row, realized_pnl_usdc=None), [], self.events())
        self.assertEqual(outcome['summary'], '含残余补平', 'unsettled PnL is never labeled a loss')

    def test_position_index_bounds_reads_even_with_unrelated_events(self):
        with sqlite3.connect(self.history.path) as db:
            db.executemany('INSERT INTO events(timestamp,event,detail) VALUES(?,?,?)',
                           [('now', 'stage_completed', '{}')] * 2000)
            plan = db.execute('EXPLAIN QUERY PLAN ' + CLOSE_EVENTS_SQL, ('vvv',)).fetchall()
        self.assertIn('idx_events_position_id', str(plan))
        self.assertNotIn('SCAN events', str(plan))
        self.add_receipt('lighter_rh', 'zero', 'canceled', 0)
        self.assertEqual(len(self.events()), 1)

    def test_same_position_with_many_non_close_events_still_has_bounded_reads(self):
        import json
        self.add_receipt('lighter_rh', 'old', 'filled', .59, 20.276)
        with sqlite3.connect(self.history.path) as db:
            db.executemany('INSERT INTO events(timestamp,event,detail) VALUES(?,?,?)',
                           [('now', 'stage_completed', json.dumps(dict(position_id='vvv')))] * 5000)
            operations = []
            def bound():
                operations.append(1)
                return int(len(operations) > 150)
            db.set_progress_handler(bound, 100)
            self.assertEqual(db.execute(CLOSE_EVENTS_SQL, ('vvv',)).fetchall(), [])
            operations.clear()
            self.assertIsNone(db.execute(CLOSE_VERSION_SQL, ('vvv',)).fetchone()[0])
        self.assertEqual(self.events(), [], 'old unreturned evidence is unknown; it is never fabricated')

    def test_new_residual_receipts_invalidate_explanation_without_diagnostic_write(self):
        cache = ProjectionCache()
        def read():
            book = dict(closed_positions=[copy.deepcopy(self.row)])
            enrich_history(book, self.root, cache)
            return book['closed_positions'][0]['close_diagnostics']['outcome']
        self.assertEqual(read()['summary'], '')
        self.add_receipt('lighter_rh', 'zero', 'canceled', 0)
        self.history.append('close_residual_retry', position_id='vvv')
        self.add_receipt('lighter_rh', 'retry', 'filled', .59, 20.276)
        self.assertEqual(read()['summary'], '补平后亏损')

    def test_repeated_zero_fill_batches_do_not_prove_residual_risk_cleanup(self):
        for venue in ('lighter', 'lighter_rh'):
            self.add_receipt(venue, venue+'-zero', 'canceled', 0)
        for venue in ('lighter', 'lighter_rh'):
            self.add_receipt(venue, venue+'-filled', 'filled', .59, 20.26)
        outcome = close_outcome(self.row, self.decisions, self.events())
        self.assertFalse(outcome['residual_retry'])
        self.assertEqual(outcome['summary'], '多次尝试后亏损')
        self.assertNotIn('补平不再等待', outcome['text'])


if __name__ == '__main__':
    unittest.main()
