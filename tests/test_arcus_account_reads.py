"""Concurrent account GETs share transport, never completed or raced evidence."""
import threading
import httpx
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter, _AccountReadFlights
from src.common.rate_limit import RateLimitError


class ArcusAccountReadTests(unittest.TestCase):
    def test_monitor_reuses_one_account_for_symbols_without_renewing_observation_time(self):
        read = Mock(return_value={'positions': [{'symbol': 'GME', 'size': 1}]})
        with patch('src.arcus.execution.time.monotonic', return_value=10):
            first = self.reads.read_monitor('/positions', read, self.token)
        first[0]['positions'][0]['size'] = 99
        with patch('src.arcus.execution.time.monotonic', return_value=10.4):
            second = self.reads.read_monitor('/positions', read, self.token)
        read.assert_called_once()
        self.assertEqual(second[0]['positions'][0]['size'], 1)
        self.assertEqual(first[1], second[1])
        with patch('src.arcus.execution.time.monotonic', return_value=10.6):
            self.reads.read_monitor('/positions', read, self.token)
        self.assertEqual(read.call_count, 2)

    def test_monitor_cache_never_authorizes_later_foreground_read(self):
        read = Mock(side_effect=[{'size': 1}, {'size': 2}])
        self.reads.read_monitor('/positions', read, self.token)
        self.assertEqual(self.reads.read('/positions', read, self.token), {'size': 2})
        self.assertEqual(read.call_count, 2)

    def test_slow_monitor_read_is_not_cached_with_a_new_completion_time(self):
        clock = [10.]
        def read():
            clock[0] += 1
            return {'positions': []}
        reader = Mock(side_effect=read)
        with patch('src.arcus.execution.time.monotonic', side_effect=lambda: clock[0]):
            self.reads.read_monitor('/positions', reader, self.token)
            self.reads.read_monitor('/positions', reader, self.token)
        self.assertEqual(reader.call_count, 2)

    def test_cached_monitor_result_is_invalidated_by_notifications_and_submissions(self):
        read = Mock(return_value={'positions': []})
        self.reads.read_monitor('/positions', read, self.token)
        self.revision += 1
        self.reads.read_monitor('/positions', read, self.token)
        self.reads.invalidate()
        self.reads.read_monitor('/positions', read, self.token)
        self.assertEqual(read.call_count, 3)

    def test_failed_read_cannot_leave_a_reusable_monitor_success(self):
        read = Mock(side_effect=[{'size': 1}, httpx.PoolTimeout('private address'), {'size': 2}])
        self.reads.read_monitor('/positions', read, self.token)
        with self.assertRaises(httpx.PoolTimeout):
            self.reads.read('/positions', read, self.token)
        self.assertEqual(self.reads.read_monitor('/positions', read, self.token)[0], {'size': 2})

    def test_public_pool_exhaustion_does_not_block_account_reads_or_move_receipt_queries(self):
        with patch('src.arcus.execution.httpx.Client') as transport:
            client = ArcusClient('test', 0, '01'*32, base_url='https://fixture.invalid')
        public, account = Mock(), Mock()
        client.http, client.account_http, client.rate_limit = public, account, Mock()
        public.get.side_effect = httpx.PoolTimeout('saturated public pool')
        account.get.return_value = httpx.Response(200, json={'positions': []},
            request=httpx.Request('GET', 'https://fixture.invalid/v1/positions'))
        self.assertEqual(client.positions(), {'positions': []})
        self.assertEqual(client._get('/v1/account'), {'positions': []})
        self.assertEqual(account.get.call_count, 2)
        with self.assertRaises(httpx.PoolTimeout):
            client.orders()
        with self.assertRaises(httpx.PoolTimeout):
            client._get('/v1/bbo/GME')
        self.assertEqual(account.get.call_count, 2)
        self.assertTrue(all(call.kwargs['trust_env'] is False for call in transport.call_args_list))
        client.close()
        public.close.assert_called_once()
        account.close.assert_called_once()

    def test_monitor_symbol_projection_retains_shared_time_and_rejects_missing_collection(self):
        adapter = object.__new__(ArcusExecutionAdapter)
        adapter.client = Mock()
        stamp = '2026-11-19T08:00:00Z'
        adapter.client.monitor_positions.return_value = ({'positions': [
            {'symbol': 'GME', 'size': 3}, {'symbol': 'WLFI', 'size': -2}]}, stamp)
        self.assertEqual(adapter.monitor_position_snapshot('GME')['qty'], 3)
        result = adapter.monitor_position_snapshot('WLFI')
        self.assertEqual((result['qty'], result['side'], result['fetched_at']), (2, 'short', stamp))
        adapter.client.monitor_positions.return_value = ({}, stamp)
        with self.assertRaisesRegex(ValueError, '持仓集合缺失'):
            adapter.monitor_position_snapshot('GME')

    def setUp(self):
        self.reads = _AccountReadFlights()
        self.revision = 0
        self.token = lambda: self.revision

    def run_overlapping(self, reader):
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()

        class ObservedFuture(Future):
            def result(self, *args, **kwargs):
                if not self.done():
                    joined.set()
                return super().result(*args, **kwargs)

        def blocked():
            entered.set()
            if not release.wait(2):
                raise AssertionError('test did not release account read')
            return reader()

        with patch('src.arcus.execution.Future', ObservedFuture), ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(self.reads.read, '/positions', blocked, self.token)
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.reads.read, '/positions', blocked, self.token)
                self.assertTrue(joined.wait(1))
            finally:
                release.set()
            return first, second

    def test_overlapping_positions_make_one_request_and_return_independent_payloads(self):
        read = Mock(return_value={'positions': [{'symbol': 'GME', 'size': 1}]})
        first, second = self.run_overlapping(read)
        read.assert_called_once()
        a, b = first.result(), second.result()
        a['positions'][0]['size'] = 99
        self.assertEqual(b['positions'][0]['size'], 1)
        self.assertFalse(self.reads.pending)

    def test_429_is_shared_without_an_inline_retry_and_next_call_can_recover(self):
        read = Mock(side_effect=RateLimitError(93))
        for future in self.run_overlapping(read):
            with self.assertRaises(RateLimitError):
                future.result()
        read.assert_called_once()
        self.assertFalse(self.reads.pending)
        self.assertEqual(self.reads.read('/positions', lambda: {'positions': []}, self.token),
                         {'positions': []})

    def test_completed_reads_are_never_reused_by_later_prechecks(self):
        read = Mock(side_effect=[{'size': 1}, {'size': 2}])
        self.assertEqual(self.reads.read('/positions', read, self.token)['size'], 1)
        self.assertEqual(self.reads.read('/positions', read, self.token)['size'], 2)
        self.assertEqual(read.call_count, 2)

    def test_different_endpoints_remain_parallel(self):
        entered, release = threading.Event(), threading.Event()
        def positions():
            entered.set()
            release.wait(2)
            return {}
        with ThreadPoolExecutor(1) as pool:
            try:
                pending = pool.submit(self.reads.read, '/positions', positions, self.token)
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.reads.read('/account', lambda: {'free': 12}, self.token), {'free': 12})
            finally:
                release.set()
            pending.result()

    def test_notification_during_read_discards_the_old_evidence(self):
        def read():
            self.revision += 1
            return {'positions': []}
        with self.assertRaisesRegex(ValueError, '状态已变化'):
            self.reads.read('/positions', read, self.token)

    def test_submission_boundary_cannot_join_an_old_inflight_read(self):
        entered, release = threading.Event(), threading.Event()
        def old():
            entered.set()
            release.wait(2)
            return {'size': 0}
        with ThreadPoolExecutor(1) as pool:
            try:
                pending = pool.submit(self.reads.read, '/positions', old, self.token)
                self.assertTrue(entered.wait(1))
                self.reads.invalidate()
                self.assertEqual(self.reads.read('/positions', lambda: {'size': 1}, self.token), {'size': 1})
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, '状态已变化'):
                pending.result()

    def test_client_uses_coalescer_only_for_account_and_positions(self):
        client = object.__new__(ArcusClient)
        client.wallet_address, client.account_index = 'test', 0
        client.ws = SimpleNamespace(_ready=threading.Event(), account_revision=0)
        client._account_reads = Mock()
        client._account_reads.read.return_value = {'data': {'positions': []}}
        client._get = Mock(return_value={'orders': []})
        self.assertEqual(client.positions(), {'positions': []})
        client.account()
        self.assertEqual([call.args[0] for call in client._account_reads.read.call_args_list],
                         ['/v1/positions', '/v1/account'])
        client.orders()
        client._get.assert_called_once_with('/v1/orders', {'address': 'test', 'accountIndex': 0})
        client._account_changed()
        client._account_reads.invalidate.assert_called_once()

    def test_submit_and_cancel_invalidate_even_when_transport_fails(self):
        client = object.__new__(ArcusClient)
        client.wallet_address, client.account_index = 'test', 0
        client.signing_key, client.api_key, client.order_timeout = None, 'test', 1
        client._account_reads = self.reads
        client.market = Mock(return_value={'market_id': 1, 'tick_size': '.01', 'step_size': '.01'})
        observed_epochs = []
        def fail(*args, **kwargs):
            observed_epochs.append(self.reads.epoch)
            raise RuntimeError('transport unavailable')
        client.ws = SimpleNamespace(rpc=fail)
        with patch('src.arcus.execution.build_order_payload', return_value=('typed', {}, 1)), \
                patch('src.arcus.execution.build_cancel_payload', return_value=('typed', {}, 1)):
            with self.assertRaises(RuntimeError):
                client.submit('GME', 'buy', 1, 10, 'test')
            self.assertEqual(self.reads.epoch, 2)
            with self.assertRaises(RuntimeError):
                client.cancel('GME', 'order-1')
            self.assertEqual(self.reads.epoch, 4)
        self.assertEqual(observed_epochs, [1, 3])


if __name__ == '__main__':
    unittest.main()
