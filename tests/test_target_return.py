import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, PropertyMock

from src.execution.target_return import (directed_spread, exit_projection, projected_return,
    completion_return, completion_target_limit)
from src.execution.worker import ExecutionWorker
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_hedge_policy import Trader
from test_spread_grid import GridTrader


def quote(bid, ask, fee=0):
    return dict(bid=bid, ask=ask, fee_bps=fee)


class TargetTrader(Trader):
    def get_position_mark_snapshot(self, symbol):
        return self.get_executable_quote(symbol, 1)


class ReturnMathTests(unittest.TestCase):
    def test_target_prices_preserve_signed_history_formula_and_charge_four_fees(self):
        buy, sell = quote(99.98, 100.02, 2), quote(100.08, 100.12, 3)
        for target in (-30, 0, 20):
            with self.subTest(target=target):
                p = exit_projection(buy, sell, target, 2, 4, 6)
                self.assertAlmostEqual(directed_spread(p['target_buy_ask'], p['target_sell_bid']), target)
                r = projected_return(100.02, 100.08, 2, 2, 3, 1, 2, p)
                protected_buy, protected_sell = 100.02 * 1.0001, 100.08 * .9998
                expected = (protected_sell-protected_buy + p['close_sell_price']-p['close_buy_price']
                    - protected_buy*.0002 - protected_sell*.0003 - p['close_fees_per_unit']
                    - p['funding_cost_per_unit']) * 2
                self.assertAlmostEqual(r['estimated_entry_edge_usdc'], expected)

    def test_unknown_fees_funding_or_invalid_targets_are_not_zero(self):
        for b, s, t, funding in ((quote(100, 100, None), quote(100, 100), 0, 0),
                (quote(100, 100), quote(100, 100), float('nan'), 0),
                (quote(100, 100), quote(100, 100), 0, None)):
            with self.assertRaises(ValueError):
                exit_projection(b, s, t, 2, 2, funding)

    def test_second_leg_limit_covers_both_sides_and_negative_opening_edges(self):
        for is_buy in (False, True):
            for credit in (-.3, .3):
                with self.subTest(is_buy=is_buy, credit=credit):
                    projection = {'exit_credit_per_unit': credit}
                    limit, budget = completion_target_limit(100, .01, 1, 100, 2, 100,
                        5, 102, is_buy, projection)
                    net, bps = completion_return(100, .01, 1, limit, 2, is_buy, projection)
                    self.assertGreaterEqual(bps, 5-1e-7)
                    self.assertGreater(net, 0)
                    self.assertLessEqual(limit, budget) if is_buy else self.assertGreaterEqual(limit, budget)
                    worse = limit + .0001 if is_buy else limit - .0001
                    if abs(limit-budget) > 1e-6:
                        self.assertLess(completion_return(100, .01, 1, worse, 2, is_buy, projection)[1], 5)

    def test_second_leg_limit_enforces_absolute_profit_independently_of_bps(self):
        for is_buy in (True, False):
            projection = {'exit_credit_per_unit': .3}
            limit, _ = completion_target_limit(100, .01, .5, 100, 2, 100,
                1, 60, is_buy, projection, minimum_usdc=.12)
            net, bps = completion_return(100, .01, .5, limit, 2, is_buy, projection)
            self.assertAlmostEqual(net, .12)
            self.assertGreaterEqual(bps, 1)
            worse = limit + .0001 if is_buy else limit - .0001
            self.assertLess(completion_return(100, .01, .5, worse, 2, is_buy, projection)[0], .12)


class TargetEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = TargetTrader(100.1, 'lighter'), TargetTrader(100, 'arcus')
        self.config = dict(mode='live', state_path=str(Path(self.tmp.name)/'state.json'),
            trading=dict(position_size=100.25, max_positions=3),
            spread_threshold=dict(min=5, max=500),
            entry_policy=dict(mode='fixed', calculation_version=2, funding_reserve_bps=0,
                              target_window_seconds=604800),
            close_policy=dict(mode='convergence', target_mode='center', convergence_bps=2,
                              max_hold_seconds=3600),
            risk=dict(slippage_tolerance=10, entry_extra_profit_bps=3, execution_price_movement_bps=2,
                      max_loss_per_trade=None),
            wash_trading=dict(trade_interval_min=0, max_trades_per_day=100))
        self.s = ArbitrageStrategy(self.config, self.a, self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)
        self.stats = {'buy_variational': -30, 'sell_variational': 30}
        self.history_calls = []
        def stats(symbol, direction):
            self.history_calls.append(direction)
            return dict(center_bps=self.stats[direction], samples=100)
        self.s._target_policy_stats = stats
        self.op = ArbitrageOpportunity('UNI', 100.1, 100, -10, 'lighter_higher', 0, datetime.now())

    def cached_quotes(self, fee=0, age=0, sizes=True):
        for trader in (self.a, self.b):
            trader.fee_bps = fee
            trader.cached_reference_quote = lambda symbol, t=trader: dict(
                bid=t.price, ask=t.price, fee_bps=fee,
                bid_size=100 if sizes else None, ask_size=100 if sizes else None,
                updated_at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat())

    def test_opposite_bbo_failure_is_observed_but_does_not_veto_next_fresh_entry(self):
        original_quote = self.a.get_executable_quote
        original_send = self.a.place_order
        widen = [False]
        def quoted(symbol, quantity):
            result = original_quote(symbol, quantity)
            if widen[0]:
                result['bid'] -= .4  # Entry ask is unchanged; future long exit worsens.
            return result
        def sent(*args, **kwargs):
            result = original_send(*args, **kwargs)
            widen[0] = True
            return result
        with patch.object(self.a, 'get_executable_quote', side_effect=quoted), \
                patch.object(self.a, 'place_order', side_effect=sent):
            self.assertFalse(self.s.execute_arbitrage(self.op))
        self.assertEqual(len(self.a.sends), 1)
        self.assertEqual(self.b.sends, [])
        self.assertEqual(self.s.positions[-1].rollback_category, 'edge')
        key = self.s._route_guard_key('UNI', 'buy_variational')
        evidence = self.s.execution_window_risk.evidence(key)
        self.assertGreater(evidence['projection_decay_bps'], 30)
        self.assertEqual(evidence['movement_bps'], 0)
        self.assertEqual(evidence['samples'], 1)
        before = self.a.quote_reads, self.b.quote_reads
        self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertEqual(len(self.a.sends), 2, 'a fresh valid entry is independent of prior transient widening')
        self.assertEqual(len(self.b.sends), 1)
        self.assertEqual((self.a.quote_reads-before[0], self.b.quote_reads-before[1]), (3, 3))
        self.assertFalse(any(e['event']=='entry_hedge_precheck' for e in self.s.history.recent(200)))
        self.assertTrue(evidence['observation_only'])
        self.assertAlmostEqual(self.s.positions[-1].entry_target['target_bps'], -30)

    def test_entry_reserve_replaces_opening_movement_debit_without_loosening_order_prices(self):
        self.stats = {'buy_variational': -23, 'sell_variational': 23}
        self.cached_quotes()
        candidate = self.s.check_arbitrage_opportunity('UNI')
        plan, v, l, _ = self.s.plan_entry(candidate, fixed_direction='buy_variational')
        self.assertGreaterEqual(plan['net_edge_bps'],8)
        self.assertLess(plan['net_edge_bps'],10)
        self.assertAlmostEqual(plan['opening_edge_bps'],directed_spread(v['ask'],l['bid']))
        self.assertEqual(plan['buy_slippage_bps'],2,'first-order price protection is retained')
        self.assertEqual(plan['sell_slippage_bps'],2,'second-order price protection is retained')
        self.assertAlmostEqual(plan['net_edge_bps'],candidate.return_preview['net_bps'])
        self.assertEqual(self.a.sends+self.b.sends,[],'preparing the new model never sends an order')

    def test_user_reserve_is_the_only_extra_entry_cushion_even_after_bad_history(self):
        self.stats = {'buy_variational': -23, 'sell_variational': 23}
        self.cached_quotes()
        candidate = self.s.check_arbitrage_opportunity('UNI')
        before, _, _, _ = self.s.plan_entry(candidate, fixed_direction='buy_variational')
        self.assertGreaterEqual(before['net_edge_bps'], 8)
        self.assertLess(before['net_edge_bps'], 10)
        key = self.s._route_guard_key('UNI', 'buy_variational')
        for _ in range(5):
            self.s.execution_window_risk.observe(key, 100.1, 100.1, 100, 99.8,
                False, 430, projection_decay_bps=35)
        self.s.execution_window_risk.outcome(key, 'edge', 12)
        refreshed = self.s.check_arbitrage_opportunity('UNI')
        after, _, _, _ = self.s.plan_entry(refreshed, fixed_direction='buy_variational')
        self.assertAlmostEqual(after['net_edge_bps'], before['net_edge_bps'])
        self.assertAlmostEqual(refreshed.return_preview['net_bps'], candidate.return_preview['net_bps'])
        self.assertEqual(after['buy_slippage_bps'], 2)
        self.assertEqual(after['sell_slippage_bps'], 2)
        original_config = copy.deepcopy(self.s.config)
        reads = self.a.quote_reads, self.b.quote_reads
        self.assertTrue(self.s.execute_arbitrage(refreshed))
        self.assertEqual((self.a.quote_reads-reads[0], self.b.quote_reads-reads[1]), (3, 3))
        self.assertEqual(self.a.sends[-1][2], self.b.sends[-1][2])
        self.assertEqual(self.s.config, original_config)

    def test_allocator_vwap_does_not_charge_the_second_venue_depth_twice(self):
        v, l = quote(100.08,100.1), quote(100,100.02)
        impacts = {d:dict(buy=1,sell=2) for d in ('buy_variational','sell_variational')}
        closing = {d:dict(variational=2,lighter=2) for d in impacts}
        models, errors = self.s._target_models('UNI',v,l,impacts,closing)
        self.assertFalse(errors)
        for direction in impacts:
            with self.subTest(direction=direction):
                buy,sell = (v['ask'],l['bid']) if direction=='buy_variational' else (l['ask'],v['bid'])
                bbo = self.s._target_edge(models,direction,buy,sell,.1)
                if direction=='buy_variational': sell *= 1-2/10000
                else: buy *= 1+1/10000
                vwap = self.s._target_edge(models,direction,buy,sell,.1,second_depth_in_price=True)
                self.assertAlmostEqual(bbo['estimated_entry_edge_usdc'],vwap['estimated_entry_edge_usdc'])

    def test_optimistic_unprofitable_route_never_reaches_depth_or_accounts(self):
        # Midpoint deviation is ~20bps, but verified four-fill fees consume it.
        self.cached_quotes(fee=6)
        with patch.object(self.s, 'plan_entry', side_effect=AssertionError('depth request')), \
                patch.object(self.s.history, 'append') as append:
            self.assertIsNone(self.s.check_arbitrage_opportunity('UNI'))
        append.assert_called()  # Reference-stage telemetry still works.
        state = self.s.entry_scan_status()
        self.assertEqual(state['preview_skipped'], 1)
        self.assertLess(state['last_preview_skip']['optimistic_net_bps'], 8)
        self.assertEqual(self.a.quote_reads+self.b.quote_reads+self.a.reads+self.b.reads, 0)
        self.assertEqual(self.a.sends+self.b.sends, [])

    def test_incomplete_fees_or_stale_quotes_do_not_prune(self):
        for fee, age in ((None, 0), (6, 3)):
            with self.subTest(fee=fee, age=age):
                self.cached_quotes(fee=fee, age=age)
                self.assertIsNotNone(self.s.check_arbitrage_opportunity('UNI'))
        self.assertEqual(self.s.entry_scan_status()['preview_skipped'], 0)

    def test_unknown_depth_can_be_pruned_only_if_best_prices_already_fail(self):
        self.cached_quotes(fee=6, sizes=False)
        self.assertIsNone(self.s.check_arbitrage_opportunity('UNI'))
        self.assertIsNone(self.s.entry_scan_status()['last_preview_skip']['net_bps'])
        self.cached_quotes(sizes=False)
        self.assertIsNotNone(self.s.check_arbitrage_opportunity('UNI'))

    def test_negative_opening_spread_and_reverse_direction_survive_prefilter(self):
        self.cached_quotes()
        candidate = self.s.check_arbitrage_opportunity('UNI')
        self.assertLess(candidate.spread_bps, 0)
        self.assertEqual(candidate.spread_direction, 'lighter_higher')
        self.stats = {'buy_variational': 30, 'sell_variational': -30}
        candidate = self.s.check_arbitrage_opportunity('UNI')
        self.assertEqual(candidate.spread_direction, 'variational_higher')

    def test_risk_budget_failure_alone_does_not_prune_optimistic_opportunity(self):
        self.cached_quotes()
        self.s.config['risk']['execution_price_movement_bps'] = 9
        self.s.config['entry_policy']['funding_reserve_bps'] = 100
        candidate = self.s.check_arbitrage_opportunity('UNI')
        self.assertIsNotNone(candidate)
        self.assertLess(candidate.return_preview['net_bps'], 8)
        self.assertGreater(candidate.return_preview['optimistic_net_bps'], 8)

    def test_grid_and_floor_boundary_are_never_removed_by_scan_optimization(self):
        self.cached_quotes(fee=6)
        with patch.object(type(self.s), 'grid_enabled', new_callable=PropertyMock, return_value=True):
            self.assertIsNotNone(self.s.check_arbitrage_opportunity('UNI'))
        self.assertTrue(self.s.discovery_preview_allowed('UNI', 'buy_variational',
            dict(optimistic_net_bps=8-1e-8, optimistic_minimum_bps=8)))

    def test_legacy_either_target_is_selected_by_sized_requote(self):
        self.cached_quotes(fee=6)
        self.s.close_target_mode = 'either'
        candidate = self.s.check_arbitrage_opportunity('UNI')
        self.assertIsNotNone(candidate)
        self.assertNotIn('optimistic_net_bps', candidate.return_preview)

    def test_dynamic_funding_is_frozen_before_first_submit_and_reused_on_final_quote(self):
        import time
        from src.execution.funding_forecast import forecast
        self.s.config['entry_policy'].update(funding_mode='dynamic',funding_reserve_bps=5,
                                           funding_hold_seconds=3600,funding_extra_bps=1)
        now=time.time();rate=[.0001]
        original_a,original_b=self.a.get_executable_quote,self.b.get_executable_quote
        self.a.get_executable_quote=lambda *args:dict(original_a(*args),funding=forecast(rate[0],now+800,now))
        self.b.get_executable_quote=lambda *args:dict(original_b(*args),funding=forecast(.00004,now+800,now))
        plan,_,_,_=self.s.plan_entry(self.op,fixed_direction='buy_variational')
        context=copy.deepcopy(plan['target_context'])
        self.assertEqual(context['funding_forecast']['mode'],'dynamic')
        self.assertLess(context['funding_reserve_bps'],5)
        rate[0]=.004
        final,_,_,_=self.s.plan_entry(self.op,fixed_quantity=plan['quantity'],
                                     fixed_direction=plan['direction'],fixed_target=context)
        self.assertEqual(final['target_context'],context)

    def test_negative_opening_edge_is_discovered_sized_and_hedged_in_return_direction(self):
        candidate = self.s.check_arbitrage_opportunity('UNI')
        self.assertLess(candidate.spread_bps, 0)
        self.assertEqual(candidate.spread_direction, 'lighter_higher')
        worker = ExecutionWorker(self.s, ['UNI'], Path(self.tmp.name)/'lock', Path(self.tmp.name)/'STOP')
        self.assertIsNotNone(worker._best_opportunity())
        plan, _, _, _ = self.s.plan_entry(candidate)
        self.assertLess(plan['opening_edge_bps'], 0)
        self.assertGreaterEqual(plan['net_edge_bps'], 8)
        self.assertEqual(plan['target_context']['target_bps'], -30)
        self.assertTrue(self.s.execute_arbitrage(candidate))
        p = self.s.positions[0]
        self.assertEqual(p.variational_side, 'long')
        self.assertEqual(p.variational_size, p.lighter_size)
        self.assertEqual(p.entry_target['target_bps'], -30)
        self.assertGreaterEqual(p.target_entry_net_bps, 5)
        mark = self.s._position_mark(p, read_market=False)
        self.assertEqual(mark['entry_target'],p.entry_target)
        self.assertEqual(mark['target_entry_net_bps'],p.target_entry_net_bps)
        restored = ArbitrageStrategy(self.config, self.a, self.b)
        self.assertEqual(restored.positions[0].entry_target, p.entry_target)

    def test_both_directions_wait_when_neither_has_net_target_room(self):
        self.stats = {'buy_variational': -10, 'sell_variational': 10}
        self.assertIsNone(self.s.check_arbitrage_opportunity('UNI'))
        with self.assertRaisesRegex(ValueError, '本次数量的预计整轮净收益未达入场门槛'):
            self.s.plan_entry(self.op)
        self.assertEqual(self.a.sends+self.b.sends, [])

    def test_opening_spread_alone_does_not_count_as_return_profit(self):
        self.a.price = 100.2
        self.stats = {'buy_variational': -25, 'sell_variational': 25}
        positive = ArbitrageOpportunity('UNI', 100.2, 100, 20, 'variational_higher', 0, datetime.now())
        with self.assertRaises(ValueError):
            self.s.plan_entry(positive)
        self.assertFalse(self.s.execute_arbitrage(positive))
        self.assertEqual(self.a.sends+self.b.sends, [])

    def test_center_missing_waits_and_zero_target_needs_no_history(self):
        self.s._target_policy_stats = lambda *args: dict(center_bps=None, samples=0)
        self.assertIsNone(self.s.check_arbitrage_opportunity('UNI'))
        with self.assertRaisesRegex(ValueError, '样本不足'):
            self.s.plan_entry(self.op)
        self.s.close_target_mode = 'zero'
        self.a.price = 100.2
        self.s._target_policy_stats = lambda *args: self.fail('zero must not read center')
        plan, _, _, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['target_context']['target_bps'], 0)

    def test_provisional_center_blocks_new_entry_but_not_a_frozen_target(self):
        plan, _, _, _ = self.s.plan_entry(self.op)
        self.s._target_policy_stats = lambda *args: dict(center_bps=30,samples=100,
            center_ready=False,center_reason='历史中心最近有效样本已过期，等待更新')
        with self.assertRaisesRegex(ValueError,'有效样本已过期'):
            self.s._entry_target_context('UNI',plan['direction'],10)
        context=self.s._entry_target_context('UNI',plan['direction'],10,frozen=plan['target_context'])
        self.assertEqual(context,plan['target_context'])

    def test_final_requote_uses_first_plan_target_and_full_reserve(self):
        plan, _, _, _ = self.s.plan_entry(self.op)
        self.stats = {'buy_variational': -100, 'sell_variational': 100}
        final, _, _, _ = self.s.plan_entry(self.op, fixed_quantity=plan['quantity'],
            fixed_direction=plan['direction'], fixed_target=plan['target_context'])
        self.assertEqual(final['target_context'], plan['target_context'])
        self.a.price = 100.18
        with self.assertRaises(ValueError):
            self.s.plan_entry(self.op, fixed_quantity=plan['quantity'],
                fixed_direction=plan['direction'], fixed_target=plan['target_context'])

    def test_adaptive_entry_checks_upper_tail_of_each_directed_route(self):
        self.s.entry_mode = 'adaptive_quantile'
        self.s.entry_min_samples = 30
        self.s._target_policy_stats = lambda *args: dict(samples=100,
            center_bps=-30,entry_lower_bps=-20,entry_upper_bps=-5)
        with self.assertRaises(ValueError):
            self.s._adaptive_entry_gate('UNI','buy_variational',100,99.7)
        self.assertIsNotNone(self.s._adaptive_entry_gate('UNI','buy_variational',100,99.97))

    def test_full_round_trip_costs_and_close_profit_floor_are_applied_before_first_submit(self):
        self.a.fee_bps = self.b.fee_bps = 5
        with self.assertRaises(ValueError):
            self.s.plan_entry(self.op)
        self.a.fee_bps = self.b.fee_bps = 0
        self.s.close_min_profit_usdc = 1
        with self.assertRaises(ValueError):
            self.s.plan_entry(self.op)
        self.s.close_min_profit_usdc = 0
        self.s.config['entry_policy']['funding_reserve_bps'] = 20
        with self.assertRaises(ValueError):
            self.s.plan_entry(self.op)

    def test_close_bps_floor_is_converted_from_two_closing_legs(self):
        self.s.close_min_profit_bps = 9
        # About 20 gross bps to target, less 8 bps of protected opening/closing
        # movement. A mistaken 9-bps floor would pass; two-leg 18 + 3 cannot.
        with self.assertRaises(ValueError):
            self.s.plan_entry(self.op)
        self.assertEqual(self.a.sends + self.b.sends, [])
        self.s.close_min_profit_usdc = .3
        self.s.close_cost_reserve_usdc = .1
        p = exit_projection(quote(100,100), quote(100,100),0,2,2,0)
        floor = self.s._target_profit_floor_usdc(2,p)
        self.assertAlmostEqual(floor,.4)
        self.s.close_min_profit_usdc = self.s.close_cost_reserve_usdc = 0
        self.assertAlmostEqual(self.s._target_profit_floor_usdc(2,p),
            9 * (p['close_sell_price']+p['close_buy_price'])*2/10000)

    def test_frozen_center_exit_uses_same_bbo_formula_and_allows_crossing(self):
        self.assertTrue(self.s.execute_arbitrage(self.op))
        p = self.s.positions[0]
        for trader in (self.a, self.b):
            trader.reconcile_funding = lambda *args: dict(complete=True, funding_usdc=0)
        self.stats = {'buy_variational': -200, 'sell_variational': 200}
        self.s.close_target_mode = 'zero'
        self.a.price = 100.15
        self.assertIsNone(self.s.close_reason(p))
        self.a.price = 100.35
        self.assertEqual(self.s.close_reason(p), '价差收敛')
        self.assertEqual(p.entry_target['target_bps'], -30)
        p.entry_fees_usdc = 1
        self.assertIsNone(self.s.close_reason(p), 'convergence must still require actual net profit')


class TargetGridTests(unittest.TestCase):
    def test_grid_layers_share_first_target_and_use_actual_net_return_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = GridTrader(100.1, 'lighter'), GridTrader(100, 'arcus')
            config = dict(mode='live', state_path=str(Path(tmp)/'state.json'),
                trading=dict(position_size=100.25, max_positions=3), spread_threshold=dict(min=5,max=500),
                entry_policy=dict(calculation_version=2, funding_reserve_bps=0),
                close_policy=dict(target_mode='center', max_hold_seconds=3600),
                risk=dict(slippage_tolerance=10, entry_extra_profit_bps=3, max_loss_per_trade=None),
                wash_trading=dict(trade_interval_min=0),
                position_policy=dict(mode='spread_grid', step_bps=5, max_layers=3,
                    layer_notional_usdc=100.25, total_notional_usdc=400))
            s = ArbitrageStrategy(config, a, b)
            s._target_policy_stats = lambda symbol,d: dict(center_bps=-40 if d=='buy_variational' else 40,samples=100)
            op = ArbitrageOpportunity('UNI',100.1,100,-10,'lighter_higher',0,datetime.now())
            self.assertTrue(s.execute_arbitrage(op))
            first_target = copy.deepcopy(s.positions[0].entry_target)
            a.price = 99.94
            s._target_policy_stats = lambda *args: self.fail('active grid must reuse its frozen target')
            self.assertTrue(s.execute_arbitrage(op))
            first, second = s.positions
            self.assertEqual(second.entry_target, first_target)
            self.assertGreaterEqual(second.grid_entry_net_bps, first.grid_entry_net_bps+5)
            self.assertFalse(s.execute_arbitrage(op), 'same grid price cannot repeatedly add layers')
            s.close_diagnostics.writer.flush(1)


if __name__ == '__main__':
    unittest.main()
