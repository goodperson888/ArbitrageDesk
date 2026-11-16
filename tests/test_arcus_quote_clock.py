"""Small exchange clock leads must not discard live Arcus BBOs."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter
from src.common.live_feed import ArcusBboFeed
from src.common.market_data import MarketDataError, age_seconds, normalize_quote_time
from src.execution.telemetry import safe_reason


def message_at(observed):
    return {'channel': 'bbo', 'id': 'SUI-USD', 'contents': {
        'timestamp': int(observed.timestamp() * 1e6),
        'bestBid': {'price': '99', 'size': '10'}, 'bestAsk': {'price': '101', 'size': '10'}}}


class ArcusQuoteClockTests(unittest.TestCase):
    def test_bounded_lead_is_clamped_but_past_age_is_preserved(self):
        receipt = datetime.now(timezone.utc)
        self.assertEqual(normalize_quote_time(receipt + timedelta(seconds=.5), receipt), receipt)
        past = receipt - timedelta(seconds=3)
        self.assertEqual(normalize_quote_time(past, receipt), past)
        with self.assertRaises(MarketDataError) as error:
            normalize_quote_time(receipt + timedelta(seconds=.501), receipt)
        self.assertEqual(safe_reason(error.exception), '行情时间超前，等待同步报价')

    def test_future_ws_quote_is_usable_for_discovery_and_holding_without_rest(self):
        feed = ArcusBboFeed({'SUI': 'SUI-USD'})
        feed.connected = True
        observed = datetime.now(timezone.utc) + timedelta(seconds=.25)
        feed.on_message(message_at(observed))
        snapshot = feed.snapshot('SUI')
        self.assertIsNotNone(snapshot)
        self.assertGreaterEqual(age_seconds(snapshot['updated_at']), 0)
        self.assertGreater(datetime.fromisoformat(snapshot['exchange_updated_at']),
                           datetime.fromisoformat(snapshot['updated_at']))
        client = Mock(api_key='test')
        adapter = ArcusExecutionAdapter(client, Mock(), 'test', enabled=True, fee_bps=0)
        adapter._reference_feed = feed
        self.assertEqual(adapter.get_market_price('SUI'), 100)
        self.assertEqual(adapter.get_position_mark_snapshot('SUI')['bid'], 99)
        client.bbo.assert_not_called()
        client.submit.assert_not_called()

    def test_ws_large_clock_error_stale_and_crossed_quotes_remain_blocked(self):
        feed = ArcusBboFeed({'SUI': 'SUI-USD'})
        feed.connected = True
        for shift in (.75, 10, -3):
            with self.subTest(shift=shift):
                feed.on_message(message_at(datetime.now(timezone.utc)))
                self.assertIsNotNone(feed.snapshot('SUI'))
                feed.on_message(message_at(datetime.now(timezone.utc) + timedelta(seconds=shift)))
                self.assertIsNone(feed.snapshot('SUI'))
        message = message_at(datetime.now(timezone.utc) + timedelta(seconds=.1))
        message['contents']['bestBid']['price'] = '102'
        feed.on_message(message)
        self.assertIsNone(feed.snapshot('SUI'))

    def test_ws_snapshot_reads_never_renew_adjusted_quote_age(self):
        feed = ArcusBboFeed({'SUI': 'SUI-USD'})
        feed.connected = True
        feed.on_message(message_at(datetime.now(timezone.utc) + timedelta(seconds=.1)))
        stamp = feed.snapshot('SUI')['updated_at']
        self.assertEqual(feed.snapshot('SUI')['updated_at'], stamp)
        with patch('src.common.live_feed.age_seconds', return_value=2.01):
            self.assertIsNone(feed.snapshot('SUI'))
        self.assertEqual(feed.quotes['SUI']['updated_at'], stamp)

    def client(self, received, shift):
        client = object.__new__(ArcusClient)
        client.market = Mock(return_value={'symbol': 'SUI-USD'})
        payload = message_at(received + timedelta(seconds=shift))['contents']
        payload['_local_fetched_at'] = received.isoformat()
        client._get = Mock(return_value=payload)
        return client

    def test_rest_small_lead_uses_original_receipt_without_renewing_cached_bbo(self):
        receipt = datetime.now(timezone.utc) - timedelta(seconds=.3)
        client = self.client(receipt, .1)
        first, second = client.bbo('SUI'), client.bbo('SUI')
        self.assertEqual(first['updated_at'], receipt.isoformat())
        self.assertEqual(first['received_at'], receipt.isoformat())
        self.assertEqual(second['updated_at'], first['updated_at'])
        self.assertGreaterEqual(age_seconds(second['updated_at']), .3)

    def test_rest_large_lead_is_rejected_and_stale_quote_not_refreshed(self):
        receipt = datetime.now(timezone.utc)
        with self.assertRaises(MarketDataError):
            self.client(receipt, .75).bbo('SUI')
        quote = self.client(receipt, -3).bbo('SUI')
        self.assertGreaterEqual(age_seconds(quote['updated_at']), 3)
