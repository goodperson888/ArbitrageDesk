import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock

from src.common.market_data import utc_now
from src.execution.position_monitor import PositionReadUnavailable
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position
from test_spread_grid import GridTrader


class ProfitExitTests(unittest.TestCase):
    def test_faster_monitor_estimate_skips_quote_stage_writes_and_preserves_profit_math(self):
        expected = self.s._single_exit_estimate(self.p)
        with patch.object(self.s.history, 'append') as append:
            monitored = self.s._single_monitor_exit_estimate(self.p)
        append.assert_not_called()
        self.assertAlmostEqual(monitored['net_pnl_usdc'], expected['net_pnl_usdc'])
        self.assertEqual(monitored['position_fingerprint'], expected['position_fingerprint'])
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_expired_exit_window_is_reported_as_close_quote_expiry(self):
        estimate = self.s._single_exit_estimate(self.p)
        for leg in estimate['legs'].values():
            leg['quote']['submit_deadline_monotonic'] = 9
        with patch('src.execution.orders.time.monotonic', return_value=10), \
                self.assertRaisesRegex(PositionReadUnavailable, '平仓报价执行窗口已过期'):
            self.s._validate_single_exit_plan(self.p, estimate)

    def test_reference_profit_and_protected_profit_are_both_explicit(self):
        self.a.fee_bps, self.b.fee_bps = 1, 4
        estimate = self.s._single_exit_estimate(self.p)
        expected = .2 - .01 - .01 - .04
        self.assertAlmostEqual(estimate['reference_net_pnl_usdc'], expected)
        self.assertAlmostEqual(estimate['movement_cost_usdc'], expected - estimate['net_pnl_usdc'])
        self.assertAlmostEqual(estimate['reference_closing_fee_usdc'], .05)
        self.assertEqual(estimate['closing_fee_source'], 'verified_account_rate_estimate')

    def test_converged_but_unprofitable_decision_is_recorded_without_order_or_log_storm(self):
        self.p.entry_price_variational = 100.02
        with patch.object(self.s, '_record_close_diagnostic') as record:
            self.assertIsNone(self.s.close_reason(self.p))
            self.assertIsNone(self.s.close_reason(self.p))
        record.assert_called_once()
        self.assertIn('保护价净盈利未达标', record.call_args.args[1])
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_old_exit_estimate_is_rejected_after_actual_entry_price_correction(self):
        monitor = Mock()
        estimate = self.s._single_exit_estimate(self.p)
        monitor.single_estimate.return_value = estimate
        self.s.position_monitor = monitor
        self.p.entry_price_lighter = 99.99
        with self.assertRaisesRegex(PositionReadUnavailable, '持仓或开仓费用已变化'):
            self.s._single_profit_estimate(self.p)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.a, self.b = GridTrader(100, 'lighter'), GridTrader(100, 'arcus')
        self.a.exposure, self.b.exposure = -1, 1
        self.s = ArbitrageStrategy({'mode': 'live', 'state_path': str(Path(tmp.name)/'state.json'),
            'funding_accounting_mode': 'defer_settlement',
            'risk': {'slippage_tolerance': 10, 'execution_price_movement_bps': 2,
                     'max_loss_per_trade': None},
            'close_policy': {'mode': 'convergence', 'target_mode': 'zero', 'max_hold_seconds': 3600}},
            self.a, self.b)
        self.p = Position('SUI', 'short', 'buy', 1, 1, 100.2, 100, datetime.now(),
            status='open', entry_size_variational=1, entry_size_lighter=1,
            variational_venue='lighter', lighter_venue='arcus', realized_pnl_usdc=-.01,
            entry_fees_usdc=.01)
        self.s.positions.append(self.p)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)
        # Drain optional background evidence before removing its temporary directory.
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)

    def test_sui_loss_is_rejected_even_when_midpoints_show_convergence(self):
        self.p.entry_price_variational, self.p.entry_price_lighter = .87322, .872
        self.p.variational_size = self.p.lighter_size = 13.7
        self.p.entry_size_variational = self.p.entry_size_lighter = 13.7
        self.p.entry_fees_usdc = .005101112
        self.a.price, self.b.price = .88116, .879
        self.a.fee_bps, self.b.fee_bps = 0, 4.27
        self.a.reconcile_funding = lambda *args: {'complete': True, 'funding_usdc': .000289}
        with patch.object(self.a, 'get_market_price', return_value=.88), \
                patch.object(self.b, 'get_market_price', return_value=.88):
            for mode in ('convergence', 'convergence_and_profit', 'profit', 'convergence_or_profit'):
                self.s.close_mode = mode
                self.assertIsNone(self.s.close_reason(self.p), mode)
                self.assertFalse(self.s.close_position(self.p, '价差收敛'), mode)
        self.assertEqual(self.a.closes + self.b.closes, [])
        self.assertEqual(self.p.status, 'open')

    def test_estimate_charges_both_exit_fees_entry_fees_funding_and_movement(self):
        self.a.fee_bps, self.b.fee_bps = 1, 4
        self.a.reconcile_funding = lambda *args: {'complete': True, 'funding_usdc': -.02}
        estimate = self.s._single_exit_estimate(self.p)
        buy_limit, sell_limit = 100.02, 99.98
        expected = (100.2-buy_limit) + (sell_limit-100) - .01 - .02
        expected -= buy_limit/10000 + sell_limit*4/10000
        self.assertAlmostEqual(estimate['net_pnl_usdc'], expected)
        self.assertTrue(self.s._single_profit_ok(estimate))
        self.s.close_min_profit_usdc = expected + .001
        self.assertIsNone(self.s.close_reason(self.p))

    def test_midpoint_profit_cannot_overrule_unprofitable_size_bound_depth(self):
        self.p.entry_price_variational = 100.02
        self.assertGreater(self.s._calculate_net_mark_pnl(self.p, 100, 100), 0)
        self.assertIsNone(self.s.close_reason(self.p))
        self.assertFalse(self.s.close_position(self.p, '盈利条件'))
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_profitable_exit_pins_both_reduce_only_orders_to_checked_bounds(self):
        self.assertEqual(self.s.close_reason(self.p), '价差收敛')
        with patch.object(self.a, 'place_order', wraps=self.a.place_order) as a, \
                patch.object(self.b, 'place_order', wraps=self.b.place_order) as b:
            self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        for call in (a.call_args, b.call_args):
            self.assertTrue(call.kwargs['reduce_only'])
            self.assertEqual(call.kwargs['slippage_bps'], 2)
            self.assertLessEqual(call.kwargs['quote']['submit_deadline_monotonic']-time.monotonic(), 2)
            self.assertEqual(call.kwargs['price'], 100)
        self.assertEqual(a.call_args.args[1], 'buy')
        self.assertEqual(b.call_args.args[1], 'sell')
        self.assertEqual(self.p.status, 'closed')

    def test_recheck_after_signal_defers_when_fee_or_price_changes(self):
        self.assertEqual(self.s.close_reason(self.p), '价差收敛')
        self.b.fee_bps = 30
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.a.closes + self.b.closes, [])
        self.assertIsNone(self.p.close_started_time)
        self.assertIsNone(self.s.halted_reason)

    def test_both_confirmed_zero_fills_wait_instead_of_retrying_unprofitably(self):
        zero = dict(confirmed=True, status='canceled', filled_size=0)
        with patch.object(self.a, 'place_order', return_value=zero) as a, \
                patch.object(self.b, 'place_order', return_value=zero) as b:
            self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual((a.call_count, b.call_count), (1, 1))
        self.assertEqual(self.p.status, 'open')
        self.assertIsNone(self.p.close_started_time)
        self.assertEqual(self.p.close_attempts, {})
        self.a.price = 101
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_missing_fee_funding_and_stale_quotes_all_block_initial_submission(self):
        self.p.pending_fee_venues = ['lighter']
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.p.pending_fee_venues = []
        self.a.reconcile_funding = lambda *args: None
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.s._exit_funding_cache.clear()
        self.a.reconcile_funding = lambda *args: {'complete': True, 'funding_usdc': 0}
        original = self.a.get_executable_quote
        with patch.object(self.a, 'get_executable_quote', side_effect=lambda *args: dict(
                original(*args), updated_at=(datetime.now().astimezone()-timedelta(seconds=3)).isoformat())):
            self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_unknown_exit_fee_and_insufficient_depth_are_never_assumed_zero(self):
        self.b.fee_bps = None
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.b.fee_bps = 0
        with patch.object(self.b, 'get_executable_quote', side_effect=ValueError('insufficient depth')):
            self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_funding_reads_are_cached_and_failed_reads_do_not_storm(self):
        with patch.object(self.a, 'reconcile_funding', wraps=self.a.reconcile_funding) as reader:
            for _ in range(3):
                self.s._single_exit_estimate(self.p)
            self.assertEqual(reader.call_count, 1)
        self.s._exit_funding_cache.clear()
        with patch.object(self.a, 'reconcile_funding', return_value=None) as reader:
            for _ in range(3):
                with self.assertRaises(PositionReadUnavailable):
                    self.s._single_exit_estimate(self.p)
            self.assertEqual(reader.call_count, 1)

    def test_slow_settlement_result_does_not_claim_current_coverage(self):
        key = (id(self.a), self.p.symbol, self.p.open_time.isoformat())
        self.s._exit_funding_cache[key] = (time.monotonic(),
            {'complete': True, 'funding_usdc': 100}, time.monotonic()-6)
        with self.assertRaises(PositionReadUnavailable):
            self.s._single_exit_estimate(self.p)

    def test_nonpositive_and_nonfinite_profit_never_pass_even_with_zero_threshold(self):
        for value in (0, -.01, float('inf'), float('nan')):
            self.assertFalse(self.s._single_profit_ok({'net_pnl_usdc': value, 'net_pnl_bps': 1}))
        self.s.close_min_profit_bps = 3
        self.assertFalse(self.s._single_profit_ok({'net_pnl_usdc': 1, 'net_pnl_bps': 2}))
        self.s.close_min_profit_usdc, self.s.close_cost_reserve_usdc = .1, .02
        self.assertFalse(self.s._single_profit_ok({'net_pnl_usdc': .11, 'net_pnl_bps': 4}))

    def test_protection_exit_still_reduces_both_legs_without_profit_gate(self):
        self.p.open_time -= timedelta(hours=2)
        with patch.object(self.s, '_single_exit_estimate', side_effect=AssertionError('protection blocked')):
            self.assertEqual(self.s.close_reason(self.p), '最长持仓保护')
            self.assertTrue(self.s.close_position(self.p, '最长持仓保护'))
        self.assertEqual(self.p.status, 'closed')

    def test_reverse_position_uses_bid_for_long_and_ask_for_short(self):
        self.p.variational_side, self.p.lighter_side = 'long', 'sell'
        self.p.entry_price_variational, self.p.entry_price_lighter = 100, 100.2
        plan = self.s._single_exit_estimate(self.p)
        self.assertEqual(plan['legs']['variational']['side'], 'sell')
        self.assertEqual(plan['legs']['lighter']['side'], 'buy')
        self.assertAlmostEqual(plan['net_pnl_usdc'], .15)

    def test_plan_expiring_before_batch_sends_neither_leg_and_defers(self):
        plan = self.s._single_exit_estimate(self.p)
        for leg in plan['legs'].values():
            leg['quote']['submit_deadline_monotonic'] = time.monotonic()-1
        with patch.object(self.s, '_single_exit_estimate', return_value=plan):
            self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.a.closes + self.b.closes, [])
        self.assertEqual(self.p.status, 'open')
        self.assertIsNone(self.p.close_started_time)
        self.assertIsNone(self.s.halted_reason)

    def test_one_peer_fill_commits_to_reduce_verified_residual_without_rechecking_profit(self):
        first = {'confirmed': True, 'status': 'filled', 'filled_size': 1,
                 'average_price': 100, 'order_id': 'first-filled', 'fees_usdc': 0, 'funding_usdc': 0}
        zero = {'confirmed': True, 'status': 'canceled', 'filled_size': 0, 'order_id': 'zero'}
        def fill_first(*args, **kwargs):
            self.a.exposure = 0
            self.b.price = 90  # Price worsens after the first venue has already filled.
            return first
        with patch.object(self.a, 'place_order', side_effect=fill_first), \
                patch.object(self.b, 'place_order', return_value=zero), \
                patch.object(self.s, '_single_exit_estimate', wraps=self.s._single_exit_estimate) as estimate:
            self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        estimate.assert_called_once()
        self.assertEqual(self.b.closes, [1])
        self.assertEqual(self.p.status, 'closed')
        self.assertLess(self.p.realized_pnl_usdc, 0)

    def test_single_loss_protection_does_not_wait_for_normal_profitability(self):
        self.s.max_loss_per_trade = .001
        self.a.price = 101
        with patch.object(self.s, '_single_exit_estimate', side_effect=AssertionError('loss protection blocked')):
            self.assertEqual(self.s.close_reason(self.p), '单笔亏损保护')
            self.assertTrue(self.s.close_position(self.p, '单笔亏损保护'))

    def test_restart_continues_confirmed_single_pair_residual_without_profit_gate(self):
        self.p.variational_size = self.a.exposure = 0
        self.p.close_price_variational = 100
        self.p.close_started_time = datetime.now()
        self.p.close_reason = '价差收敛'
        self.p.status = 'reconcile_required'
        self.p.close_attempts = {'variational': 1, 'lighter': 1}
        self.p.close_order_results = {
            'variational': {'confirmed': True, 'status': 'filled', 'filled_size': 1},
            'lighter': {'confirmed': True, 'status': 'canceled', 'filled_size': 0}}
        self.p.realized_pnl_usdc = -.2
        self.b.price = 90
        self.s._save()
        restarted = ArbitrageStrategy(self.s.config, self.a, self.b)
        with patch.object(restarted, '_single_exit_estimate', side_effect=AssertionError('residual blocked')):
            self.assertEqual(restarted.recover_single_leg_positions(), 'recovered')
        self.assertEqual(restarted.positions[0].status, 'closed')
        self.assertEqual(self.b.closes, [1])
