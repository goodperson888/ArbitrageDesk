import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from src.execution.console import ExecutionConsole
from src.execution.runtime import runtime_blockers
from src.web.diagnostics import Diagnostics


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'config').mkdir()
        (self.root/'config/config.yaml').write_text('strategy:\n  trading:\n    position_size: 1\nlighter: {}\n')
        (self.root/'config/variational-execution.local.json').write_text(json.dumps({'symbol':'ETH','response_schema':{'adapter':'omni_v1'}}))
        self.console=ExecutionConsole(self.root, Diagnostics(self.root/'data/diagnostics.sqlite'))

    def test_close_only_cannot_launch_an_opening_validation(self):
        with patch('src.execution.console.subprocess.Popen') as spawn, patch.object(self.console,'check') as check:
            for launch in (self.console.launch_once,self.console.launch_wait_once):
                with self.assertRaisesRegex(ValueError,'仅管理已有仓位'):
                    launch('ETH',1,close_only=True)
            check.assert_not_called()
            spawn.assert_not_called()

    def test_minimum_and_schema_blocks_never_spawn(self):
        public=Mock()
        public.get_market.return_value=({'symbol':'ETH','market_type':'perp','status':'active',
            'min_quote_amount':'10','min_base_amount':'.002','supported_size_decimals':4,'mark_price':'2700'},'now')
        with patch('src.execution.console.LighterApiTrader',return_value=public), patch('src.execution.console.subprocess.Popen') as spawn:
            result=self.console.launch_once('ETH',1)
            self.assertFalse(result['started'])
            self.assertFalse(result['budget']['budget_allowed'])
            self.assertEqual(result['max_notional'],1)
            self.assertIn('最低开仓',result['blockers'][0])
            spawn.assert_not_called()

    def test_route_pauses_only_read_the_recorded_pair_and_filter_expired_entries(self):
        import time
        path=self.root/'data/hedge_state_lighter_arcus.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'route_rollback_guards':{
            'UNI|arcus->lighter':{'rollback_count':2,'paused_until':time.time()+900},
            'OLD|arcus->lighter':{'rollback_count':2,'paused_until':1}}}))
        pauses=self.console._active_route_pauses(['lighter','arcus'])
        self.assertEqual(len(pauses),1)
        self.assertEqual(pauses[0]['route'],'UNI|arcus->lighter')
        self.assertEqual(self.console._active_route_pauses(['lighter','lighter_rh']),[])

    def test_entry_risk_status_exposes_only_recorded_pair_samples_and_primary_causes(self):
        from src.execution.window_risk import ExecutionWindowRisk
        risk = ExecutionWindowRisk()
        key = 'UNI|arcus->lighter'
        for _ in range(5):
            risk.observe(key, 100.2, 100.2, 100, 100.04, True, 1200)
        risk.outcome(key, 'zero_fill', 12)
        path = self.root/'data/hedge_state_lighter_arcus.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'execution_window_risk': risk.routes}))
        status = self.console._route_risk_status(['lighter', 'arcus'])
        row = status['entry_risk_routes'][0]
        self.assertEqual(row['samples'], 5)
        self.assertAlmostEqual(row['movement_bps'], 4)
        self.assertEqual(row['window_ms'], 1200)
        self.assertEqual(row['size_cap_usdc'], 6)
        self.assertEqual(row['counts'], {'zero_fill': 1})
        self.assertEqual(self.console._route_risk_status(['lighter','lighter_rh'])['entry_risk_routes'], [])

    def test_cannot_raise_saved_cap_or_submit_bool_nan(self):
        for cap in (12,True,'NaN','Infinity',0):
            with self.subTest(cap=cap), self.assertRaises((ValueError, ArithmeticError)):
                self.console.check('ETH',cap)

    def test_unknown_or_running_persistent_state_survives_restart(self):
        self.console._save({'state':'running'})
        replacement=ExecutionConsole(self.root,self.console.diagnostics)
        self.assertTrue(replacement.status()['running'])
        self.console.state_path.write_text('broken')
        self.assertTrue(replacement.status()['running'])
        self.assertEqual(replacement.status()['state'],'unknown')

    def test_stop_does_not_kill_or_claim_flat(self):
        self.console.process=Mock()
        self.console.request_stop()
        self.assertTrue(self.console.stop_path.exists())
        self.console.process.terminate.assert_not_called()
        self.assertEqual(self.console.status()['state'],'idle')

    def test_restart_recovers_explicit_stop_completion_for_current_run(self):
        from src.execution.history import TradeHistory
        history = TradeHistory(self.root/'data/hedge_events.sqlite')
        history.append('execution_stop_completed', reason='已确认结束')
        for state in ('running', 'failed'):
            self.console._save({'state': state, 'started_at': '2026-01-01T00:00:00+00:00', 'pid': 123})
            self.console.stop_path.touch()
            with patch.object(self.console, '_worker_alive', return_value=False), \
                    patch.object(self.console, '_maybe_start_auto_reconcile'):
                result = self.console.status()
            self.assertEqual(result['state'], 'finished')
            self.assertTrue(result['stop_completed'])
            self.assertFalse(result['running'])
            self.assertTrue(self.console.stop_path.exists(), 'completion must preserve stop marker')

    def test_old_event_or_alive_worker_cannot_be_called_stop_completed(self):
        from src.execution.history import TradeHistory
        TradeHistory(self.root/'data/hedge_events.sqlite').append('execution_stop_completed')
        for started_at, alive in [('2099-01-01T00:00:00+00:00', False),
                                  ('2026-01-01T00:00:00+00:00', True)]:
            self.console._save({'state': 'running', 'started_at': started_at, 'pid': 123})
            with patch.object(self.console, '_worker_alive', return_value=alive), \
                    patch.object(self.console, '_maybe_start_auto_reconcile'):
                result = self.console.status()
            self.assertFalse(result.get('stop_completed', False))
            self.assertEqual(result['running'], alive)
            if not alive:
                self.assertEqual(result['state'], 'failed')

    def test_actual_contract_cannot_be_enabled_by_flags_without_costs(self):
        profile={'live_enabled':True,'account_verified':True,'receipt_schema_verified':True,
                 'response_schema':{'adapter':'omni_v1','account_session_verified':True,'executable_quote_verified':True}}
        self.assertTrue(any('结算字段仍缺失' in item for item in runtime_blockers(profile)))

    def test_readonly_check_omits_only_default_off_gate(self):
        profile={'live_enabled':False,'funding_accounting_mode':'defer_settlement',
                 'response_schema':{'adapter':'omni_v1'}}
        blocked=runtime_blockers(profile)
        checks=runtime_blockers(profile,require_enabled=False)
        self.assertIn('执行配置默认关闭',blocked)
        self.assertNotIn('执行配置默认关闭',checks)
        self.assertEqual([b for b in blocked if b!='执行配置默认关闭'],checks)
        self.assertTrue(any('账户' in b for b in checks))

    def test_prepare_does_not_launch_orders_or_mark_receipts_without_replay(self):
        report={'ready':True,'orders_submitted':False,'checks':{'submit_labels':{'buy':'买 ETH','sell':'卖 ETH'}}}
        with patch('src.execution.console.subprocess.run',return_value=Mock(stdout=json.dumps(report))) as run:
            result=self.console.prepare_browser('ETH',1)
        self.assertFalse(result['orders_submitted'])
        self.assertIn('prepare_variational_execution.py',run.call_args.args[0][-1])
        self.assertNotIn('--run',run.call_args.args[0])
        profile=json.loads((self.root/'config/variational-execution.local.json').read_text())
        self.assertFalse(profile.get('live_enabled',False))
        self.assertFalse(profile.get('receipt_schema_verified',False))

    def test_prepare_forbidden_during_execution(self):
        self.console._save({'state':'running'})
        with patch('src.execution.console.subprocess.run') as run,self.assertRaises(ValueError):
            self.console.prepare_browser('ETH',1)
        run.assert_not_called()

    def test_signed_preview_forwards_pair_without_execution_flags(self):
        report = {'ready': True, 'orders_submitted': False, 'plan': {}}
        with patch('src.execution.console.subprocess.run',
                   return_value=Mock(stdout=json.dumps(report), returncode=0)) as run:
            result = self.console.preview_plan('SOL', 1, ['lighter', 'arcus'])
        self.assertTrue(result['ready'])
        command = run.call_args.args[0]
        self.assertEqual(command[-3:], ['SOL', '--venues', 'lighter,arcus'])
        self.assertNotIn('--run', command)
        self.assertNotIn('--arm-once', command)

    def test_invalid_preview_pair_never_launches_child(self):
        for venues in (['arcus', 'arcus'], ['variational', 'lighter'], []):
            with self.subTest(venues=venues), patch('src.execution.console.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    self.console.preview_plan('SOL', 1, venues)
                run.assert_not_called()

    def test_wait_once_starts_waiter_not_immediate_once(self):
        public=Mock()
        public.get_market.return_value=({'symbol':'ETH','market_type':'perp','status':'active',
            'min_quote_amount':'1','min_base_amount':'.0001','supported_size_decimals':4,'mark_price':'2700'},'now')
        ready={'ready':True,'blockers':[],'max_notional':1.0,'symbol':'ETH','orders_submitted':False}
        with patch.object(self.console,'check',return_value=ready), patch.object(self.console,'_watch'), patch('src.execution.console.subprocess.Popen') as spawn:
            spawn.return_value=Mock()
            result=self.console.launch_wait_once('ETH',1)
        self.assertTrue(result['started'])
        command=spawn.call_args.args[0]
        self.assertIn('--wait-once',command)
        self.assertNotIn('--once',command)
        self.assertIn('--arm-once',command)

    def test_reset_forbidden_during_execution(self):
        self.console._save({'state':'running'})
        with patch('src.execution.console.subprocess.run') as run:
            self.assertFalse(self.console.reset_stop()['success'])
        run.assert_not_called()

    def test_changed_pair_cannot_start_while_original_worker_runs(self):
        old = {'running': True, 'venues': ['lighter', 'lighter_rh']}
        with patch.object(self.console, 'status', return_value=old), \
                patch.object(self.console, '_public_pair_budget', return_value={}), \
                patch('src.execution.runtime.pair_runtime_blockers', return_value=[]), \
                patch('src.execution.console.subprocess.Popen') as spawn:
            for launch in (self.console.launch_once, self.console.launch_wait_once, self.console.launch_monitor):
                result = launch('ETH', 1, ['lighter', 'arcus'])
                self.assertFalse(result['started'])
                self.assertIn('lighter_rh', ' '.join(result['blockers']))
            spawn.assert_not_called()

    def test_route_candidates_bind_first_ready_pair_and_keep_diagnostics(self):
        reports = {
            ('lighter', 'arcus'): {'ready': False, 'blockers': ['Arcus 费率证据已过期'], 'venues': ['lighter', 'arcus'], 'budget': None},
            ('lighter', 'lighter_rh'): {'ready': True, 'blockers': [], 'venues': ['lighter', 'lighter_rh'], 'budget': {'effective_minimum_notional': 1}},
            ('arcus', 'lighter_rh'): {'ready': False, 'blockers': ['手续费未核验'], 'venues': ['arcus', 'lighter_rh'], 'budget': None},
            ('lighter_rh', 'arcus'): {'ready': False, 'blockers': ['手续费未核验'], 'venues': ['lighter_rh', 'arcus'], 'budget': None},
        }
        def check_pair(symbol, cap, pair, **kwargs):
            return {**reports[tuple(pair)], 'symbol': symbol, 'max_notional': cap,
                    'orders_submitted': False, 'checked_at': 'now'}
        with patch.object(self.console, 'check_pair', side_effect=check_pair):
            result = self.console.check_routes('SOL', 1, ['lighter', 'lighter_rh', 'arcus'],
                                               preferred=['lighter', 'arcus'])
        self.assertTrue(result['ready'])
        self.assertEqual(result['venues'], ['lighter', 'lighter_rh'])
        self.assertEqual([item['venues'] for item in result['route_candidates']],
                         [['lighter', 'arcus'], ['lighter', 'lighter_rh'], ['lighter_rh', 'arcus']])
        self.assertEqual([item['venues'] for item in result['ready_routes']], [['lighter', 'lighter_rh']])
        self.assertIn('Arcus 费率证据已过期', result['route_candidates'][0]['blockers'])

    def test_route_candidates_do_not_need_a_visible_opportunity(self):
        self.assertEqual(
            self.console._route_pairs(['arcus', 'lighter_rh', 'lighter'], preferred=None),
            [['arcus', 'lighter_rh'], ['arcus', 'lighter'], ['lighter_rh', 'lighter']])

    def test_clean_pre_submit_exit_can_start_next_route(self):
        self.console.log_path.parent.mkdir(exist_ok=True)
        state = {
            'state': 'running', 'mode': 'once', 'symbol': 'SOL', 'max_notional': 1,
            'started_at': '2026-01-01T00:00:00+00:00', 'venues': ['lighter', 'arcus'],
            'symbols': ['SOL'], 'route_index': 0,
            'route_candidates': [{'venues': ['lighter', 'arcus'], 'ready': True},
                                 {'venues': ['lighter', 'lighter_rh'], 'ready': True}],
        }
        child = Mock(pid=321)
        with patch.object(self.console, '_pair_has_pending_state', return_value=False), \
                patch('src.execution.console.subprocess.Popen', return_value=child) as spawn, \
                patch.object(self.console, '_watch'):
            with self.console.log_path.open('a', encoding='utf-8') as log:
                result = self.console._start_route_fallback(state, [], log)
        self.assertEqual(result['venues'], ['lighter', 'lighter_rh'])
        self.assertEqual(result['route_index'], 1)
        self.assertIn('--venues', spawn.call_args.args[0])

    def test_switch_requires_old_positions_resolved_but_not_old_fees(self):
        old = {'running': False, 'venues': ['lighter', 'lighter_rh']}
        state_path = self.root/'data/hedge_state_lighter_lighter_rh.json'
        state_path.parent.mkdir(exist_ok=True)
        with patch.object(self.console, 'status', return_value=old), \
                patch.object(self.console, '_public_pair_budget', return_value={}), \
                patch('src.execution.runtime.pair_runtime_blockers', return_value=[]):
            for status in ('open', 'reconcile_required'):
                state_path.write_text(json.dumps({'positions': [{'status': status}]}))
                result = self.console.check_pair('ETH', 1, ['lighter', 'arcus'])
                self.assertFalse(result['ready'])
                self.assertIn('上一组合', ' '.join(result['blockers']))
            state_path.write_text(json.dumps({'positions': [{'status': 'closed', 'accounting_pending': True}]}))
            self.assertTrue(self.console.check_pair('ETH', 1, ['lighter', 'arcus'])['ready'])

    def test_stop_reset_checks_original_accounts_after_scanner_changes(self):
        old = {'running': False, 'venues': ['lighter', 'lighter_rh']}
        self.console.stop_path.parent.mkdir(exist_ok=True)
        self.console.stop_path.touch()
        report = {'success': True, 'orders_submitted': False}
        with patch.object(self.console, 'status', return_value=old), \
                patch('src.execution.console.subprocess.run', return_value=Mock(stdout=json.dumps(report), returncode=0)) as run:
            result = self.console.reconcile_pair(['lighter', 'arcus'])
            self.assertEqual(result['venues'], old['venues'])
            self.assertEqual(run.call_args.args[0][-1], 'lighter,lighter_rh')
            self.console.reset_stop()
            self.assertEqual(run.call_args.args[0][-1], 'lighter,lighter_rh')
