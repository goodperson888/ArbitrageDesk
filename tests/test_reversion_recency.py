"""Soft recency cannot bypass sample/count, executable profit or scope guards."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.execution.console import ExecutionConsole
from src.execution.runtime import run_runtime
import test_candidate_preparation as fixtures


class RecencyModeTests(unittest.TestCase):
    def setUp(self):
        fixtures.PreparedPlanTests.setUp(self)
        self.s.market_history = Mock()
        self.stats = dict(samples=100, reversion_count=3, last_reversion_age_seconds=7201)
        self.s.market_history.reversion_stats.return_value = self.stats
        self.s.reversion_min_samples = 30
        self.s.reversion_min_count = 1
        self.s.reversion_max_age_seconds = 7200

    def test_hard_default_excludes_old_or_missing_recency_soft_keeps_other_filters(self):
        self.assertEqual(self.s.reversion_recency_mode, 'hard')
        for age in (None, 7201):
            self.stats['last_reversion_age_seconds'] = age
            self.assertFalse(self.s._reversion_filter_allowed('BTC', 'buy_variational'))
            self.s.reversion_recency_mode = 'soft'
            self.assertTrue(self.s._reversion_filter_allowed('BTC', 'buy_variational'))
            self.s.reversion_recency_mode = 'hard'
        self.s.reversion_recency_mode = 'soft'
        for field in ('samples', 'reversion_count'):
            old = self.stats[field]
            self.stats[field] = 0
            self.assertFalse(self.s._reversion_filter_allowed('BTC', 'buy_variational'))
            self.stats[field] = old

    def test_soft_rank_uses_same_directed_history_and_profit_remains_primary(self):
        self.s.reversion_recency_mode = 'soft'
        def opportunity(direction, profit):
            return SimpleNamespace(symbol='BTC', spread_direction=direction, spread_bps=30,
                target_context=dict(target_bps=0, samples=100, route='route'),
                return_preview=dict(net_bps=profit*100, profit_usdc=profit))
        recent = opportunity('lighter_higher', .02)
        old = opportunity('variational_higher', .02)
        # Determine which route has the recent matching history rather than
        # assuming a platform alias from the fixture.
        self.s.market_history.reversion_stats.side_effect = lambda symbol, pair, *args: {
            **self.stats, 'last_reversion_age_seconds': 10 if pair == self.s._route_key_for_direction('buy_variational') else 8000}
        self.assertGreater(self.s.target_discovery_rank(recent), self.s.target_discovery_rank(old))
        old.return_preview['profit_usdc'] = .03
        self.assertGreater(self.s.target_discovery_rank(old), self.s.target_discovery_rank(recent))
        self.s.reversion_max_age_seconds = 0
        self.assertEqual(self.s.reversion_recency_rank('BTC', 'buy_variational'), 0)

    def test_soft_history_does_not_approve_unprofitable_entry_or_send_orders(self):
        self.s.reversion_recency_mode = 'soft'
        self.a.price = self.b.price = 100
        item = self.s.prepare_entry_candidate(self.op)
        self.assertTrue(item.get('error'))
        self.assertFalse(self.a.sends + self.b.sends)

    def test_mode_participates_in_prepared_policy_key(self):
        before = self.s.entry_preparation_key()
        self.s.config['execution_filters'] = dict(reversion_recency_mode='soft')
        self.assertNotEqual(before, self.s.entry_preparation_key())

    def test_pending_or_expired_shared_summary_never_approves_entry(self):
        self.stats.update(statistics_ready=False, last_reversion_age_seconds=10)
        for mode in ('soft','hard'):
            self.s.reversion_recency_mode=mode
            self.assertFalse(self.s._reversion_filter_allowed('BTC','buy_variational'))
            self.assertEqual(self.s.reversion_recency_rank('BTC','buy_variational'),0)


class RecencyPolicyTests(unittest.TestCase):
    def test_policy_preserves_default_and_cli_carries_explicit_soft_mode(self):
        hard = ExecutionConsole._policy(12)
        self.assertEqual(hard['reversion_recency_mode'], 'hard')
        soft = ExecutionConsole._policy(12, reversion_recency_mode='soft', reversion_max_age_seconds=7200)
        command = ExecutionConsole._policy_command(['python'], soft)
        index = command.index('--reversion-recency-mode')
        self.assertEqual(command[index+1], 'soft')
        self.assertIn('--reversion-max-age-seconds', command)
        with self.assertRaisesRegex(ValueError, '时效'):
            ExecutionConsole._policy(12, reversion_recency_mode='invalid')
        with self.assertRaisesRegex(ValueError, '时效'):
            run_runtime('missing', 'missing', reversion_recency_mode='invalid')


if __name__ == '__main__':
    unittest.main()
