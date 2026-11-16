import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from src.common.hedge_cost import entry_breakdown
from src.execution.worker import ExecutionWorker
from src.variational.schema_audit import describe, safe_path


class EntryBreakdownTests(unittest.TestCase):
    def test_actual_funding_period_is_not_silently_eight_hours(self):
        row=entry_breakdown({'base_quantity':1,'variational_ask':100,'variational_bid':100,
                             'lighter_bid':101,'lighter_ask':101,'lighter_sell_vwap':101,'lighter_buy_vwap':101},
                            0, funding_bps_per_8h=2, funding_interval_s=14400,
                            funding_source='website_display')
        self.assertEqual(row['funding_rate_bps'],1)
        self.assertEqual(row['directions']['a']['funding_estimate_usdc'],.01)
        self.assertEqual(row['funding_interval_s'],14400)
    def setUp(self):
        self.c = {'base_quantity':2, 'variational_ask':100, 'variational_bid':99,
                  'lighter_bid':102, 'lighter_ask':103, 'lighter_sell_vwap':101.5, 'lighter_buy_vwap':103.5}

    def test_depth_is_already_included_and_fees_use_each_leg_notional(self):
        a = entry_breakdown(self.c, 10)['directions']['a']
        self.assertEqual(a['gross_usdc'], 3)
        self.assertEqual(a['price_gap_usdc_per_unit'], 1.5)
        self.assertEqual(a['entry_notional_usdc'], 200)
        self.assertEqual(a['gross_convergence_usdc'], 3)
        self.assertEqual(a['depth_impact_usdc'], 1)
        self.assertAlmostEqual(a['fees_usdc'], .203)
        self.assertAlmostEqual(a['net_entry_usdc'], 2.797)
        self.assertAlmostEqual(a['after_known_entry_fees_usdc'], 2.797)
        b = entry_breakdown(self.c, 10)['directions']['b']
        self.assertAlmostEqual(b['fees_usdc'], .207)
        self.assertAlmostEqual(b['net_entry_usdc'], -9.207)

    def test_unknown_fee_is_not_zero_and_missing_depth_cannot_quote_profit(self):
        self.assertIsNone(entry_breakdown(self.c)['directions']['a']['net_entry_usdc'])
        self.assertEqual(entry_breakdown(self.c,0)['directions']['a']['net_entry_usdc'], 3)
        self.c['lighter_sell_vwap']=None
        self.assertFalse(entry_breakdown(self.c,0)['directions']['a']['available'])

    def test_configured_variational_fee_and_funding_are_predictions(self):
        row = entry_breakdown(self.c, 10, variational_fee_bps=2, funding_bps_per_8h=1)
        a = row['directions']['a']
        self.assertAlmostEqual(a['variational_fees_usdc'], .04)
        self.assertAlmostEqual(a['fees_usdc'], .243)
        self.assertAlmostEqual(a['funding_estimate_usdc'], .02)
        self.assertEqual(row['funding_bps_per_8h'], 1)
        self.assertIsNone(row['funding_usdc'])

    def test_unconfigured_funding_remains_unknown(self):
        a = entry_breakdown(self.c, 0)['directions']['a']
        self.assertIsNone(a['funding_estimate_usdc'])


class SingleAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.strategy=Mock();self.strategy.live_blockers.return_value=[]
        self.strategy.halted_reason=None;self.strategy.positions=[];self.strategy.position_size=12
        self.strategy.last_execution_submitted=False
        self.worker=ExecutionWorker(self.strategy,['ETH'],Path(self.temp.name)/'lock',Path(self.temp.name)/'STOP',live=True)

    def test_close_only_rejects_both_single_entry_paths_without_scanning(self):
        self.worker.close_only=True
        for run in (self.worker.run_once,self.worker.run_until_opportunity):
            self.assertFalse(run()['attempted'])
        self.strategy.check_arbitrage_opportunity.assert_not_called()
        self.strategy.execute_arbitrage.assert_not_called()

    def test_attempts_once_and_never_manages_old_positions(self):
        result=self.worker.run_once()
        self.assertTrue(result['attempted'])
        self.strategy.execute_arbitrage.assert_called_once()
        self.strategy.close_position.assert_not_called()

    def test_existing_positions_and_kill_switch_block(self):
        self.strategy.positions=[SimpleNamespace(status='open')]
        with self.assertRaises(ValueError):self.worker.run_once()
        self.strategy.execute_arbitrage.assert_not_called()
        self.worker.kill_path.touch()
        self.assertFalse(self.worker.run_once()['attempted'])

    def test_exception_not_retried_and_history_recorded(self):
        self.strategy.execute_arbitrage.side_effect=RuntimeError('test')
        with self.assertRaises(RuntimeError):self.worker.run_once()
        self.strategy.execute_arbitrage.assert_called_once()
        self.assertEqual(self.strategy.history.append.call_args.args[0], 'single_attempt_stopped')

    def test_no_opportunity_and_dry_run_never_submit(self):
        self.strategy.check_arbitrage_opportunity.return_value=None
        self.assertFalse(self.worker.run_once()['attempted'])
        self.worker.live=False
        self.assertFalse(self.worker.run_once()['submitted'])
        self.strategy.execute_arbitrage.assert_not_called()

    def test_reference_outlier_does_not_starve_lower_valid_opportunity(self):
        self.strategy.spread_threshold_max = 50
        high = type('Opportunity', (), {'spread_bps': 88})()
        valid = type('Opportunity', (), {'spread_bps': 12})()
        self.strategy.check_arbitrage_opportunity.side_effect = [high, valid]
        worker = ExecutionWorker(self.strategy, ['OPENAI', 'AAPL'],
                                 Path(self.temp.name)/'rank-lock', Path(self.temp.name)/'STOP', live=True)
        self.assertIs(worker._best_opportunity(), valid)

    def test_wait_once_keeps_polling_until_first_opportunity(self):
        self.worker.interval=1
        self.strategy.check_arbitrage_opportunity.side_effect=[None, object()]
        result=self.worker.run_until_opportunity(max_cycles=3)
        self.assertTrue(result['attempted'])
        self.strategy.execute_arbitrage.assert_called_once()
        self.strategy.close_position.assert_not_called()

    def test_wait_once_keeps_polling_after_plan_rejected_without_submission(self):
        self.worker.interval=0
        self.strategy.check_arbitrage_opportunity.side_effect=[object(), None]
        self.strategy.execute_arbitrage.return_value=False
        result=self.worker.run_until_opportunity(max_cycles=2)
        self.assertFalse(result['attempted'])
        self.strategy.execute_arbitrage.assert_called_once()
        self.strategy.history.append.assert_any_call(
            'wait_once_skipped', symbol='ETH', reason='本轮报价未达到可执行净价差；未提交订单，继续等待')

    def test_wait_once_stops_without_submission_when_killed(self):
        self.worker.kill_path.touch()
        result=self.worker.run_until_opportunity()
        self.assertFalse(result['attempted'])
        self.assertTrue(result['stopped'])
        self.strategy.execute_arbitrage.assert_not_called()


class SchemaAuditTests(unittest.TestCase):
    def test_secrets_and_arbitrary_values_are_not_copied(self):
        secret='never-copy-this'
        rows=describe({'token':secret,'wallet_address':secret,'account_id':secret,'quote_id':secret,
                       'bid':'2700.1','nested':{'cookie':secret}, 'orders':[{'filled_size':'0.004','status':'filled'}]})
        self.assertNotIn(secret,str(rows))
        self.assertIn({'path':'bid','type':'str','sample':'2700.1'},rows)
        self.assertIn({'path':'account_id','type':'str'},rows)

    def test_official_origin_and_auth_paths_only(self):
        self.assertEqual(safe_path('https://omni.variational.io/api/quotes/indicative?x=secret'),'/api/quotes/indicative')
        for url in ('https://omni.variational.io.evil.com/api/quotes','https://omni.variational.io/api/auth/login'):
            self.assertIsNone(safe_path(url))
