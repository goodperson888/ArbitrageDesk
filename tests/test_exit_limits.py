import copy
import unittest
from unittest.mock import patch

from src.execution.exit_limits import CloseLiquidity, allocate_close_movement


def legs():
    return {name: dict(reference_price=price, quote={'size': '2'}, side=side,
                       fee_bps=fee, movement_bps=4, impact_bps=impact,
                       slippage_bps=4 + impact, bound=price * (1 + sign * (4 + impact) / 10000),
                       liquidity_weight=weight)
            for name, price, side, sign, fee, impact, weight in
            [('a', 100, 'buy', 1, 3, 0, 1), ('b', 101, 'sell', -1, 7, 1, 2)]}


def movement_cost(plans):
    return sum(p['reference_price'] * float(p['quote']['size']) *
               (p['slippage_bps'] - p['impact_bps']) / 10000 *
               (1 + (1 if p['side'] == 'buy' else -1) * p['fee_bps'] / 10000)
               for p in plans.values())


class ExitLimitsTests(unittest.TestCase):
    def test_less_liquid_leg_receives_existing_allowance_with_same_total_cost(self):
        plans = legs()
        previous = movement_cost(plans)
        allocate_close_movement(plans, 10)
        self.assertLess(plans['a']['slippage_bps'], 4)
        self.assertGreater(plans['b']['slippage_bps'], 5)
        self.assertAlmostEqual(movement_cost(plans), previous, places=12)
        for p in plans.values():
            movement = p['slippage_bps'] - p['impact_bps']
            self.assertGreaterEqual(movement, p['movement_bps'] * .5 - 1e-12)
            self.assertLessEqual(movement, p['movement_bps'] * 1.5 + 1e-12)
            self.assertLessEqual(p['slippage_bps'], 10)

    def test_individual_ceiling_and_required_depth_impact_are_preserved(self):
        plans = legs()
        plans['b'].update(impact_bps=6, slippage_bps=10, liquidity_weight=100)
        previous = movement_cost(plans)
        allocate_close_movement(plans, 10)
        self.assertLessEqual(plans['b']['slippage_bps'], 10)
        self.assertEqual(plans['b']['impact_bps'], 6)
        self.assertAlmostEqual(movement_cost(plans), previous)

    def test_neutral_evidence_or_zero_budget_changes_no_limits(self):
        for mode in ('neutral', 'zero', 'one_leg'):
            plans = legs()
            if mode == 'neutral': plans['b']['liquidity_weight'] = 1
            if mode == 'zero': plans['a']['movement_bps'] = 0
            if mode == 'one_leg': plans.pop('b')
            before = copy.deepcopy(plans)
            allocate_close_movement(plans, 10)
            self.assertEqual(plans, before)

    def test_recent_verified_failures_are_scoped_and_expire(self):
        memory = CloseLiquidity()
        quote = {}
        with patch('src.execution.exit_limits.time.monotonic', return_value=100):
            zero = dict(confirmed=True, status='canceled', filled_size=0)
            memory.record('rh', 'UNI', 'buy', zero, 1)
            self.assertEqual(memory.weight('rh', 'UNI', 'buy', quote, 1), 2)
            self.assertEqual(memory.weight('rh', 'UNI', 'sell', quote, 1), 1)
            self.assertEqual(memory.weight('main', 'UNI', 'buy', quote, 1), 1)
            memory.record('main', 'UNI', 'buy', dict(zero, confirmed=False), 1)
            self.assertEqual(memory.weight('main', 'UNI', 'buy', quote, 1), 1)
        with patch('src.execution.exit_limits.time.monotonic', return_value=1001):
            self.assertEqual(memory.weight('rh', 'UNI', 'buy', quote, 1), 1)

    def test_only_existing_correct_side_depth_is_used(self):
        memory = CloseLiquidity()
        quote = dict(sizing_book=dict(bids=[dict(size=1)], asks=[dict(size=100)]))
        self.assertGreater(memory.weight('rh', 'UNI', 'sell', quote, 10),
                           memory.weight('rh', 'UNI', 'buy', quote, 10))


if __name__ == '__main__':
    unittest.main()
