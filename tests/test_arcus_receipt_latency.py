"""Receipt acceleration must preserve terminal evidence and single submission."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter, _ArcusWebSocket
from src.common.market_data import utc_now
from src.common.rate_limit import RateLimitError
from src.execution.journal import OrderJournal


FILLED = dict(order_id='o1', status='filled', filled_size=1,
              average_price=100, fees_usdc=0)


class StreamReceiptTests(unittest.TestCase):
    def setUp(self):
        self.ws = _ArcusWebSocket('unused', None, 'test', 0)

    def test_cached_terminal_is_visible_even_with_zero_timeout(self):
        self.ws._handle({'channel': 'orders', 'contents': {'orders': [
            {'orderId': 'o1', 'status': 'FILLED', 'filledSize': 1, 'averagePrice': 100}]}})
        self.assertEqual(self.ws.wait_order('o1', 0)['status'], 'filled')
        self.assertIsNone(self.ws.wait_order('different-order', 0))

    def test_list_payload_wakes_waiter_immediately(self):
        waiting = threading.Event()
        original_wait = self.ws._order_updates.wait
        def wait(timeout):
            waiting.set()
            return original_wait(timeout)
        with patch.object(self.ws._order_updates, 'wait', side_effect=wait), \
                ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.ws.wait_order, 'o1', .5)
            self.assertTrue(waiting.wait(.2))
            self.ws._handle({'channel': 'orders', 'contents': [
                {'orderId': 'o1', 'status': 'FILLED', 'filledSize': 1, 'averagePrice': 100}]})
            self.assertEqual(result.result(timeout=.2)['filled_size'], 1)

    def test_fill_alone_does_not_prove_order_terminal(self):
        self.ws._handle({'channel': 'userFills', 'contents': [
            {'orderId': 'o1', 'size': .5, 'price': 100}]})
        self.assertEqual(self.ws.fills['o1']['size'], .5)
        self.assertIsNone(self.ws.wait_order('o1', 0))


class ReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(api_key='verified')
        self.client.market.return_value = {'tick_size': '0.01'}
        self.client.ws.fills = {}
        self.adapter = ArcusExecutionAdapter(self.client, None, 'test', enabled=True, fee_bps=0)

    def test_fast_stream_receipt_needs_no_rest(self):
        self.client.ws.wait_order.return_value = FILLED
        self.assertEqual(self.adapter._wait_for_order('o1', 'BTC'), FILLED)
        self.client.ws.wait_order.assert_called_once_with('o1', 1.0)
        self.client.order_status.assert_not_called()

    def test_missing_stream_uses_one_early_collection_read(self):
        self.client.ws.wait_order.return_value = None
        self.client.order_status.return_value = FILLED
        self.assertEqual(self.adapter._wait_for_order('o1', 'BTC'), FILLED)
        self.client.order_status.assert_called_once_with('o1', collection_only=True)
        self.client.ws.wait_order.assert_called_once_with('o1', 1.0)

    def test_rate_limit_does_not_discard_later_stream_receipt(self):
        self.client.ws.wait_order.side_effect = [None, FILLED]
        self.client.order_status.side_effect = RateLimitError(5)
        self.assertEqual(self.adapter._wait_for_order('o1', 'BTC'), FILLED)
        self.assertEqual(self.client.order_status.call_count, 1)

    def test_delayed_history_retains_final_recovery(self):
        self.client.ws.wait_order.return_value = None
        self.client.order_status.side_effect = [None, FILLED]
        self.assertEqual(self.adapter._wait_for_order('o1', 'BTC'), FILLED)
        self.assertEqual(self.client.order_status.call_count, 2)
        self.client.order_status.assert_called_with('o1')

    def test_wrong_order_or_nonterminal_cannot_confirm(self):
        self.client.ws.wait_order.return_value = None
        for candidate in (dict(FILLED, order_id='other'), dict(FILLED, status='open')):
            self.client.order_status.return_value = candidate
            self.assertIsNone(self.adapter._wait_for_order('o1', 'BTC'))

    def test_partial_fill_keeps_actual_quantity(self):
        self.client.ws.wait_order.return_value = None
        self.client.order_status.return_value = dict(FILLED, status='partial', filled_size=.25)
        observed = self.adapter._wait_for_order('o1', 'BTC')
        self.assertEqual(self.adapter._receipt(observed, None, 1)['filled_size'], .25)

    def test_unknown_result_keeps_order_id_and_never_resubmits(self):
        self.client.ws.wait_order.return_value = None
        self.client.order_status.side_effect = RateLimitError(5)
        self.client.submit.return_value = dict(accepted=True, order_id='o1')
        with tempfile.TemporaryDirectory() as tmp:
            self.adapter.journal = OrderJournal(Path(tmp) / 'orders.sqlite')
            quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                         executable=True, instrument_type='perpetual')
            with patch.object(self.adapter, 'validate_order'):
                result = self.adapter.place_order('BTC', 'buy', 1, 100, 10, 'intent1', quote)
                repeated = self.adapter.place_order('BTC', 'buy', 1, 100, 10, 'intent1', quote)
            self.assertEqual(result['order_id'], 'o1')
            self.assertFalse(result['confirmed'])
            self.assertEqual(repeated, result)
            self.client.submit.assert_called_once()


class StatusIdentityTests(unittest.TestCase):
    def test_early_check_only_reads_collection_and_matches_id(self):
        client = object.__new__(ArcusClient)
        client.wallet_address, client.account_index = 'test', 0
        client._get = Mock(return_value={'orders': [
            {'orderId': 'other', 'status': 'FILLED'},
            {'orderId': 'o1', 'status': 'FILLED', 'filledSize': 1}]})
        self.assertEqual(client.order_status('o1', collection_only=True)['filled_size'], 1)
        client._get.assert_called_once_with('/v1/orders', {'address': 'test', 'accountIndex': 0})

    def test_dedicated_route_cannot_return_another_orders_terminal(self):
        client = object.__new__(ArcusClient)
        client.wallet_address, client.account_index = 'test', 0
        client._get = Mock(side_effect=[{'orderId': 'other', 'status': 'FILLED'}, {}, {'orders': []}])
        self.assertIsNone(client.order_status('o1'))

    def test_shared_cooldown_blocks_actual_http_read(self):
        client = object.__new__(ArcusClient)
        client.wallet_address, client.account_index = 'test', 0
        client.rate_limit, client.http = Mock(), Mock()
        client.rate_limit.check.side_effect = RateLimitError(5)
        with self.assertRaises(RateLimitError):
            client.order_status('o1', collection_only=True)
        client.http.get.assert_not_called()
