import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import yaml
from src.web import app as web
from src.common.config_loader import ConfigLoader
from src.variational.public_client import VariationalPublicClient
from src.lighter.api_trader import LighterApiTrader
from src.strategy.arbitrage_strategy import ArbitrageStrategy
from src.variational.quote_bridge import QuoteBridge
from src.common.market_data import utc_now
from src.web.diagnostics import Diagnostics
from src.execution.console import ExecutionConsole
from test_market_data import stats, lighter_stats, session_for


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'config.yaml'
        self.path.write_text(yaml.safe_dump({'strategy': {'mode': 'monitor',
            'state_path': str(Path(self.temp.name)/'state.json'),
            'spread_threshold': {'min': 5, 'max': 50}},
            'variational': {'markets': ['BTC', 'US100']}, 'lighter': {'markets': ['BTC', 'US100']}}))
        self.patches = [patch.object(web, name, value) for name, value in {
            'config_loader': ConfigLoader(self.path),
            'variational_trader': VariationalPublicClient(session=session_for(stats())),
            'lighter_trader': LighterApiTrader(session=session_for(lighter_stats())),
            'symbols': ['BTC', 'US100'], 'current_prices': {}, 'spread_history': {},
            'trading_status': {'running': False}, 'quote_bridge': QuoteBridge(),
            'v_feed': None, 'l_feed': None, 'book_cache': {},
            'diagnostics': Diagnostics(Path(self.temp.name)/'diagnostics.sqlite'),
            'manual_events_path': Path(self.temp.name)/'manual.jsonl',
            'credentials_path': Path(self.temp.name)/'.env'}.items()]
        self.patches.append(patch.object(web, 'history_sync', Mock(poll=Mock(return_value={'state': 'idle'}))))
        for p in self.patches: p.start()
        p = patch.object(web, 'execution_console', ExecutionConsole(Path(self.temp.name), web.diagnostics))
        p.start(); self.patches.append(p)
        p = patch.object(web, 'strategy', ArbitrageStrategy(web.config_loader.get_strategy_config(),
                        web.variational_trader, web.lighter_trader))
        p.start(); self.patches.append(p)
        self.client = web.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def test_prices_from_both_official_payloads(self):
        data = self.client.get('/api/prices').get_json()
        self.assertEqual(data['BTC']['variational'], 83000)
        self.assertEqual(data['BTC']['lighter'], 83020)
        self.assertEqual(data['US100']['variational'], 30633.55)
        self.assertTrue(data['US100']['instrument_mismatch'])
        self.assertFalse(data['BTC']['demo'])
        self.assertFalse(data['BTC']['executable'])
        self.client.get('/api/prices')
        self.assertEqual(len(self.client.get('/api/spread-history').get_json()['BTC']), 1)

    def test_network_failure_is_null_not_synthetic_or_last_success(self):
        self.client.get('/api/prices')
        with patch.object(web.variational_trader, 'get_snapshot', side_effect=RuntimeError('offline')):
            result = self.client.get('/api/prices').get_json()
        self.assertIsNone(result['BTC']['variational'])
        self.assertIsNone(result['BTC']['spread_bps'])
        self.assertIn('variational', result['BTC']['errors'])
        self.assertFalse(result['BTC']['demo'])

    def test_preflight_blocks_fake_start(self):
        result = self.client.post('/api/trading/start', json={})
        self.assertEqual(result.status_code, 409)
        self.assertFalse(result.get_json()['success'])
        self.assertFalse(self.client.get('/api/status').get_json()['running'])

    def test_operations_export_is_readonly_local_and_never_ready(self):
        result = self.client.get('/api/operations/export')
        self.assertEqual(result.status_code, 200)
        data = result.get_json()
        self.assertFalse(data['live_enabled'])
        self.assertFalse(data['capture']['position']['known'])
        self.assertTrue(data['blockers'])
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertIn('attachment', result.headers['Content-Disposition'])
        self.assertEqual(self.client.get('/api/operations', base_url='http://evil.example').status_code, 403)

    def test_operations_matches_quote_to_rejection_instead_of_latest_other_symbol(self):
        history = web.strategy.history
        with history.execution_scope('a'):
            history.append('quote_evaluation', symbol='MRVL', direction='buy_variational', quantity=1,
                           net_edge_bps=-24, min_edge_bps=5)
            history.append('quote_evaluation', symbol='MRVL', direction='sell_variational', quantity=1,
                           net_edge_bps=-2.1, min_edge_bps=5)
            history.append('blocked', symbol='MRVL', reason='没有符合净价差与金额上限的深度')
        with history.execution_scope('b'):
            history.append('quote_evaluation', symbol='BOT', direction='sell_variational', quantity=1,
                           net_edge_bps=99, min_edge_bps=5)
        execution = self.client.get('/api/operations').get_json()['execution']
        self.assertEqual(execution['last_quote_evaluation']['detail']['symbol'], 'BOT')
        matched = execution['rejection_quote_evaluation']['detail']
        self.assertEqual((matched['execution_id'], matched['symbol'], matched['net_edge_bps']), ('a', 'MRVL', -2.1))

    def test_resumed_live_position_is_not_reported_as_flat_or_pending(self):
        root = Path(self.temp.name)
        (root/'data').mkdir(exist_ok=True)
        (root/'data/hedge_state_lighter_arcus.json').write_text(json.dumps({
            'last_reconciled_at': utc_now(), 'positions': [
                {'id': 'old', 'symbol': 'PUMP', 'status': 'open', 'resumed_at': utc_now(),
                 'variational_size': 3065, 'lighter_size': 3065}]}))
        execution = {'running': True, 'state': 'running', 'venues': ['lighter', 'arcus'],
                     'note': '原进程持仓监控中'}
        with patch.object(web, 'ROOT', root), patch.object(web.execution_console, 'status', return_value=execution):
            data = self.client.get('/api/operations?venues=lighter,arcus').get_json()
        self.assertEqual(data['execution']['note'], '原进程持仓监控中')
        self.assertEqual(data['execution']['managed_positions'][0]['symbol'], 'PUMP')
        self.assertFalse(data['execution']['recovery_blocked'])
        self.assertFalse(data['pair_reconcile_required'])

    def test_running_process_with_unmanaged_position_does_not_claim_takeover(self):
        root = Path(self.temp.name)
        (root/'data').mkdir(exist_ok=True)
        (root/'data/hedge_state_lighter_arcus.json').write_text(json.dumps({
            'positions': [{'id': 'old', 'symbol': 'PUMP', 'status': 'reconcile_required'}]}))
        with patch.object(web, 'ROOT', root), patch.object(web.execution_console, 'status', return_value={
                'running': True, 'venues': ['lighter', 'arcus']}):
            data = self.client.get('/api/operations?venues=lighter,arcus').get_json()
        self.assertTrue(data['execution']['recovery_blocked'])
        self.assertEqual(data['execution']['managed_positions'], [])

    def test_stopped_multivenue_worker_exposes_real_unmanaged_position(self):
        root = Path(self.temp.name)
        (root/'data').mkdir(exist_ok=True)
        (root/'data/hedge_state_lighter_lighter_rh_arcus.json').write_text(json.dumps({
            'halted_reason': '账户核对待处理', 'positions': [{'id': 'cashcat', 'symbol': 'CASHCAT',
                'status': 'reconcile_required', 'variational_venue': 'lighter', 'lighter_venue': 'arcus',
                'variational_size': 139.3, 'lighter_size': 139.3}]}))
        execution = dict(running=False, state='finished', venues=['lighter', 'lighter_rh', 'arcus'])
        with patch.object(web, 'ROOT', root), patch.object(web.execution_console, 'status', return_value=execution):
            data = self.client.get('/api/operations?venues=lighter,lighter_rh,arcus').get_json()
        self.assertEqual(data['execution']['tracked_position_count'], 1)
        self.assertTrue(data['execution']['recovery_blocked'])
        self.assertTrue(data['pair_reconcile_required'])
        self.assertIn('CASHCAT', data['execution']['note'])
        self.assertIn('自动平仓监控已停止', data['execution']['note'])

    def test_history_includes_each_actual_pair_from_a_multivenue_file(self):
        from dataclasses import asdict
        from datetime import datetime
        from src.strategy.arbitrage_strategy import Position
        root = Path(self.temp.name)
        (root/'data').mkdir(exist_ok=True)
        path = root/'data/hedge_state_lighter_lighter_rh_arcus.json'
        rows = [Position(symbol, 'short', 'buy', 0, 0, 100, 99, datetime.now(), id=symbol,
                         status='closed', entry_size_variational=1, entry_size_lighter=1,
                         variational_venue=first, lighter_venue=second)
                for symbol, first, second in (('CASHCAT', 'lighter', 'arcus'),
                                               ('SUI', 'lighter_rh', 'arcus'))]
        path.write_text(json.dumps({'positions': [asdict(p) for p in rows],
            'daily_trades': 2, 'daily_pnl': 0, 'day': datetime.now().date().isoformat()},
            default=lambda d:d.isoformat()))
        before = path.read_bytes()
        data = self.client.get('/api/execution-history').get_json()
        self.assertFalse(data['history_errors'], data['history_errors'])
        self.assertEqual({(r['symbol'], r['variational_venue'], r['lighter_venue'])
                          for r in data['closed_positions']},
                         {('CASHCAT', 'lighter', 'arcus'), ('SUI', 'lighter_rh', 'arcus')})
        filtered = self.client.get('/api/execution-history?venues=lighter,arcus').get_json()
        self.assertEqual([r['symbol'] for r in filtered['closed_positions']], ['CASHCAT'])
        self.assertEqual(path.read_bytes(), before)

    def test_running_close_only_mode_keeps_its_note_with_a_flat_snapshot(self):
        root=Path(self.temp.name)
        (root/'data').mkdir(exist_ok=True)
        (root/'data/hedge_state_lighter_arcus.json').write_text(json.dumps({
            'positions': [],'last_reconciled_at':utc_now()}))
        execution={'running':True,'state':'running','close_only':True,
                   'venues':['lighter','arcus'],'note':'只平仓模式，不开新仓'}
        with patch.object(web,'ROOT',root),patch.object(web.execution_console,'status',return_value=execution):
            data=self.client.get('/api/operations?venues=lighter,arcus').get_json()
        self.assertEqual(data['execution']['tracked_position_count'],0)
        self.assertTrue(data['execution']['close_only'])
        self.assertEqual(data['execution']['note'],'只平仓模式，不开新仓')

    def test_operations_uses_actual_worker_backoff_instead_of_web_strategy(self):
        from datetime import datetime, timedelta, timezone
        root = Path(self.temp.name)
        (root/'data').mkdir(exist_ok=True)
        until = (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        (root/'data/hedge_state_lighter_arcus.monitor.json').write_text(json.dumps({
            'worker_pid': 123, 'running': True, 'updated_at': utc_now(), 'positions': [],
            'entry_scan': {'backoff_until': until, 'backoff_reason': '实际工作进程限流'}}))
        execution = dict(running=True, pid=123, venues=['lighter','arcus'], phase='连接签名平台', note='启动提示')
        self.assertIsNone(web.strategy.entry_scan_backoff_until)
        with patch.object(web, 'ROOT', root), patch.object(web.execution_console, 'status', return_value=execution):
            data = self.client.get('/api/operations?venues=lighter,arcus').get_json()
        self.assertEqual(data['entry_scan_backoff_until'], until)
        self.assertEqual(data['entry_scan_backoff_reason'], '实际工作进程限流')
        self.assertEqual(data['execution']['phase'], '新开仓扫描限流等待')

    def test_running_resume_requires_token_and_only_targets_recorded_run(self):
        payload = dict(pid=123, started_at='run-start', stop_generation='1:123')
        with patch.object(web.execution_console, 'request_resume', return_value={
                'success': True, 'pid':123, 'message':'已恢复'}) as resume:
            self.assertEqual(self.client.post('/api/execution/resume', json=payload).status_code, 403)
            resume.assert_not_called()
            headers = {'X-Local-Token':web.credentials_token}
            response = self.client.post('/api/execution/resume', json=payload, headers=headers)
            self.assertEqual(response.status_code, 200)
            resume.assert_called_once_with(**payload)
            resume.reset_mock()
            response = self.client.post('/api/execution/resume', json={**payload, 'venues':['arcus','lighter']}, headers=headers)
            self.assertEqual(response.status_code, 400)
            resume.assert_not_called()
            resume.return_value = {'success':False, 'message':'原仓位正在核对'}
            response = self.client.post('/api/execution/resume', json=payload, headers=headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()['error'], '原仓位正在核对')

    def test_live_scope_requires_token_exact_payload_and_original_run(self):
        payload = dict(pid=123,started_at='run-start',venues=['lighter','arcus'],symbols=['BTC'])
        with patch.object(web.execution_console,'request_scope',return_value=dict(success=True,request_id='x')) as scope:
            self.assertEqual(self.client.post('/api/execution/scope',json=payload).status_code,403)
            scope.assert_not_called()
            headers = {'X-Local-Token':web.credentials_token}
            self.assertEqual(self.client.post('/api/execution/scope',json=payload,headers=headers).status_code,200)
            scope.assert_called_once_with(**payload)
            scope.reset_mock()
            self.assertEqual(self.client.post('/api/execution/scope',json={**payload,'resume':True},headers=headers).status_code,400)
            scope.assert_not_called()
            scope.return_value = dict(success=False,message='原进程已变化')
            self.assertEqual(self.client.post('/api/execution/scope',json=payload,headers=headers).status_code,409)

    def test_execution_requires_local_nonce_and_readonly_check_does_not_launch(self):
        payload = {'symbol':'ETH','max_notional':1}
        self.assertEqual(self.client.post('/api/execution/once', json=payload).status_code,403)
        headers = {'X-Local-Token':web.credentials_token}
        result = {'ready':False,'blockers':['最低金额10'], 'orders_submitted':False}
        with patch.object(web.execution_console,'check',return_value=result), patch.object(web.execution_console,'launch_once') as launch:
            response=self.client.post('/api/execution/check',json=payload,headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.get_json()['ready'])
            launch.assert_not_called()
        self.assertEqual(self.client.post('/api/execution/check',json=payload,headers={**headers,'Origin':'https://evil.example'}).status_code,403)

    def test_readonly_check_propagates_recency_mode_and_rejects_unknown_mode_before_reads(self):
        payload = {'symbol': 'ETH', 'max_notional': 1, 'reversion_max_age_seconds': 7200}
        headers = {'X-Local-Token': web.credentials_token}
        with patch.object(web.execution_console, 'check', return_value={'ready': False}) as check:
            for mode in ('hard', 'soft'):
                result = self.client.post('/api/execution/check', json={**payload, 'reversion_recency_mode': mode}, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(check.call_args.kwargs['reversion_recency_mode'], mode)
                self.assertEqual(check.call_args.kwargs['reversion_max_age_seconds'], 7200)
            check.reset_mock()
            result = self.client.post('/api/execution/check', json={**payload, 'reversion_recency_mode': 'invalid'}, headers=headers)
            self.assertEqual(result.status_code, 400)
            check.assert_not_called()
            result = self.client.post('/api/execution/check', json={**payload, 'future_policy': True}, headers=headers)
            self.assertEqual(result.status_code, 400)
            self.assertIn('当前服务不支持的参数', result.get_json()['error'])
            check.assert_not_called()

    def test_amount_mode_is_forwarded_for_checks_and_starts_and_types_are_validated(self):
        payload = dict(symbol='ETH', max_notional=1, follow_saved_cap=True)
        headers = {'X-Local-Token': web.credentials_token}
        routes = [('check', 'check'), ('monitor', 'launch_monitor'), ('once', 'launch_once'),
                  ('wait-once', 'launch_wait_once')]
        for route, method in routes:
            with self.subTest(route=route), patch.object(web.execution_console, method,
                    return_value=dict(ready=True, started=False)) as action:
                result = self.client.post('/api/execution/'+route, json=payload, headers=headers)
                self.assertIn(result.status_code, (200, 409))
                self.assertIs(action.call_args.kwargs['follow_saved_cap'], True)
        with patch.object(web.execution_console, 'check') as check:
            for value in (1, 'true', None):
                result = self.client.post('/api/execution/check',
                    json={**payload, 'follow_saved_cap': value}, headers=headers)
                self.assertEqual(result.status_code, 400)
            check.assert_not_called()

    def test_readonly_check_forwards_all_scan_venues_while_binding_a_pair(self):
        payload = {'symbol': 'SOL', 'max_notional': 1,
                   'venues': ['lighter', 'arcus'],
                   'scan_venues': ['lighter', 'lighter_rh', 'arcus']}
        headers = {'X-Local-Token': web.credentials_token}
        with patch.object(web.execution_console, 'check', return_value={'ready': False, 'blockers': []}) as check:
            response = self.client.post('/api/execution/check', json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(check.call_args.kwargs['scan_venues'], payload['scan_venues'])

    def test_full_catalog_readonly_check_accepts_complete_scope_over_one_kib(self):
        headers = {'X-Local-Token': web.credentials_token}
        for count in (79, web.EXECUTION_SCOPE_MAX_SYMBOLS):
            symbols = [f'MARKET{number:04d}' for number in range(count)]
            payload = {'symbol': 'ALL', 'symbols': symbols, 'mode': 'all', 'max_notional': 12,
                       'venues': ['lighter', 'arcus'], 'scan_venues': ['lighter', 'lighter_rh', 'arcus'],
                       'reversion_target_mode': 'center', 'reversion_recency_mode': 'soft',
                       'candidate_fallback': True, 'single_position_cycle': True}
            self.assertGreater(len(json.dumps(payload).encode()), 1024)
            with self.subTest(count=count), \
                    patch.object(web, 'refresh_saved_verifications'), \
                    patch.object(web.execution_console, 'check', return_value={'ready': False, 'orders_submitted': False}) as check, \
                    patch.object(web.execution_console, 'launch_monitor') as monitor, \
                    patch.object(web.execution_console, 'launch_once') as once:
                response = self.client.post('/api/execution/check', json=payload, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(check.call_args.kwargs['symbols'], symbols)
                self.assertFalse(response.get_json()['orders_submitted'])
                monitor.assert_not_called()
                once.assert_not_called()
                check.reset_mock()
                denied = self.client.post('/api/execution/check', json=payload)
                self.assertEqual(denied.status_code, 403)
                denied = self.client.post('/api/execution/check', json=payload,
                                          headers={**headers, 'Origin': 'https://evil.example'})
                self.assertEqual(denied.status_code, 403)
                check.assert_not_called()

    def test_execution_scope_size_limit_is_bounded_and_route_specific(self):
        headers = {'X-Local-Token': web.credentials_token}
        with patch.object(web.execution_console, 'check') as check:
            for path in web.EXECUTION_SCOPE_PATHS:
                response = self.client.post(path, data='x' * (web.EXECUTION_SCOPE_MAX_BYTES + 1),
                                             content_type='application/json', headers=headers)
                self.assertEqual(response.status_code, 413)
                self.assertIn('128 KiB', response.get_json()['error'])
            for path in ('/api/execution/stop', '/api/execution/reset-stop', '/api/quote-observer/start'):
                response = self.client.post(path, data='x' * 1025,
                                             content_type='application/json', headers=headers)
                self.assertEqual(response.status_code, 413)
            check.assert_not_called()

    def test_oversized_symbol_scope_is_rejected_without_truncating_authorization(self):
        headers = {'X-Local-Token': web.credentials_token}
        for symbols in ([f'MARKET{number}' for number in range(web.EXECUTION_SCOPE_MAX_SYMBOLS + 1)],
                        ['x' * (web.EXECUTION_SYMBOL_MAX_LENGTH + 1)]):
            with self.subTest(count=len(symbols)), patch.object(web.execution_console, 'check') as check:
                response = self.client.post('/api/execution/check', json={
                    'symbol': 'ALL', 'symbols': symbols, 'mode': 'all', 'max_notional': 12}, headers=headers)
                self.assertEqual(response.status_code, 400)
                check.assert_not_called()

    def test_signed_preview_routes_to_selected_pair_without_launching_orders(self):
        payload = {'symbol': 'SOL', 'max_notional': 1, 'venues': ['lighter', 'arcus']}
        report = {'ready': True, 'orders_submitted': False, 'plan': {}}
        with patch.object(web.execution_console, 'preview_plan', return_value=report) as preview, \
                patch.object(web.execution_console, 'launch_once') as launch:
            response = self.client.post('/api/execution/preview', json=payload,
                                        headers={'X-Local-Token': web.credentials_token})
        self.assertEqual(response.status_code, 200)
        preview.assert_called_once_with('SOL', 1, ['lighter', 'arcus'])
        launch.assert_not_called()

    def test_stop_reset_requires_nonce_and_never_launches(self):
        self.assertEqual(self.client.post('/api/execution/reset-stop',json={}).status_code,403)
        with patch.object(web.execution_console,'reset_stop',return_value={'success':True,'orders_submitted':False}) as reset, patch.object(web.execution_console,'launch_once') as launch:
            response=self.client.post('/api/execution/reset-stop',json={},headers={'X-Local-Token':web.credentials_token})
            self.assertEqual(response.status_code,200)
            reset.assert_called_once();launch.assert_not_called()

    def test_configuration_really_persists_and_reloads(self):
        result = self.client.post('/api/config', json={'strategy': {'trading': {'position_size': 25, 'max_positions': 2}}})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.trading.position_size'), 25)
        self.assertEqual(web.strategy.position_size, 25)

    def test_hedge_policy_settings_persist_and_invalid_values_do_not_change_file(self):
        risk={'execution_price_movement_bps':2,'hedge_max_delay_seconds':5,
              'route_rollback_limit':2,'route_rollback_cooldown_seconds':900}
        result=self.client.post('/api/config',json={'strategy':{'risk':risk}})
        self.assertEqual(result.status_code,200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.risk'),risk)
        self.assertEqual(web.strategy.hedge_policy.rollback_limit,2)
        before=self.path.read_text()
        for bad in ({'route_rollback_limit':1.5},{'hedge_max_delay_seconds':0},
                    {'execution_price_movement_bps':-1},{'route_rollback_cooldown_seconds':0}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post('/api/config',json={'strategy':{'risk':bad}}).status_code,400)
                self.assertEqual(self.path.read_text(),before)

    def test_book_utilization_persists_and_rejects_invalid_values(self):
        response = self.client.post('/api/config', json={'strategy': {
            'risk': {'book_utilization_percent': 65}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.risk.book_utilization_percent'), 65)
        self.assertEqual(web.strategy.book_utilization_percent, 65)
        before = self.path.read_text()
        for value in (0, -1, 101, True, None, float('nan')):
            with self.subTest(value=value):
                response = self.client.post('/api/config', json={'strategy': {
                    'risk': {'book_utilization_percent': value}}})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_text(), before)

    def test_configuration_saves_entry_limits_without_rebuilding_running_strategy(self):
        with patch.object(web.execution_console, 'status', return_value={'running': True}):
            result = self.client.post('/api/config', json={'strategy': {'trading': {'position_size': 25}}})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()['pending'])
        self.assertEqual(result.get_json()['apply_scope'], 'new_entries')
        self.assertIn('下次开仓检查前', result.get_json()['message'])
        self.assertIn('全部平仓', result.get_json()['message'])
        self.assertEqual(ConfigLoader(self.path).get('strategy.trading.position_size'), 25)
        self.assertNotEqual(web.strategy.position_size, 25)

    def test_capital_budget_settings_persist_and_reject_invalid_values(self):
        values=dict(capital_reserve_percent=20,capital_reserve_usdc=10,capital_allocation_mode='remaining_slots')
        response=self.client.post('/api/config',json={'strategy':{'trading':values}})
        self.assertEqual(response.status_code,200)
        for key,value in values.items():
            self.assertEqual(ConfigLoader(self.path).get('strategy.trading.'+key),value)
        before=self.path.read_text()
        for bad in ({'capital_reserve_percent':100},{'capital_reserve_usdc':-1},
                    {'capital_allocation_mode':'margin_equal'},{'capital_reserve_percent':True}):
            self.assertEqual(self.client.post('/api/config',json={'strategy':{'trading':bad}}).status_code,400)
            self.assertEqual(self.path.read_text(),before)

    def test_old_worker_cannot_silently_ignore_enabled_capital_reserve(self):
        before=self.path.read_text()
        with patch.object(web.execution_console,'status',return_value={'running':True}),\
                patch.object(web,'_position_monitor_payload',return_value={'monitor':{'entry_limits':{'version':1}}}):
            response=self.client.post('/api/config',json={'strategy':{'trading':{'capital_reserve_percent':20}}})
        self.assertEqual(response.status_code,409)
        self.assertIn('未加载资金预留',response.get_json()['error'])
        self.assertEqual(self.path.read_text(),before)

    def test_old_running_worker_cannot_silently_load_unrecognized_channel_mode(self):
        before=self.path.read_bytes()
        with patch.object(web.execution_console,'status',return_value={'running':True}), \
                patch.object(web,'_position_monitor_payload',return_value={'monitor':{'channel_policy_version':0}}):
            result=self.client.post('/api/config',json={'strategy':{'entry_policy':{'mode':'channel'}}})
        self.assertEqual(result.status_code,409)
        self.assertIn('尚未加载通道策略',result.get_json()['error'])
        self.assertEqual(self.path.read_bytes(),before)

    def test_touch_channel_cannot_be_saved_into_old_worker_and_new_worker_accepts_it(self):
        before=self.path.read_bytes()
        payload={'strategy':{'entry_policy':{'mode':'channel','calculation_version':2,'funding_reserve_bps':0},
            'channel_policy':{'method':'touches','touch_min_count':4,'touch_tolerance_bps':1.5}}}
        with patch.object(web.execution_console,'status',return_value={'running':True}):
            with patch.object(web,'_position_monitor_payload',return_value={'monitor':{'channel_policy_version':1}}):
                result=self.client.post('/api/config',json=payload)
            self.assertEqual(result.status_code,409)
            self.assertIn('尚未加载反复触碰通道',result.get_json()['error'])
            self.assertEqual(self.path.read_bytes(),before)
            with patch.object(web,'_position_monitor_payload',return_value={'monitor':{'channel_policy_version':2}}):
                result=self.client.post('/api/config',json=payload)
            self.assertEqual(result.status_code,200)
            self.assertTrue(result.get_json()['pending'])
        self.assertEqual(ConfigLoader(self.path).get('strategy.channel_policy.touch_min_count'),4)
        saved=self.path.read_bytes()
        payload['strategy']['channel_policy']['touch_min_count']=2.5
        with patch.object(web.execution_console,'status',return_value={'running':False}):
            result=self.client.post('/api/config',json=payload)
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.path.read_bytes(),saved)

    def test_strategy_preview_is_token_protected_and_read_only(self):
        payload={'symbol':'SUI','venues':['arcus','lighter'],'channel_policy':{'method':'fixed'}}
        self.assertEqual(self.client.post('/api/strategy-preview',json=payload).status_code,403)
        before=self.path.read_bytes()
        with patch('src.web.strategy_preview.strategy_preview',return_value={'orders_submitted':False}) as preview:
            result=self.client.post('/api/strategy-preview',json=payload,headers={'X-Local-Token':web.credentials_token})
        self.assertEqual(result.status_code,200);preview.assert_called_once()
        self.assertEqual(self.path.read_bytes(),before)

    def test_close_policy_persists_and_is_exposed_in_status(self):
        policy = {'mode': 'convergence_and_profit', 'convergence_basis': 'reduction', 'convergence_bps': 1.5,
                  'min_profit_usdc': 0.25, 'min_profit_bps': 2, 'cost_reserve_usdc': 0.1,
                  'max_hold_seconds': 1800}
        result = self.client.post('/api/config', json={'strategy': {'close_policy': policy}})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.close_policy'), policy)
        self.assertEqual(web.strategy.get_strategy_status()['close_policy'],
                         {**policy, 'target_mode': 'zero'})

    def test_invalid_close_policy_does_not_mutate_disk(self):
        original = self.path.read_text()
        for bad in ({'mode': 'unknown'}, {'convergence_basis': 'unknown'}, {'convergence_bps': -1},
                    {'max_hold_seconds': 1.5}, {'min_profit_usdc': float('nan')}):
            with self.subTest(bad=bad):
                response = self.client.post('/api/config', json={'strategy': {'close_policy': bad}})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_text(), original)

    def test_shared_target_policy_requires_explicit_valid_funding_budget(self):
        original = self.path.read_text()
        for values in ({'calculation_version':2},
                       {'calculation_version':2,'funding_reserve_bps':None},
                       {'calculation_version':2,'funding_reserve_bps':float('nan')},
                       {'calculation_version':3,'funding_reserve_bps':6},
                       {'calculation_version':2,'funding_reserve_bps':6,'target_window_seconds':299},
                       {'calculation_version':2,'funding_reserve_bps':6,'target_window_seconds':86400.5}):
            with self.subTest(values=values):
                result = self.client.post('/api/config',json={'strategy':{'entry_policy':values}})
                self.assertEqual(result.status_code,400)
                self.assertEqual(self.path.read_text(),original)
        policy = dict(calculation_version=2,funding_reserve_bps=6,target_window_seconds=604800)
        result = self.client.post('/api/config',json={'strategy':{'entry_policy':policy}})
        self.assertEqual(result.status_code,200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.entry_policy'),policy)

    def test_dynamic_funding_config_round_trip_and_invalid_values_do_not_persist(self):
        policy = dict(funding_mode='dynamic',funding_reserve_bps=5,
                      funding_hold_seconds=5400,funding_extra_bps=.5)
        result = self.client.post('/api/config',json={'strategy':{'entry_policy':policy}})
        self.assertEqual(result.status_code,200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.entry_policy'),policy)
        original = self.path.read_text()
        for change in ({'funding_mode':'automatic'}, {'funding_hold_seconds':59},
                       {'funding_hold_seconds':604801}, {'funding_extra_bps':-1},
                       {'funding_extra_bps':float('nan')}):
            with self.subTest(change=change):
                result = self.client.post('/api/config',json={'strategy':{'entry_policy':change}})
                self.assertEqual(result.status_code,400)
                self.assertEqual(self.path.read_text(),original)

    def test_one_dollar_cap_and_disabled_money_limits(self):
        result=self.client.post('/api/config',json={'strategy':{'trading':{'position_size':1},
            'risk':{'max_loss_per_trade':None,'max_daily_loss':None}}})
        self.assertEqual(result.status_code,200)
        self.assertIsNone(web.strategy.max_daily_loss)
        self.assertEqual(web.strategy.position_size,1)
        self.assertEqual(self.client.get('/api/dashboard?symbol=BTC&notional=1').status_code,200)
        self.assertEqual(self.client.get('/api/execution-history').get_json()['events'],[])

    def test_history_includes_all_pairs_independent_of_active_worker(self):
        import json
        from dataclasses import asdict
        from datetime import datetime
        from src.strategy.arbitrage_strategy import Position
        root = Path(self.temp.name) / 'data'
        root.mkdir(exist_ok=True)
        for second, identity in [('arcus', 'arcus-fill'), ('lighter_rh', 'rh-fill')]:
            position = Position('BABA', 'short', 'buy', 0, 0, 100, 99, datetime(2026, 11, 1),
                                status='closed', id=identity, entry_size_variational=1,
                                entry_size_lighter=1, close_price_variational=100,
                                close_price_lighter=99, realized_pnl_usdc=0,
                                closed_time=datetime(2026, 11, 1, 11))
            payload = {'positions': [asdict(position)], 'daily_trades': 1, 'daily_pnl': 0,
                       'day': '2026-11-01', 'last_trade_time': None}
            (root / ('hedge_state_lighter_' + second + '.json')).write_text(
                json.dumps(payload, default=lambda v: v.isoformat()))
        from src.execution.history import TradeHistory
        from src.execution.journal import OrderJournal
        TradeHistory(root / 'hedge_events.sqlite').append(
            'entry_receipt', position_id='rh-fill', venue='lighter', order_id='rh-order')
        journal = OrderJournal(root / 'execution_orders.sqlite')
        for venue, client_id, order_id in [('lighter', 'rh-fill-v', 'rh-order'),
                                            ('arcus', 'arcus-open-test', 'arcus-test')]:
            reserved, _ = journal.reserve(venue, 'test', client_id, {'symbol': 'BABA', 'side': 'buy'})
            journal.finish(reserved['seq'], {'confirmed': True, 'status': 'filled',
                                           'filled_size': 1, 'average_price': 100,
                                           'fees_usdc': 0, 'order_id': order_id})
        with patch.object(web.execution_console, 'status', return_value={'venues': ['lighter', 'lighter_rh']}), \
                patch.object(web, 'LighterApiTrader', side_effect=AssertionError('closed history needs no network')):
            data = self.client.get('/api/execution-history').get_json()
            self.assertEqual(data['scope'], 'all')
            self.assertEqual(data['summary']['closed_count'], 2)
            self.assertEqual({p['lighter_venue'] for p in data['closed_positions']}, {'arcus', 'lighter_rh'})
            selected = self.client.get('/api/execution-history?venues=arcus,lighter').get_json()
            self.assertEqual(len(selected['closed_positions']), 1)
            self.assertEqual(selected['closed_positions'][0]['id'], 'arcus-fill')
            self.assertEqual([row['order_id'] for row in selected['other_fills']], ['arcus-test'],
                             'filtering Arcus must not reclassify a known RH pair fill as an unmatched Lighter order')
            missing = self.client.get('/api/execution-history?venues=arcus,standx').get_json()
            self.assertEqual(missing['closed_positions'], [])
            self.assertEqual(self.client.get('/api/execution-history?venues=../data,arcus').status_code, 400)

    def test_open_history_corrects_entry_cost_in_view_without_touching_live_state(self):
        from dataclasses import asdict
        from datetime import datetime, timezone
        from src.strategy.arbitrage_strategy import Position
        from src.web.history_marks import HistoryMarks
        root = Path(self.temp.name) / 'data'
        root.mkdir(exist_ok=True)
        position = Position('PUMP', 'short', 'buy', 2, 2, 104, 99, datetime.now(),
            id='cost-test', status='open', variational_venue='lighter', lighter_venue='arcus',
            entry_size_variational=2, entry_size_lighter=2, entry_fees_usdc=.1)
        path = root / 'hedge_state_lighter_arcus.json'
        path.write_text(json.dumps({'positions': [asdict(position)], 'daily_trades': 1,
            'daily_pnl': 0, 'day': datetime.now().date().isoformat()}, default=lambda v: v.isoformat()))
        before = path.read_bytes()
        rows = [dict(venue=venue, symbol='PUMP', order_id=venue, client_order_id='cost-test-'+leg,
            side=side, reduce_only=False, status='filled', filled_size='2', average_price=str(price))
            for venue,leg,side,price in [('lighter','v','sell',104), ('arcus','l','buy',98.9)]]
        (root/'platform-order-history.json').write_text(json.dumps({'rows':rows}))
        def quotes(venue, symbol):
            return [dict(venue=venue, symbol=symbol, bid=100, ask=100, stale=False,
                age_seconds=0, fetched_at=datetime.now(timezone.utc).isoformat(), source='public_websocket')]
        monitor = {'heartbeat_stale':False, 'positions':[{'id':'cost-test'}]}
        with patch.object(web, 'history_marks', HistoryMarks(fetch=quotes)), \
                patch('src.execution.position_monitor.read_position_monitor', return_value=monitor), \
                patch.object(web, 'LighterApiTrader', side_effect=AssertionError('no account client')):
            data = self.client.get('/api/execution-history?venues=lighter,arcus').get_json()
        self.assertEqual(data['history_errors'], [])
        row = data['open_positions'][0]
        self.assertEqual(row['entry_price_lighter'], 98.9)
        self.assertAlmostEqual(row['unrealized_pnl_usdc'], 10.1)
        self.assertTrue(row['entry_accounting_pending_reload'])
        self.assertEqual(path.read_bytes(), before)

    def test_open_history_uses_shared_public_marks_without_account_clients(self):
        from dataclasses import asdict
        from datetime import datetime, timezone
        from src.strategy.arbitrage_strategy import Position
        from src.web.history_marks import HistoryMarks
        root = Path(self.temp.name) / 'data'
        root.mkdir(exist_ok=True)
        position = Position('PUMP', 'short', 'buy', 2, 2, 104, 99, datetime.now(),
                            variational_venue='lighter', lighter_venue='arcus',
                            entry_size_variational=2, entry_size_lighter=2, entry_fees_usdc=.1)
        (root / 'hedge_state_lighter_arcus.json').write_text(json.dumps(
            {'positions': [asdict(position)], 'daily_trades': 1, 'daily_pnl': 0,
             'day': datetime.now().date().isoformat(), 'last_trade_time': None}, default=lambda v: v.isoformat()))
        def quotes(venue, symbol):
            return [dict(venue=venue, symbol=symbol, bid=103 if venue=='lighter' else 100,
                         ask=103 if venue=='lighter' else 100, stale=False, age_seconds=0,
                         fetched_at=datetime.now(timezone.utc).isoformat(), source='public_websocket')]
        with patch.object(web, 'history_marks', HistoryMarks(fetch=quotes)), \
                patch.object(web, 'LighterApiTrader', side_effect=AssertionError('no account client')):
            data = self.client.get('/api/execution-history?venues=lighter,arcus').get_json()
        row = data['open_positions'][0]
        self.assertEqual(row['mark_status'], 'live')
        self.assertAlmostEqual(row['unrealized_pnl_usdc'], 3.9)
        self.assertEqual(row['mark_sources']['lighter'], 'public_websocket')
        self.assertEqual(data['history_errors'], [])

    def test_manually_closed_history_skips_quotes_and_updates_all_derived_columns(self):
        from dataclasses import asdict
        from datetime import datetime
        from src.strategy.arbitrage_strategy import Position
        from src.execution.history import TradeHistory
        from test_history_accounting import evidence
        root = Path(self.temp.name)/'data'
        root.mkdir(exist_ok=True)
        position = Position('BABA', 'long', 'sell', 0, 1, 100, 101,
            datetime.fromisoformat('2026-11-01T01:00:00+00:00'), id='p', status='reconcile_required',
            entry_size_variational=1, entry_size_lighter=1,
            variational_venue='arcus', lighter_venue='lighter', accounting_pending=True,
            close_started_time=datetime.fromisoformat('2026-11-01T01:59:59+00:00'))
        path = root/'hedge_state_arcus_lighter.json'
        path.write_text(json.dumps({'positions':[asdict(position)], 'daily_trades':1, 'daily_pnl':0,
                                   'day':'2026-11-01', 'last_trade_time':None}, default=lambda value:value.isoformat()))
        before = path.read_bytes()
        history = TradeHistory(root/'hedge_events.sqlite')
        for venue, oid in [('arcus','a-open'), ('lighter','l-open')]:
            history.append('entry_receipt', position_id='p', venue=venue, order_id=oid)
        _, cache = evidence()
        (root/'platform-order-history.json').write_text(json.dumps(cache))
        with patch.object(web.HistoryMarkAdapter, 'get_mark_snapshot', side_effect=AssertionError('closed trade needs no quote')):
            report = self.client.get('/api/execution-history?venues=arcus,lighter').get_json()
        self.assertEqual(report['history_errors'], [])
        self.assertEqual(report['summary']['open_count'], 0)
        self.assertEqual(report['summary']['closed_count'], 1)
        row = report['closed_positions'][0]
        self.assertTrue(row['history_recovered'])
        self.assertEqual(row['close_elapsed_ms'], 1000)
        self.assertEqual(row['close_spread_bps'], 0)
        self.assertAlmostEqual(row['realized_pnl_usdc'], .81)
        self.assertEqual(path.read_bytes(), before, 'display reconciliation cannot unlock execution state')

    def test_grid_mode_is_selectable_validated_and_roundtrips_to_single(self):
        grid = {'mode':'spread_grid','step_bps':5,'max_layers':3,
                'layer_notional_usdc':12,'total_notional_usdc':36}
        result = self.client.post('/api/config',json={'strategy':{'position_policy':grid,
            'trading':{'max_positions':3},'risk':{'entry_extra_profit_bps':0}}})
        self.assertEqual(result.status_code,200)
        self.assertTrue(web.strategy.grid_enabled)
        before=self.path.read_text()
        for bad in ({'max_layers':1.5},{'step_bps':0},{'mode':'invalid'},
                    {'total_notional_usdc':5},{'layer_notional_usdc':True}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post('/api/config',json={'strategy':{'position_policy':bad}}).status_code,400)
                self.assertEqual(self.path.read_text(),before)
        self.assertEqual(self.client.post('/api/config', json={'strategy':{
            'trading': {'max_positions': 2}}}).status_code, 200)
        self.assertEqual(ConfigLoader(self.path).get('strategy.position_policy.max_layers'), 3)
        self.assertEqual(self.client.post('/api/config',json={'strategy':{
            'position_policy':{'mode':'single'},'trading':{'max_positions':1}}}).status_code,200)
        self.assertFalse(web.strategy.grid_enabled)
        self.assertEqual(ConfigLoader(self.path).get('strategy.position_policy.step_bps'),5)

    def test_invalid_config_does_not_mutate_disk(self):
        original = self.path.read_text()
        for bad in [{'mode': 'live'}, {'trading': {'position_size': -1}},
                    {'spread_threshold': {'min': 100, 'max': 5}},
                    {'trading': {'max_positions': 1.2}}, {'risk': {'slippage_tolerance': float('nan')}}]:
            with self.subTest(bad=bad):
                response = self.client.post('/api/config', json={'strategy': bad})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_text(), original)

    def test_no_cross_origin_writes(self):
        self.assertEqual(self.client.post('/api/trading/start', json={},
                                          headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/trading/start').status_code, 415)

    def test_orderbook_failure_returns_empty_with_error(self):
        with patch.object(web.lighter_trader, 'get_order_book', side_effect=RuntimeError('offline')):
            result = self.client.get('/api/orderbook/BTC')
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.get_json()['bids'], [])
        self.assertFalse(result.get_json()['demo'])

    def test_bridge_pair_and_ingest_require_local_pairing(self):
        self.assertEqual(self.client.post('/api/bridge/pair', json={},
                         headers={'Origin': 'https://evil.example'}).status_code, 403)
        token = self.client.post('/api/bridge/pair', json={}).get_json()['token']
        payload = {'source_url': 'https://omni.variational.io/api/quotes/indicative',
                   'symbol': 'BTC', 'instrument_type': 'perpetual_future',
                   'qty': '0.1', 'bid': '100', 'ask': '101', 'observed_at': utc_now()}
        self.assertEqual(self.client.post('/api/bridge/quote', json=payload).status_code, 403)
        result = self.client.post('/api/bridge/quote', json=payload,
                                  headers={'Authorization': 'Bearer '+token})
        self.assertEqual(result.status_code, 200)
        # Browser quotes must survive even if REST and mark feeds have no snapshot.
        row = self.client.get('/api/dashboard?symbol=BTC').get_json()
        self.assertEqual(row['comparison']['variational_ask'], 101)
        self.assertEqual(row['comparison']['base_quantity'], .1)
        self.assertFalse(row['comparison']['executable'])

    def test_invalid_stream_and_dashboard_parameters(self):
        for endpoint in ('/api/dashboard', '/api/stream'):
            for query in ('symbol=BAD', 'notional=NaN', 'notional=Infinity', 'notional=-1',
                          'slippage_bps=NaN', 'slippage_bps=-1', 'fee_bps=101'):
                with self.subTest(endpoint=endpoint, query=query):
                    self.assertEqual(self.client.get(endpoint+'?'+query).status_code, 400)

    def test_sse_emits_snapshot_and_can_close(self):
        with patch.object(web, 'live_row', return_value={'symbol': 'BTC', 'executable': False}):
            response = self.client.get('/api/stream?symbol=BTC', buffered=False)
            try:
                frames = iter(response.response)
                self.assertIn(b'retry: 2000', next(frames))
                self.assertIn(b'event: quote', next(frames))
                self.assertEqual(response.mimetype, 'text/event-stream')
            finally:
                response.close()
