import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.web.execution_book import pair_sources, merge_books, unmatched_fills
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position


class ExecutionBookTests(unittest.TestCase):
    def row(self, **changes):
        return dict(id='p1', symbol='BABA', status='closed',
                    open_time='2026-11-02T10:00:00', closed_time='2026-11-02T10:01:00',
                    entry_size_variational=1, entry_size_lighter=1,
                    close_price_variational=101, close_price_lighter=102,
                    realized_pnl_usdc=1, hedge_status='paired', **changes)

    def test_all_pairs_preserve_labels_dedupe_and_flag_missing_accounting(self):
        first = self.row()
        second = {**first, 'id': 'p2', 'close_price_lighter': None, 'realized_pnl_usdc': -0.002}
        books = [({'closed_positions': [first]}, ['lighter', 'lighter_rh'], 'rh'),
                 ({'closed_positions': [second]}, ['lighter', 'arcus'], 'arcus'),
                 ({'closed_positions': [first]}, ['variational', 'lighter'], 'duplicate')]
        data = merge_books(books, date(2026, 11, 2))
        self.assertEqual(data['summary']['closed_count'], 2)
        self.assertEqual(data['summary']['pending_accounting_count'], 1)
        self.assertIsNone(data['summary']['realized_pnl_usdc'])
        self.assertEqual(data['summary']['known_daily_realized_pnl_usdc'], 1)
        by_id = {p['id']: p for p in data['closed_positions']}
        self.assertEqual(by_id['p1']['lighter_venue'], 'lighter_rh')
        self.assertEqual(by_id['p2']['lighter_venue'], 'arcus')
        self.assertIsNone(by_id['p2']['realized_pnl_usdc'])
        self.assertIn('arcus', by_id['p2']['accounting_note'])
        self.assertEqual(second['realized_pnl_usdc'], -0.002, 'read-only projection preserves stored evidence')

    def test_partial_fees_are_unknown_and_old_pnl_is_not_today(self):
        row = self.row()
        row.update(pending_fee_venues=['arcus'])
        data = merge_books([({'closed_positions': [row]}, ['lighter', 'arcus'], 'arcus')], date(2026, 11, 3))
        self.assertIsNone(data['closed_positions'][0]['realized_pnl_usdc'])
        self.assertEqual(data['summary']['realized_pnl_usdc'], 0)

    def test_sources_handle_underscore_venues_reverse_pairs_and_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ['hedge_state_lighter_lighter_rh.json', 'hedge_state_arcus_lighter.json',
                         'hedge_state_fake_arcus.json']:
                (root / name).write_text('{}')
            self.assertEqual(len(list(pair_sources(root))), 2)
            selected = list(pair_sources(root, ['lighter', 'arcus']))
            self.assertEqual(selected[0][1], ['arcus', 'lighter'])

    def test_grid_cycle_total_includes_remaining_float_loss_and_unknown_settlement(self):
        closed=dict(self.row(),grid_cycle_id='cycle',grid_layer=1)
        opened=dict(closed,id='open',status='open',closed_time=None,grid_layer=2,unrealized_pnl_usdc=-3)
        result=merge_books([({'closed_positions':[closed],'open_positions':[opened]},['lighter','arcus'],'test')])
        cycle=result['grid_cycles'][0]
        self.assertEqual(cycle['realized_pnl_usdc'],1)
        self.assertEqual(cycle['estimated_total_pnl_usdc'],-2)
        self.assertEqual(cycle['active_layers'],1)
        closed['pending_funding_venues']=['lighter']
        result=merge_books([({'closed_positions':[closed],'open_positions':[opened]},['lighter','arcus'],'test')])
        self.assertIsNone(result['grid_cycles'][0]['estimated_total_pnl_usdc'])

    def test_standalone_receipts_are_visible_rejections_and_linked_fills_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with sqlite3.connect(root / 'execution_orders.sqlite') as db:
                db.execute('CREATE TABLE orders(seq INTEGER,venue TEXT,client_id TEXT,intent TEXT,result TEXT)')
                for seq, cid, oid, filled in [(1, 'hedge-v', 'linked', 1),
                                             (2, 'rejected', 'bad', 0), (3, 'arcus-open-abc', 'test', .05)]:
                    db.execute('INSERT INTO orders VALUES(?,?,?,?,?)', (seq, 'arcus', cid,
                               json.dumps({'symbol': 'BABA', 'side': 'buy', 'reduce_only': False}),
                               json.dumps({'order_id': oid, 'filled_size': filled, 'average_price': 100,
                                           'fees_usdc': None, 'status': 'filled' if filled else 'rejected'})))
            (root / 'arcus-round-trip-test.json').write_text(json.dumps({
                'open': {'order_id': 'test'}, 'started_at': '2026-11-02T09:11:06+00:00'}))
            book = {'closed_positions': [{'entry_order_ids': ['linked'], 'lighter_venue': 'arcus'}]}
            rows = unmatched_fills(root, [(book, ['lighter', 'arcus'], 'pair')])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['source'], 'arcus_round_trip')
            self.assertEqual(rows[0]['order_id'], 'test')
            self.assertIsNone(rows[0]['fees_usdc'])
            self.assertEqual(unmatched_fills(root, [(book, [], '')], ['lighter', 'lighter_rh']), [])

    def test_actual_lighter_close_is_not_reused_for_missing_arcus_close(self):
        with tempfile.TemporaryDirectory() as folder:
            view = ArbitrageStrategy({'state_path': str(Path(folder) / 'state.json')}, None, None)
            position = Position('BABA', 'short', 'buy', 0, 0, 100, 99, datetime(2026, 11, 1),
                                status='closed', entry_size_variational=1, entry_size_lighter=1,
                                variational_venue='lighter', lighter_venue='arcus')
            row = view._closed_position_row(position, receipt_prices={'lighter': 101})
            self.assertEqual(row['close_price_variational'], 101)
            self.assertIsNone(row['close_price_lighter'])
            legacy = view._closed_position_row(position, receipt_prices={'variational': 101, 'lighter': 102})
            self.assertEqual(legacy['close_price_variational'], 101)
            self.assertEqual(legacy['close_price_lighter'], 102)


if __name__ == '__main__':
    unittest.main()
