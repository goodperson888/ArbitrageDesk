"""Local push/REST races must never manufacture a fill or resend an order."""
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.common.rate_limit import RateLimitError, RateLimitGate
from src.lighter.execution import OfficialSDKTransport, SDKLoop, terminal_receipt
from src.lighter.order_stream import LighterOrderStream, stream_url
from test_execution import market, order
from src.execution.orders import lighter_plan


def assert_observed_order(case, row, source, path):
    case.assertEqual({k: v for k, v in row.items() if k != '_receipt_diagnostics'}, order())
    diagnostics = row['_receipt_diagnostics']
    case.assertEqual(diagnostics['receipt_source'], source)
    case.assertEqual(diagnostics['receipt_path'], path)
    case.assertGreaterEqual(diagnostics['receipt_lookup_ms'], 0)
    case.assertGreaterEqual(diagnostics['receipt_ws_grace_ms'], 0)
    case.assertIsInstance(diagnostics['receipt_ws_ready'], bool)
    case.assertIsInstance(diagnostics['receipt_rest_started'], bool)


class LighterReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = lighter_plan(market(), 'BTC', 'buy', 1, 100, 10)
        self.sdk = Mock()
        self.transport = OfficialSDKTransport(self.sdk, 7, 5)
        self.stream = LighterOrderStream('unused', 7, Mock(return_value='private-token'),
            lambda row, plan, account, index: terminal_receipt(row, plan, account, index, []) is not None)
        self.transport.order_stream = self.stream
        self.transport._find_order_rest = AsyncMock(return_value=order())
        self.stream.arm(self.plan, 123)
        self.push([])  # Confirm subscription, not execution.

    def push(self, rows, **changes):
        self.stream.handle(dict(type='update/account_all_orders', channel='account_all_orders:7',
                                orders={'1': rows}, **changes))

    async def test_terminal_before_submit_returns_needs_no_rest(self):
        async def create_order(**kwargs):
            self.push([order()])
            await asyncio.sleep(0)
            return None, {'code': 200}, None
        self.sdk.create_order = AsyncMock(side_effect=create_order)
        self.sdk.ORDER_TYPE_LIMIT = self.sdk.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
        self.sdk.DEFAULT_IOC_EXPIRY = 0
        await self.transport.submit(self.plan, 123)
        assert_observed_order(self, await self.transport.find_order(1, 123), 'websocket', 'cached_push')
        self.assertNotIn('_receipt_diagnostics', self.stream.order(1, 123))
        self.transport._find_order_rest.assert_not_awaited()
        self.sdk.create_order.assert_awaited_once()

    async def test_push_head_start_avoids_rest(self):
        async def update():
            await asyncio.sleep(.005)
            self.push([order()])
        update_task = asyncio.create_task(update())
        row = await self.transport.find_order(1, 123)
        assert_observed_order(self, row, 'websocket', 'grace_push')
        self.assertFalse(row['_receipt_diagnostics']['receipt_rest_started'])
        await update_task
        self.transport._find_order_rest.assert_not_awaited()

    async def test_valid_terminal_releases_submit_before_http_without_canceling_send(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def slow_send(**kwargs):
            entered.set()
            try:
                await release.wait()
                return None, {'code': 200}, None
            finally:
                finished.set()
        self.sdk.create_order = AsyncMock(side_effect=slow_send)
        submitting = asyncio.create_task(self.transport.submit(self.plan, 123))
        try:
            await entered.wait()
            self.push([order()])
            result = await asyncio.wait_for(submitting, .2)
            self.assertTrue(result['terminal_observed'])
            self.assertFalse(finished.is_set())
            assert_observed_order(self, await self.transport.find_order(1, 123), 'websocket', 'cached_push')
            self.transport._find_order_rest.assert_not_awaited()
            self.sdk.create_order.assert_awaited_once()
        finally:
            release.set()
            await self.transport._drain_sends()
            await submitting
        self.assertTrue(finished.is_set())

    async def test_same_signer_waits_for_http_cleanup_then_rechecks_deadline(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_send(**kwargs):
            entered.set()
            await release.wait()
            return None, {'code': 200}, None
        self.sdk.create_order = AsyncMock(side_effect=slow_send)
        submitting = asyncio.create_task(self.transport.submit(self.plan, 123))
        second = None
        try:
            await entered.wait()
            self.push([order()])
            await asyncio.wait_for(submitting, .2)
            # The second plan expires while the first HTTP is still returning.
            from src.execution.orders import SubmissionWindowExpired
            import time
            plan = dict(self.plan, submit_deadline_monotonic=time.monotonic() + .02)
            second = asyncio.create_task(self.transport.submit(plan, 124))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            self.sdk.create_order.assert_awaited_once()
            with self.assertRaises(SubmissionWindowExpired):
                await asyncio.wait_for(second, .2)
            self.sdk.create_order.assert_awaited_once()
            self.assertTrue(self.transport._sends, 'expiry cannot cancel the previous signed HTTP')
        finally:
            release.set()
            await self.transport._drain_sends()
            await submitting
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)

    async def test_invalid_push_cannot_release_an_unfinished_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_send(**kwargs):
            entered.set()
            await release.wait()
            return None, {'code': 200}, None
        self.sdk.create_order = AsyncMock(side_effect=slow_send)
        submitting = asyncio.create_task(self.transport.submit(self.plan, 123))
        try:
            await entered.wait()
            for row in (order(status='open', size='0'), dict(order(), reduce_only=True),
                        dict(order(), owner_account_index=8)):
                self.push([row])
                await asyncio.sleep(0)
                self.assertFalse(submitting.done())
            release.set()
            self.assertTrue((await submitting)['accepted'])
        finally:
            release.set()
            await self.transport._drain_sends()
            await submitting

    async def test_late_http_failure_cannot_replace_verified_terminal_or_resend(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_send(**kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError('private transport detail')
        self.sdk.create_order = AsyncMock(side_effect=slow_send)
        submitting = asyncio.create_task(self.transport.submit(self.plan, 123))
        try:
            await entered.wait()
            self.push([order(status='canceled', size='0')])
            self.assertTrue((await asyncio.wait_for(submitting, .2))['terminal_observed'])
            release.set()
            await self.transport._drain_sends()
            row = await self.transport.find_order(1, 123)
            self.assertEqual(terminal_receipt(row, self.plan, 7, 123, [])['filled_size'], 0)
            self.sdk.create_order.assert_awaited_once()
        finally:
            release.set()
            await self.transport._drain_sends()
            await submitting

    async def test_push_interrupts_inflight_slow_rest(self):
        entered, canceled = asyncio.Event(), asyncio.Event()
        async def slow_rest(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()
        self.transport._find_order_rest.side_effect = slow_rest
        task = asyncio.create_task(self.transport.find_order(1, 123))
        await asyncio.wait_for(entered.wait(), .5)
        self.push([order()])
        row = await asyncio.wait_for(task, .2)
        assert_observed_order(self, row, 'websocket', 'race_push')
        self.assertTrue(row['_receipt_diagnostics']['receipt_rest_started'])
        self.assertTrue(canceled.is_set())

    async def test_lagging_history_does_not_force_another_poll_cycle(self):
        for recovered in (None, order(status='open', size='0')):
            self.stream.arm(self.plan, 123)
            entered = asyncio.Event()
            async def lagging(*args):
                entered.set()
                return recovered
            self.transport._find_order_rest.side_effect = lagging
            task = asyncio.create_task(self.transport.find_order(1, 123))
            await asyncio.wait_for(entered.wait(), .5)
            await asyncio.sleep(0)
            self.push([order()])
            assert_observed_order(self, await asyncio.wait_for(task, .2), 'websocket', 'after_lagging_rest')

    async def test_ready_push_bypasses_shared_rest_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = RateLimitGate('https://receipt.test', tmp)
            with self.assertRaises(RateLimitError):
                gate.reject(SimpleNamespace(status_code=429, headers={'Retry-After': '30'}))
            self.transport.rate_limit = gate
            self.push([order()])
            assert_observed_order(self, await self.transport.find_order(1, 123), 'websocket', 'cached_push')
            self.transport._find_order_rest.assert_not_awaited()

    async def test_rest_failure_can_still_receive_imminent_terminal(self):
        entered = asyncio.Event()
        async def limited(*args):
            entered.set()
            raise RateLimitError(30)
        self.transport._find_order_rest.side_effect = limited
        task = asyncio.create_task(self.transport.find_order(1, 123))
        await asyncio.wait_for(entered.wait(), .5)
        self.push([order()])
        assert_observed_order(self, await asyncio.wait_for(task, .2), 'websocket', 'after_rest_error')

    async def test_disconnected_falls_back_without_grace(self):
        self.stream._reset()
        row = await asyncio.wait_for(self.transport.find_order(1, 123), .05)
        assert_observed_order(self, row, 'rest', 'race_rest')
        self.assertFalse(row['_receipt_diagnostics']['receipt_ws_ready'])
        self.assertEqual(row['_receipt_diagnostics']['receipt_ws_grace_ms'], 0)
        self.transport._find_order_rest.assert_awaited_once_with(1, 123)

    async def test_open_missing_partial_or_wrong_identity_use_rest(self):
        invalid = [dict(order(), status='open'), dict(order(), status='pending'),
                   dict(order(size='.5'), status='filled'), dict(order(), owner_account_index=8),
                   dict(order(), is_ask=True), dict(order(), reduce_only=True),
                   dict(order(), initial_base_amount='2'), dict(order(), filled_quote_amount='999'),
                   dict(order(), market_index=2), dict(order(), client_order_index=124),
                   dict(order(), filled_base_amount='NaN'), dict(order(), is_ask=0),
                   dict(order(), owner_account_index=True)]
        for field in ('reduce_only', 'filled_quote_amount', 'order_id'):
            row = order(); row.pop(field); invalid.append(row)
        for row in invalid:
            with self.subTest(row=row):
                self.stream.arm(self.plan, 123)
                self.push([row])
                self.assertIsNone(self.stream.order(1, 123))
        assert_observed_order(self, await self.transport.find_order(1, 123), 'rest', 'race_rest')
        self.transport._find_order_rest.assert_awaited_once()

    async def test_terminal_partial_preserves_actual_size_and_vwap(self):
        row = order(status='canceled-not-enough-liquidity', size='.4')
        self.push([row])
        observed = await self.transport.find_order(1, 123)
        receipt = terminal_receipt(observed, self.plan, 7, 123, [])
        self.assertEqual(receipt['filled_size'], .4)
        self.assertEqual(receipt['average_price'], 100)
        self.assertIsNone(receipt['fees_usdc'])  # Never invent fee evidence.
        self.transport._find_order_rest.assert_not_awaited()

    async def test_receipt_source_survives_validation_and_history_without_private_payload(self):
        from src.execution.history import TradeHistory
        from src.strategy.arbitrage_strategy import ArbitrageStrategy
        self.push([order()])
        row = await self.transport.find_order(1, 123)
        row['_receipt_diagnostics']['auth'] = 'private-token'
        receipt = terminal_receipt(row, self.plan, 7, 123, [])
        self.assertEqual(receipt['receipt_source'], 'websocket')
        self.assertNotIn('auth', receipt)
        self.assertEqual(receipt['filled_size'], 1)
        with tempfile.TemporaryDirectory() as tmp:
            history = TradeHistory(tmp + '/events.sqlite')
            for event in ('entry_receipt', 'close_receipt'):
                ArbitrageStrategy._record_receipt(SimpleNamespace(history=history), event,
                    SimpleNamespace(id='test-position', symbol='BTC'), 'lighter', receipt)
            for event in history.recent():
                self.assertEqual(event['detail']['receipt_path'], 'cached_push')
                self.assertEqual(event['detail']['receipt_source'], 'websocket')
                self.assertNotIn('private-token', json.dumps(event))

    async def test_rest_without_subscription_labels_receipt_and_does_not_mutate_order(self):
        self.transport.order_stream = None
        raw = order()
        self.transport._find_order_rest.return_value = raw
        row = await self.transport.find_order(1, 123)
        assert_observed_order(self, row, 'rest', 'no_stream')
        self.assertTrue(row['_receipt_diagnostics']['receipt_rest_started'])
        self.assertNotIn('_receipt_diagnostics', raw)
        self.transport._find_order_rest.assert_awaited_once_with(1, 123)

    async def test_zero_fill_cancellation_is_terminal(self):
        self.push([order(status='canceled', size='0')])
        receipt = terminal_receipt(await self.transport.find_order(1, 123), self.plan, 7, 123, [])
        self.assertEqual(receipt['filled_size'], 0)
        self.assertEqual(receipt['status'], 'canceled')

    async def test_unarmed_wrong_channel_and_empty_snapshot_cannot_confirm(self):
        self.stream.handle(dict(type='update/account_all_orders', channel='account_all_orders:8',
                                orders={'1': [order()]}))
        self.push([order(index=999)])
        self.push([])
        self.assertIsNone(self.stream.order(1, 123))
        self.assertIsNone(self.stream.order(1, 999))

    async def test_reset_or_expired_intent_cannot_reuse_terminal(self):
        self.push([order()])
        self.stream._reset()
        self.assertIsNone(self.stream.order(1, 123))
        self.stream._intents[(1, 123)]['at'] -= 121
        self.push([order()])
        self.assertIsNone(self.stream.order(1, 123))

    async def test_later_open_update_does_not_overwrite_terminal(self):
        self.push([order()]); self.push([order(status='open', size='0')])
        self.assertEqual(self.stream.order(1, 123), order())

    async def test_no_push_or_history_stays_unknown_without_resubmit(self):
        self.transport._find_order_rest.return_value = None
        self.assertIsNone(await self.transport.find_order(1, 123))
        self.sdk.create_order.assert_not_called()

    async def test_real_rest_gate_is_checked_only_for_fallback(self):
        transport = OfficialSDKTransport(self.sdk, 7, 5)
        with tempfile.TemporaryDirectory() as tmp:
            transport.rate_limit = RateLimitGate('https://receipt.test', tmp)
            with self.assertRaises(RateLimitError):
                transport.rate_limit.reject(SimpleNamespace(status_code=429, headers={'Retry-After': '30'}))
            with self.assertRaises(RateLimitError):
                await transport.find_order(1, 123)
            self.sdk.create_auth_token_with_expiry.assert_not_called()

    async def test_429_in_rest_recovery_records_shared_cooldown(self):
        class TooManyRequests(Exception):
            status = 429
            headers = {'Retry-After': '30'}
        transport = OfficialSDKTransport(self.sdk, 7, 5)
        transport._read_order_history = AsyncMock(side_effect=TooManyRequests('private raw message'))
        with tempfile.TemporaryDirectory() as tmp:
            transport.rate_limit = RateLimitGate('https://receipt.test', tmp)
            with self.assertRaises(RateLimitError) as error:
                await transport.find_order(1, 123)
            self.assertNotIn('private', str(error.exception))
            with self.assertRaises(RateLimitError):
                transport.rate_limit.check()

    async def test_close_wakes_waiters_and_cancels_connection(self):
        waiting = asyncio.create_task(self.stream.wait_order(1, 123))
        self.stream._task = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        await asyncio.wait_for(self.stream.close(), .2)
        self.assertIsNone(await asyncio.wait_for(waiting, .2))
        self.assertIsNone(self.stream._task)

    async def test_connection_auth_ping_and_shutdown_without_exposing_token(self):
        messages, sent = asyncio.Queue(), asyncio.Queue()
        async def receive():
            message = await messages.get()
            if isinstance(message, Exception):
                raise message
            return message
        socket = SimpleNamespace(recv=receive, send=AsyncMock(side_effect=sent.put))
        class Connection:
            async def __aenter__(self):
                return socket
            async def __aexit__(self, *args):
                pass
        with patch('websockets.asyncio.client.connect', return_value=Connection()) as connect:
            self.stream.start()
            subscribed = json.loads(await asyncio.wait_for(sent.get(), .5))
            self.assertEqual(subscribed, dict(type='subscribe', channel='account_all_orders/7', auth='private-token'))
            self.assertEqual(connect.call_args.args[0], 'unused')
            self.assertIs(connect.call_args.kwargs['logger'], self.stream._logger)
            await messages.put(json.dumps(dict(type='subscribed/account_all_orders',
                                                channel='account_all_orders:7', orders={})))
            await messages.put(json.dumps({'type': 'ping'}))
            self.assertEqual(json.loads(await asyncio.wait_for(sent.get(), .5)), {'type': 'pong'})
            self.assertTrue(self.stream.ready)
            await messages.put(asyncio.TimeoutError())
            self.assertEqual(json.loads(await asyncio.wait_for(sent.get(), .5)), {'type': 'ping'})
            self.assertTrue(self.stream.ready)  # Quiet accounts retain the connection.
            await asyncio.wait_for(self.stream.close(), .5)
            self.assertFalse(self.stream.ready)

    async def test_factory_starts_separate_authenticated_deployment_streams(self):
        for url in ('https://mainnet.zklighter.elliot.ai', 'https://api.rh.lighter.xyz'):
            sdk = Mock(); sdk.check_client.return_value = None
            sdk_module = SimpleNamespace(SignerClient=Mock(return_value=sdk))
            with patch.dict('sys.modules', lighter=sdk_module), patch.object(LighterOrderStream, 'start') as start:
                transport = OfficialSDKTransport.from_credentials(7, 5, 'private-key', url=url,
                                                                  proxy_url='http://proxy.test:7890')
                self.assertEqual(transport.order_stream.url, url.replace('https:', 'wss:') + '/stream')
                self.assertEqual(transport.order_stream.proxy_url, 'http://proxy.test:7890')
                start.assert_called_once()

    def test_url_never_contains_auth_or_other_deployments_query(self):
        self.assertEqual(stream_url('https://api.rh.lighter.xyz/?foo=bar'),
                         'wss://api.rh.lighter.xyz/stream')


class SDKLoopPushTests(unittest.TestCase):
    def test_loop_rest_gate_does_not_intercept_ready_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            def factory():
                sdk = Mock(); sdk.close = AsyncMock()
                transport = OfficialSDKTransport(sdk, 7, 5)
                transport.rate_limit = RateLimitGate('https://receipt-loop.test', tmp)
                stream = LighterOrderStream('unused', 7, Mock(),
                    lambda row, plan, account, index: terminal_receipt(row, plan, account, index, []) is not None)
                stream.arm(lighter_plan(market(), 'BTC', 'buy', 1, 100, 10), 123)
                stream.handle(dict(type='update/account_all_orders', channel='account_all_orders:7',
                                   orders={'1': [order()]}))
                transport.order_stream = stream
                return transport
            loop = SDKLoop(factory)
            try:
                with self.assertRaises(RateLimitError):
                    loop.transport.rate_limit.reject(SimpleNamespace(status_code=429, headers={'Retry-After': '30'}))
                assert_observed_order(self, loop.call('find_order', 1, 123), 'websocket', 'cached_push')
                loop.transport.sdk.create_auth_token_with_expiry.assert_not_called()
            finally:
                loop.close()


if __name__ == '__main__':
    unittest.main()
