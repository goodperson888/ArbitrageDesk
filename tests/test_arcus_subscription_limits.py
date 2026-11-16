import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.api_v2.arcus_adapter import ArcusAdapter
from src.common.discovery import ArcusSubscriptionError, arcus_subscription_batches
from src.common.live_feed import ArcusBboFeed, ArcusFeedGroup
from src.common.market_data import utc_now
from src.execution.scan_status import annotate_entry_scan


LIMIT_ERROR = {'type': 'error', 'message': 'Too many subscriptions on this connection (max 100)'}


def bbo(symbol):
    return dict(type='subscribed', channel='bbo', id=symbol+'-USD', contents=dict(
        timestamp=int(datetime.now(timezone.utc).timestamp()*1e6),
        bestBid=dict(price='100', size='10'), bestAsk=dict(price='101', size='10')))


class GroupTests(unittest.TestCase):
    def test_expansion_preserves_existing_connections_and_quotes(self):
        group = ArcusFeedGroup({'BTC':'BTC-USD'},proxy_url='http://proxy',ws_url='wss://test/ws')
        original = group.feeds[0]
        original.on_message(bbo('BTC'))
        before = group.snapshot('BTC')
        with patch.object(ArcusBboFeed,'start') as start, patch.object(ArcusBboFeed,'stop') as stop:
            group.add_markets({'BTC':'BTC-USD','ETH':'ETH-USD'})
            group.add_markets({'ETH':'ETH-USD'})
        self.assertEqual(len(group.feeds),2)
        self.assertIs(group.feeds[0],original)
        self.assertEqual(group.snapshot('BTC'),before)
        self.assertEqual(group.feeds[1].proxy_url,'http://proxy')
        self.assertEqual(group.feeds[1].url,'wss://test/ws')
        start.assert_called_once(); stop.assert_not_called()

    def test_sixty_two_markets_keep_bbo_and_funding_below_limit_without_dropping_tail(self):
        markets = {'M'+str(i): 'M'+str(i)+'-USD' for i in range(62)}
        group = ArcusFeedGroup(markets)
        sockets = [Mock(), Mock()]
        for feed, socket in zip(group.feeds, sockets):
            feed.subscribe(socket)
            sent = [json.loads(call.args[0]) for call in socket.send.call_args_list]
            self.assertLessEqual(len(sent), 100)
            self.assertEqual({m['channel'] for m in sent}, {'bbo', 'predictedFunding'})
        self.assertEqual([s.send.call_count for s in sockets], [100, 24])
        self.assertEqual(set(group.by_symbol), set(markets))
        for feed in group.feeds:
            feed.connected = True
        group.by_symbol['M61'].on_message(bbo('M61'))
        self.assertEqual(group.snapshot('M61')['bid'], 100)
        self.assertEqual(group.quote_health('M61')['quote_status'], 'fresh')
        self.assertTrue(group.health()['connected'])
        group.feeds[0].connected = False
        group.feeds[0].error = '订阅失败'
        self.assertFalse(group.health()['connected'])
        self.assertEqual(group.health()['connected_connections'], 1)
        self.assertIsNotNone(group.snapshot('M61'))
        self.assertEqual(group.health()['error'], '订阅失败')

    def test_group_starts_and_stops_each_connection(self):
        group = ArcusFeedGroup({str(i): str(i)+'-USD' for i in range(101)})
        for feed in group.feeds:
            feed.start, feed.stop = Mock(), Mock()
        group.start(); group.stop()
        for feed in group.feeds:
            feed.start.assert_called_once()
            feed.stop.assert_called_once()

    def test_single_socket_cannot_silently_send_an_oversized_subscription_set(self):
        feed = ArcusBboFeed({str(i): str(i) for i in range(51)})
        socket = Mock()
        with self.assertRaises(ArcusSubscriptionError):
            feed.subscribe(socket)
        socket.send.assert_not_called()

    def test_subscription_rejection_is_an_error_not_an_ignored_data_frame(self):
        feed = ArcusBboFeed({'BTC': 'BTC-USD'})
        with self.assertRaisesRegex(ArcusSubscriptionError, '100'):
            feed.on_message(LIMIT_ERROR)
        # Market-specific downtime remains an ordinary per-market cooldown.
        feed.on_message(dict(type='error', message="Market 'BTC-USD' is not available"))
        self.assertIn('BTC', feed.unavailable)

    def test_exact_limit_and_empty_scope(self):
        for channels, count, sizes in ((2, 50, [50]), (3, 62, [33, 29]), (2, 0, [])):
            self.assertEqual([len(x) for x in arcus_subscription_batches(range(count), channels)], sizes)

    def test_subscription_error_is_visible_in_scan_banner(self):
        execution = dict(running=True, position_monitor=dict(heartbeat_stale=False, positions=[],
            entry_scan=dict(cached_scan=dict(scope=62,cached_symbols=50,missing_symbols=12,updated_at=utc_now()),
                reference_feeds=dict(arcus=dict(error='Arcus 行情订阅被拒绝')))))
        annotate_entry_scan(execution, [])
        self.assertIn('50/62', execution['note'])
        self.assertIn('行情订阅被拒绝', execution['note'])


class PublicAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_channels_per_market_are_split_and_cancelled_together(self):
        adapter = ArcusAdapter()
        adapter._market_by_id = {str(i): SimpleNamespace(venue_symbol='M'+str(i)+'-USD',
            canonical_symbol='M'+str(i)+'-PERP') for i in range(62)}
        sockets = []
        class Socket:
            def __init__(self): self.sent=[]; self.closed=False
            async def __aenter__(self): sockets.append(self); return self
            async def __aexit__(self,*args): self.closed=True
            async def send(self, text): self.sent.append(json.loads(text))
            def __aiter__(self): return self
            async def __anext__(self): await asyncio.Future()
        with patch('websockets.connect', side_effect=lambda *a, **kw: Socket()):
            adapter._ws_task = asyncio.create_task(adapter._run_ws())
            for _ in range(10):
                await asyncio.sleep(0)
                if len(sockets)==2 and sum(len(s.sent) for s in sockets)==186: break
            self.assertEqual([len(s.sent) for s in sockets], [99, 87])
            self.assertTrue(adapter._ws_connected)
            self.assertEqual({m['id'] for s in sockets for m in s.sent},
                             {'M'+str(i)+'-USD' for i in range(62)})
            await adapter.close()
        self.assertTrue(all(s.closed for s in sockets))
        self.assertFalse(adapter._ws_connected)

    async def test_rejection_stays_visible_when_other_connection_is_healthy(self):
        adapter = ArcusAdapter()
        try:
            with self.assertRaisesRegex(ArcusSubscriptionError, '100'):
                adapter._handle_ws_message(LIMIT_ERROR)
            adapter._ws_connected = True
            adapter._ws_shard_errors[1] = 'Arcus 行情订阅被拒绝'
            self.assertEqual(adapter.status().status, 'degraded')
            self.assertIn('订阅被拒绝', adapter.status().last_error)
        finally:
            await adapter.close()
