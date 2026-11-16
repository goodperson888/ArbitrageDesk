"""Restart recovery uses account evidence; these tests never access a venue."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.common.rate_limit import RateLimitError
from src.execution.console import ExecutionConsole
from src.execution.journal import execution_lease
from src.execution.position_recovery import position_fingerprint, resume_proof_valid
from src.execution.reconcile_pair import reconcile_pair
from src.execution.worker import ExecutionWorker
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position


VENUES = ['lighter', 'arcus']


class PositionRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root/'data').mkdir()
        (self.root/'config').mkdir()
        (self.root/'config/config.yaml').write_text('strategy:\n  trading:\n    position_size: 12\n')
        (self.root/'.env').write_text('LIGHTER_ACCOUNT_INDEX=7\nARCUS_ACCOUNT_INDEX=0\n'
                                    'ARCUS_WALLET_ADDRESS=test\nARCUS_API_SIGNING_KEY=test-only\n')
        self.path = self.root/'data/hedge_state_lighter_arcus.json'
        self.position = Position('PUMP', 'short', 'buy', 3065, 3065, .003913, .003907,
            datetime.now(), id='original', status='reconcile_required',
            variational_venue='lighter', lighter_venue='arcus',
            entry_size_variational=3065, entry_size_lighter=3065,
            execution_id='original-execution', close_reason='价差收敛')
        self.state = {'positions': [asdict(self.position)], 'daily_trades': 1, 'daily_pnl': 0,
                      'day': datetime.now().date().isoformat(), 'last_trade_time': None,
                      'halted_reason': '平仓回执需核对'}
        self.write_state()
        self.lighter, self.arcus = Mock(), Mock()
        self.account = {'account_index': 7, 'pending_order_count': 0, 'positions': [
            {'market_id': 45, 'position': '3065', 'sign': -1, 'open_order_count': 0}]}
        self.arcus_positions = [{'marketId': 45, 'size': '3065', 'side': 'long'}]
        self.lighter.client.get.side_effect = lambda *a, **kw: ({'accounts': [self.account]}, utc_now())
        self.lighter.get_market.return_value = ({'market_id': 45}, utc_now())
        self.arcus.account.side_effect = lambda: {'positions': self.arcus_positions}
        self.arcus.orders.return_value = {'orders': []}
        self.arcus.market.return_value = {'market_id': 45}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('src.execution.reconcile_pair.LighterApiTrader', return_value=self.lighter))
        stack.enter_context(patch('src.execution.reconcile_pair.ArcusClient', return_value=self.arcus))

    def write_state(self):
        self.path.write_text(json.dumps(self.state, default=lambda d: d.isoformat()))

    def strategy(self):
        first, second = Mock(venue='lighter', live_ready=True), Mock(venue='arcus', live_ready=True)
        strategy = ArbitrageStrategy({'mode': 'live', 'state_path': str(self.path),
            'trading': {'position_size': 12}, 'risk': {}, 'wash_trading': {}}, first, second)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, strategy.market_history)
        return strategy, first, second

    def test_match_resumes_same_position_and_enters_close_monitor(self):
        report = reconcile_pair(self.root, VENUES)
        self.assertTrue(report['resume_ready'])
        self.assertFalse(report['orders_submitted'])

        self.assertEqual(json.loads(self.path.read_text())['positions'][0]['status'], 'reconcile_required')
        strategy, first, second = self.strategy()
        self.assertTrue(strategy.resume_verified_position())
        position = strategy.positions[0]
        self.assertEqual(position.status, 'open')
        self.assertEqual(position.id, 'original')
        self.assertEqual(position.execution_id, 'original-execution')
        self.assertEqual(position.entry_price_variational, .003913)
        self.assertEqual(position.entry_size_lighter, 3065)
        self.assertEqual(position.resume_count, 1)
        saved = json.loads(self.path.read_text())
        self.assertFalse(saved['resume_verified'])
        self.assertNotIn('resume_proof', saved)
        self.assertEqual(saved['positions'][0]['status'], 'open')
        self.assertFalse(strategy.resume_verified_position(), 'the proof is consumed exactly once')
        self.assertEqual(strategy.history.recent()[0]['event'], 'position_resumed')
        strategy.reconcile_open_positions = Mock(return_value='ok')
        strategy.close_reason = Mock(return_value='价差收敛')
        strategy.close_position = Mock(side_effect=lambda p, **kw: setattr(p, 'status', 'closed'))
        worker = ExecutionWorker(strategy, ['OTHER'], self.root/'worker.lock', self.root/'STOP',
                                 live=True, close_only=True)
        with patch.object(worker, '_attempt_candidates') as discover:
            worker.run(max_cycles=1)
        strategy.close_position.assert_called_once_with(position, reason='价差收敛')
        discover.assert_not_called()
        first.place_order.assert_not_called()
        second.place_order.assert_not_called()
        self.arcus.submit.assert_not_called()

    def test_multivenue_recovery_writes_the_original_scope_without_touching_pair_file(self):
        scope = ['lighter', 'lighter_rh', 'arcus']
        multi_path = self.root/'data/hedge_state_lighter_lighter_rh_arcus.json'
        multi_path.write_bytes(self.path.read_bytes())
        pair_before = self.path.read_bytes()
        report = reconcile_pair(self.root, VENUES, state_venues=scope)
        self.assertTrue(report['resume_ready'])
        self.assertFalse(report['orders_submitted'])
        self.assertEqual(self.path.read_bytes(), pair_before)
        saved = json.loads(multi_path.read_text())
        self.assertTrue(resume_proof_valid(saved, VENUES))
        self.assertEqual(saved['positions'][0]['status'], 'reconcile_required')

    def test_multivenue_wrong_hedge_pair_is_rejected_before_account_reads(self):
        scope = ['lighter', 'lighter_rh', 'arcus']
        multi_path = self.root/'data/hedge_state_lighter_lighter_rh_arcus.json'
        multi_path.write_bytes(self.path.read_bytes())
        before = multi_path.read_bytes()
        with self.assertRaisesRegex(ValueError, '原持仓平台'):
            reconcile_pair(self.root, ['lighter', 'lighter_rh'], state_venues=scope)
        self.assertEqual(multi_path.read_bytes(), before)
        self.lighter.client.get.assert_not_called()
        self.arcus.account.assert_not_called()

    def flat_multi_scope(self):
        scope = ['lighter', 'lighter_rh', 'arcus']
        saved = dict(self.state, positions=[dict(self.state['positions'][0], status='closed')])
        path = self.root/'data/hedge_state_lighter_lighter_rh_arcus.json'
        path.write_text(json.dumps(saved, default=lambda d: d.isoformat()))
        with (self.root/'.env').open('a') as env:
            env.write('LIGHTER_RH_ACCOUNT_INDEX=8\n')
        self.account['positions'] = []
        self.arcus_positions = []
        rh = Mock()
        rh_account = {'account_index': 8, 'pending_order_count': 0, 'positions': []}
        rh.client.get.side_effect = lambda *a, **kw: ({'accounts': [rh_account]}, utc_now())
        return scope, path, rh, rh_account

    def test_empty_multivenue_scope_checks_all_accounts_and_clears_stop(self):
        from scripts.reconcile_pair_state import reconcile_and_reset
        from src.execution.journal import OrderJournal
        scope, path, rh, _ = self.flat_multi_scope()
        stop = self.root/'data/STOP_EXECUTION'
        stop.touch()
        journal = OrderJournal(self.root/'data/execution_orders.sqlite')
        journal.reserve('lighter_rh', 'test', 'unknown-rh', {'symbol':'PUMP'})
        journal.finish(1, {'confirmed':True, 'filled_size':1, 'fees_usdc':None})
        pair_before = self.path.read_bytes()
        with patch('src.execution.reconcile_pair.LighterApiTrader',
                side_effect=lambda *a, **kw: rh if kw['account_index'] == 8 else self.lighter):
            report = reconcile_and_reset(self.root, ['arcus', 'lighter'], scope)
        self.assertTrue(report['success'])
        self.assertEqual(report['checked_venues'], scope)
        self.assertTrue(report['accounts_flat'])
        self.assertFalse(report['orders_submitted'])
        self.assertFalse(stop.exists())
        self.assertEqual(journal.get(1)['state'], 'terminal')
        self.assertEqual(self.path.read_bytes(), pair_before)
        self.assertIsNotNone(json.loads(path.read_text())['last_reconciled_at'])
        rh.client.get.assert_called_once()
        self.arcus.submit.assert_not_called()

    def test_empty_multivenue_scope_preserves_stop_if_third_account_is_not_flat(self):
        from scripts.reconcile_pair_state import reconcile_and_reset
        for pending in (False, True):
            with self.subTest(pending=pending):
                scope, path, rh, account = self.flat_multi_scope()
                if pending:
                    account['pending_order_count'] = 1
                else:
                    account['positions'] = [{'position':'1', 'open_order_count':0}]
                stop = self.root/'data/STOP_EXECUTION'
                stop.touch()
                before = path.read_bytes()
                with patch('src.execution.reconcile_pair.LighterApiTrader',
                        side_effect=lambda *a, **kw: rh if kw['account_index'] == 8 else self.lighter):
                    with self.assertRaisesRegex(ValueError, '仍有持仓或挂单|仍有活动订单'):
                        reconcile_and_reset(self.root, ['arcus', 'lighter'], scope)
                self.assertTrue(stop.exists())
                self.assertEqual(path.read_bytes(), before)

    def test_empty_multivenue_console_retains_scope_instead_of_missing_reverse_pair_file(self):
        scope, _, _, _ = self.flat_multi_scope()
        previous = dict(running=False, venues=scope, route_index=0,
                        route_candidates=[{'venues':['arcus','lighter']}])
        console = ExecutionConsole(self.root, Mock())
        console.stop_path.touch()
        report = {'success':True, 'orders_submitted':False, 'checked_venues':scope}
        with patch.object(console, 'status', return_value=previous), \
                patch('src.execution.console.subprocess.run', return_value=Mock(
                    stdout=json.dumps(report), returncode=0)) as run:
            self.assertTrue(console.reconcile_pair(['lighter','lighter_rh'])['success'])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--state-venues')+1], ','.join(scope))
        self.assertEqual(command[command.index('--venues')+1], 'arcus,lighter')

    def test_console_recovery_uses_persisted_hedge_over_scan_route_index(self):
        scope = ['lighter', 'lighter_rh', 'arcus']
        multi_path = self.root/'data/hedge_state_lighter_lighter_rh_arcus.json'
        multi_path.write_bytes(self.path.read_bytes())
        previous = dict(running=False, venues=scope, route_index=0,
                        route_candidates=[{'venues': ['lighter', 'lighter_rh']}])
        console = ExecutionConsole(self.root, Mock())
        self.assertEqual(console._execution_pair(previous), VENUES)
        report = {'success': True, 'orders_submitted': False, 'resume_ready': True}
        with patch('src.execution.console.subprocess.run', return_value=Mock(
                stdout=json.dumps(report), returncode=0)) as run:
            self.assertTrue(console._check_pair_recovery(VENUES, previous)['resume_ready'])
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--state-venues')+1], ','.join(scope))
            self.assertEqual(command[command.index('--venues')+1], ','.join(VENUES))

    def test_all_grid_layers_resume_without_changing_layer_or_anchor(self):
        row = self.state['positions'][0]
        row.update(grid_cycle_id='cycle', grid_layer=1, grid_anchor_bps=20)
        row['variational_size'] = row['lighter_size'] = 1500
        second = dict(row, id='layer2', grid_layer=2, grid_anchor_bps=30,
                      variational_size=1565, lighter_size=1565)
        self.state['positions'].append(second)
        self.write_state()
        self.assertTrue(reconcile_pair(self.root, VENUES)['resume_ready'])
        strategy, _, _ = self.strategy()
        self.assertTrue(strategy.resume_verified_position())
        self.assertEqual([(p.status, p.grid_layer, p.grid_anchor_bps) for p in strategy.positions],
                         [('open', 1, 20), ('open', 2, 30)])

    def test_one_leg_or_mismatched_quantity_never_authorizes_resume(self):
        for sizes in ((0, 3065), (3064, 3065)):
            self.account['positions'][0]['position'] = str(sizes[0])
            self.arcus_positions[0]['size'] = str(sizes[1])
            with self.assertRaises(ValueError):
                reconcile_pair(self.root, VENUES)
            self.assertFalse(json.loads(self.path.read_text()).get('resume_verified'))

    def test_active_orders_block_resume(self):
        self.arcus.orders.return_value = {'orders': [{'orderId': 'live', 'status': 'OPEN'}]}
        with self.assertRaises(ValueError):
            reconcile_pair(self.root, VENUES)
        self.assertFalse(json.loads(self.path.read_text()).get('resume_verified'))

    def test_failed_fresh_read_revokes_previous_success(self):
        reconcile_pair(self.root, VENUES)
        self.lighter.client.get.side_effect = RateLimitError(5)
        with self.assertRaises(RateLimitError):
            reconcile_pair(self.root, VENUES)
        strategy, _, _ = self.strategy()
        self.assertFalse(strategy.resume_verified_position())
        self.assertTrue(strategy.halted_reason)
        self.assertEqual(strategy.positions[0].status, 'reconcile_required')
        self.assertFalse(json.loads(self.path.read_text()).get('resume_verified'))

    def test_unknown_arcus_positions_or_orders_are_not_flat_proof(self):
        self.arcus.account.side_effect = lambda: {}
        with self.assertRaises(ValueError):
            reconcile_pair(self.root, VENUES)
        self.arcus.account.side_effect = lambda: {'positions': self.arcus_positions}
        self.arcus.orders.return_value = {}
        with self.assertRaises(ValueError):
            reconcile_pair(self.root, VENUES)

    def test_both_flat_settles_exposure_but_keeps_missing_accounting_pending(self):
        self.account['positions'] = []
        self.arcus_positions.clear()
        report = reconcile_pair(self.root, VENUES)
        self.assertTrue(report['accounts_flat'])
        self.assertFalse(report.get('resume_ready', False))
        self.assertEqual(report['pending_accounting_count'], 1)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['positions'][0]['status'], 'closed')
        self.assertTrue(saved['positions'][0]['accounting_pending'])
        self.assertEqual(saved['positions'][0]['entry_size_lighter'], 3065)
        self.assertIsNone(saved['positions'][0]['realized_pnl_usdc'])

    def test_stale_changed_or_legacy_boolean_proof_cannot_clear_halt(self):
        reconcile_pair(self.root, VENUES)
        valid = json.loads(self.path.read_text())
        self.assertTrue(resume_proof_valid(valid, VENUES))
        for kind in ('stale', 'changed', 'legacy', 'other-venues'):
            saved = json.loads(json.dumps(valid))
            if kind == 'stale':
                saved['resume_proof']['checked_at'] = (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
            elif kind == 'changed':
                saved['positions'][0]['lighter_size'] = 3000
            elif kind == 'legacy':
                saved.pop('resume_proof')
            else:
                saved['resume_proof']['venues'] = ['lighter', 'lighter_rh']
            self.path.write_text(json.dumps(saved))
            strategy, _, _ = self.strategy()
            self.assertFalse(strategy.resume_verified_position(), kind)
            self.assertTrue(strategy.halted_reason)

    def test_unmanaged_position_blocks_new_entry_even_without_halt_flag(self):
        strategy, first, second = self.strategy()
        strategy.halted_reason = None
        self.assertIn('存在未核对或未接管持仓，禁止新开仓', strategy.live_blockers())
        first.place_order.assert_not_called()
        second.place_order.assert_not_called()

    def test_readiness_check_automatically_runs_readonly_recovery(self):
        console = ExecutionConsole(self.root, Mock())
        report = {'success': True, 'orders_submitted': False, 'resume_ready': True}
        with patch('src.execution.console.subprocess.run', return_value=Mock(
                stdout=json.dumps(report), returncode=0)) as run:
            result = console._check_pair_recovery(VENUES, {'running': False, 'venues': VENUES})
        self.assertTrue(result['resume_ready'])
        self.assertIn('reconcile_pair_state.py', ' '.join(run.call_args.args[0]))
        self.assertNotIn('--run', run.call_args.args[0])

    def test_running_worker_or_other_pair_is_never_reconciled_by_check(self):
        console = ExecutionConsole(self.root, Mock())
        with patch('src.execution.console.subprocess.run') as run:
            self.assertIsNone(console._check_pair_recovery(VENUES, {'running': True, 'venues': VENUES}))
            self.assertIsNone(console._check_pair_recovery(VENUES, {'running': False, 'venues': ['lighter', 'lighter_rh']}))
        run.assert_not_called()

    def test_account_check_cannot_mutate_state_while_runtime_lease_is_held(self):
        from scripts.reconcile_pair_state import reconcile_and_reset
        before = self.path.read_bytes()
        with execution_lease(self.root/'data/runtime.lock'), self.assertRaises(RuntimeError):
            reconcile_and_reset(self.root, VENUES)
        self.assertEqual(self.path.read_bytes(), before)

    def test_valid_proof_prevents_a_new_account_read_on_every_page_poll(self):
        reconcile_pair(self.root, VENUES)
        console = ExecutionConsole(self.root, Mock())
        with patch('src.execution.console.threading.Thread') as thread:
            console._maybe_start_auto_reconcile({'running': False, 'venues': VENUES})
        thread.assert_not_called()

    def test_other_pair_file_cannot_be_used_as_recovery_state(self):
        self.path.unlink()
        other = self.root/'data/hedge_state_lighter_lighter_rh.json'
        other.write_text(json.dumps(self.state, default=lambda d: d.isoformat()))
        console = ExecutionConsole(self.root, Mock())
        with patch('src.execution.console.subprocess.run') as run:
            self.assertIsNone(console._check_pair_recovery(VENUES, {'running': False}))
        run.assert_not_called()

    def test_actual_start_defers_only_pending_state_gate_to_locked_recovery(self):
        from src.execution import runtime
        profile = {'venues': VENUES, 'live_enabled': True, 'symbols': ['PUMP']}
        with patch.object(runtime, '__file__', str(self.root/'src/execution/runtime.py')):
            normal = runtime.pair_runtime_blockers(profile)
            startup = runtime.pair_runtime_blockers(profile, recover_at_start=True)
        self.assertTrue(any('该平台组合有未核对成交/持仓' in reason for reason in normal))
        self.assertFalse(any('该平台组合有未核对成交/持仓' in reason for reason in startup))
        self.assertEqual([reason for reason in normal if '该平台组合有未核对成交/持仓' not in reason], startup)
