"""Offline account caching regressions: no exchange calls or real orders."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusExecutionAdapter, _ArcusWebSocket, ArcusClient
from src.common.market_data import utc_now
from src.common.rate_limit import RateLimitError
from src.execution.account_snapshot import AccountSnapshotCache
from src.execution.history import TradeHistory
from src.execution.journal import OrderJournal
from src.execution.precheck import expected_entry_positions
from src.execution.worker import ExecutionWorker
from src.lighter.account_feed import LighterAccountFeed
from src.lighter.execution import LighterExecutionAdapter


class SnapshotCacheTests(unittest.TestCase):
    def test_query_age_begins_before_io_and_does_not_reset_on_access(self):
        clock = [100.0]
        def slow():
            clock[0] = 102.1
            return {'balance': 100}
        cache = AccountSnapshotCache(slow, lambda: 'account')
        with patch('src.execution.account_snapshot.time.monotonic', side_effect=lambda: clock[0]):
            cache.refresh()
            self.assertIsNone(cache.get())

    def test_identity_and_notifications_invalidate_before_expiry(self):
        identity, revision = ['a'], [0]
        cache = AccountSnapshotCache(lambda: {}, lambda: identity[0], lambda: revision[0])
        cache.refresh()
        self.assertIsNotNone(cache.get())
        identity[0] = 'b'
        self.assertIsNone(cache.get())
        cache.refresh()
        revision[0] += 1
        self.assertIsNone(cache.get())

    def test_repeated_hits_do_not_extend_the_original_evidence_lifetime(self):
        clock = [100.0]
        read = Mock(return_value={})
        cache = AccountSnapshotCache(read, lambda: 'a')
        with patch('src.execution.account_snapshot.time.monotonic', side_effect=lambda: clock[0]):
            cache.refresh()
            clock[0] = 101.9
            cache.refresh()
            clock[0] = 102.1
            self.assertIsNone(cache.get())
        self.assertEqual(read.call_count, 1)

    def test_reservation_rejects_another_execution_and_release_restores_capacity(self):
        cache = AccountSnapshotCache(lambda: {}, lambda: 'a')
        _, _, token = cache.refresh()
        cache.reserve('first', token)
        with self.assertRaisesRegex(ValueError, '预占'):
            cache.reserve('second', token)
        cache.release('first')
        cache.reserve('second', token)
        self.assertTrue(cache.valid_proof('second', token))

    def test_unknown_result_blocks_cache_until_explicit_reconciliation(self):
        cache = AccountSnapshotCache(lambda: {}, lambda: 'a')
        cache.refresh()
        with cache.mutation():
            self.assertIsNone(cache.get())
        with self.assertRaisesRegex(ValueError, '待核对'):
            cache.refresh()
        cache.reconciled(False)
        self.assertTrue(cache.uncertain)
        cache.reconciled(True)
        cache.refresh()
        self.assertIsNotNone(cache.get())

    def test_known_receipt_invalidates_old_evidence_then_allows_new_read(self):
        cache = AccountSnapshotCache(lambda: {}, lambda: 'a')
        _, _, token = cache.refresh()
        cache.reserve('first', token)
        with cache.mutation() as outcome:
            with self.assertRaises(ValueError):
                cache.refresh()
            outcome['confirmed'] = True
        self.assertIsNone(cache.get())
        self.assertFalse(cache.uncertain)
        cache.refresh()
        self.assertFalse(cache.valid_proof('first', token))

    def test_429_clears_cache_and_honors_full_cooldown_without_retry(self):
        clock = [100.0]
        read = Mock(side_effect=RateLimitError(93))
        cache = AccountSnapshotCache(read, lambda: 'a')
        with patch('src.execution.account_snapshot.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(RateLimitError):
                cache.refresh()
            clock[0] = 150
            with self.assertRaises(ValueError):
                cache.refresh()
            self.assertIsNone(cache.get())
            self.assertEqual(cache.retry_at, 193)
        self.assertEqual(read.call_count, 1)

    def test_singleflight_and_late_read_cannot_restore_pre_mutation_state(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def read():
            entered.set()
            release.wait(2)
            return {}
        cache = AccountSnapshotCache(read, lambda: 'a')
        def refresh():
            try:
                cache.refresh()
            except ValueError as exc:
                errors.append(exc)
        reader = threading.Thread(target=refresh)
        reader.start()
        self.assertTrue(entered.wait(1))
        with cache.mutation() as outcome:
            outcome['confirmed'] = True
        release.set()
        reader.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIsNone(cache.get())

    def test_two_concurrent_reads_share_one_network_query(self):
        entered, release = threading.Event(), threading.Event()
        def read():
            entered.set()
            release.wait(2)
            return {}
        read = Mock(side_effect=read)
        cache = AccountSnapshotCache(read, lambda: 'a')
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.refresh())) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(1))
        release.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(results), 2)


class AccountAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = TradeHistory(Path(self.tmp.name) / 'events.sqlite')
        self.journal = OrderJournal(Path(self.tmp.name) / 'orders.sqlite')

    def attach(self, adapter):
        cache = AccountSnapshotCache(adapter.read_entry_account_state,
                                     adapter.account_snapshot_identity,
                                     adapter.account_snapshot_revision)
        adapter._account_snapshot_cache = cache
        cache.refresh()
        return cache

    def lighter(self, positions=None, orders=None):
        public, bridge = Mock(), Mock()
        public.base_url = 'https://fixture'
        public.config = {'api': {}}
        self.account = {'account_index': 7, 'available_balance': '1000', 'positions': positions or []}
        public.client.get.return_value = ({'accounts': [self.account]}, utc_now())
        public.get_market.side_effect = lambda symbol: ({
            'symbol': symbol, 'market_id': {'BTC': 1, 'SUI': 2}[symbol],
            'market_type': 'perp', 'status': 'active', 'supported_size_decimals': 3,
            'supported_price_decimals': 2, 'min_base_amount': '.001', 'min_quote_amount': '1'}, utc_now())
        bridge.call.return_value = orders or []
        adapter = LighterExecutionAdapter(public, bridge, self.journal, 7, enabled=True)
        adapter.verified_fee_bps = 0
        adapter.get_market_price = Mock(return_value=100)
        return adapter

    def arcus(self, positions=None, orders=None):
        client = Mock()
        client.api_key, client.base_url, client.wallet_address, client.account_index = 'public', 'fixture', 'fixture', 7
        client.ws = _ArcusWebSocket('wss://fixture', None, 'fixture', 7)
        client.account.return_value = {'freeCollateral': '1000'}
        client.positions.return_value = {'positions': positions or []}
        client.orders.return_value = {'orders': orders or []}
        client.market.side_effect = lambda symbol: {
            'market_id': {'BTC': 1, 'SUI': 2}[symbol], 'symbol': symbol + '-USD',
            'tick_size': '.01', 'min_base_amount': '.001', 'min_quote_amount': '1'}
        adapter = ArcusExecutionAdapter(client, self.journal, 'fixture', enabled=True, fee_bps=0)
        adapter.get_market_price = Mock(return_value=100)
        return adapter

    def test_full_account_snapshot_reused_for_other_symbol_without_account_reads(self):
        for factory in (self.lighter, self.arcus):
            with self.subTest(venue=factory.__name__):
                adapter = factory()
                self.attach(adapter)
                with self.history.execution_scope('first'):
                    adapter.precheck_order('BTC', 1, 'buy')
                    self.assertTrue(adapter.consume_entry_precheck('BTC', 1, 'buy', {'bid': 99, 'ask': 100}))
                    adapter.release_entry_precheck()
                with self.history.execution_scope('second'):
                    adapter.precheck_order('SUI', .5, 'sell')
                    self.assertTrue(adapter.consume_entry_precheck('SUI', .5, 'sell', {'bid': 99, 'ask': 100}))
                    adapter.release_entry_precheck()
                if isinstance(adapter, LighterExecutionAdapter):
                    self.assertEqual(adapter.public.client.get.call_count, 1)
                    adapter.bridge.call.assert_called_once_with('active_orders', None)
                else:
                    self.assertEqual(adapter.client.account.call_count, 1)
                    self.assertEqual(adapter.client.orders.call_count, 1)
                    self.assertEqual(adapter.client.positions.call_count, 1)

    def test_other_symbol_position_is_checked_instead_of_assuming_previous_symbol_flat(self):
        cases = ((self.lighter, [{'market_id': 2, 'position': '1', 'sign': 1}]),
                 (self.arcus, [{'marketDisplayName': 'SUI-USD', 'positionSize': '1', 'side': 'long'}]))
        for factory, positions in cases:
            with self.subTest(venue=factory.__name__):
                adapter = factory(positions=positions)
                self.attach(adapter)
                with self.history.execution_scope('a'):
                    adapter.precheck_order('BTC', 1, 'buy')
                    adapter.release_entry_precheck()
                    with self.assertRaisesRegex(ValueError, '敞口'):
                        adapter.precheck_order('SUI', 1, 'buy')

    def test_active_orders_are_filtered_by_actual_target_market(self):
        cases = ((self.lighter, [{'market_index': 2, 'owner_account_index': 7}]),
                 (self.arcus, [{'marketDisplayName': 'SUI-USD', 'orderId': '1', 'status': 'OPEN'}]))
        for factory, orders in cases:
            with self.subTest(venue=factory.__name__):
                adapter = factory(orders=orders)
                self.attach(adapter)
                with self.history.execution_scope('a'):
                    adapter.precheck_order('BTC', 1, 'buy')
                    adapter.release_entry_precheck()
                    with self.assertRaisesRegex(ValueError, '活动订单'):
                        adapter.precheck_order('SUI', 1, 'buy')

    def test_balance_resize_reuses_cached_state_without_platform_reads(self):
        for factory in (self.lighter,self.arcus):
            with self.subTest(venue=factory.__name__):
                adapter = factory(); self.attach(adapter)
                with patch.object(adapter,'read_entry_account_state',side_effect=AssertionError('extra account read')):
                    with self.history.execution_scope('resize'):
                        quantity = adapter.precheck_order('BTC',20,'buy',allow_resize=True,
                            reference_price=100,quantity_step='.001',cost_bps=10)
                        self.assertEqual(float(quantity),9.99)
                        adapter.resize_entry_precheck('BTC',9,'buy')
                        self.assertTrue(adapter.consume_entry_precheck('BTC',9,'buy',{'bid':99,'ask':100}))
                        adapter.release_entry_precheck()

    def test_cached_balance_still_limits_quantity_and_grid_checks_remain(self):
        adapter = self.lighter([{'market_id': 1, 'position': '1', 'sign': 1}])
        self.attach(adapter)
        with self.history.execution_scope('a'):
            with self.assertRaisesRegex(ValueError, '敞口'):
                adapter.precheck_order('BTC', 1, 'buy')
            with self.assertRaisesRegex(ValueError, '保证金'):
                adapter.precheck_order('SUI', 11, 'buy')

    def test_grid_uses_exact_expected_position_even_with_cached_evidence(self):
        adapter = self.lighter([{'market_id': 1, 'position': '1', 'sign': 1}])
        self.attach(adapter)
        with self.history.execution_scope('grid'):
            with expected_entry_positions({(id(adapter), 'BTC'): 1}):
                adapter.precheck_order('BTC', .1, 'buy')
                self.assertTrue(adapter.consume_entry_precheck('BTC', .1, 'buy', {'bid': 99, 'ask': 100}))
                adapter.release_entry_precheck()
            with expected_entry_positions({(id(adapter), 'BTC'): 2}):
                with self.assertRaisesRegex(ValueError, '合计数量或方向不符'):
                    adapter.precheck_order('BTC', .1, 'buy')

    def test_account_notification_invalidates_a_prepared_proof(self):
        adapter = self.arcus()
        self.attach(adapter)
        with self.history.execution_scope('a'):
            adapter.precheck_order('BTC', 1, 'buy')
            adapter.client.ws._handle({'channel': 'positions', 'contents': {'positions': []}})
            self.assertFalse(adapter.consume_entry_precheck('BTC', 1, 'buy', {'bid': 99, 'ask': 100}))

    def test_lighter_real_submit_uses_cached_precheck_and_invalidates_afterwards(self):
        adapter = self.lighter()
        cache = self.attach(adapter)
        receipt = dict(success=True, confirmed=True, status='filled', filled_size=1,
                       average_price=100, order_id='fixture', fees_usdc=0, funding_usdc=0)
        def send(*args):
            self.assertIsNone(cache.get())
            self.assertEqual(cache.mutations, 1)
        adapter.bridge.call.side_effect = send
        quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                     executable=True, instrument_type='perpetual')
        with self.history.execution_scope('a'), patch.object(adapter, '_reconcile', return_value=receipt):
            adapter.precheck_order('BTC', 1, 'buy')
            result = adapter.place_order('BTC', 'buy', 1, 100, 0, 'entry-1', quote)
            adapter.release_entry_precheck()
        self.assertTrue(result['success'])
        self.assertEqual(adapter.public.client.get.call_count, 1)
        self.assertIsNone(cache.get())
        self.assertFalse(cache.uncertain)

    def test_arcus_real_submit_uses_cache_and_invalidates_before_signed_send(self):
        adapter = self.arcus()
        cache = self.attach(adapter)
        def send(*args):
            self.assertIsNone(cache.get())
            self.assertEqual(cache.mutations, 1)
            return {'accepted': False, 'error': 'fixture rejection'}
        adapter.client.submit.side_effect = send
        quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                     executable=True, instrument_type='perpetual')
        with self.history.execution_scope('a'):
            adapter.precheck_order('BTC', 1, 'buy')
            result = adapter.place_order('BTC', 'buy', 1, 100, 0, 'entry-1', quote)
            adapter.release_entry_precheck()
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(adapter.client.account.call_count, 1)
        self.assertEqual(adapter.client.positions.call_count, 1)
        self.assertEqual(adapter.client.orders.call_count, 1)
        self.assertIsNone(cache.get())
        self.assertFalse(cache.uncertain)

    def test_external_position_update_after_precheck_forces_read_and_prevents_submit(self):
        adapter = self.arcus()
        self.attach(adapter)
        quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                     executable=True, instrument_type='perpetual')
        with self.history.execution_scope('a'):
            adapter.precheck_order('BTC', 1, 'buy')
            adapter.client.positions.return_value = {'positions': [
                {'marketDisplayName': 'BTC-USD', 'positionSize': '1', 'side': 'long'}]}
            adapter.client.ws._handle({'channel': 'positions', 'contents': {}})
            result = adapter.place_order('BTC', 'buy', 1, 100, 0, 'entry-1', quote)
            adapter.release_entry_precheck()
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(adapter.client.positions.call_count, 2)
        adapter.client.submit.assert_not_called()

    def test_ambiguous_lighter_receipt_cannot_be_replaced_by_a_later_empty_snapshot(self):
        adapter = self.lighter()
        cache = self.attach(adapter)
        quote = dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                     executable=True, instrument_type='perpetual')
        with self.history.execution_scope('a'), patch.object(adapter, '_reconcile', return_value={
                'success': False, 'confirmed': False, 'status': 'unknown', 'filled_size': 0}):
            adapter.precheck_order('BTC', 1, 'buy')
            result = adapter.place_order('BTC', 'buy', 1, 100, 0, 'entry-1', quote)
            adapter.release_entry_precheck()
        self.assertFalse(result['confirmed'])
        with self.assertRaisesRegex(ValueError, '待核对'):
            cache.refresh()
        self.assertEqual(adapter.public.client.get.call_count, 1)

    def test_arcus_http_order_error_is_not_an_empty_order_list(self):
        client = ArcusClient.__new__(ArcusClient)
        client.wallet_address, client.account_index = 'fixture', 7
        client._get = Mock(side_effect=RuntimeError('unavailable'))
        with self.assertRaises(RuntimeError):
            client.orders()

    def test_bad_snapshot_schemas_do_not_authorize_flat_account(self):
        adapter = self.arcus()
        adapter.client.orders.return_value = {'error': 'unavailable'}
        with self.assertRaises(ValueError):
            self.attach(adapter)
        adapter = self.lighter()
        self.account.pop('positions')
        with self.assertRaises(ValueError):
            self.attach(adapter)

    def test_restarting_a_session_replaces_stopped_cache_without_reusing_its_proof(self):
        adapter = self.lighter()
        previous = self.attach(adapter)
        adapter.stop_account_snapshots()
        adapter.start_account_snapshots()
        self.addCleanup(adapter.stop_account_snapshots)
        self.assertIsNot(adapter._account_snapshot_cache, previous)
        self.assertTrue(previous.stop_event.is_set())
        self.assertFalse(adapter._account_snapshot_cache.stop_event.is_set())

    def test_restarting_session_does_not_erase_unreconciled_order(self):
        adapter = self.lighter()
        previous = self.attach(adapter)
        previous.uncertain = True
        adapter.stop_account_snapshots()
        adapter.start_account_snapshots()
        self.addCleanup(adapter.stop_account_snapshots)
        with self.assertRaisesRegex(ValueError, '待核对'):
            adapter._account_snapshot_cache.refresh()


class AccountFeedAndLifecycleTests(unittest.TestCase):
    def test_lighter_account_feed_does_not_accept_other_account_or_extend_freshness(self):
        feed = LighterAccountFeed(7, 'wss://fixture')
        socket = Mock()
        feed.subscribe(socket)
        self.assertEqual(json.loads(socket.send.call_args.args[0])['channel'], 'account_all/7')
        initial = feed.token()
        feed.on_message({'type': 'update/account_all', 'channel': 'account_all:8'})
        self.assertEqual(feed.token(), initial)
        feed.on_message({'type': 'update/account_all', 'channel': 'account_all:7'})
        self.assertNotEqual(feed.token(), initial)
        changed = feed.token()
        feed.reset()
        self.assertNotEqual(feed.token(), changed)

    def test_worker_starts_and_stops_background_only_inside_live_execution_session(self):
        class Adapter:
            start_account_snapshots = Mock()
            stop_account_snapshots = Mock()
        adapter = Adapter()
        strategy = SimpleNamespace(variational_trader=adapter, lighter_trader=None)
        worker = ExecutionWorker(strategy, ['BTC'], live=False)
        with worker._discovery_session():
            pass
        adapter.start_account_snapshots.assert_not_called()
        worker.live = True
        with self.assertRaises(RuntimeError):
            with worker._discovery_session():
                raise RuntimeError('fixture')
        adapter.start_account_snapshots.assert_called_once()
        adapter.stop_account_snapshots.assert_called_once()


if __name__ == '__main__':
    unittest.main()
