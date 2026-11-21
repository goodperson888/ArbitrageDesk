import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.execution.window_risk import ExecutionWindowRisk, hedge_failures
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_hedge_policy import Trader


class WindowEvidenceTests(unittest.TestCase):
    def observe(self, risk, key='UNI|arcus->lighter', move=4, now=1000, seconds=1.3):
        risk.observe(key, 100.2, 100.2, 100, 100 * (1 + move / 10000), True, seconds * 1000, now)

    def test_five_real_windows_required_and_old_or_future_samples_do_not_count(self):
        risk = ExecutionWindowRisk()
        for _ in range(4): self.observe(risk)
        self.assertEqual(risk.evidence('UNI|arcus->lighter', 1000)['movement_bps'], 0)
        self.observe(risk)
        self.assertAlmostEqual(risk.evidence('UNI|arcus->lighter', 1000)['movement_bps'], 4)
        self.assertEqual(risk.evidence('UNI|arcus->lighter', 1000)['window_ms'], 1300)
        self.assertEqual(risk.evidence('UNI|arcus->lighter', 1000 + 86401)['samples'], 0)
        self.assertEqual(risk.evidence('UNI|arcus->lighter', 999)['samples'], 0)

    def test_percentile_uses_first_actual_fill_and_both_directions(self):
        risk = ExecutionWindowRisk()
        for _ in range(5):
            risk.observe('short', 100, 99.95, 99, 99, True, 1000, 1000)
            risk.observe('long', 100, 100.06, 101, 101, False, 1000, 1000)
        self.assertAlmostEqual(risk.evidence('short', 1000)['movement_bps'], 5)
        self.assertAlmostEqual(risk.evidence('long', 1000)['movement_bps'], 6)
        self.assertEqual(risk.evidence('other', 1000)['samples'], 0)

    def test_samples_bounded_persisted_and_invalid_optional_state_ignored(self):
        risk = ExecutionWindowRisk()
        for _ in range(100): self.observe(risk)
        restored = ExecutionWindowRisk(json.loads(json.dumps(risk.routes)))
        self.assertEqual(restored.evidence('UNI|arcus->lighter', 1000)['samples'], 64)
        for saved in (None, [], {'bad': {'samples': None, 'counts': []}}, {'bad': {'samples': [{'at': float('nan')}]} }):
            self.assertEqual(ExecutionWindowRisk(saved).evidence('bad', 1000)['samples'], 0)
        risk.observe('bad-price', 1e-310, 100, 100, 100, True, 1000)
        self.assertNotIn('bad-price', risk.routes)

    def test_quality_ranks_only_recent_route_outcomes_without_changing_guards(self):
        risk = ExecutionWindowRisk()
        risk.outcome('a', 'paired', 12, 1000)
        risk.outcome('b', 'zero_fill', 12, 1000)
        self.assertGreater(risk.quality('a', 1000), risk.quality('unknown', 1000))
        self.assertLess(risk.quality('b', 1000), risk.quality('unknown', 1000))
        self.assertEqual(risk.quality('a', 1901), .5)
        self.assertEqual(risk.quality('a', 999), .5)
        restored = ExecutionWindowRisk(json.loads(json.dumps(risk.routes)))
        self.assertEqual(restored.quality('a', 1000), .5, 'ranking evidence adds no durable state payload')
        self.assertTrue(restored.evidence('b', 1000)['observation_only'])

    def test_size_only_reduces_for_known_liquidity_outcomes_and_restores_gradually(self):
        risk = ExecutionWindowRisk()
        for reason in ('edge', 'price', 'timeout', 'amount', 'rejected', 'unknown'):
            risk.outcome('route', reason, 12, 1000)
        self.assertIsNone(risk.size_cap('route', 1000))
        risk.outcome('route', 'zero_fill', 12, 1000)
        self.assertEqual(risk.size_cap('route', 1000), 6)
        self.assertIsNone(risk.size_cap('reverse', 1000))
        risk.outcome('route', 'paired', 6, 1001)
        self.assertEqual(risk.size_cap('route', 1001), 7.5)
        risk.outcome('route', 'partial_fill', 7, 1002)
        self.assertEqual(risk.size_cap('route', 1002), 3.5)
        self.assertIsNone(risk.size_cap('route', 1002 + 86401))
        self.assertEqual(risk.routes['route']['counts']['zero_fill'], 1)
        risk.outcome('route', 'paired', 3.5, 1003, ceiling=4)
        self.assertIsNone(risk.size_cap('route', 1003))

    def test_multiple_constraints_are_distinct_from_hypothetical_bound_profit(self):
        self.assertEqual(hedge_failures(timely=False, within_cap=True, within_price=False,
            observed_net_bps=4, minimum_bps=5), ['timeout', 'edge', 'price'])
        self.assertEqual(hedge_failures(timely=True, within_cap=True, within_price=False,
            observed_net_bps=7, minimum_bps=5), ['price'])

    def test_first_rollback_teaches_its_window_before_five_samples_and_expires(self):
        risk = ExecutionWindowRisk()
        risk.observe('route', 100, 100, 100, 99.96, False, 430, 1000,
                     projection_decay_bps=35)
        self.assertEqual(risk.evidence('route', 1000)['projection_decay_bps'], 0)
        risk.outcome('route', 'edge', 12, 1001)
        evidence = risk.evidence('route', 1001)
        self.assertAlmostEqual(evidence['movement_bps'], 4)
        self.assertEqual(evidence['projection_decay_bps'], 35)
        self.assertEqual(evidence['recent_failures'], 1)
        self.assertEqual(risk.evidence('reverse', 1001)['movement_bps'], 0)
        restored = ExecutionWindowRisk(json.loads(json.dumps(risk.routes)))
        self.assertEqual(restored.evidence('route', 1001), evidence)
        self.assertEqual(restored.evidence('route', 1901)['projection_decay_bps'], 0)
        self.assertEqual(restored.evidence('route', 999)['movement_bps'], 0)

    def test_recent_rollback_is_not_hidden_by_many_earlier_flat_samples(self):
        risk = ExecutionWindowRisk()
        for _ in range(20):
            risk.observe('route', 100, 100, 100, 100, True, 400, 1000)
        risk.observe('route', 100, 100, 100, 100.04, True, 430, 1001,
                     projection_decay_bps=35)
        risk.outcome('route', 'price', 12, 1002)
        self.assertEqual(risk.evidence('route', 1002)['projection_decay_bps'], 35)
        self.assertAlmostEqual(risk.evidence('route', 1002)['movement_bps'], 4)
        self.assertEqual(risk.evidence('route', 1902)['movement_bps'], 0)

    def test_legacy_or_invalid_decay_evidence_cannot_corrupt_new_preflight(self):
        risk = ExecutionWindowRisk({'route': {'samples': [dict(at=1000, elapsed_ms=430,
            first_bps=0, second_bps=3, projection_decay_bps=float('nan'), failed='true')], 'counts': {}}})
        self.assertEqual(risk.evidence('route', 1000)['projection_decay_bps'], 0)
        self.assertEqual(risk.evidence('route', 1000)['recent_failures'], 0)
        risk.observe('bad', 100, 100, 100, 100, True, 430, 1000,
                     projection_decay_bps=float('inf'))
        self.assertNotIn('bad', risk.routes)

    def test_requotes_keep_one_sample_with_worst_downside_and_matching_outcome(self):
        risk = ExecutionWindowRisk()
        risk.observe('route', 100, 100, 100, 100, True, 430, 1000, attempt_id='a')
        risk.observe('route', 100, 100, 100, 100.04, True, 435, 1000,
                     projection_decay_bps=35, attempt_id='a')
        self.assertEqual(len(risk.routes['route']['samples']), 1)
        self.assertEqual(risk.routes['route']['samples'][0]['elapsed_ms'], 435)
        risk.outcome('route', 'edge', 12, 1001, attempt_id='other')
        self.assertEqual(risk.evidence('route', 1001)['projection_decay_bps'], 0)
        risk.outcome('route', 'edge', 12, 1001, attempt_id='a')
        self.assertEqual(risk.evidence('route', 1001)['projection_decay_bps'], 35)
        self.assertAlmostEqual(risk.evidence('route', 1001)['movement_bps'], 4)


class EntryWindowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first, self.second = Trader(100.2, 'lighter'), Trader(100, 'arcus')
        self.config = {'mode': 'live', 'state_path': str(Path(self.tmp.name) / 'state.json'),
            'trading': {'position_size': 100.25, 'max_positions': 1},
            'spread_threshold': {'min': 5, 'max': 500},
            'risk': {'slippage_tolerance': 10, 'entry_extra_profit_bps': 3},
            'wash_trading': {'trade_interval_min': 0, 'max_trades_per_day': 100}}
        self.s = ArbitrageStrategy(self.config, self.first, self.second)
        self.op = ArbitrageOpportunity('UNI', 100.2, 100, 20, 'variational_higher', 0, datetime.now())

    def seed(self, move=4):
        key = self.s._route_guard_key('UNI', 'sell_variational')
        for _ in range(5):
            self.s.execution_window_risk.observe(key, 100.2, 100.2, 100, 100 * (1 + move / 10000), True, 1300)
        return key

    def test_history_does_not_enlarge_configured_order_or_profit_budgets(self):
        key = self.seed()
        with patch.object(self.s, '_route_reversion_stats', side_effect=AssertionError('no market-history reads')):
            plan, _, _, _ = self.s.plan_entry(self.op, fixed_direction='sell_variational')
        self.assertAlmostEqual(plan['price_movement_bps'], 2)
        self.assertAlmostEqual(plan['buy_slippage_bps'], 2)
        self.assertAlmostEqual(plan['sell_slippage_bps'], 2)
        self.assertAlmostEqual(self.s.execution_window_risk.evidence(key)['movement_bps'], 4)
        self.assertEqual(self.s.execution_entry_movement_bps('UNI', 'buy_variational'), 2)
        self.assertEqual(self.s.execution_min_bps(), 8)

    def test_large_old_move_does_not_veto_a_fresh_profitable_equal_size_entry(self):
        key = self.seed(15)
        self.s.execution_window_risk.outcome(key, 'price', 12)
        self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertEqual((self.first.quote_reads, self.second.quote_reads), (3, 3))
        self.assertEqual(self.first.sends[0][2], self.second.sends[0][2])
        self.assertGreater(self.s.execution_window_risk.evidence(key)['movement_bps'], 10)
        self.assertEqual(self.s.execution_entry_movement_bps('UNI', 'sell_variational'), 2)

    def test_final_pre_submit_check_cannot_spend_reserve(self):
        # Initial protected edge is ~10 bps; final ~7 bps is above the
        # 5-bps floor but below 5+3. Neither order may be sent.
        self.first.price = 100.14
        check = self.first.precheck_order
        def changed(*args):
            check(*args)
            self.first.price = 100.11
        with patch.object(self.first, 'precheck_order', side_effect=changed):
            self.assertFalse(self.s.execute_arbitrage(self.op))
        self.assertEqual(self.first.sends + self.second.sends, [])

    def test_live_window_reuses_quotes_preserves_equal_base_and_persists_only_once(self):
        clock = [100.0]
        original = self.first.place_order
        def send(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 1.3
            self.second.price = 100.01
            return result
        with patch('src.strategy.arbitrage_strategy.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.first, 'place_order', side_effect=send):
            self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertEqual((self.first.quote_reads, self.second.quote_reads), (3, 3))
        self.assertEqual(self.first.sends[0][2], self.second.sends[0][2])
        key = self.s._route_guard_key('UNI', 'sell_variational')
        profile = self.s.execution_window_risk.routes[key]
        self.assertEqual(len(profile['samples']), 1)
        self.assertAlmostEqual(profile['samples'][0]['elapsed_ms'], 1300)
        saved = json.loads(self.s.state_path.read_text())
        self.assertEqual(saved['execution_window_risk'][key]['samples'], profile['samples'])

    def test_zero_fill_reduces_next_attempt_only_and_second_leg_uses_same_actual_quantity(self):
        with patch.object(self.second, 'place_order', return_value=dict(confirmed=True, status='canceled',
                filled_size=0, order_id='zero', fees_usdc=0)):
            self.assertFalse(self.s.execute_arbitrage(self.op))
        first_quantity = self.first.sends[0][2]
        self.assertEqual(self.s.positions[-1].rollback_category, 'zero_fill')
        self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertLess(self.first.sends[1][2], first_quantity)
        self.assertEqual(self.first.sends[1][2], self.second.sends[0][2])
        key = self.s._route_guard_key('UNI', 'sell_variational')
        self.assertEqual(self.s.execution_window_risk.routes[key]['counts']['zero_fill'], 1)

    def test_price_and_net_failures_both_recorded_and_do_not_shrink_size(self):
        original = self.first.place_order
        def send(*args, **kwargs):
            result = original(*args, **kwargs)
            self.second.price = 100.16
            return result
        with patch.object(self.first, 'place_order', side_effect=send):
            self.assertFalse(self.s.execute_arbitrage(self.op))
        decision = next(e['detail'] for e in self.s.history.recent() if e['event'] == 'hedge_decision')
        self.assertEqual(decision['constraint_failures'], ['edge', 'price'])
        self.assertEqual(self.s.positions[-1].rollback_category, 'edge')
        key = self.s._route_guard_key('UNI', 'sell_variational')
        self.assertIsNone(self.s.execution_window_risk.size_cap(key))
        self.assertEqual(len(self.s.execution_window_risk.routes[key]['samples']), 1)

    def test_learning_and_reason_counts_survive_restart(self):
        key = self.seed()
        self.s.execution_window_risk.outcome(key, 'partial_fill', 12)
        self.s._save()
        restored = ArbitrageStrategy(self.config, self.first, self.second)
        self.assertAlmostEqual(restored.execution_entry_movement_bps('UNI', 'sell_variational'), 2)
        self.assertAlmostEqual(restored.execution_window_risk.evidence(key)['movement_bps'], 4)
        self.assertEqual(restored.execution_window_risk.size_cap(key), 6)
        self.assertEqual(restored.execution_window_risk.routes[key]['counts']['partial_fill'], 1)

    def test_first_price_rollback_is_observational_and_keeps_user_budget(self):
        key = self.s._route_guard_key('UNI', 'sell_variational')
        self.s.execution_window_risk.observe(key, 100.2, 100.2, 100, 100.0312, True, 430)
        self.s.execution_window_risk.outcome(key, 'price', 12)
        self.assertAlmostEqual(self.s.execution_entry_movement_bps('UNI', 'sell_variational'), 2)
        self.assertAlmostEqual(self.s.execution_window_risk.evidence(key)['movement_bps'], 3.12)
        self.assertTrue(self.s.execution_window_risk.evidence(key)['observation_only'])
        self.assertEqual(self.s.execution_entry_movement_bps('UNI', 'buy_variational'), 2)
        self.assertEqual(self.s.slippage_tolerance, 10)
        self.assertEqual(self.s.spread_threshold_min, 5)

    def test_below_market_minimum_after_shrinking_sends_neither_leg(self):
        key = self.s._route_guard_key('UNI', 'sell_variational')
        self.s.execution_window_risk.outcome(key, 'zero_fill', .001)
        self.assertFalse(self.s.execute_arbitrage(self.op))
        self.assertEqual(self.first.sends + self.second.sends, [])


if __name__ == '__main__':
    unittest.main()
