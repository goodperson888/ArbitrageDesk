import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.execution.history import TradeHistory
from src.execution.journal import OrderJournal
from src.execution.history_receipts import receipt_evidence, merge_receipts, historical_market_id
from src.execution.history_recovery import apply_close_history


class HistoryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.history = TradeHistory(self.root/'hedge_events.sqlite')
        self.journal = OrderJournal(self.root/'execution_orders.sqlite')
        for number, closing in enumerate((False, True)):
            order_id = 'close' if closing else 'open'
            intent = dict(symbol='PUMP', side='buy' if closing else 'sell', size='2',
                          market_index=45, reduce_only=closing)
            row, _ = self.journal.reserve('lighter', 'test-account', 'p-'+order_id, intent)
            self.journal.finish(row['seq'], dict(confirmed=True, status='filled', filled_size=2,
                average_price=100, order_id=order_id, fees_verified=True, fees_usdc=0))
            self.history.append('close_receipt' if closing else 'entry_receipt',
                position_id='p', venue='lighter', order_id=order_id, filled_size=2)

    def test_reuses_confirmed_identity_without_inventing_exchange_fill_time(self):
        rows = receipt_evidence(self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['fees_usdc'], '0.0')
        self.assertNotIn('first_fill_at', rows[0])
        position = dict(id='p', symbol='PUMP', status='reconcile_required',
            variational_venue='lighter', lighter_venue='arcus',
            variational_side='short', lighter_side='buy',
            entry_order_ids=['open'], entry_order_ids_by_venue={'lighter':['open']},
            close_order_ids_by_venue={'lighter':['close']},
            variational_size=2, lighter_size=0, entry_size_variational=2, entry_size_lighter=0)
        book = {'open_positions':[position]}
        apply_close_history(book, self.root)
        self.assertEqual(book['open_positions'], [])
        row = book['closed_positions'][0]
        self.assertEqual(row['status'], 'closed')
        self.assertEqual(row['close_price_variational'], 100)
        self.assertIsNone(row['realized_pnl_usdc'], 'receipt arrival cannot prove a funding interval')
        self.assertIn('lighter 持仓期间资金费未完整读取', row['accounting_missing'])

    def test_unconfirmed_receipt_cannot_supply_a_close(self):
        with self.journal.connect() as db:
            db.execute("UPDATE orders SET result=? WHERE client_id='p-close'", (json.dumps(dict(
                confirmed=False, status='unknown', filled_size=2, average_price=100, order_id='close')),))
        self.assertEqual([r['order_id'] for r in receipt_evidence(self.root)], ['open'])

    def test_platform_fill_times_win_and_economic_mismatch_never_borrows_fees(self):
        row = receipt_evidence(self.root)[0]
        official = dict(row, timestamp='2026-11-01T00:00:00+00:00',
                        first_fill_at='2026-11-01T00:00:00+00:00', fees_usdc=None)
        merged = merge_receipts([official], [row])[0]
        self.assertEqual(merged['first_fill_at'], official['first_fill_at'])
        self.assertEqual(merged['fees_usdc'], '0.0')
        mismatched = dict(official, filled_size='3')
        self.assertIsNone(merge_receipts([mismatched], [row])[0]['fees_usdc'])

    def test_original_market_identity_is_scoped_to_account_and_deployment(self):
        path = self.root/'execution_orders.sqlite'
        self.assertEqual(historical_market_id(path, 'lighter', 'test-account', 'PUMP'), 45)
        self.assertIsNone(historical_market_id(path, 'lighter_rh', 'test-account', 'PUMP'))
        self.assertIsNone(historical_market_id(path, 'lighter', 'another-account', 'PUMP'))

    def test_sdk_audit_read_respects_shared_cooldown_and_records_a_429(self):
        from scripts.query_platform_history import sdk_read
        from src.common.rate_limit import RateLimitError
        gate, sdk = Mock(), Mock()
        public = SimpleNamespace(client=SimpleNamespace(rate_limit=gate))
        gate.check.side_effect = RateLimitError(30)
        with self.assertRaises(RateLimitError):
            sdk_read(public, sdk, 'inactive_orders', 45)
        sdk.call.assert_not_called()
        gate.check.side_effect = None
        failure = RuntimeError('body is not logged')
        failure.status, failure.headers = 429, {'Retry-After':'60'}
        sdk.call.side_effect = failure
        with self.assertRaises(RuntimeError):
            sdk_read(public, sdk, 'inactive_orders', 45)
        response = gate.reject.call_args.args[0]
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '60')
