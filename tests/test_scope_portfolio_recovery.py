"""Recover all held routes using the original state file after scope changes."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.position_recovery import validate_portfolio_accounts, resume_proof_valid
from src.execution.reconcile_pair import reconcile_pair
import test_position_recovery as recovery


class ScopePortfolioRecoveryTests(unittest.TestCase):
    setUp = recovery.PositionRecoveryTests.setUp
    write_state = recovery.PositionRecoveryTests.write_state

    def mixed(self):
        extra = copy.deepcopy(self.state['positions'][0])
        extra.update(id='second',lighter_venue='lighter_rh')
        self.state['positions'].append(extra)
        managed = ['lighter','arcus','lighter_rh']
        self.state['runtime_scope'] = dict(version=1,managed_venues=managed,
                                          entry_venues=['arcus','lighter_rh'])
        self.write_state()
        self.account['positions'][0]['position'] = '6130'
        with (self.root/'.env').open('a') as out:
            out.write('LIGHTER_RH_ACCOUNT_INDEX=9\n')
        self.rh = Mock()
        self.rh.client.get.return_value = ({'accounts':[dict(account_index=9,pending_order_count=0,
            positions=[dict(market_id=45,position='3065',sign=1,open_order_count=0)])]},utc_now())
        self.rh.get_market.return_value = ({'market_id':45},utc_now())
        return managed

    def reconcile(self):
        with patch('src.execution.reconcile_pair.LighterApiTrader',side_effect=lambda *a,**k:
                   self.rh if k['account_index']==9 else self.lighter):
            return reconcile_pair(self.root,['lighter','lighter_rh'],state_venues=['lighter','arcus'])

    def test_mixed_routes_share_exposure_and_recover_original_two_venue_anchor(self):
        self.mixed()
        self.assertTrue(self.reconcile()['resume_ready'])
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved['resume_proof']['portfolio'])
        self.assertTrue(resume_proof_valid(saved,['lighter','lighter_rh']))
        self.assertTrue(resume_proof_valid(saved,['lighter','arcus']))
        self.assertFalse((self.root/'data/hedge_state_lighter_lighter_rh.json').exists())
        self.assertEqual(len(saved['positions']),2)
        self.assertEqual(saved['positions'][0]['lighter_venue'],'arcus')
        self.assertEqual(saved['positions'][1]['lighter_venue'],'lighter_rh')

    def test_mismatch_or_unrelated_position_blocks_portfolio_proof(self):
        self.mixed()
        self.account['positions'].append(dict(market_id=99,position='1',sign=1,open_order_count=0))
        with self.assertRaisesRegex(ValueError,'不匹配'):
            self.reconcile()
        self.assertFalse(json.loads(self.path.read_text()).get('resume_verified',False))

    def test_local_symbols_flat_but_other_exposure_cannot_claim_accounts_flat(self):
        self.mixed()
        self.account['positions'] = [dict(market_id=99,position='1',sign=1,open_order_count=0)]
        self.arcus_positions.clear()
        self.rh.client.get.return_value[0]['accounts'][0]['positions'].clear()
        with self.assertRaisesRegex(ValueError,'其他敞口'):
            self.reconcile()

    def test_original_anchor_required_before_launching_new_worker_with_old_positions(self):
        self.mixed()
        from src.execution.console import ExecutionConsole
        console = ExecutionConsole(self.root,Mock())
        previous = dict(running=False,state='finished',venues=['lighter','arcus'])
        with patch.object(console,'status',return_value=previous), \
                patch.object(console,'check_pair') as check:
            result = console.check('PUMP',12,['lighter','lighter_rh'])
            self.assertFalse(result['ready'])
            self.assertIn('原管理范围',result['blockers'][0])
            check.assert_not_called()
            check.return_value = dict(ready=True,venues=['lighter','arcus'])
            result = console.check('PUMP',12,['arcus','lighter'])
            self.assertEqual(result['recovery_anchor'],['lighter','arcus'])
            self.assertEqual(console._approved_execution_venues(result),['lighter','arcus'])

    def test_aggregate_wrong_side_cannot_resume(self):
        p = self.state['positions'][0]
        snapshots = dict(lighter=dict(positions=[dict(position='3065',sign=1)]),
                         arcus=dict(positions=[dict(position='3065',sign=1)]))
        with self.assertRaisesRegex(ValueError,'方向或数量'):
            validate_portfolio_accounts([p],['lighter','arcus'],snapshots,
                lambda venue,position:snapshots[venue]['positions'])

    def test_manual_reconcile_passes_original_anchor_when_only_new_route_is_first(self):
        self.mixed()
        self.state['positions'].reverse(); self.write_state()
        from src.execution.console import ExecutionConsole
        console = ExecutionConsole(self.root,Mock())
        previous = dict(running=False,state='finished',venues=['lighter','arcus'])
        console.stop_path.touch()
        with patch.object(console,'status',return_value=previous), \
                patch('src.execution.console.subprocess.run',return_value=Mock(returncode=0,
                    stdout=json.dumps(dict(success=True,orders_submitted=False,resume_ready=True)))) as run:
            self.assertTrue(console.reconcile_pair(['lighter','arcus'])['success'])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--venues')+1],'lighter,lighter_rh')
        self.assertEqual(args[args.index('--state-venues')+1],'lighter,arcus')

    def test_background_reconcile_passes_same_original_anchor(self):
        self.mixed()
        from src.execution.console import ExecutionConsole
        console = ExecutionConsole(self.root,Mock())
        console._save(dict(state='finished',started_at='old-run',venues=['lighter','arcus']))
        with patch('src.execution.reconcile_pair.reconcile_pair',return_value=dict(
                success=True,orders_submitted=False,resume_ready=True)) as read:
            console._auto_reconcile_loop(['lighter','lighter_rh'],'old-run',state_venues=['lighter','arcus'])
        read.assert_called_once_with(self.root,['lighter','lighter_rh'],state_venues=['lighter','arcus'])
