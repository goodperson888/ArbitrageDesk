"""Manual exits remain group scoped, reduce-only, concurrent and receipt driven."""
import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.console import ExecutionConsole
from src.execution.live_scope import write_scope_request
from src.execution.manual_close import ManualCloseController, annotate_controls, identity, REASON
from src.execution.worker import ExecutionWorker
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position
from test_parallel_close import Trader, fill


class ManualCloseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.console = ExecutionConsole(self.root, Mock())
        self.run = dict(state='running', running=True, pid=os.getpid(), started_at=utc_now(),
                        mode='continuous_monitor', venues=['lighter', 'arcus'])
        self.path = self.console._pair_state_path(self.run['venues'])
        self.a, self.b = Trader('lighter', 'short'), Trader('arcus', 'long')
        self.strategy = ArbitrageStrategy({'mode':'live', 'state_path':str(self.path),
            'risk':{'slippage_tolerance':7, 'single_leg_recovery_max_attempts':2}}, self.a, self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.strategy.market_history)
        self.addCleanup(self.strategy.close_diagnostics.writer.flush, 1)
        self.position = Position('SUI', 'short', 'buy', 2, 2, 90, 110, datetime.now(),
            status='open', entry_size_variational=2, entry_size_lighter=2,
            variational_venue='lighter', lighter_venue='arcus')
        self.strategy.positions.append(self.position)
        self.strategy._save()
        self.monitor = Mock(started_at=utc_now())
        self.worker = ExecutionWorker(self.strategy, ['SUI'], self.root/'worker.lock',
                                      self.console.stop_path, live=True)
        self.control = ManualCloseController(self.worker, self.monitor)
        self.worker.manual_close_controller = self.control
        self.snapshot = dict(worker_pid=os.getpid(), started_at=self.monitor.started_at,
                             updated_at=utc_now(), running=True, manual_close_version=1)
        self.monitor_path = self.path.with_suffix('.monitor.json')
        self.write_monitor()
        for name, value in (('status', self.run), ('_worker_alive', True)):
            p = patch.object(self.console, name, return_value=value)
            p.start(); self.addCleanup(p.stop)

    def write_monitor(self):
        self.monitor_path.write_text(json.dumps(self.snapshot))

    def request(self, **changes):
        return self.console.request_manual_close(**dict(dict(position_id=self.position.id,
            symbol='SUI', venues=['lighter','arcus']), **changes))

    def test_request_only_queues_deduplicates_and_preserves_stop_and_state(self):
        self.console.stop_path.touch()
        original = self.path.read_bytes()
        with patch('src.execution.console.subprocess.Popen') as spawn:
            first, second = self.request(), self.request()
        self.assertTrue(first['success'])
        self.assertFalse(first['orders_submitted'])
        self.assertEqual(first['request_id'], second['request_id'])
        self.assertEqual(len(self.a.calls + self.b.calls), 0)
        self.assertEqual(original, self.path.read_bytes())
        self.assertTrue(self.console.stop_path.exists())
        spawn.assert_not_called()

    def test_wrong_group_route_or_symbol_does_not_create_a_request(self):
        for change in ({'position_id':'other'}, {'venues':['arcus','lighter']}, {'symbol':'BTC'},
                       {'venues':['arcus','arcus']}, {'position_id':True}):
            self.assertFalse(self.request(**change)['success'])
        self.assertFalse(self.control.path.exists())

    def test_concurrent_browser_clicks_share_one_queued_command(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.request(), range(2)))
        self.assertTrue(all(item['success'] for item in results))
        self.assertEqual(results[0]['request_id'], results[1]['request_id'])
        self.assertEqual(len(json.loads(self.control.path.read_text())['requests']),1)

    def test_corrupt_queue_cannot_break_history_rendering_or_submit(self):
        write_scope_request(self.control.path,dict(pid=os.getpid(),
            worker_started_at=self.monitor.started_at,requests=[42]))
        self.assertFalse(self.request()['success'])
        row=json.loads(self.path.read_text())['positions'][0]
        annotate_controls(self.console,[row])
        self.assertFalse(row['manual_close_control']['available'])
        self.control.poll()
        self.assertEqual(self.a.calls+self.b.calls,[])

    def test_idle_without_positions_does_not_poll_command_file(self):
        self.strategy.positions.clear()
        with patch.object(self.control,'_load') as load:
            self.assertFalse(self.control.poll())
        load.assert_not_called()

    def test_old_stale_halted_or_dead_worker_cannot_accept(self):
        for changes in ({'manual_close_version':0}, {'stop_reason':'unknown receipt'},
                        {'running':False}, {'updated_at':'2020-01-01T00:00:00Z'}):
            original = dict(self.snapshot)
            self.snapshot.update(changes); self.write_monitor()
            self.assertFalse(self.request()['success'])
            self.snapshot = original
        self.write_monitor()
        with patch.object(self.console, '_worker_alive', return_value=False):
            self.assertFalse(self.request()['success'])
        self.assertFalse(self.control.path.exists())

    def test_manual_loss_exit_sends_both_legs_before_receipts_and_keeps_price_guard(self):
        barrier = threading.Barrier(2)
        self.a.on_close = self.b.on_close = lambda: barrier.wait(timeout=1)
        self.assertTrue(self.request()['success'])
        with patch.object(self.strategy, '_single_exit_estimate', side_effect=AssertionError('profit gate')):
            self.assertTrue(self.control.poll())
        self.assertEqual(self.position.status, 'closed')
        self.assertLess(self.position.realized_pnl_usdc, 0)
        self.assertEqual(self.position.close_reason, REASON)
        self.assertEqual(self.position.manual_close['state'], 'closed')
        for trader in (self.a,self.b):
            self.assertEqual(len(trader.calls), 1)
            self.assertEqual(trader.calls[0]['size'], 2)
            self.assertTrue(trader.calls[0]['reduce_only'])
            self.assertEqual(trader.calls[0]['slippage_bps'], 7)

    def test_same_symbol_peer_group_is_untouched(self):
        peer = copy.deepcopy(self.position); peer.id = 'other-group'
        self.strategy.positions.append(peer)
        self.a.qty = self.b.qty = 4
        self.strategy._save()
        self.request(); self.control.poll()
        self.assertEqual((self.a.qty, self.b.qty), (2,2))
        self.assertEqual(peer.status, 'open')
        self.assertEqual((peer.variational_size, peer.lighter_size), (2,2))

    def test_waits_for_accounts_then_can_close_during_graceful_stop(self):
        self.console.stop_path.touch()
        self.request()
        self.control.poll(account_ready=False)
        self.assertEqual(self.position.manual_close['state'], 'waiting_account')
        self.assertEqual(self.a.calls + self.b.calls, [])
        self.control.poll(account_ready=True)
        self.assertEqual(self.position.status, 'closed')
        self.assertTrue(self.console.stop_path.exists())

    def test_unknown_receipt_collects_peer_and_cannot_be_resent(self):
        self.a.responses = [TimeoutError('unknown')]
        self.request(); self.control.poll()
        self.assertEqual(self.position.status, 'reconcile_required')
        self.assertEqual((self.position.variational_size,self.position.lighter_size), (2,0))
        self.assertEqual(self.position.manual_close['state'], 'reconcile_required')
        self.control.stamp = None; self.control.next_read = 0
        self.control.poll()
        self.assertEqual(len(self.a.calls),1)
        self.assertEqual(len(self.b.calls),1)
        self.assertFalse(self.request()['success'])

    def test_confirmed_zero_fill_retries_only_verified_remainder(self):
        self.b.responses = [dict(confirmed=True,status='canceled',filled_size=0,order_id='zero'),fill(2)]
        self.request(); self.control.poll()
        self.assertEqual(self.position.status,'closed')
        self.assertEqual(len(self.a.calls),1)
        self.assertEqual(len(self.b.calls),2)

    def test_expired_different_run_and_changed_identity_never_send(self):
        self.request()
        original = json.loads(self.control.path.read_text())
        for kind in ('expired', 'run', 'pid', 'identity'):
            payload = copy.deepcopy(original)
            if kind == 'expired':
                payload['requests'][0]['requested_at'] = (datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()
            if kind == 'run': payload['worker_started_at'] = 'old-run'
            if kind == 'pid': payload['pid'] += 1
            if kind == 'identity': payload['requests'][0]['fingerprint'] = 'wrong'
            write_scope_request(self.control.path,payload)
            other = ManualCloseController(self.worker,self.monitor)
            other.poll()
            self.assertEqual(self.a.calls+self.b.calls,[],kind)

    def test_pending_and_foreign_groups_are_annotated_without_trades(self):
        row = json.loads(self.path.read_text())['positions'][0]
        foreign = dict(row,id='foreign')
        annotate_controls(self.console,[row,foreign])
        self.assertTrue(row['manual_close_control']['available'])
        self.assertFalse(foreign['manual_close_control']['available'])
        self.request()
        annotate_controls(self.console,[row])
        self.assertTrue(row['manual_close_control']['pending'])
        self.assertFalse(row['manual_close_control']['available'])
        self.assertEqual(self.a.calls+self.b.calls,[])

    def test_worker_handles_manual_close_before_scanning_and_does_not_reopen_same_cycle(self):
        self.request()
        with patch.object(self.strategy,'live_blockers',return_value=[]), patch.object(self.strategy,'reconcile_open_positions',return_value='ok'), patch.object(self.worker,'_attempt_candidates') as scan:
            self.worker.run(max_cycles=1)
        self.assertEqual(self.position.status,'closed')
        scan.assert_not_called()

    def test_single_cycle_monitor_handles_manual_close(self):
        self.request()
        with patch.object(self.strategy,'reconcile_open_positions',return_value='ok'):
            self.assertTrue(self.worker._finish_single_cycle())
        self.assertEqual(self.position.status,'closed')

    def test_api_requires_local_token_and_only_queues_exact_group_payload(self):
        from src.web import app as web
        client = web.app.test_client()
        payload = dict(position_id=self.position.id,symbol='SUI',venues=['lighter','arcus'])
        with patch.object(web,'execution_console',self.console):
            self.assertEqual(client.post('/api/execution/manual-close',json=payload).status_code,403)
            headers = {'X-Local-Token':web.credentials_token}
            self.assertEqual(client.post('/api/execution/manual-close',json=payload,headers=headers,base_url='http://evil.example').status_code,403)
            self.assertEqual(client.post('/api/execution/manual-close',json=dict(payload,force=True),headers=headers).status_code,400)
            response = client.post('/api/execution/manual-close',json=payload,headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.get_json()['orders_submitted'])
        self.assertEqual(self.a.calls+self.b.calls,[])
