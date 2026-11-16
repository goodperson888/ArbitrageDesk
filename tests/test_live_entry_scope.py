"""Live scope preparation never submits orders or blocks position management."""
import json
import os
import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.live_scope import LiveScopeController, validate_scope, write_scope_request
from src.execution.scope_preparation import ScopePreparer, assert_new_account_flat
import test_multi_position_execution as portfolio


class LiveEntryScopeTests(unittest.TestCase):
    setUp = portfolio.MultiPositionTests.setUp
    opportunity = portfolio.MultiPositionTests.opportunity
    enter = portfolio.MultiPositionTests.enter
    worker = portfolio.MultiPositionTests.worker

    def controller(self, prepare=None):
        self.w = self.worker()
        self.monitor = SimpleNamespace(started_at=utc_now(), venue_traders={
            t.venue:t for t in (self.a, self.b, self.c)})
        self.dispose = Mock()
        ctrl = LiveScopeController(self.w, self.monitor, ['lighter', 'lighter_rh', 'arcus'],
                                   prepare or Mock(), self.dispose)
        self.addCleanup(ctrl.close)
        return ctrl

    def request(self, ctrl, venues=None, symbols=None, identity='new'):
        return dict(id=identity, pid=os.getpid(), worker_started_at=self.monitor.started_at,
                    requested_at=utc_now(), venues=venues or ['lighter_rh','arcus'],
                    symbols=symbols or ['ETH'])

    def complete(self, ctrl, request=None, adapters=None):
        request = request or self.request(ctrl)
        ctrl.desired = request
        ctrl.status.update(request_id=request['id'])
        prepared = dict(adapters=adapters or [self.b, self.c], commit=Mock())
        future = Future(); future.set_result(prepared)
        ctrl.future = request, future
        return prepared

    def test_apply_retains_old_position_route_and_closes_original_accounts(self):
        old = self.enter('BTC', 0)
        ctrl = self.controller()
        prepared = self.complete(ctrl)
        self.assertTrue(ctrl.poll())
        self.assertEqual(ctrl.status['entry_venues'], ['lighter_rh','arcus'])
        self.assertEqual(self.w.symbols, ('ETH',))
        self.assertFalse(self.s.entry_route_allowed(0))
        self.assertTrue(self.s.entry_route_allowed(2))
        saved = json.loads(self.s.state_path.read_text())
        self.assertEqual(saved['runtime_scope']['managed_venues'], ['lighter','lighter_rh','arcus'])
        self.assertTrue(self.s.close_position(old, 'test'))
        self.assertEqual(len(self.a.closes), 1)
        self.assertEqual(len(self.b.closes), 1)
        self.assertEqual(self.c.closes, [])
        prepared['commit'].assert_called_once()

    def test_slow_preparation_does_not_block_monitor_or_create_orders(self):
        started, release = threading.Event(), threading.Event()
        def prepare(request):
            started.set(); release.wait(2)
            return dict(adapters=[self.b, self.c])
        ctrl = self.controller(prepare)
        old = self.enter('BTC', 0)
        ctrl.desired = self.request(ctrl)
        self.addCleanup(release.set)
        self.assertFalse(ctrl.poll())
        self.assertTrue(started.wait(1))
        self.assertFalse(ctrl.poll())
        self.assertFalse(ctrl.future[1].done())
        self.assertTrue(self.s.close_position(old, 'test'))
        release.set()

    def test_superseded_and_expired_results_are_disposed_without_applying(self):
        ctrl = self.controller()
        prepared = self.complete(ctrl)
        ctrl.desired = self.request(ctrl, identity='newest')
        with patch.object(ctrl, 'prepare', return_value=dict(adapters=[self.a, self.b])):
            self.assertFalse(ctrl.poll())
        self.dispose.assert_called_once_with(prepared)
        self.assertEqual(ctrl.status['entry_venues'], ['lighter','lighter_rh','arcus'])

    def test_save_failure_restores_initial_two_adapter_route(self):
        self.s._route_trader_pairs = []
        ctrl = self.controller()
        prepared = self.complete(ctrl)
        with patch.object(self.s, '_save', side_effect=OSError('disk full')):
            self.assertFalse(ctrl.poll())
        self.assertIs(self.s.variational_trader, self.a)
        self.assertIs(self.s.lighter_trader, self.b)
        self.assertEqual(self.s._route_trader_pairs, [])
        self.assertEqual(self.w.symbols, ('BTC','ETH','SOL'))
        self.assertEqual(ctrl.status['state'], 'failed')
        prepared['commit'].assert_not_called()
        self.dispose.assert_called_once_with(prepared)

    def test_scope_waits_for_existing_prepare_without_waiting_on_future(self):
        ctrl = self.controller()
        self.complete(ctrl)
        pending = Future()
        self.w._preparation_pending['old'] = (pending,)
        self.assertFalse(ctrl.poll())
        self.assertEqual(ctrl.status['entry_venues'], ['lighter','lighter_rh','arcus'])
        pending.set_result(None)
        self.assertTrue(ctrl.poll())

    def test_request_bound_to_process_and_latest_file_and_preserves_stop(self):
        ctrl = self.controller()
        self.w.kill_path.touch()
        before = self.w.kill_path.stat().st_mtime_ns
        request = self.request(ctrl)
        request['pid'] += 1
        write_scope_request(ctrl.path, request)
        ctrl._read_request(); self.assertIsNone(ctrl.desired)
        request = self.request(ctrl)
        write_scope_request(ctrl.path, request)
        ctrl._read_request()
        self.assertEqual(ctrl.desired, request)
        self.assertEqual(self.w.kill_path.stat().st_mtime_ns, before)
        self.assertEqual(ctrl.path.stat().st_mode & 0o777, 0o600)

    def test_late_removed_route_probe_cannot_enter(self):
        ctrl = self.controller()
        old = self.opportunity('SOL', 0)
        self.complete(ctrl); self.assertTrue(ctrl.poll())
        self.s.activate_route_for_opportunity(old)
        self.assertFalse(self.s.execute_arbitrage(old))
        self.assertFalse(self.a.sends or self.b.sends or self.c.sends)

    def test_validation_handles_malformed_values(self):
        for venues, symbols in (([{},'arcus'], ['BTC']), (['arcus','arcus'], ['BTC']),
                                (['lighter','arcus'], []), (['lighter','arcus'], ['X'*65])):
            with self.assertRaises(ValueError):
                validate_scope(venues, symbols)

    def test_preparer_failure_disposes_only_new_connections(self):
        fresh = Mock(venue='arcus')
        fresh._account_snapshot_cache.refresh.return_value = ({'bad':True}, 0, None)
        client = Mock()
        original = Mock(venue='lighter')
        prepare = ScopePreparer(None, None, [original], [], [], self.s)
        with patch('src.execution.scope_preparation.build_scope_adapter', return_value=(fresh, [client])), \
                patch('src.execution.scope_preparation.assert_new_account_flat', side_effect=ValueError('dirty')):
            with self.assertRaises(ValueError):
                prepare(dict(venues=['arcus','lighter'], symbols=['BTC']))
        fresh.stop_account_snapshots.assert_called_once()
        fresh.close_reference_feed.assert_called_once()
        client.close.assert_called_once()
        original.stop_account_snapshots.assert_not_called()

    def test_added_account_exposure_or_order_blocks(self):
        trader = SimpleNamespace(venue='lighter')
        for state in (dict(account=dict(positions=[dict(position=1)]), orders=[]),
                      dict(account=dict(positions=[]), orders=[dict(id=1)])):
            with self.assertRaisesRegex(ValueError, '外部持仓或活动订单'):
                assert_new_account_flat(trader, state)
