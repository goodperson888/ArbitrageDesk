import tempfile
import json
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from src.common.live_feed import VariationalPriceFeed, LighterBookFeed
from src.common.comparison import compare, depth_price, estimate_entry_cost, history_point
from src.common.market_data import utc_now, age_seconds
from src.common.history import HistoryStore
from src.variational.quote_bridge import QuoteBridge


def book_message(kind='subscribed/order_book', nonce=10, begin=0, offset=100, bids=None, asks=None):
    return {'type': kind, 'channel': 'order_book:1', 'order_book': {
        'nonce':nonce, 'begin_nonce':begin, 'offset':offset,
        'bids':bids if bids is not None else [{'price':'100','size':'2'}],
        'asks':asks if asks is not None else [{'price':'101','size':'3'}]}}


class LiveFeedTests(unittest.TestCase):
    def test_adding_markets_keeps_book_and_socket_and_only_subscribes_missing(self):
        feed = LighterBookFeed({'BTC':1})
        feed.connected = True; feed.socket = Mock()
        feed.on_message(book_message())
        before = feed.snapshot('BTC')
        feed.add_markets({'BTC':1,'ETH':2})
        feed.add_markets({'BTC':1,'ETH':2})
        self.assertEqual(feed.socket.send.call_count,1)
        self.assertEqual(json.loads(feed.socket.send.call_args.args[0])['channel'],'order_book/2')
        feed.socket.close.assert_not_called()
        self.assertEqual(feed.snapshot('BTC'),before)
        reconnect = Mock(); feed.subscribe(reconnect)
        self.assertEqual({json.loads(c.args[0])['channel'] for c in reconnect.send.call_args_list},
                         {'market_stats/all','order_book/1','order_book/2'})

    def test_busy_websocket_sends_keepalive_without_idle_timeout(self):
        feed = LighterBookFeed({'BTC': 1})
        socket = Mock()
        def receive():
            if socket.recv.call_count == 3:
                feed.stop_event.set()
            return '{"type":"pong"}'
        socket.recv.side_effect = receive
        with patch('src.common.live_feed.websocket.create_connection', return_value=socket), \
                patch('src.common.live_feed.time.monotonic', side_effect=[0, 10, 31, 31, 40]):
            feed._run()
        socket.ping.assert_called_once()
        socket.close.assert_called_once()
        self.assertEqual(feed.timeout_count, 0)

    def test_connected_stream_and_stale_symbol_are_reported_separately(self):
        feed = LighterBookFeed({'BTC': 1})
        feed.connected = True
        feed.on_message(book_message())
        self.assertEqual(feed.quote_health('BTC')['quote_status'], 'fresh')
        feed.books['BTC']['received_at'] = '2000-01-01T00:00:00Z'
        health = feed.quote_health('BTC')
        self.assertTrue(health['connected'])
        self.assertEqual(health['quote_status'], 'stale')
        feed.stop()
        self.assertFalse(feed.quote_health('BTC')['connected'])

    def test_silent_connection_reconnects_instead_of_pinging_forever(self):
        import websocket
        feed = LighterBookFeed({'BTC': 1})
        socket = Mock()
        socket.recv.side_effect = websocket.WebSocketTimeoutException()
        with patch('src.common.live_feed.websocket.create_connection', return_value=socket), \
                patch('src.common.live_feed.age_seconds', return_value=46), \
                patch.object(feed.stop_event, 'wait', return_value=True):
            feed._run()
        self.assertFalse(feed.connected)
        self.assertIn('TimeoutError', feed.last_disconnect_reason)
        self.assertEqual(feed.books, {})
        socket.close.assert_called_once()

    def test_entry_cost_uses_omni_zero_fee_and_mark_slippage(self):
        c = {'variational_bid': 99, 'variational_ask': 101, 'base_quantity': 10,
             'variational_quote_age': 1}
        cost = estimate_entry_cost(c, 100.5, utc_now())
        self.assertEqual(cost['fee_bps'], 0)
        self.assertEqual(cost['buy']['spread_cost_usdc'], 10)
        self.assertEqual(cost['buy']['midpoint_deviation_usdc'], -5)
        self.assertEqual(cost['buy']['quoted_cost_usdc'], 5)
        self.assertEqual(cost['sell']['quoted_cost_usdc'], 15)
        self.assertAlmostEqual(cost['buy']['quoted_slippage_bps'], (101/100.5-1)*10000)
        self.assertTrue(cost['aligned'])
        self.assertIsNone(cost['actual_slippage_bps'])
        self.assertFalse(cost['executable'])

    def test_entry_cost_stale_missing_mark_and_favorable_quote(self):
        c = {'variational_bid': 99, 'variational_ask': 101, 'base_quantity': 10,
             'variational_quote_age': 50}
        self.assertFalse(estimate_entry_cost(c, 100, utc_now())['aligned'])
        self.assertIsNone(estimate_entry_cost(c)['buy']['quoted_cost_usdc'])
        self.assertEqual(estimate_entry_cost(c, 102)['buy']['quoted_cost_usdc'], -10)
        self.assertFalse(estimate_entry_cost({})['available'])
        self.assertFalse(estimate_entry_cost({**c, 'variational_bid': 102})['available'])

    def test_chart_reference_and_fresh_series_are_separate(self):
        c = {'quote_source': 'variational_public_api', 'variational_quote_age': 50,
             'lighter_quote_age': 1, 'fresh': False,
             'buy_variational_sell_lighter_bps': 2, 'buy_lighter_sell_variational_bps': -3}
        row = {'timestamp': utc_now(), 'comparison': c, 'spread_bps': 4}
        point = history_point(row)
        self.assertEqual(point['reference_buy_variational_sell_lighter_bps'], 2)
        self.assertIsNone(point['buy_variational_sell_lighter_bps'])
        c['variational_quote_age'] = 601
        self.assertIsNone(history_point(row)['reference_buy_variational_sell_lighter_bps'])
        c.update(variational_quote_age=1, lighter_quote_age=20)
        self.assertIsNone(history_point(row)['reference_buy_variational_sell_lighter_bps'])
        c.update(lighter_quote_age=1, fresh=True)
        self.assertEqual(history_point(row)['buy_variational_sell_lighter_bps'], 2)

    def test_nanosecond_exchange_timestamp_on_python39(self):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + '.000000123Z'
        age = age_seconds(now)
        self.assertIsNotNone(age)
        self.assertLess(age, 2)

    def test_lighter_snapshot_delta_delete_and_skipped_offsets(self):
        f=LighterBookFeed({'BTC':1});f.connected=True
        f.on_message(book_message())
        f.on_message(book_message('update/order_book',nonce=20,begin=10,offset=105,
                                 bids=[{'price':'100','size':'0'},{'price':'99','size':'4'}],asks=[]))
        s=f.snapshot('BTC')
        self.assertEqual(s['bids'],[{'price':99.,'size':4.}])
        self.assertEqual(s['asks'][0]['price'],101.)
        self.assertEqual(s['offset'],105)

    def test_lighter_rh_uses_its_own_websocket_origin_and_protocol(self):
        f=LighterBookFeed({'BTC':1}, ws_url='wss://api.rh.lighter.xyz/stream')
        self.assertEqual(f.url, 'wss://api.rh.lighter.xyz/stream')
        f.connected=True
        f.on_message(book_message('subscribed/order_book', nonce=10, offset=100))
        self.assertEqual(f.snapshot('BTC')['source'], 'lighter_websocket')

    def test_lighter_exchange_age_is_not_renewed_by_local_receipt_or_delta(self):
        for origin in ('wss://mainnet.zklighter.elliot.ai/stream', 'wss://api.rh.lighter.xyz/stream'):
            f = LighterBookFeed({'BTC': 1}, ws_url=origin); f.connected = True
            old = (datetime.now(timezone.utc) - timedelta(seconds=20)).timestamp() * 1000
            f.on_message(dict(book_message(), timestamp=old))
            self.assertIsNone(f.snapshot('BTC'), 'a delayed frame cannot masquerade as a fresh book')
            self.assertEqual(f.quote_health('BTC')['quote_status'], 'stale')
            new = datetime.now(timezone.utc).timestamp() * 1000
            f.on_message(dict(book_message('update/order_book', nonce=20, begin=10, offset=105), timestamp=new))
            snapshot = f.snapshot('BTC')
            self.assertEqual(snapshot['time_basis'], 'exchange')
            self.assertEqual(snapshot['nonce'], 20)
            self.assertLessEqual(snapshot['exchange_at'], snapshot['received_at'])
            self.assertEqual(snapshot['fetched_at'], snapshot['exchange_at'])

    def test_lighter_duplicate_frame_never_refreshes_either_timestamp(self):
        f = LighterBookFeed({'BTC': 1}); f.connected = True
        at = datetime.now(timezone.utc).timestamp() * 1000
        f.on_message(dict(book_message(), timestamp=at))
        original = f.snapshot('BTC')
        f.on_message(dict(book_message('update/order_book', offset=100), timestamp=at+100))
        self.assertEqual(f.snapshot('BTC'), original)

    def test_lighter_bad_or_future_exchange_time_cannot_update_book(self):
        for invalid in (None, True, 'NaN', 'bad', 10**50,
                        (datetime.now(timezone.utc) + timedelta(seconds=10)).timestamp()*1000):
            f = LighterBookFeed({'BTC': 1}); f.connected = True
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'timestamp'):
                f.on_message(dict(book_message(), timestamp=invalid))
            self.assertIsNone(f.snapshot('BTC'))

    def test_lighter_missing_or_older_exchange_clock_never_renews_exchange_age(self):
        f = LighterBookFeed({'BTC': 1}); f.connected = True
        at = datetime.now(timezone.utc).timestamp()*1000
        f.on_message(dict(book_message(), timestamp=at))
        original = f.snapshot('BTC')
        f.on_message(book_message('update/order_book', nonce=20, begin=10, offset=105))
        self.assertEqual(f.snapshot('BTC')['fetched_at'], original['fetched_at'])
        self.assertEqual(f.snapshot('BTC')['time_basis'], 'exchange_last_observed')
        # An older valid-clock frame makes the book stale. Keep its sequence
        # so a subsequent fresh delta can recover without a reconnect storm.
        f.on_message(dict(book_message('update/order_book', nonce=30, begin=20, offset=110), timestamp=at-20000))
        self.assertIsNone(f.snapshot('BTC'))
        f.on_message(book_message('update/order_book', nonce=40, begin=30, offset=115))
        self.assertIsNone(f.snapshot('BTC'), 'missing clock cannot downgrade to fresh receipt-only data')
        f.on_message(dict(book_message('update/order_book', nonce=50, begin=40, offset=120), timestamp=at))
        self.assertIsNotNone(f.snapshot('BTC'))

    def test_lighter_unprovided_timestamp_is_explicit_and_clock_lead_is_bounded(self):
        f = LighterBookFeed({'BTC': 1}); f.connected = True
        f.on_message(book_message())
        self.assertEqual(f.snapshot('BTC')['time_basis'], 'receipt_only')
        self.assertIsNone(f.snapshot('BTC')['exchange_at'])
        now = datetime.now(timezone.utc)
        with patch('src.common.live_feed.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            f.on_message(dict(book_message(), timestamp=(now+timedelta(seconds=.2)).timestamp()*1000))
        snapshot = f.snapshot('BTC')
        self.assertEqual(snapshot['fetched_at'], now.isoformat())
        self.assertGreater(snapshot['exchange_at'], snapshot['received_at'], 'preserve raw platform time for diagnosis')

    def test_lighter_gap_duplicate_and_missing_snapshot(self):
        f=LighterBookFeed({'BTC':1});f.connected=True
        with self.assertRaises(ValueError): f.on_message(book_message('update/order_book'))
        f.on_message(book_message())
        f.on_message(book_message('update/order_book',nonce=10,begin=0,offset=100,bids=[]))
        self.assertEqual(f.snapshot('BTC')['bids'][0]['size'],2)
        with self.assertRaisesRegex(ValueError,'sequence gap'):
            f.on_message(book_message('update/order_book',nonce=20,begin=8,offset=105))

    def test_lighter_reconnect_and_stale_book(self):
        f=LighterBookFeed({'BTC':1});f.connected=True;f.on_message(book_message())
        f.books['BTC']['received_at']='2000-01-01T00:00:00Z'
        self.assertIsNone(f.snapshot('BTC'))
        f.reset();self.assertIsNone(f.snapshot('BTC'))

    def test_variational_only_correct_market_and_timestamp(self):
        f=VariationalPriceFeed(['BTC','ETH','US100']);f.connected=True
        msg={'channel':'instrument_price:P-BTC-USDC-3600','pricing':{
            'price':'100','underlying_price':'101','timestamp':utc_now()}}
        f.on_message(msg)
        self.assertEqual(f.snapshot('BTC')['mark_price'],100)
        msg['pricing']['timestamp']='2000-01-01T00:00:00Z';msg['pricing']['price']='200'
        f.on_message(msg);self.assertEqual(f.snapshot('BTC')['mark_price'],100)
        self.assertIsNone(f.snapshot('ETH'));self.assertNotIn('US100',f.symbols)
        f.connected=False;self.assertIsNone(f.snapshot('BTC'))

    def test_depth_vwap_and_insufficient_depth(self):
        levels=[{'price':100,'size':1},{'price':110,'size':2}]
        self.assertEqual(depth_price(levels,2)['vwap'],105)
        self.assertIsNone(depth_price(levels,4)['vwap'])

    def test_directional_spreads_use_crossed_sides_and_keep_negative(self):
        row={'details':{'variational':{'bid':100,'ask':102,'quote_updated_at':utc_now()}}}
        book={'bids':[{'price':101,'size':20}],'asks':[{'price':103,'size':20}],'fetched_at':utc_now()}
        c=compare(row,book,1000)
        self.assertAlmostEqual(c['buy_variational_sell_lighter_bps'],(101/102-1)*10000)
        self.assertAlmostEqual(c['buy_lighter_sell_variational_bps'],(100/103-1)*10000)
        self.assertFalse(c['executable']);self.assertTrue(c['reference_only'])
        self.assertIn('Variational 为缓存参考报价，非当前数量 RFQ',c['reasons'])

    def test_bridge_quantity_used_on_both_legs(self):
        row={'details':{'variational':{'bid':100,'ask':102,'quote_size':2,'quote_source':'variational_browser_quote','quote_updated_at':utc_now()}}}
        book={'bids':[{'price':101,'size':1},{'price':100,'size':1}],
              'asks':[{'price':103,'size':1},{'price':104,'size':1}],'fetched_at':utc_now()}
        c=compare(row,book,99999)
        self.assertEqual(c['base_quantity'],2)
        self.assertEqual(c['lighter_sell_vwap'],100.5)
        self.assertEqual(c['lighter_buy_vwap'],103.5)

    def test_slippage_observation_separates_page_estimate_from_lighter_depth(self):
        row={'details':{'variational':{
            'bid':100,'ask':102,'quote_size':2,'quote_source':'variational_browser_quote',
            'quote_updated_at':utc_now(),
            'page_slippage':{'side':'buy','estimated_bps':3},
        }}}
        book={'bids':[{'price':101,'size':1},{'price':100,'size':1}],
              'asks':[{'price':103,'size':1},{'price':104,'size':1}],
              'fetched_at':utc_now()}
        c=compare(row,book,99999)
        a=c['slippage']['directions']['a']
        b=c['slippage']['directions']['b']
        self.assertEqual(a['variational_bps'],3)
        self.assertAlmostEqual(a['lighter_bps'],(1-100.5/101)*10000)
        self.assertEqual(a['known_legs'],2)
        self.assertIsNone(b['variational_bps'])
        self.assertAlmostEqual(b['lighter_bps'],(103.5/103-1)*10000)

    def test_sqlite_history_survives_reload_and_preserves_null(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'history.sqlite';first=HistoryStore(p)
            first.append('BTC',{'timestamp':utc_now(),'buy_variational_sell_lighter_bps':None})
            second=HistoryStore(p)
            self.assertIsNone(second.read('BTC')[0]['buy_variational_sell_lighter_bps'])
            self.assertEqual(second.read('ETH'),[])

    def test_history_keeps_service_downtime_as_gap(self):
        with tempfile.TemporaryDirectory() as t:
            store = HistoryStore(Path(t)/'history.sqlite')
            with patch('src.common.history.time.time', return_value=1000):
                store.append('BTC', {'timestamp': '1970-01-01T00:16:40Z',
                                     'reference_buy_variational_sell_lighter_bps': 2})
            with patch('src.common.history.time.time', return_value=1020):
                store.append('BTC', {'timestamp': '1970-01-01T00:17:00Z',
                                     'reference_buy_variational_sell_lighter_bps': 3})
                points = store.read('BTC')
            self.assertEqual(len(points), 3)
            self.assertTrue(points[1]['gap'])
            self.assertIsNone(points[1].get('reference_buy_variational_sell_lighter_bps'))


class BridgeTests(unittest.TestCase):
    def test_current_quote_cost_limits_and_funding_are_allowlisted(self):
        b=QuoteBridge()
        p={**self.payload(), 'estimated_fees_bid':0, 'estimated_fees_ask':0,
           'page_funding_bps':.92, 'page_funding_interval_s':28800,
           'qty_limits':{s:{'min_qty_tick':.001,'min_qty':.001,'max_qty':10,'private':'secret'} for s in ('bid','ask')}}
        b.ingest(p)
        quote=b.snapshot('BTC')
        self.assertEqual(quote['fee_bps'],0)
        self.assertEqual(quote['funding']['rate_bps'],.92)
        self.assertEqual(quote['qty_limits']['bid']['min_qty_tick'],.001)
        self.assertNotIn('secret',str(quote))
        b.ingest({**p,'observed_at':utc_now(),'estimated_fees_ask':1})
        self.assertIsNone(b.snapshot('BTC')['fee_bps'])
        with self.assertRaises(ValueError):
            b.ingest({**p,'observed_at':utc_now(),'page_funding_interval_s':0})

    def test_page_slippage_allowlist_and_atomic_rejection(self):
        b=QuoteBridge()
        p={**self.payload(), 'page_side':'buy', 'page_estimated_slippage_bps':2,
           'page_max_slippage_bps':50}
        b.ingest(p)
        self.assertEqual(b.snapshot('BTC')['page_slippage']['estimated_bps'],2)
        with self.assertRaises(ValueError):
            b.ingest({**p,'observed_at':utc_now(),'bid':99,'page_max_slippage_bps':-1})
        self.assertEqual(b.snapshot('BTC')['bid'],100)

    def payload(self):
        return {'source_url':'https://omni.variational.io/api/quotes/indicative',
                'symbol':'BTC','instrument_type':'perpetual_future','qty':'0.01',
                'bid':'100','ask':'101','observed_at':utc_now()}

    def test_allowlist_and_no_extra_secret_retention(self):
        b=QuoteBridge();p=self.payload();p['cookie']='sensitive';p['wallet']='secret'
        b.ingest(p);s=b.snapshot('BTC')
        self.assertEqual(s['quote_size'],.01)
        self.assertNotIn('cookie',s);self.assertNotIn('wallet',s)
        self.assertFalse(s['executable'])
        self.assertEqual(s['quote_timestamp_kind'],'browser_observed')

    def test_rejects_server_cached_quote_even_when_browser_just_received_it(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=48)).isoformat()
        b = QuoteBridge()
        with self.assertRaisesRegex(ValueError, '服务端报价已过期'):
            b.ingest({**self.payload(), 'timestamp': stale})
        self.assertIsNone(b.snapshot('BTC'))
        self.assertEqual(b.status()['last_rejection']['symbol'], 'BTC')

    def test_keeps_fresh_server_timestamp_as_exchange_time(self):
        b = QuoteBridge()
        b.ingest({**self.payload(), 'timestamp': utc_now()})
        self.assertEqual(b.snapshot('BTC')['quote_timestamp_kind'], 'exchange')

    def test_reject_wrong_market_endpoint_stale_crossed_and_bad_size(self):
        for changes in ({'symbol':'US100'},{'source_url':'https://evil.example'},
                        {'observed_at':'2000-01-01T00:00:00Z'},{'bid':'102'},{'qty':'NaN'},
                        {'instrument_type':'swap'}):
            with self.subTest(changes=changes),self.assertRaises((ValueError,RuntimeError)):
                QuoteBridge().ingest({**self.payload(),**changes})

    def test_expiry_duplicate_and_timestamp(self):
        b=QuoteBridge();p=self.payload();b.ingest(p)
        with self.assertRaises(ValueError):b.ingest(p)
        b.quotes['BTC']['observed_at']='2000-01-01T00:00:00Z'
        self.assertIsNone(b.snapshot('BTC'))
        b.quotes['BTC']['observed_at']='invalid'
        self.assertIsNone(b.snapshot('BTC'))
