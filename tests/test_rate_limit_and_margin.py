"""Regression coverage for untouched markets, REST cooldowns and bidirectional entry."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter
from src.common.live_feed import ArcusBboFeed
from src.common.market_data import PublicClient, utc_now
from src.common.rate_limit import RateLimitGate, RateLimitError, DiscoveryThrottleError
from src.execution.journal import OrderJournal
from src.execution.telemetry import safe_reason, latest_reference_failure
from src.execution.worker import ExecutionWorker
from src.lighter.api_trader import LighterApiTrader
from src.lighter.execution import LighterExecutionAdapter, SDKLoop
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_execution_latency import NativeTrader


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = RateLimitGate('https://exchange.test', self.tmp.name)

    def response(self, retry=None):
        return httpx.Response(429, headers={'Retry-After': retry} if retry else {},
                              request=httpx.Request('GET', 'https://exchange.test/book'))

    def test_retry_after_shared_between_independent_clients_and_origins_isolated(self):
        with self.assertRaises(RateLimitError) as error:
            self.gate.reject(self.response('60'))
        self.assertGreaterEqual(error.exception.retry_after_seconds, 60)
        other = RateLimitGate('https://exchange.test/other-path', self.tmp.name)
        with self.assertRaises(RateLimitError):
            other.check()
        RateLimitGate('https://another.test', self.tmp.name).check()

    def test_http_date_retry_after_is_respected(self):
        with patch('src.common.rate_limit.time.time', return_value=1000):
            with self.assertRaises(RateLimitError) as error:
                self.gate.reject(self.response('Thu, 01 Jan 1970 00:18:20 GMT'))
            self.assertEqual(error.exception.retry_after_seconds, 100)

    def test_discovery_allowance_is_shared_without_extending_exchange_cooldown(self):
        other = RateLimitGate('https://exchange.test/other', self.tmp.name)
        with patch('src.common.rate_limit.time.time', return_value=1000):
            self.gate.reserve_discovery()
            with self.assertRaises(DiscoveryThrottleError) as error:
                other.reserve_discovery()
            self.assertIn('共享节流', safe_reason(error.exception))
            self.assertNotIn('429', safe_reason(error.exception))
            self.assertFalse(hasattr(error.exception, 'retry_after_seconds'))
            other.check()
            self.assertEqual(self.gate._read(), {})
            RateLimitGate('https://other.test', self.tmp.name).reserve_discovery()
        with patch('src.common.rate_limit.time.time', return_value=1002):
            other.reserve_discovery()

    def test_discovery_cache_hits_and_required_reads_do_not_consume_allowance(self):
        session = Mock()
        session.get.return_value = httpx.Response(200, json={'price': 42},
            request=httpx.Request('GET', 'https://exchange.test/book'))
        client = PublicClient('https://exchange.test', session=session)
        client.rate_limit = self.gate
        other = PublicClient('https://exchange.test', session=session)
        other.rate_limit = RateLimitGate('https://exchange.test', self.tmp.name)
        with patch('src.common.rate_limit.time.time', return_value=1000):
            client.get('/book', discovery=True)
            client.get('/book', discovery=True)
            with self.assertRaises(DiscoveryThrottleError):
                other.get('/book', discovery=True)
            other.get('/book')  # Required sizing/precheck/close reads bypass optional quota.
            other.get('/account')
            self.assertEqual(session.get.call_count, 3)
            with self.assertRaises(RateLimitError):
                self.gate.reject(self.response('60'))
            with self.assertRaises(RateLimitError):
                other.get('/required-fresh-book')

    def test_preparation_reserves_next_slot_without_bypassing_shared_interval(self):
        other = RateLimitGate('https://exchange.test', self.tmp.name)
        with patch('src.common.rate_limit.time.time', return_value=1000):
            self.gate.reserve_discovery()
        with patch('src.common.rate_limit.time.time', return_value=1001):
            with self.assertRaises(DiscoveryThrottleError):
                other.reserve_discovery(priority=True)
        with patch('src.common.rate_limit.time.time', return_value=1002):
            with self.assertRaises(DiscoveryThrottleError):
                self.gate.reserve_discovery()  # Reference read must yield.
            other.reserve_discovery(priority=True)
            with self.assertRaises(DiscoveryThrottleError):
                other.reserve_discovery(priority=True)  # Still just one read.
        with patch('src.common.rate_limit.time.time', return_value=1004):
            self.gate.reserve_discovery(priority=True)

    def test_abandoned_priority_ticket_expires_and_retries_do_not_extend_it(self):
        with patch('src.common.rate_limit.time.time', return_value=1000):
            self.gate.reserve_discovery()
        for now in (1001, 1001.5, 1001.9):
            with patch('src.common.rate_limit.time.time', return_value=now):
                with self.assertRaises(DiscoveryThrottleError):
                    self.gate.reserve_discovery(priority=True)
        with patch('src.common.rate_limit.time.time', return_value=1004):
            self.gate.reserve_discovery()  # No candidate ever consumed its slot.

    def test_priority_still_obeys_real_platform_cooldown(self):
        with self.assertRaises(RateLimitError):
            self.gate.reject(self.response('60'))
        with self.assertRaises(RateLimitError):
            self.gate.reserve_discovery(priority=True)

    def test_public_book_preparation_requests_priority_only_inside_context(self):
        from src.execution.preparation import readonly_preparation
        session = Mock()
        session.get.return_value = httpx.Response(200, json={'price': 42},
            request=httpx.Request('GET', 'https://exchange.test/book'))
        client = PublicClient('https://exchange.test', session=session)
        client.rate_limit = Mock()
        client.get('/reference', discovery=True)
        client.rate_limit.reserve_discovery.assert_called_once_with(priority=False)
        with readonly_preparation():
            client.get('/candidate-book', discovery=True)
        client.rate_limit.reserve_discovery.assert_called_with(priority=True)

    def test_parallel_discovery_reserves_only_one_request(self):
        from concurrent.futures import ThreadPoolExecutor
        def reserve(_):
            try:
                RateLimitGate('https://exchange.test', self.tmp.name).reserve_discovery()
                return True
            except DiscoveryThrottleError:
                return False
        with patch('src.common.rate_limit.time.time', return_value=1000), \
                ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 1)

    def test_exponential_backoff_and_expiry(self):
        with patch('src.common.rate_limit.time.time', return_value=1000), \
                patch('src.common.rate_limit.random.uniform', return_value=0):
            for seconds in (5, 10, 20):
                with self.assertRaises(RateLimitError) as error:
                    self.gate.reject(self.response('invalid'))
                self.assertEqual(error.exception.retry_after_seconds, seconds)
        with patch('src.common.rate_limit.time.time', return_value=1021):
            self.gate.check()

    def test_public_client_never_retries_429_even_with_zero_cache_ttl(self):
        session = Mock()
        session.get.return_value = self.response('30')
        client = PublicClient('https://exchange.test', session=session)
        client.rate_limit = self.gate
        for _ in range(3):
            with self.assertRaises(RateLimitError):
                client.get('/book', ttl=0)
        self.assertEqual(session.get.call_count, 1)
        self.assertIn('30 秒', safe_reason(RateLimitError(30)))

    def test_http_rejection_does_not_retry_direct(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(403, request=request))
        client = PublicClient('https://exchange.test', session=httpx.Client(transport=transport))
        client.rate_limit = self.gate
        self.addCleanup(client.close)
        with patch.object(client.session, 'get', wraps=client.session.get) as get:
            with self.assertRaisesRegex(Exception, 'HTTP 403'):
                client.get('/book', ttl=0)
            self.assertEqual(get.call_count, 1)

    def test_405_retries_wait_five_seconds_even_with_zero_quote_ttl(self):
        response = httpx.Response(405, request=httpx.Request('GET', 'https://exchange.test/book'))
        session = Mock(); session.get.return_value = response
        client = PublicClient('https://exchange.test', session=session)
        client.rate_limit = self.gate
        with patch('src.common.market_data.time.monotonic', return_value=100):
            for _ in range(3):
                with self.assertRaisesRegex(Exception, 'HTTP 405'):
                    client.get('/book', ttl=0)
        self.assertEqual(session.get.call_count, 1)
        session.get.return_value = httpx.Response(200, json={'code': 200, 'price': 42},
                              request=httpx.Request('GET', 'https://exchange.test/book'))
        with patch('src.common.market_data.time.monotonic', return_value=105):
            self.assertEqual(client.get('/book', ttl=0)[0]['price'], 42)
        self.assertEqual(session.get.call_count, 2)

    def test_parallel_reference_success_does_not_clear_other_venue_failure(self):
        def event(stage, kind, symbol='BOT'):
            return {'event': kind, 'detail': {'stage': stage + ' 参考价格', 'symbol': symbol}}
        failure = event('Lighter 主网', 'stage_failed')
        arcus = event('Arcus', 'stage_completed')
        lighter = event('Lighter 主网', 'stage_completed')
        self.assertEqual(latest_reference_failure([arcus, failure]), failure)
        self.assertIsNone(latest_reference_failure([lighter, arcus, failure]))
        other = event('Lighter 主网', 'stage_completed', 'BTC')
        self.assertEqual(latest_reference_failure([other, failure]), failure)

    def test_arcus_public_depth_deduplicates_without_caching_account_reads(self):
        client = object.__new__(ArcusClient)
        client.base_url, client.api_key = 'https://exchange.test', 'test'
        client.rate_limit = self.gate
        client._public_cache = {}
        client._public_cache_lock = __import__('threading').RLock()
        client._public_key_locks = tuple(__import__('threading').RLock() for _ in range(32))
        client.http = Mock()
        client.http.get.return_value = httpx.Response(200, json={'bids': [1]},
                                request=httpx.Request('GET', 'https://exchange.test/book'))
        with patch('src.arcus.execution.time.monotonic', return_value=100):
            first = client._get('/book', ttl=.2)
            fetched_at = first['_local_fetched_at']
            first['bids'].append(2)
            self.assertEqual(client._get('/book', ttl=.2)['bids'], [1])
            self.assertEqual(client._get('/book', ttl=.2)['_local_fetched_at'], fetched_at)
            self.assertEqual(client.http.get.call_count, 1)
            client._get('/account'); client._get('/account')
            self.assertEqual(client.http.get.call_count, 3)
        with patch('src.arcus.execution.time.monotonic', return_value=101):
            client._get('/book', ttl=.2)
            self.assertEqual(client.http.get.call_count, 4)

    def test_sdk_429_joins_public_client_cooldown_without_resending(self):
        class TooManyRequests(Exception):
            status = 429
            headers = {'Retry-After': '45'}
        class Transport:
            rate_limit = self.gate
            calls = 0
            async def active_orders(self, market):
                self.calls += 1
                raise TooManyRequests('sensitive raw response')
            async def close(self):
                pass
        transport = Transport()
        loop = SDKLoop(lambda: transport)
        try:
            for _ in range(2):
                with self.assertRaises(RateLimitError) as error:
                    loop.call('active_orders', 1)
                self.assertNotIn('sensitive', str(error.exception))
            self.assertEqual(transport.calls, 1)
            with self.assertRaises(RateLimitError):
                RateLimitGate('https://exchange.test', self.tmp.name).check()
        finally:
            loop.close()


class UntouchedMarketMarginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.public = Mock()
        self.public.get_market.return_value = ({'market_id': 221, 'symbol': 'CASHCAT'}, utc_now())
        self.public.get_market_risk.return_value = ({'market_id': 221, 'symbol': 'CASHCAT',
            'default_initial_margin_fraction': 3333, 'min_initial_margin_fraction': 3333}, utc_now())
        self.public.client.get.return_value = ({'accounts': [{'account_index': 7, 'positions': []}]}, utc_now())
        self.bridge = Mock()
        self.adapter = LighterExecutionAdapter(self.public, self.bridge,
                OrderJournal(Path(self.tmp.name) / 'orders.sqlite'), 7, enabled=True)

    def test_verified_market_default_used_only_for_absent_account_row(self):
        info = self.adapter.leverage_info('CASHCAT')
        self.assertAlmostEqual(info['current_leverage'], 10000 / 3333)
        self.assertEqual(info['setting_source'], 'market_default_no_account_row')
        self.adapter.leverage_info('CASHCAT')
        self.public.client.get.assert_called_once()
        self.public.get_market_risk.assert_called_once_with('CASHCAT')
        self.bridge.call.assert_not_called()

    def test_account_setting_takes_precedence_with_string_ids(self):
        self.public.client.get.return_value = ({'accounts': [{'account_index': '7', 'positions': [
            {'market_id': '221', 'initial_margin_fraction': '20.00', 'margin_mode': 1}]}]}, utc_now())
        info = self.adapter.leverage_info('CASHCAT')
        self.assertEqual(info['current_leverage'], 5)
        self.assertEqual(info['setting_source'], 'account')
        self.public.get_market_risk.assert_not_called()

    def test_existing_row_without_imf_cannot_be_assumed_default(self):
        self.public.client.get.return_value = ({'accounts': [{'account_index': 7, 'positions': [
            {'market_id': 221}]}]}, utc_now())
        with self.assertRaisesRegex(ValueError, '账户已有市场记录但保证金设置缺失') as error:
            self.adapter.leverage_info('CASHCAT')
        self.assertIn('CASHCAT', safe_reason(error.exception))
        self.bridge.call.assert_not_called()

    def test_missing_risk_fails_closed_with_visible_reason(self):
        self.public.get_market_risk.return_value = ({}, utc_now())
        with self.assertRaises(ValueError) as error:
            self.adapter.leverage_info('CASHCAT')
        self.assertEqual(safe_reason(error.exception), 'Lighter CASHCAT 未返回账户杠杆或初始保证金设置')

    def test_metadata_uses_basis_points_even_below_one_hundred(self):
        self.public.get_market_risk.return_value = ({'default_initial_margin_fraction': 100,
                                                    'min_initial_margin_fraction': 50}, utc_now())
        info = self.adapter.leverage_info('CASHCAT')
        self.assertEqual((info['current_leverage'], info['max_leverage']), (100, 200))

    def test_market_risk_rejects_wrong_contract(self):
        public = LighterApiTrader(session=Mock())
        public.get_market = Mock(return_value=({'market_id': 221}, utc_now()))
        public.client.get = Mock(return_value=({'order_book_details': [
            {'symbol': 'BTC', 'market_id': 1}]}, utc_now()))
        with self.assertRaisesRegex(Exception, '合约不匹配'):
            public.get_market_risk('CASHCAT')

    def test_forced_leverage_verification_bypasses_both_setting_caches(self):
        self.adapter.leverage_info('CASHCAT')
        self.public.client.get.return_value = ({'accounts': [{'account_index': 7, 'positions': [
            {'market_id': 221, 'initial_margin_fraction': '50.00', 'margin_mode': 0}]}]}, utc_now())
        self.assertEqual(self.adapter.leverage_info('CASHCAT', force=True)['current_leverage'], 2)
        self.assertEqual(self.public.client.get.call_args.kwargs['ttl'], 0)


class ReferenceAndDirectionTests(unittest.TestCase):
    def test_arcus_reference_is_public_bbo_and_invalidated_on_stale_update(self):
        feed = ArcusBboFeed({'CASHCAT': 'CASHCAT-USD'})
        feed.connected = True
        message = {'channel': 'bbo', 'id': 'CASHCAT-USD', 'contents': {
            'bestBid': {'price': '100'}, 'bestAsk': {'price': '101'},
            'timestamp': int(datetime.now(timezone.utc).timestamp() * 1e6)}}
        feed.on_message(message)
        self.assertEqual(feed.snapshot('CASHCAT')['bid'], 100)
        ws = Mock(); feed.subscribe(ws)
        self.assertEqual({json.loads(c.args[0])['channel'] for c in ws.send.call_args_list},
                         {'bbo', 'predictedFunding'})
        message['contents']['timestamp'] = 1_700_000_000_000_000
        feed.on_message(message)
        self.assertIsNone(feed.snapshot('CASHCAT'))

    def test_arcus_discovery_uses_fresh_ws_but_depth_remains_independent(self):
        client = Mock(api_key='test')
        adapter = ArcusExecutionAdapter(client, Mock(), 'test', enabled=True, fee_bps=0)
        adapter._reference_feed = Mock()
        adapter._reference_feed.snapshot.return_value = {'bid': 100, 'ask': 102}
        self.assertEqual(adapter.get_market_price('BTC'), 101)
        client.bbo.assert_not_called()
        adapter.get_sizing_book('BTC')
        client.orderbook.assert_called_once_with('BTC')
        client.ws.rpc.assert_not_called()

    def test_plan_reverses_discovery_direction_when_live_quotes_reverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = NativeTrader(98, 'lighter'), NativeTrader(100, 'arcus')
            strategy = ArbitrageStrategy({'mode': 'live', 'state_path': str(Path(tmp) / 'state.json'),
                'trading': {'position_size': 12, 'max_positions': 1},
                'spread_threshold': {'min': 5, 'max': 500}, 'risk': {'slippage_tolerance': 10}}, first, second)
            old_opportunity = ArbitrageOpportunity('BTC', 102, 100, 198,
                                                  'variational_higher', 0, datetime.now())
            plan, *_ = strategy.plan_entry(old_opportunity)
            self.assertEqual(plan['direction'], 'buy_variational')
            self.assertGreater(plan['net_edge_bps'], 5)
            self.assertFalse(first.sends + second.sends)

    def test_both_negative_routes_cannot_create_entry_plan(self):
        from src.common.sizing import allocate
        plans = allocate({'bid': 99, 'ask': 101, 'fee_bps': 0},
            {'bids': [{'price': 100, 'size': 10}], 'asks': [{'price': 102, 'size': 10}]},
            12, 3, 5, 500, lighter_fee_bps=0)
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(not p['available'] for p in plans))

    def test_rate_limit_stops_candidate_fallback_immediately(self):
        paused = []
        def execute(item):
            paused.append(True)
            return False
        strategy = SimpleNamespace(execute_arbitrage=execute, last_execution_submitted=False,
            entry_scan_backoff_seconds=lambda: 30 if paused else 0)
        with tempfile.TemporaryDirectory() as tmp:
            worker = ExecutionWorker(strategy, ['A', 'B'], kill_path=Path(tmp) / 'STOP')
            worker._candidate_opportunities = Mock(return_value=[SimpleNamespace(symbol='A'), SimpleNamespace(symbol='B')])
            worker._attempt_candidates()
        self.assertEqual(len(paused), 1)

    def test_entry_backoff_does_not_slow_existing_position_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy = SimpleNamespace(live_blockers=lambda: [], halted_reason=None,
                positions=[SimpleNamespace(status='open')], close_reason=Mock(return_value=None),
                entry_scan_backoff_seconds=lambda: 60, get_strategy_status=lambda: {'positions': 1})
            worker = ExecutionWorker(strategy, ['BTC'], lock_path=Path(tmp) / 'worker.lock',
                                     kill_path=Path(tmp) / 'STOP', live=True)
            worker.stop_event = Mock()
            worker.stop_event.is_set.return_value = False
            worker.run(max_cycles=2)
        worker.stop_event.wait.assert_called_once()
        self.assertGreater(worker.stop_event.wait.call_args.args[0], 0)
        self.assertLessEqual(worker.stop_event.wait.call_args.args[0], .25)
        self.assertEqual(strategy.close_reason.call_count, 2)
