import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from src.common.market_data import utc_now
from src.execution.journal import OrderJournal

from src.arcus.execution import (
    ArcusExecutionAdapter,
    build_cancel_payload,
    build_order_payload,
    normalize_fill,
    normalize_order_update,
    protective_limit,
)
from src.execution.runtime import pair_runtime_blockers


class ArcusSigningTest(unittest.TestCase):
    def test_fill_average_never_uses_limit_price_and_fees_keep_provenance(self):
        order = normalize_order_update(dict(orderId='arc', status='FILLED', filledSize='139.3',
            avgFillPrice='.085835', price='.085852'))
        adapter = ArcusExecutionAdapter(Mock(api_key='verified'), None, 'test', enabled=True, fee_bps=4.5)
        receipt = adapter._receipt(order, None, 139.3)
        self.assertEqual(receipt['average_price'], .085835)
        self.assertEqual(receipt['fee_source'], 'account_rate_estimate')
        self.assertTrue(receipt['fee_estimated'])
        self.assertIn('估算', receipt['fee_basis'])
        fill = normalize_fill(dict(orderId='arc', size='139.3', price='.085830', fee='.005'))
        receipt = adapter._receipt(order, fill, 139.3)
        self.assertEqual(receipt['average_price'], .085830)
        self.assertEqual(receipt['fees_usdc'], .005)
        self.assertFalse(receipt['fee_estimated'])
        self.assertEqual(receipt['fee_source'], 'arcus_fills')
        from_order = adapter._receipt(dict(order, fees_usdc=.0049), fill, 139.3)
        self.assertEqual(from_order['fees_usdc'], .0049)
        self.assertEqual(from_order['fee_source'], 'arcus_order')
        missing = normalize_order_update(dict(orderId='arc', status='FILLED', filledSize='139.3', price='.085852'))
        self.assertIsNone(missing['average_price'])
        self.assertFalse(adapter._receipt(missing, None, 139.3)['confirmed'])
        fill['size'] = 1
        receipt = adapter._receipt(order, fill, 139.3)
        self.assertEqual(receipt['average_price'], .085835)
        self.assertTrue(receipt['fee_estimated'])

    def test_slippage_limits_are_side_aware_and_never_round_past_budget(self):
        self.assertEqual(protective_limit('0.003905', 'sell', 10, '.000001'), '0.003902')
        self.assertEqual(protective_limit('0.003905', 'buy', 10, '.000001'), '0.003908')
        self.assertEqual(protective_limit('100.009', 'buy', 0, '.01'), '100')
        self.assertEqual(protective_limit('100.009', 'sell', 0, '.01'), '100.01')
        for bad in (-1, 101, float('nan'), True):
            with self.assertRaises((ValueError, ArithmeticError)):
                protective_limit(100, 'buy', bad, '.01')

    def test_actual_submit_uses_protected_limit_and_journal_records_both_prices(self):
        client = Mock(api_key='verified')
        client.market.return_value = {'tick_size': '.000001'}
        client.submit.return_value = {'accepted': False, 'error': 'fixture rejection'}
        quote = dict(symbol='PUMP', size=3065, bid=.003905, ask=.003906,
                     updated_at=utc_now(), executable=True, instrument_type='perpetual')
        with tempfile.TemporaryDirectory() as directory:
            journal = OrderJournal(Path(directory)/'orders.sqlite')
            adapter = ArcusExecutionAdapter(client, journal, 'test', enabled=True, fee_bps=0)
            with patch.object(adapter, 'validate_order'):
                first = adapter.place_order('PUMP', 'sell', 3065, .003905, 10, 'close-1', quote, reduce_only=True)
                repeated = adapter.place_order('PUMP', 'sell', 3065, .003905, 10, 'close-1', quote, reduce_only=True)
            self.assertEqual(first, repeated)
            client.submit.assert_called_once_with('PUMP', 'sell', 3065, '0.003902', 'close-1', True)
            with journal.connect() as db:
                intent = json.loads(db.execute('select intent from orders').fetchone()['intent'])
            self.assertEqual(intent['reference_price'], '0.003905')
            self.assertEqual(intent['price'], '0.003902')
            self.assertTrue(intent['reduce_only'])
            self.assertEqual(intent['time_in_force'], 'IOC')
            self.assertEqual(intent['quote_updated_at'], quote['updated_at'])
            self.assertEqual(first['limit_price'], .003902)
            self.assertEqual(first['time_in_force'], 'IOC')

    def test_place_payload_is_deterministic_and_uses_ioc_units(self):
        typed, body, ts = build_order_payload(
            "0xAbC", 7, 12, "buy", "100.00", "0.01", "0.01", "0.001",
            "intent-1", timestamp_ns=123, good_til_us=2_000_000_000_000,
        )
        self.assertEqual(ts, 123)
        self.assertIn('"c":"intent-1"', typed)
        self.assertIn('"r":0', typed)
        self.assertIn('"t":2', typed)
        self.assertEqual(body["timeInForce"], "IOC")
        self.assertEqual(body["quantity"], "0.01")
        self.assertEqual(body["price"], "100")
        self.assertEqual(body["clientId"], "intent-1")
        self.assertNotIn("clientOrderId", body)
        self.assertEqual(json.loads(typed)["g"], 2_000_000_000_000_000)

    def test_reduce_only_and_cancel_payload(self):
        typed, body, _ = build_order_payload(
            "0xAbC", 1, 2, "sell", 101, 0.5, 0.01, 0.1, "close-1",
            reduce_only=True, timestamp_ns=42, good_til_us=2_000_000_000_000,
        )
        self.assertEqual(json.loads(typed)["r"], 1)
        self.assertTrue(body["reduceOnly"])
        cancel, cancel_body, _ = build_cancel_payload("0xAbC", 1, 2, "ord-9", timestamp_ns=43)
        self.assertEqual(json.loads(cancel)["op"], 2)
        self.assertEqual(cancel_body["orderId"], "ord-9")

    def test_order_and_fill_normalization(self):
        order = normalize_order_update({
            "orderId": "o1", "status": "FILLED", "filledSize": "0.25",
            "averagePrice": "101.2", "clientOrderId": "c1",
        })
        fill = normalize_fill({"orderId": "o1", "fillQty": "0.25", "fillPrice": "101.2", "feeUsd": "0.01"})
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["filled_size"], 0.25)
        self.assertEqual(fill["fee"], 0.01)

    def test_cancellation_diagnostics_keep_original_status_without_inventing_cause(self):
        order = normalize_order_update(dict(orderId='zero', status='CANCELED', filledSize='0',
            price='0.093293', timeInForce='IOC'))
        client = Mock(api_key='verified')
        adapter = ArcusExecutionAdapter(client, None, 'test', enabled=True, fee_bps=0)
        receipt = adapter._receipt(order, None, 128.5)
        self.assertEqual(receipt['raw_status'], 'CANCELED')
        self.assertEqual(receipt['time_in_force'], 'IOC')
        self.assertEqual(receipt['limit_price'], .093293)
        self.assertFalse(receipt['platform_reason_reported'])
        self.assertIsNone(receipt['platform_reason'])
        margin = normalize_order_update(dict(orderId='margin', status='MARGIN_CANCELED',
                                            cancelReason='INSUFFICIENT_MARGIN'))
        self.assertEqual(margin['raw_status'], 'MARGIN_CANCELED')
        self.assertEqual(margin['platform_reason'], '保证金不足')
        unsafe = normalize_order_update(dict(status='CANCELED', timeInForce='secret-token',
            cancelReason='secret-token=abcdef', responseBody={'private_key': 'hidden'}))
        self.assertTrue(unsafe['platform_reason_reported'])
        self.assertIsNone(unsafe['platform_reason_code'])
        self.assertIsNone(unsafe['time_in_force'])
        self.assertNotIn('secret', json.dumps(unsafe))
        self.assertNotIn('private_key', json.dumps(unsafe))


class ArcusRuntimeGateTest(unittest.TestCase):
    def test_arcus_requires_credentials_and_fee_evidence(self):
        # Runtime checks deliberately read the current local .env and saved
        # evidence too. Keep this missing-credentials case independent of
        # the operator's real account configuration.
        with patch.dict("os.environ", {}, clear=True), \
                patch("dotenv.dotenv_values", return_value={}), \
                patch("pathlib.Path.read_text", return_value="{}"):
            blockers = pair_runtime_blockers({
                "live_enabled": True, "venues": ["lighter_rh", "arcus"], "symbols": ["BTC"]
            })
        self.assertTrue(any("ARCUS_WALLET_ADDRESS" in item for item in blockers))
        self.assertTrue(any("arcus 尚未通过本机只读鉴权" in item for item in blockers))

    def test_quote_expiring_during_account_read_never_reaches_submit(self):
        client = Mock(api_key='test-key')
        client.market.return_value = {'tick_size': '0.01'}
        with tempfile.TemporaryDirectory() as directory:
            journal = OrderJournal(Path(directory) / 'orders.sqlite')
            adapter = ArcusExecutionAdapter(client, journal, 'test', enabled=True, fee_bps=0)
            quote = {'symbol': 'BABA', 'size': '.05', 'bid': 100, 'ask': 101,
                     'updated_at': utc_now(), 'executable': True, 'instrument_type': 'perpetual'}
            with patch.object(adapter, 'validate_order'), \
                    patch('src.execution.orders.age_seconds', side_effect=[0, 6]):
                result = adapter.place_order('BABA', 'buy', '.05', 101, 10,
                                             'test-stale-quote', quote)
            self.assertFalse(result['submitted'])
            self.assertTrue(result['confirmed'])
            self.assertEqual(result['filled_size'], 0)
            client.submit.assert_not_called()
            self.assertEqual(journal.summary()[0]['state'], 'terminal')


class ArcusOrderPrecheckTest(unittest.TestCase):
    class Client:
        api_key = "verified"

        def __init__(self, orders):
            self._orders = orders

        def market(self, symbol):
            return {
                "market_id": 1, "symbol": "CRCL-USD", "tick_size": "0.01",
                "step_size": "0.001", "min_base_amount": "0.001",
                "min_quote_amount": "1", "status": "active",
            }

        def account(self):
            return {"freeCollateral": "1000"}

        def orders(self):
            return {"orders": self._orders}

        def positions(self):
            return {"positions": [{
                "marketDisplayName": "CRCL-USD", "positionSize": "0.164",
                "side": "long",
            }]}

    def test_filled_order_history_does_not_block_reduce_only_close(self):
        client = self.Client([{
            "orderId": "filled-entry", "marketDisplayName": "CRCL-USD",
            "status": "FILLED", "filledSize": "0.164",
        }])
        adapter = ArcusExecutionAdapter(client, journal=None, account="acct",
                                        enabled=True, fee_bps=0)
        # The endpoint's ``orders`` collection contains filled history.  It
        # must not be mistaken for an active order during a close precheck.
        adapter.validate_order("CRCL", 0.164, reduce_only=True, side="sell")

    def test_live_order_blocks_and_explains_the_order(self):
        client = self.Client([{
            "orderId": "live-1", "marketDisplayName": "CRCL-USD",
            "status": "OPEN", "originalSize": "0.164", "side": "SELL",
            "reduceOnly": True,
        }])
        adapter = ArcusExecutionAdapter(client, journal=None, account="acct",
                                        enabled=True, fee_bps=0)
        with self.assertRaisesRegex(ValueError, "订单号 live-1.*数量 0.164.*只减仓 是"):
            adapter.validate_order("CRCL", 0.164, reduce_only=True, side="sell")

