import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from src.execution.history_accounting import (arcus_pages, enrich_arcus_orders, enrich_lighter_order,
                                               journal_fees, funding_record, merge_evidence)
from src.execution.history_recovery import apply_close_history


def evidence():
    position = {'id': 'p', 'symbol': 'BABA', 'entry_order_ids': ['a-open', 'l-open'],
                'variational_venue': 'arcus', 'lighter_venue': 'lighter',
                'variational_side': 'long', 'lighter_side': 'sell',
                'entry_size_variational': 1, 'entry_size_lighter': 1,
                'pending_fee_venues': ['arcus'], 'pending_funding_venues': ['arcus'],
                'accounting_pending': True}
    rows = []
    for venue, prefix, prices, sides in [('arcus', 'a', [100, 102], ['buy', 'sell']),
                                         ('lighter', 'l', [101, 102], ['sell', 'buy'])]:
        for i, suffix in enumerate(['open', 'close']):
            rows.append({'venue': venue, 'symbol': 'BABA', 'order_id': prefix + '-' + suffix,
                         'filled_size': '1', 'average_price': str(prices[i]), 'reduce_only': bool(i),
                         'status': 'filled',
                         'side': sides[i], 'fees_usdc': '.1' if venue == 'arcus' else '0',
                         'timestamp': '2026-11-01T0' + str(i+1) + ':00:00+00:00',
                         'first_fill_at': '2026-11-01T0' + str(i+1) + ':00:00+00:00'})
    # Arcus reports its fill fee already subtracted in closedPnl.
    rows[0]['fee_inclusive_pnl_usdc'] = '-.1'
    rows[1]['fee_inclusive_pnl_usdc'] = '1.9'
    intervals = [{'venue': venue, 'symbol': 'BABA', 'start': rows[0]['timestamp'],
                  'end': rows[1]['timestamp'], 'complete': True,
                  'payments': [{'timestamp': '2026-11-01T01:30:00+00:00', 'payment_usdc': value}]}
                 for venue, value in [('arcus', '-.02'), ('lighter', '.03')]]
    return position, {'rows': rows, 'complete_markets': [['arcus', 'BABA'], ['lighter', 'BABA']],
                      'funding_intervals': intervals}


class HistoryAccountingTests(unittest.TestCase):
    def apply(self, position, cache):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'platform-order-history.json'
            path.write_text(json.dumps(cache)); before = path.read_bytes()
            book = {'closed_positions': [copy.deepcopy(position)]}
            apply_close_history(book, folder)
            self.assertEqual(path.read_bytes(), before)
            return book['closed_positions'][0]

    def test_net_includes_both_legs_fees_and_funding_without_double_fee(self):
        position, cache = evidence()
        row = self.apply(position, cache)
        self.assertAlmostEqual(row['realized_pnl_usdc'], .81)
        self.assertAlmostEqual(row['gross_price_pnl_usdc'], 1)
        self.assertFalse(row['accounting_pending'])
        self.assertEqual(row['pending_fee_venues'], [])
        self.assertEqual(row['pending_funding_venues'], [])
        self.assertTrue(position['accounting_pending'], 'original state is unchanged')

    def test_missing_fee_is_not_zero_and_partial_does_not_become_net(self):
        position, cache = evidence()
        del cache['rows'][3]['fees_usdc']
        row = self.apply(position, cache)
        self.assertIsNone(row['realized_pnl_usdc'])
        self.assertAlmostEqual(row['accounting_subtotal_usdc'], .81)
        self.assertTrue(row['accounting_pending'])
        self.assertIn('lighter 平仓手续费（订单 l-close）', row['accounting_missing'])

    def test_empty_funding_requires_complete_interval_covering_actual_leg(self):
        position, cache = evidence()
        for interval in cache['funding_intervals']:
            interval['payments'] = []
        self.assertAlmostEqual(self.apply(position, cache)['realized_pnl_usdc'], .8)
        for interval in cache['funding_intervals']:
            interval['start'] = '2026-11-01T01:00:01+00:00'
        row = self.apply(position, cache)
        self.assertIsNone(row['realized_pnl_usdc'])
        self.assertEqual(set(row['pending_funding_venues']), {'arcus', 'lighter'})

    def test_arcus_cost_basis_mismatch_stays_pending(self):
        position, cache = evidence(); cache['rows'][1]['fee_inclusive_pnl_usdc'] = '2.4'
        row = self.apply(position, cache)
        self.assertIsNone(row['realized_pnl_usdc'])
        self.assertIn('成本基准', row['accounting_note'])

    def test_shared_venue_refresh_does_not_erase_other_pairs_settled_ledger(self):
        position, cache = evidence()
        position.update(accounting_pending=False, pending_fee_venues=[], pending_funding_venues=[],
                        realized_pnl_usdc=.81, close_price_variational=102, close_price_lighter=102)
        cache['complete_markets'] = [['lighter', 'BABA']]
        cache['funding_intervals'] = []
        row = self.apply(position, cache)
        self.assertFalse(row['accounting_pending'])
        self.assertEqual(row['realized_pnl_usdc'], .81)

    def test_funding_from_later_reopening_is_excluded(self):
        position, cache = evidence()
        cache['funding_intervals'][0]['end'] = '2026-11-01T04:00:00+00:00'
        cache['funding_intervals'][0]['payments'].append({'timestamp': '2026-11-01T03:00:00+00:00',
                                                        'payment_usdc': '999'})
        self.assertAlmostEqual(self.apply(position, cache)['realized_pnl_usdc'], .81)

    def test_open_local_residual_is_closed_from_full_platform_evidence_without_state_write(self):
        position, cache = evidence()
        position.update(status='reconcile_required', partially_closed=True, variational_size=0,
                        lighter_size=1, close_started_time='2026-11-01T01:59:59+00:00')
        original = copy.deepcopy(position)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'platform-order-history.json'
            path.write_text(json.dumps(cache))
            before = path.read_bytes()
            book = {'open_positions':[copy.deepcopy(position)], 'closed_positions':[]}
            apply_close_history(book, folder)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(position, original)
        self.assertEqual(book['open_positions'], [])
        row = book['closed_positions'][0]
        self.assertTrue(row['history_recovered'])
        self.assertTrue(row['local_state_requires_reconcile'])
        self.assertEqual(row['status'], 'closed')
        self.assertFalse(row['partially_closed'])
        self.assertEqual((row['variational_size'], row['lighter_size']), (0, 0))
        self.assertEqual(datetime.fromisoformat(row['closed_time']), datetime.fromisoformat('2026-11-01T02:00:00+00:00'))
        self.assertEqual(row['close_elapsed_ms'], 1000)
        self.assertEqual(row['close_spread_bps'], 0)
        self.assertAlmostEqual(row['realized_pnl_usdc'], .81)

    def test_closed_status_and_accounting_are_independent_when_a_fee_is_missing(self):
        position, cache = evidence()
        position.update(status='reconcile_required', variational_size=0, lighter_size=1)
        del cache['rows'][3]['fees_usdc']
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'platform-order-history.json').write_text(json.dumps(cache))
            book = {'open_positions':[position]}
            apply_close_history(book, folder)
        row = book['closed_positions'][0]
        self.assertEqual(row['status'], 'closed')
        self.assertTrue(row['accounting_pending'])
        self.assertIsNone(row['realized_pnl_usdc'])
        self.assertAlmostEqual(row['accounting_subtotal_usdc'], .81)

    def test_partial_ambiguous_or_nonterminal_history_never_marks_local_exposure_closed(self):
        for issue in ('partial', 'ambiguous', 'active'):
            with self.subTest(issue=issue), tempfile.TemporaryDirectory() as folder:
                position, cache = evidence()
                position.update(status='reconcile_required', variational_size=0, lighter_size=1)
                if issue == 'partial':
                    cache['rows'][3]['filled_size'] = '.5'
                elif issue == 'ambiguous':
                    cache['rows'].append(dict(cache['rows'][3], order_id='unrelated-close'))
                else:
                    cache['rows'][3]['status'] = 'partially_filled'
                for row in cache['rows']:
                    row.setdefault('status', 'filled')
                (Path(folder)/'platform-order-history.json').write_text(json.dumps(cache))
                book = {'open_positions':[position], 'closed_positions':[]}
                apply_close_history(book, folder)
                self.assertEqual(len(book['open_positions']), 1)
                self.assertEqual(book['closed_positions'], [])
                self.assertEqual(position['lighter_size'], 1)

    def test_exchange_retention_does_not_erase_older_verified_orders(self):
        _, cache = evidence()
        updated = merge_evidence(cache, [cache['rows'][1]], [['arcus','BABA']], [])
        self.assertEqual(len(updated['rows']), len(cache['rows']))

    def test_arcus_pagination_overlaps_deduplicates_and_fails_on_full_boundary(self):
        client = Mock(wallet_address='test', account_index=1)
        a, b, c = [{'tradeId': str(i), 'createdAt': i} for i in (3, 2, 1)]
        client._get.side_effect = [{'fills': [a, b]}, {'fills': [b, c]}, {'fills': [c]}]
        self.assertEqual(len(arcus_pages(client, '/v1/fills', 'fills', 0, 4, limit=2)), 3)
        client._get.side_effect = [{'fills': [a, b]}, {'fills': [b, {**b, 'tradeId': 'other'}]}]
        with self.assertRaises(ValueError):
            arcus_pages(client, '/v1/fills', 'fills', 0, 4, limit=2)

    def test_arcus_fill_size_must_match_and_fee_is_not_assumed(self):
        _, cache = evidence(); order = cache['rows'][0]; order.pop('fees_usdc')
        fill = {'orderId': 'a-open', 'tradeId': '1', 'marketDisplayName': 'BABA-USD', 'side': 'BUY',
                'size': '.5', 'price': '100', 'createdAt': 1793494800000000, 'fee': '.1'}
        enrich_arcus_orders([order], [fill]); self.assertNotIn('fees_usdc', order)
        fill['size'] = '1'; enrich_arcus_orders([order], [fill]); self.assertEqual(order['fees_usdc'], '0.1')

    def test_journal_receipt_requires_exact_identity_quantity_price_and_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'orders.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE orders(venue TEXT,intent TEXT,result TEXT)')
                db.execute('INSERT INTO orders VALUES(?,?,?)', ('lighter', json.dumps({'symbol': 'BABA', 'side': 'buy'}),
                    json.dumps({'order_id': 'l-close', 'filled_size': 1, 'average_price': 102,
                                'fees_usdc': 0, 'fees_verified': True})))
            _, cache = evidence(); order = cache['rows'][3]; order.pop('fees_usdc')
            order['filled_size'] = '2'; journal_fees(path, [order]); self.assertNotIn('fees_usdc', order)
            order['filled_size'] = '1'; journal_fees(path, [order]); self.assertEqual(order['fees_usdc'], '0')

    def test_lighter_omitted_fee_fields_are_unknown(self):
        _, cache = evidence(); order = cache['rows'][3]; order.pop('fees_usdc')
        trade = {'trade_id': 1, 'bid_id': 'l-close', 'bid_account_id': 7, 'market_id': 9,
                 'size': '1', 'price': '102', 'timestamp': 1793498400000, 'is_maker_ask': True}
        enrich_lighter_order(order, [trade], 7, 9); self.assertNotIn('fees_usdc', order)
        trade.update(taker_fee=0, integrator_taker_fee=0)
        enrich_lighter_order(order, [trade], 7, 9); self.assertEqual(order['fees_usdc'], '0')

    def test_invalid_funding_amount_or_window_cannot_settle(self):
        for payment, stamp in [('NaN', 1793498400000), ('1', 1793508400000)]:
            with self.assertRaises(ValueError):
                funding_record('lighter', 'BABA', '2026-11-01T01:00:00+00:00',
                               '2026-11-01T02:00:00+00:00', [{'change': payment, 'timestamp': stamp}],
                               'change', 'timestamp')

    def test_endpoint_outage_keeps_exact_prior_fees_and_funding_but_not_changed_quantity(self):
        _, cache = evidence()
        cache['rows'][0].update(first_fill_at=cache['rows'][0]['timestamp'], fill_ids=['f1'])
        rows = copy.deepcopy(cache['rows'])
        rows[0].pop('fees_usdc'); rows[0].pop('first_fill_at'); rows[0].pop('fill_ids')
        rows[0]['timestamp'] = '2026-11-01T01:00:01+00:00'
        merged = merge_evidence(cache, rows, cache['complete_markets'], [])
        self.assertEqual(merged['rows'][0]['fees_usdc'], '.1')
        self.assertEqual(merged['rows'][0]['timestamp'], cache['rows'][0]['timestamp'])
        self.assertEqual(merged['funding_intervals'], cache['funding_intervals'])
        rows[0].pop('fees_usdc'); rows[0]['filled_size'] = '2'
        self.assertNotIn('fees_usdc', merge_evidence(cache, rows, cache['complete_markets'], [])['rows'][0])
