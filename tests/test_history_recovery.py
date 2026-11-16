import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from src.execution.history_recovery import normalized_order, recover_leg, apply_close_history
from src.lighter.execution import OfficialSDKTransport


def order(oid, size='1', price='101', close=False, stamp='2026-11-01T01:00:00+00:00', side=None):
    return {'venue': 'arcus', 'symbol': 'BABA', 'order_id': oid, 'filled_size': size,
            'average_price': price, 'reduce_only': close, 'timestamp': stamp,
            'side': side or ('sell' if close else 'buy'), 'status': 'filled'}


class HistoryRecoveryTests(unittest.TestCase):
    def test_rate_derived_fee_cannot_become_final_actual_fee(self):
        rows = [dict(order('open'), fees_usdc='.01', fee_estimated=True,
                     fee_source='account_rate_estimate'),
                dict(order('closed', price='102', close=True,
                           stamp='2026-11-01T02:00:00+00:00'), fees_usdc='.01')]
        fill = recover_leg(self.position(), 'lighter', rows)
        self.assertIsNone(fill['fees_usdc'])
        self.assertTrue(fill['missing_fees'])

    def position(self):
        return {'id': 'position', 'symbol': 'BABA', 'entry_order_ids': ['open'],
                'variational_venue': 'lighter', 'lighter_venue': 'arcus',
                'variational_side': 'short', 'lighter_side': 'buy', 'entry_size_lighter': 1,
                'entry_size_variational': 1, 'close_price_variational': 100,
                'close_price_lighter': None, 'realized_pnl_usdc': -.002}

    def test_actual_fill_price_instead_of_protective_limit_and_lighter_vwap(self):
        row = {'orderId': 'o', 'filledSize': '1', 'avgFillPrice': '98.72', 'price': '96.25',
               'side': 'SELL', 'reduceOnly': True, 'updatedAt': 1793531736115000, 'status': 'FILLED'}
        actual = normalized_order('arcus', 'BABA', row)
        self.assertEqual(actual['average_price'], '98.72')
        self.assertEqual(actual['timestamp'], '2026-11-01T11:15:36.115000+00:00')
        del row['avgFillPrice']
        self.assertIsNone(normalized_order('arcus', 'BABA', row), 'a limit alone cannot prove execution price')
        lighter = normalized_order('lighter', 'BABA', {'order_id': 'l', 'filled_base_amount': '2',
                                   'filled_quote_amount': '198', 'is_ask': False, 'price': '150'})
        self.assertEqual(lighter['average_price'], '99')

    def test_partial_closes_use_quantity_weighted_mean(self):
        rows = [order('open'), order('a', '.4', '100', True, '2026-11-01T02:00:00+00:00'),
                order('b', '.6', '102', True, '2026-11-01T03:00:00+00:00')]
        fill = recover_leg(self.position(), 'lighter', rows)
        self.assertAlmostEqual(fill['average_price'], 101.2)
        self.assertEqual(fill['order_ids'], ['a', 'b'])

    def test_incomplete_or_ambiguous_closes_and_new_openings_do_not_match(self):
        opening = order('open')
        close = order('close', close=True, stamp='2026-11-01T03:00:00+00:00')
        for rows in ([opening, {**close, 'filled_size': '.5'}],
                     [opening, close, {**close, 'order_id': 'duplicate-size'}],
                     [opening, order('next-open', stamp='2026-11-01T02:00:00+00:00'), close],
                     [close], [opening, {**close, 'symbol': 'ENA'}]):
            with self.subTest(rows=rows):
                self.assertIsNone(recover_leg(self.position(), 'lighter', rows))

    def test_grid_matches_close_identity_across_overlapping_entries(self):
        position=dict(self.position(),grid_cycle_id='cycle',grid_layer=1,
                      entry_order_ids_by_venue={'arcus':['open']},
                      close_order_ids_by_venue={'arcus':['my-close']})
        rows=[order('open'),order('another-entry',stamp='2026-11-01T02:00:00+00:00'),
              order('my-close',price='102',close=True,stamp='2026-11-01T03:00:00+00:00'),
              order('other-close',price='99',close=True,stamp='2026-11-01T04:00:00+00:00')]
        fill=recover_leg(position,'lighter',rows)
        self.assertEqual(fill['order_ids'],['my-close'])
        self.assertEqual(fill['average_price'],102)
        position['close_order_ids_by_venue']={'lighter':['my-close']}
        self.assertIsNone(recover_leg(position,'lighter',rows))

    def test_spread_is_recovered_independently_of_fee_settlement_and_without_state_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'platform-order-history.json'
            payload = {'complete_markets': [['arcus', 'BABA']], 'rows': [order('open'),
                       order('close', price='99', close=True, stamp='2026-11-01T02:00:00+00:00')]}
            path.write_text(json.dumps(payload)); before = path.read_bytes()
            book = {'closed_positions': [self.position()]}
            apply_close_history(book, root)
            row = book['closed_positions'][0]
            self.assertEqual(row['close_price_lighter'], 99)
            self.assertAlmostEqual(row['close_spread_bps'], 100)
            self.assertIsNone(row['realized_pnl_usdc'])
            self.assertTrue(row['accounting_pending'])
            self.assertEqual(path.read_bytes(), before)
            payload['complete_markets'] = [];path.write_text(json.dumps(payload))
            untouched = {'closed_positions': [self.position()]}
            apply_close_history(untouched, root)
            self.assertIsNone(untouched['closed_positions'][0]['close_price_lighter'])

    def test_sdk_history_arguments_match_current_official_signature_and_paginate(self):
        calls = []
        async def inactive_orders(*, authorization, account_index, market_id, limit, _request_timeout, cursor=None):
            calls.append(cursor)
            return {'code': 200, 'orders': [{'order_id': cursor or 'first'}],
                    'next_cursor': 'second' if cursor is None else None}
        sdk = Mock()
        sdk.create_auth_token_with_expiry.return_value = ('read-only-test-token', None)
        sdk.order_api.account_inactive_orders = inactive_orders
        transport = OfficialSDKTransport(sdk, 1, 4)
        rows = asyncio.run(transport.inactive_orders(17))
        self.assertEqual(len(rows), 2)
        self.assertEqual(calls, [None, 'second'])
        sdk.create_order.assert_not_called()
