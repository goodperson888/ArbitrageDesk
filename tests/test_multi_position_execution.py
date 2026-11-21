"""Several hedges retain their own accounts while discovery changes routes."""
import tempfile
import copy
import json
import os
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import yaml

from src.api_v2.center_statistics import close_service
from src.common.market_data import utc_now
from src.execution.entry_wait import entry_call, entry_result, entry_wait_scope
from src.execution.orders import direction
from src.execution.position_monitor import PositionMonitor
from src.execution.worker import ExecutionWorker
from src.strategy.arbitrage_strategy import ArbitrageOpportunity, ArbitrageStrategy
from test_execution_latency import NativeTrader


class PortfolioTrader(NativeTrader):
    supports_grid_additions = True

    def __init__(self, price, venue):
        super().__init__(price, venue)
        self.exposures, self.closes, self.funding_reads = {}, [], []

    def validate_order(self, symbol, quantity, reduce_only=False, side=None):
        if not reduce_only and side is not None:
            self.validate_entry_position(symbol, self.exposures.get(symbol, 0), direction(side))
        return super().validate_order(symbol, quantity, reduce_only, side)

    def get_executable_quote(self, symbol, quantity):
        return dict(super().get_executable_quote(symbol, quantity), venue=self.venue)

    def get_position_mark_snapshot(self, symbol):
        return dict(bid=self.price, ask=self.price, updated_at=utc_now(), source='test_stream', fee_bps=0)

    def reference_health(self, symbol):
        return dict(connected=True, quote_status='fresh')

    def place_order(self, symbol, side, size, *args, **kwargs):
        if kwargs.pop('reduce_only', False):
            return self.close_position(symbol, 'short' if direction(side) == 'buy' else 'long', size)
        receipt = super().place_order(symbol, side, size, *args, **kwargs)
        self.exposures[symbol] = self.exposures.get(symbol, 0) + size * (1 if direction(side) == 'buy' else -1)
        return receipt

    def position_snapshot(self, symbol):
        qty = self.exposures.get(symbol, 0)
        return dict(qty=abs(qty), side='long' if qty > 0 else 'short', fetched_at=utc_now())

    def close_position(self, symbol, side, size, **kwargs):
        qty = self.exposures.get(symbol, 0)
        if size > abs(qty) + 1e-8:
            raise AssertionError('close reached the wrong account')
        self.closes.append((symbol, size))
        self.exposures[symbol] = qty + size * (1 if qty < 0 else -1)
        return dict(confirmed=True, status='filled', filled_size=size, average_price=self.price,
                    fees_usdc=0, funding_usdc=0, order_id=self.venue + str(len(self.closes)))

    def reconcile_funding(self, symbol, *args):
        self.funding_reads.append(symbol)
        return dict(complete=True, funding_usdc=0)

    def assert_flat(self, symbol):
        return abs(self.exposures.get(symbol, 0)) < 1e-8


class MultiPositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b, self.c = (PortfolioTrader(price, venue) for price, venue in
            ((102, 'lighter'), (100, 'lighter_rh'), (99, 'arcus')))
        self.config = dict(mode='live', state_path=str(Path(self.tmp.name)/'state.json'),
            trading=dict(position_size=102.02, max_positions=3),
            position_policy=dict(mode='single'), spread_threshold=dict(min=1, max=500),
            risk=dict(slippage_tolerance=5, entry_extra_profit_bps=0),
            wash_trading=dict(trade_interval_min=0, max_trades_per_day=100),
            close_policy=dict(mode='profit', min_profit_usdc=0, max_hold_seconds=0))
        self.s = ArbitrageStrategy(self.config, self.a, self.b)
        self.addCleanup(close_service, self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)
        self.pairs = [(self.a, self.b), (self.a, self.c), (self.b, self.c)]
        self.s.configure_routes(self.pairs)

    def opportunity(self, symbol, route=0):
        first, second = self.pairs[route]
        return ArbitrageOpportunity(symbol, first.price, second.price,
            (first.price-second.price)/first.price*10000, 'variational_higher', 0, datetime.now(), route_index=route)

    def enter(self, symbol, route=0):
        opportunity = self.opportunity(symbol, route)
        self.s.activate_route_for_opportunity(opportunity)
        self.assertTrue(self.s.execute_arbitrage(opportunity))
        position = self.s.positions[-1]
        self.assertEqual((position.variational_venue, position.lighter_venue),
                         tuple(t.venue for t in self.pairs[route]))
        return position

    def worker(self):
        return ExecutionWorker(self.s, ['BTC', 'ETH', 'SOL'], live=True,
            single_position_cycle=False, lock_path=Path(self.tmp.name)/'lease',
            kill_path=Path(self.tmp.name)/'STOP')

    def saved_config(self):
        from src.common.config_loader import ConfigLoader
        path = Path(self.tmp.name)/'config.yaml'
        path.write_text(yaml.safe_dump({'strategy': self.s.config}))
        self.s.config_path = path
        self.s._config_mtime_ns = self.s._read_config_mtime()
        return ConfigLoader(path)

    def save_limits(self, loader, values):
        before = loader.config_path.stat().st_mtime_ns
        loader.update_strategy({'strategy': values})
        stamp = max(before + 1000000, loader.config_path.stat().st_mtime_ns)
        os.utime(loader.config_path, ns=(stamp, stamp))

    def run_holding(self, worker, candidates=None, cycles=1):
        with patch.object(worker, '_candidate_opportunities', return_value=candidates or []) as scan, \
                patch.object(self.s, 'close_reason', return_value=None), \
                patch.object(self.s, 'reconcile_open_positions', return_value='ok'), \
                patch.object(worker.stop_event, 'wait'):
            worker.run(max_cycles=cycles)
        return scan

    def test_saved_one_to_two_adds_a_group_without_flattening_or_restart(self):
        self.s.max_positions = self.s.config['trading']['max_positions'] = 1
        first = self.enter('BTC')
        saved = self.saved_config()
        worker = self.worker()
        worker.single_position_cycle = True  # An older single-cycle launch.
        self.save_limits(saved, {'trading': {'max_positions': 2}})
        scan = self.run_holding(worker, [self.opportunity('ETH', 1)], cycles=2)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual([p.symbol for p in self.s.positions if p.status == 'open'], ['BTC', 'ETH'])
        self.assertIs(self.s.positions[0], first)
        self.assertFalse(worker.single_position_cycle)
        self.assertEqual(sum(len(t.closes) for t in (self.a, self.b, self.c)), 0)

    def test_book_utilization_hot_reload_with_a_held_group_only_affects_new_entries(self):
        first = self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        old_key = self.s.entry_preparation_key()
        old_exit = copy.deepcopy(self.s.get_strategy_status()['close_policy'])
        self.save_limits(saved, {'risk': {'book_utilization_percent': 65}})
        worker._reload_entry_configuration()
        self.assertEqual(self.s.book_utilization_percent, 65)
        self.assertNotEqual(self.s.entry_preparation_key(), old_key)
        self.assertFalse(self.s.entry_limits_status()['pending_other_settings'])
        self.assertEqual(self.s.get_strategy_status()['close_policy'], old_exit)
        self.assertIs(self.s.positions[0], first)
        self.assertEqual(first.status, 'open')
        self.assertEqual(sum(len(t.closes) for t in (self.a, self.b, self.c)), 0)

    def test_capital_settings_reload_with_existing_group_and_preserve_exit_policy(self):
        first=self.enter('BTC')
        saved,worker=self.saved_config(),self.worker()
        old_key=self.s.entry_preparation_key()
        old_exit=copy.deepcopy(self.s.get_strategy_status()['close_policy'])
        self.save_limits(saved,{'trading':dict(capital_reserve_percent=20,capital_reserve_usdc=10,
                                              capital_allocation_mode='remaining_slots')})
        worker._reload_entry_configuration()
        policy=self.s.entry_capital_policy()
        self.assertEqual(policy['remaining_slots'],2)
        self.assertEqual(policy['capital_reserve_percent'],20)
        self.assertEqual(self.s.entry_limits_status()['capital_policy_version'],1)
        self.assertNotEqual(self.s.entry_preparation_key(),old_key)
        self.assertFalse(self.s.entry_limits_status()['pending_other_settings'])
        self.assertEqual(self.s.get_strategy_status()['close_policy'],old_exit)
        self.assertIs(self.s.positions[0],first)
        self.assertEqual(sum(len(t.closes) for t in (self.a,self.b,self.c)),0)

    def test_three_to_two_waits_below_two_before_reusing_a_slot(self):
        first, second, third = (self.enter(symbol, route) for symbol, route in
                                [('BTC', 0), ('ETH', 1), ('SOL', 2)])
        saved, worker = self.saved_config(), self.worker()
        self.save_limits(saved, {'trading': {'max_positions': 2}})
        self.run_holding(worker).assert_not_called()
        self.assertTrue(all(p.status == 'open' for p in (first, second, third)))
        self.assertEqual(sum(len(t.closes) for t in (self.a, self.b, self.c)), 0)
        self.assertTrue(self.s.close_position(first, 'test'))
        self.run_holding(worker).assert_not_called()
        self.assertTrue(self.s.close_position(second, 'test'))
        self.run_holding(worker, [self.opportunity('ETH', 1)]).assert_called_once()
        self.assertEqual([p.symbol for p in self.s.positions if p.status == 'open'], ['SOL', 'ETH'])

    def test_saved_size_and_total_limit_apply_but_exit_policy_stays_frozen(self):
        first = self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        old_exit = copy.deepcopy(self.s.get_strategy_status()['close_policy'])
        self.s.config['execution_notional_cap'] = 80
        self.save_limits(saved, {'trading': {'position_size': 90, 'max_total_position_notional': 80},
                                'close_policy': {'min_profit_usdc': 99}})
        self.run_holding(worker).assert_not_called()
        self.assertEqual(self.s.position_size, 80)
        self.assertEqual(self.s.max_total_position_notional, 80)
        self.assertEqual(self.s.get_strategy_status()['close_policy'], old_exit)
        self.assertEqual(first.status, 'open')
        self.assertTrue(self.s.entry_limits_status()['pending_other_settings'])
        # Pending non-entry settings do not make every tick parse YAML again.
        with patch('yaml.safe_load', side_effect=AssertionError('reparsed unchanged file')):
            self.assertFalse(self.s.reload_runtime_config())
        self.assertTrue(self.s.close_position(first, 'test'))
        worker._reload_entry_configuration()
        self.assertEqual(self.s.close_min_profit_usdc, 99)
        self.assertEqual(self.s.position_size, 80)
        self.assertFalse(self.s.entry_limits_status()['pending_other_settings'])

    def test_follow_saved_size_can_increase_and_decrease_with_a_held_group(self):
        first = self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        self.save_limits(saved, {'trading': {'position_size': 150}})
        worker._reload_entry_configuration()
        status = self.s.entry_limits_status()
        self.assertEqual((self.s.position_size, worker.base_position_size), (150, 150))
        self.assertEqual(status['cap_mode'], 'follow_saved')
        self.assertEqual(status['effective_entry_cap'], 150)
        self.assertEqual(status['limiting_sources'], ['每腿设置'])
        self.save_limits(saved, {'trading': {'position_size': 60}})
        worker._reload_entry_configuration()
        self.assertEqual(self.s.entry_limits_status()['effective_entry_cap'], 60)
        self.assertEqual(first.status, 'open')
        self.assertFalse(self.a.closes or self.b.closes or self.c.closes)

    def test_temporary_ceiling_and_remaining_exposure_source_are_reported(self):
        self.s.config['execution_notional_cap'] = 80
        self.s._apply_entry_limits({'trading': {'position_size': 150, 'max_positions': 2,
                                               'max_total_position_notional': 50}})
        status = self.s.entry_limits_status()
        self.assertEqual(status['cap_mode'], 'temporary')
        self.assertEqual(status['effective_entry_cap'], 50)
        self.assertEqual(status['limiting_sources'], ['总持仓剩余额度'])
        self.s.max_total_position_notional = 0
        status = self.s.entry_limits_status()
        self.assertEqual(status['effective_entry_cap'], 80)
        self.assertEqual(status['limiting_sources'], ['本次临时上限'])

    def test_a_save_during_first_leg_waits_until_the_hedge_is_complete(self):
        self.s.max_positions = self.s.config['trading']['max_positions'] = 2
        self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        original = self.a.place_order
        def save_during_submit(symbol, *args, **kwargs):
            receipt = original(symbol, *args, **kwargs)
            if symbol == 'ETH':
                self.save_limits(saved, {'trading': {'max_positions': 1}})
                self.assertEqual(self.s.max_positions, 2)
            return receipt
        with patch.object(self.a, 'place_order', side_effect=save_during_submit):
            scan = self.run_holding(worker, [self.opportunity('ETH', 1)], cycles=2)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.s.max_positions, 1)
        self.assertEqual([p.symbol for p in self.s.positions if p.status == 'open'], ['BTC', 'ETH'])
        self.assertEqual(sum(len(t.closes) for t in (self.a, self.b, self.c)), 0)

    def test_saving_larger_limit_cannot_clear_stop_or_close_only(self):
        self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        worker.close_only = True
        self.save_limits(saved, {'trading': {'max_positions': 4}})
        self.run_holding(worker).assert_not_called()
        self.assertEqual(self.s.max_positions, 4)
        worker.close_only = False
        worker.kill_path.touch()
        self.save_limits(saved, {'trading': {'max_positions': 5}})
        self.run_holding(worker).assert_not_called()
        self.assertTrue(worker.kill_path.exists())
        self.assertEqual(self.s.max_positions, 5, 'saved limits must apply even while draining')

    def test_explicit_resume_adds_second_group_without_replacing_first(self):
        self.s.max_positions = self.s.config['trading']['max_positions'] = 1
        first = self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        worker.kill_path.touch()
        self.save_limits(saved, {'trading': {'max_positions': 2}})
        self.run_holding(worker, [self.opportunity('ETH', 1)]).assert_not_called()
        self.assertEqual(self.s.max_positions, 2)
        worker.kill_path.unlink()  # Explicit user resume; saving alone did not clear it.
        self.run_holding(worker, [self.opportunity('ETH', 1)])
        self.assertEqual([p.symbol for p in self.s.positions if p.status == 'open'], ['BTC', 'ETH'])
        self.assertIs(self.s.positions[0], first)
        self.assertEqual(sum(len(t.closes) for t in (self.a, self.b, self.c)), 0)

    def test_save_while_scanning_is_checked_before_any_new_order(self):
        self.s.max_positions = self.s.config['trading']['max_positions'] = 2
        self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        before = sum(len(t.sends) for t in (self.a, self.b, self.c))
        def scan_then_reduce():
            self.save_limits(saved, {'trading': {'max_positions': 1}})
            return [self.opportunity('ETH', 1)]
        with patch.object(worker, '_candidate_opportunities', side_effect=scan_then_reduce), \
                patch.object(self.s, 'close_reason', return_value=None), \
                patch.object(self.s, 'reconcile_open_positions', return_value='ok'):
            worker.run(max_cycles=1)
        self.assertEqual(self.s.max_positions, 1)
        self.assertEqual(before, sum(len(t.sends) for t in (self.a, self.b, self.c)))

    def test_saved_new_entry_size_respects_launch_and_cumulative_target_caps(self):
        self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        self.s.config['execution_notional_cap'] = 80
        worker.target_notional, worker.target_filled_notional = 120, 100
        self.save_limits(saved, {'trading': {'position_size': 90}})
        self.run_holding(worker, [self.opportunity('ETH', 1)])
        second = self.s.positions[-1]
        self.assertEqual(second.symbol, 'ETH')
        self.assertLessEqual(max(second.variational_size * second.entry_price_variational,
                                 second.lighter_size * second.entry_price_lighter), 20)
        self.assertEqual(worker.base_position_size, 80)

    def test_invalid_saved_limits_do_not_partially_mutate_running_limits(self):
        self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        original = (self.s.position_size, self.s.max_positions, self.s.max_total_position_notional)
        for invalid in (True, 1.5, float('nan'), 0, 101):
            value = copy.deepcopy(self.s.config)
            value['trading'].update(position_size=50, max_positions=invalid)
            saved.config_path.write_text(yaml.safe_dump({'strategy': value}))
            worker._reload_entry_configuration()
            self.assertEqual((self.s.position_size, self.s.max_positions, self.s.max_total_position_notional), original)

    def test_monitor_acknowledges_applied_limits_while_peer_groups_remain(self):
        self.enter('BTC')
        saved, worker = self.saved_config(), self.worker()
        self.save_limits(saved, {'trading': {'max_positions': 2}})
        self.run_holding(worker)
        monitor = PositionMonitor(self.s)
        self.addCleanup(monitor.close)
        monitor.positions = list(self.s.positions)
        monitor.publish()
        payload = json.loads(monitor.path.read_text())
        self.assertEqual(payload['entry_limits']['max_positions'], 2)
        self.assertEqual(payload['entry_limits']['active_groups'], 1)
        self.assertFalse(payload['entry_limits']['single_position_cycle'])

    def test_initial_launch_cap_does_not_change_the_saved_global_amount(self):
        config = copy.deepcopy(self.config)
        config['execution_notional_cap'] = 12
        other = ArbitrageStrategy(config, self.a, self.b)
        self.addCleanup(close_service, other.market_history)
        self.addCleanup(other.close_diagnostics.writer.flush, 1)
        self.assertEqual(other.position_size, 12)
        self.assertEqual(other.entry_limits_status()['saved_position_size'], 102.02)

    def test_two_routes_close_the_original_accounts_and_leave_peer_hedge_intact(self):
        first, second = self.enter('BTC', 0), self.enter('ETH', 1)
        self.s.activate_route(2)
        self.assertTrue(self.s.close_position(first, 'test'))
        self.assertEqual([row[0] for row in self.a.closes], ['BTC'])
        self.assertEqual([row[0] for row in self.b.closes], ['BTC'])
        self.assertEqual(self.c.closes, [])
        self.assertEqual(second.status, 'open')
        self.assertIs(self.s.variational_trader, self.b)
        self.assertIs(self.s.lighter_trader, self.c)
        self.s.activate_route(0)
        self.assertTrue(self.s.close_position(second, 'test'))
        self.assertEqual([row[0] for row in self.c.closes], ['ETH'])
        receipts = [e['detail']['venue'] for e in self.s.history.recent()
                    if e['event'] == 'close_receipt' and e['detail']['position_id'] == first.id]
        self.assertCountEqual(receipts, ['lighter', 'lighter_rh'])

    def test_exit_prices_fees_and_funding_stay_on_the_recorded_route(self):
        position = self.enter('BTC')
        self.a.price = self.b.price = 101
        self.c.price = 150
        self.s.activate_route(2)
        before = self.c.quote_reads
        estimate = self.s._single_monitor_exit_estimate(position)
        self.assertEqual([estimate['legs'][leg]['quote']['venue'] for leg in ('variational', 'lighter')],
                         ['lighter', 'lighter_rh'])
        self.assertGreater(estimate['net_pnl_usdc'], 0)
        self.assertEqual(self.c.quote_reads, before)
        self.assertEqual(self.c.funding_reads, [])
        self.assertEqual(self.s.close_reason(position), '盈利条件')
        self.assertTrue(self.s.close_position(position, '盈利条件'))
        self.assertEqual(self.c.closes, [])

    def test_execution_history_marks_each_groups_original_pair(self):
        position = self.enter('BTC')
        self.s.activate_route(2)
        self.c.price = 150
        row = self.s._position_mark(position)
        self.assertEqual((row['mark_price_variational'], row['mark_price_lighter']), (102, 100))
        self.assertEqual(row['mark_status'], 'live')
        position.lighter_venue = 'missing'
        row = self.s._position_mark(position)
        self.assertEqual(row['mark_status'], 'unavailable')
        self.assertIsNone(row['unrealized_pnl_usdc'])

    def test_closed_legacy_row_cannot_become_pending_during_a_duplicate_close(self):
        position = self.enter('BTC')
        self.assertTrue(self.s.close_position(position, 'test'))
        position.variational_venue = position.lighter_venue = None
        self.assertFalse(self.s.close_position(position, 'test'))
        self.assertEqual(position.status, 'closed')
        self.assertIsNone(self.s.halted_reason)

    def test_restart_keeps_each_route_when_approved_pairs_are_reordered(self):
        self.enter('BTC', 0)
        self.enter('ETH', 1)
        restored = ArbitrageStrategy(self.config, self.a, self.b)
        self.addCleanup(close_service, restored.market_history)
        restored.configure_routes(list(reversed(self.pairs)))
        restored.activate_route(0)
        self.assertTrue(restored.halted_reason, 'restart still requires read-only reconciliation')
        self.assertEqual([[t.venue for _, t, _, _ in restored._close_legs(p)] for p in restored.positions],
                         [['lighter', 'lighter_rh'], ['lighter', 'arcus']])

    def test_concurrent_estimates_do_not_change_discovery_or_each_others_route(self):
        first, second = self.enter('BTC', 0), self.enter('ETH', 1)
        arrived, release = threading.Barrier(3), threading.Event()
        original = self.a.get_executable_quote
        def blocked(symbol, qty):
            arrived.wait(2)
            if not release.wait(2):
                raise AssertionError('test did not release quotes')
            return original(symbol, qty)
        with patch.object(self.a, 'get_executable_quote', side_effect=blocked), \
                patch.object(self.s, '_single_exit_funding', return_value=dict(variational=0, lighter=0)), \
                ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.s._single_monitor_exit_estimate, p) for p in (first, second)]
            try:
                arrived.wait(2)
                self.s.activate_route(2)
            finally:
                release.set()
            estimates = [job.result(2) for job in jobs]
        self.assertEqual([e['legs']['lighter']['quote']['venue'] for e in estimates], ['lighter_rh', 'arcus'])
        self.assertIs(self.s.variational_trader, self.b)
        self.assertIs(self.s.lighter_trader, self.c)

    def test_missing_or_unapproved_identity_blocks_without_fallback_orders(self):
        position = self.enter('BTC')
        for names in ((None, None), ('lighter', 'missing'), ('lighter', 'lighter')):
            with self.subTest(names=names):
                position.variational_venue, position.lighter_venue = names
                self.s.halted_reason, position.status = None, 'open'
                self.assertFalse(self.s.close_position(position, 'test'))
                self.assertEqual(position.status, 'reconcile_required')
                self.assertTrue(self.s.halted_reason)
        self.assertFalse(any(t.closes for t in (self.a, self.b, self.c)))

    def test_reconciliation_and_remaining_leg_use_original_accounts_without_monitor(self):
        first = self.enter('BTC', 0)
        self.enter('ETH', 1)
        self.s.activate_route(2)
        self.assertEqual(self.s.reconcile_open_positions(0), 'ok')
        self.b.exposures['BTC'] = first.lighter_size = 0
        name, trader, side, qty = self.s._single_leg_snapshot(first)
        self.assertEqual(name, 'variational')
        self.assertIs(trader, self.a)
        self.assertEqual(qty, first.variational_size)

    def test_grid_cannot_authorize_existing_layer_sizes_on_another_pair(self):
        self.s.config['position_policy'] = dict(mode='spread_grid', step_bps=5, max_layers=3,
            layer_notional_usdc=102.02, total_notional_usdc=400)
        self.s.config['close_policy']['max_hold_seconds'] = 3600
        self.s._load_position_policy()
        self.enter('BTC', 0)
        before = len(self.a.sends) + len(self.b.sends) + len(self.c.sends)
        self.s.activate_route(1)
        self.assertFalse(self.s.execute_arbitrage(self.opportunity('BTC', 1)))
        self.assertEqual(before, sum(len(t.sends) for t in (self.a, self.b, self.c)))

    def test_worker_opens_sequential_groups_monitors_both_and_skips_scan_at_cap(self):
        self.s.max_positions = 2
        monitor = SimpleNamespace(entry_blocked=False, poll=Mock(), publish=Mock(), publish_tick=Mock())
        self.s.position_monitor = monitor
        worker = self.worker()
        candidates = iter(([self.opportunity('BTC', 0)], [self.opportunity('ETH', 1)]))
        with patch.object(worker, '_candidate_opportunities', side_effect=lambda: next(candidates)) as scan, \
                patch.object(self.s, 'reconcile_open_positions', return_value='ok'), \
                patch.object(self.s, 'close_reason', return_value=None) as checks, \
                patch.object(worker.stop_event, 'wait'):
            worker.run(max_cycles=3)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual([p.symbol for p in self.s.positions if p.status == 'open'], ['BTC', 'ETH'])
        self.assertEqual([call.args[0].symbol for call in checks.call_args_list], ['BTC', 'BTC', 'ETH'])

    def test_trade_interval_and_total_notional_gate_before_candidate_work(self):
        position = self.enter('BTC')
        worker = self.worker()
        self.s.trade_interval_min = 60
        self.assertEqual(self.s.entry_capacity_reason(), '尚未达到交易间隔')
        self.s.trade_interval_min = 0
        self.s.max_total_position_notional = self.s.open_position_notional()
        self.assertEqual(self.s.entry_capacity_reason(), '已达最大总持仓金额上限')
        with patch.object(worker, '_candidate_opportunities') as scan, \
                patch.object(self.s, 'close_reason', return_value=None), \
                patch.object(worker.stop_event, 'wait'):
            worker.run(max_cycles=2)
        scan.assert_not_called()
        self.assertEqual(position.status, 'open')

    def test_single_cycle_still_waits_for_flat_even_with_a_larger_position_cap(self):
        self.enter('BTC')
        worker = self.worker()
        worker.single_position_cycle = True
        with patch.object(worker, '_candidate_opportunities') as scan, \
                patch.object(self.s, 'close_reason', return_value=None), \
                patch.object(worker.stop_event, 'wait'):
            worker.run(max_cycles=2)
        scan.assert_not_called()

    def test_a_closed_slot_can_be_reused_without_closing_the_other_group(self):
        self.s.max_positions = 2
        first, second = self.enter('BTC', 0), self.enter('ETH', 1)
        worker = self.worker()
        with patch.object(worker, '_candidate_opportunities', return_value=[self.opportunity('SOL', 2)]), \
                patch.object(self.s, 'close_reason', side_effect=lambda p: 'test' if p is first else None):
            worker.run(max_cycles=1)
        self.assertEqual(first.status, 'closed')
        self.assertEqual(second.status, 'open')
        self.assertEqual([p.symbol for p in self.s.positions if p.status == 'open'], ['ETH', 'SOL'])
        self.assertEqual(self.s.reconcile_open_positions(0), 'ok')

    def test_receipt_wait_keeps_other_group_observed_and_stop_does_not_abandon_hedge(self):
        first = self.enter('BTC')
        monitor = SimpleNamespace(entry_blocked=False, poll=Mock(), publish=Mock())
        self.s.position_monitor = monitor
        worker = self.worker()
        self.s.activate_route(1)
        original = self.a.place_order
        release, entered = threading.Event(), threading.Event()
        coordinator = threading.get_ident()
        threads = []
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('monitor never ran during receipt wait')
            return original(*args, **kwargs)
        def poll(*args, **kwargs):
            self.assertTrue(entered.is_set())
            self.assertFalse(kwargs['wait_for_reads'])
            threads.append(threading.get_ident())
            worker.kill_path.touch()
            release.set()
        monitor.poll.side_effect = poll
        with patch.object(self.a, 'place_order', side_effect=slow), \
                patch.object(worker, '_candidate_opportunities', return_value=[self.opportunity('ETH', 1)]), \
                patch.object(self.s, 'close_reason', return_value='盈利条件') as checks, \
                patch.object(self.s, 'close_position') as close, worker._discovery_session():
            result, *_ = worker._attempt_candidates()
        self.assertTrue(result)
        self.assertEqual(first.status, 'open')
        self.assertEqual(self.s.positions[-1].status, 'open')
        self.assertEqual(threads, [coordinator])
        checks.assert_called_with(first)
        close.assert_not_called()
        self.assertIsNone(self.s.entry_wait_callback)
        self.assertEqual(self.s.daily_trades, 2)

    def test_monitor_failure_cannot_discard_a_terminal_transport_result(self):
        owner = threading.get_ident()
        release = threading.Event()
        calls = []
        def observe(symbol):
            calls.append((symbol, threading.get_ident()))
            release.set()
            raise ValueError('optional monitor read failed')
        def transport():
            # A copied entry context in a transport thread must not pump a
            # second coordinator or mutate the strategy from that thread.
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: release.wait(2))
                return entry_result(future)
        with entry_wait_scope(observe, 'ETH'):
            self.assertTrue(entry_call(transport))
        self.assertEqual(calls, [('ETH', owner)])

    def test_legacy_browser_execution_remains_on_the_calling_thread(self):
        self.enter('BTC')
        self.s.parallel_discovery = False
        self.s.entry_wait_callback = Mock()
        owner, threads = threading.get_ident(), []
        original = self.a.place_order
        def record_thread(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)
        with patch.object(self.a, 'place_order', side_effect=record_thread):
            self.enter('ETH')
        self.assertEqual(threads, [owner])
        self.s.entry_wait_callback.assert_not_called()

    def test_background_estimates_for_two_groups_can_start_without_waiting_for_peer(self):
        first, second = self.enter('BTC', 0), self.enter('ETH', 1)
        monitor = PositionMonitor(self.s)
        self.addCleanup(monitor.close)
        self.s.position_monitor = monitor
        entered, release = threading.Barrier(3), threading.Event()
        def estimate(position):
            entered.wait(2)
            release.wait(2)
            return None
        try:
            with patch.object(self.s, '_single_monitor_exit_estimate', side_effect=estimate):
                monitor.poll([first, second], wait_for_reads=False)
                entered.wait(2)
                self.assertEqual(sum(k[0] == 'grid' for k in monitor.jobs), 2)
        finally:
            release.set()

    def test_repeated_updates_of_first_group_do_not_starve_a_later_group(self):
        positions = [self.enter('BTC', 0), self.enter('ETH', 1), self.enter('SOL', 2)]
        monitor = PositionMonitor(self.s)
        self.s.position_monitor = monitor
        self.addCleanup(monitor.close)
        monitor.generations = {p.symbol: monitor._generation([p]) for p in positions}
        completed, pending = Future(), Future()
        completed.set_result(None)
        for p, future in zip(positions, (completed, pending)):
            monitor.jobs[('grid', p.symbol, p.id)] = (future, monitor.generations[p.symbol])
        # First group has just used a slot. Another group still has a slow
        # request. All streams change again before the third group is read.
        monitor._poll_cursor = 1
        started, release, symbols = threading.Event(), threading.Event(), []
        def estimate(position):
            symbols.append(position.symbol)
            started.set()
            release.wait(2)
            return None
        try:
            with patch.object(self.s, '_single_monitor_exit_estimate', side_effect=estimate), \
                    patch.object(monitor, '_exit_stream_revision', return_value='changed'):
                monitor.poll(positions, wait_for_reads=False)
                self.assertTrue(started.wait(1))
                self.assertEqual(symbols, ['SOL'])
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
