import unittest
from unittest.mock import Mock, patch

from src.arcus.round_trip import run_round_trip
from src.common.market_data import utc_now


class ArcusRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Mock()
        self.adapter.position_snapshot.return_value = {'qty': 0, 'side': None}
        self.adapter._active_orders.return_value = []
        self.adapter._market.return_value = {
            'min_quote_amount': '5', 'min_base_amount': '.01', 'step_size': '.0000001'}
        self.adapter.get_executable_quote.side_effect = lambda symbol, quantity: {
            'symbol': symbol, 'size': quantity, 'bid': 100, 'ask': 101,
            'updated_at': utc_now(), 'instrument_type': 'perpetual', 'executable': True}
        self.adapter.place_order.return_value = self.fill(.05)
        self.adapter.close_position.return_value = self.fill(.05)

    @staticmethod
    def fill(size):
        return {'success': True, 'confirmed': True, 'status': 'filled',
                'filled_size': size, 'average_price': 101, 'submitted': True}

    def test_round_trip_closes_actual_fill_and_confirms_flat(self):
        report = run_round_trip(self.adapter, 'BABA', 0, 12)
        self.assertTrue(report['success'])
        self.assertTrue(report['orders_submitted'])
        self.assertLessEqual(report['requested_notional'], 12)
        self.assertEqual(float(self.adapter.close_position.call_args.args[2]), .05)
        self.assertTrue(self.adapter.close_position.call_args.kwargs['reduce_only'])
        self.assertLessEqual(len(self.adapter.close_position.call_args.args[3]), 36)

    def test_check_only_never_submits_or_starts_order_connection(self):
        report = run_round_trip(self.adapter, 'BABA', 0, 12, check_only=True)
        self.assertTrue(report['success'])
        self.assertFalse(report['orders_submitted'])
        self.adapter.place_order.assert_not_called()
        self.adapter.close_position.assert_not_called()
        self.adapter.client.ws.start.assert_not_called()

    def test_slow_account_check_occurs_before_final_quote(self):
        now = [0]
        def quote(symbol, quantity):
            return {'symbol': symbol, 'size': quantity, 'bid': 100, 'ask': 101,
                    'updated_at': str(now[0]), 'instrument_type': 'perpetual', 'executable': True}
        self.adapter.get_executable_quote.side_effect = quote
        self.adapter.validate_order.side_effect = lambda *a, **kw: now.__setitem__(0, 6)
        with patch('src.execution.orders.age_seconds', side_effect=lambda stamp: now[0] - int(stamp)), \
                patch('src.arcus.round_trip.age_seconds', side_effect=lambda stamp: now[0] - int(stamp)):
            report = run_round_trip(self.adapter, 'BABA', 0, 12, check_only=True)
        self.assertTrue(report['success'])
        self.assertEqual(report['quote_age_seconds'], 0)

    def test_close_failure_keeps_open_receipt_and_sent_status(self):
        self.adapter.close_position.side_effect = RuntimeError('close disconnected')
        report = run_round_trip(self.adapter, 'BABA', 0, 12)
        self.assertFalse(report['success'])
        self.assertTrue(report['orders_submitted'])
        self.assertEqual(report['open']['filled_size'], .05)
        self.assertTrue(report['reconcile_required'])
        self.adapter.place_order.assert_called_once()

    def test_unknown_open_or_close_is_never_retried(self):
        for leg in ('place_order', 'close_position'):
            with self.subTest(leg=leg):
                self.adapter.reset_mock()
                self.adapter.place_order.return_value = self.fill(.05)
                self.adapter.close_position.return_value = self.fill(.05)
                getattr(self.adapter, leg).return_value = {
                    'success': False, 'confirmed': False, 'status': 'unknown', 'submitted': True}
                report = run_round_trip(self.adapter, 'BABA', 0, 12)
                self.assertTrue(report['reconcile_required'])
                self.adapter.place_order.assert_called_once()
                if leg == 'place_order':
                    self.adapter.close_position.assert_not_called()
                else:
                    self.adapter.close_position.assert_called_once()

    def test_confirmed_partial_close_uses_remaining_quantity(self):
        self.adapter.close_position.side_effect = [self.fill(.02), self.fill(.03)]
        report = run_round_trip(self.adapter, 'BABA', 0, 12)
        self.assertTrue(report['success'])
        calls = self.adapter.close_position.call_args_list
        self.assertEqual(float(calls[1].args[2]), .03)

    def test_existing_position_and_over_cap_never_open(self):
        self.adapter.position_snapshot.return_value = {'qty': .1, 'side': 'long'}
        report = run_round_trip(self.adapter, 'BABA', 0, 12)
        self.assertFalse(report['success'])
        self.adapter.place_order.assert_not_called()
        self.adapter.position_snapshot.return_value = {'qty': 0, 'side': None}
        report = run_round_trip(self.adapter, 'BABA', 20, 12)
        self.assertFalse(report['success'])
        self.adapter.place_order.assert_not_called()
