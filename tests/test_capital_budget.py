"""Deterministic funding capacity tests, with no venue access."""
import unittest
from decimal import Decimal
from src.execution.capital_budget import available_entry_budget, capital_policy


class CapitalBudgetTests(unittest.TestCase):
    def test_disabled_preserves_all_current_free_collateral(self):
        self.assertEqual(available_entry_budget('100', {}), Decimal('100'))

    def test_reserve_uses_larger_floor_or_percentage_not_both(self):
        policy = dict(capital_reserve_percent=20, capital_reserve_usdc=30)
        self.assertEqual(available_entry_budget(100, policy), Decimal(70))
        self.assertEqual(available_entry_budget(200, policy), Decimal(160))
        self.assertEqual(available_entry_budget(20, policy), Decimal(0))

    def test_remaining_slots_split_each_account_independently(self):
        policy = dict(capital_reserve_percent=20, capital_allocation_mode='remaining_slots',remaining_slots=2)
        budgets = [available_entry_budget(balance, policy) for balance in (100,60)]
        self.assertEqual(budgets, [Decimal(40),Decimal(24)])
        # Pair the same quantity, not the same margin deposit or dollar spend.
        quantity = min(budgets[0]/Decimal(10),budgets[1]/Decimal(12))
        self.assertEqual(quantity, Decimal(2))
        self.assertEqual(available_entry_budget(60, dict(policy,remaining_slots=1)),Decimal(48))

    def test_fixed_floor_survives_repeated_groups_when_percentage_shrinks(self):
        policy = dict(capital_reserve_percent=20,capital_reserve_usdc=20,
                      capital_allocation_mode='remaining_slots',remaining_slots=2)
        first=available_entry_budget(100,policy)
        second=available_entry_budget(100-first,dict(policy,remaining_slots=1))
        self.assertEqual((first,second), (Decimal(40),Decimal(40)))

    def test_invalid_settings_and_unknown_capacity_fail_closed(self):
        for policy in ({'capital_reserve_percent':100},{'capital_reserve_usdc':-1},
                       {'capital_allocation_mode':'leveraged'}, {'capital_reserve_percent':True},
                       {'capital_reserve_usdc':float('nan')}):
            with self.subTest(policy=policy),self.assertRaises(ValueError):
                capital_policy(policy)
        for balance,policy in ((-1,{}),('NaN',{}),(100,{'remaining_slots':0})):
            with self.subTest(balance=balance),self.assertRaises(ValueError):
                available_entry_budget(balance,policy)
