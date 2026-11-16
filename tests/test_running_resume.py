import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.common.market_data import utc_now
from src.execution.console import ExecutionConsole
from src.execution.running_resume import resume_control, stop_generation


class RunningResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.console = ExecutionConsole(Path(self.temp.name), Mock())
        self.execution = dict(state='running', running=True, pid=123,
                              started_at=utc_now(), mode='continuous_monitor',
                              close_only=False, venues=['lighter', 'lighter_rh', 'arcus'])
        self.console._save(self.execution)
        self.console.request_stop()
        self.path = self.console._pair_state_path(self.execution['venues'])
        self.state = dict(positions=[dict(id='hedge1', status='open')], halted_reason=None)
        self.monitor = dict(worker_pid=123, started_at=utc_now(), updated_at=utc_now(),
                            running=True, entry_paused=False, stop_reason=None,
                            entry_limits=dict(version=1, max_positions=1),
                            positions=[dict(id='hedge1', position_status='open',
                                            account_status='ready', last_success_at=utc_now(),
                                            status='monitoring', error=None)])
        self.write_evidence()
        for method in ('_worker_alive', '_maybe_start_auto_reconcile'):
            p = patch.object(self.console, method, return_value=True)
            p.start()
            self.addCleanup(p.stop)

    def write_evidence(self):
        self.path.write_text(json.dumps(self.state))
        self.path.with_suffix('.monitor.json').write_text(json.dumps(self.monitor))

    def control(self):
        return resume_control(self.console, self.console.status())

    def resume(self, control):
        return self.console.request_resume(**{k: control[k] for k in ('pid', 'started_at', 'stop_generation')})

    def test_resumes_same_worker_without_touching_holdings_or_startup_policy(self):
        before = [p.read_bytes() for p in (self.path, self.console.state_path)]
        with patch('src.execution.console.subprocess.Popen') as spawn:
            control = self.control()
            self.assertTrue(control['available'])
            self.assertTrue(self.console.stop_path.exists(), 'a status read cannot resume')
            result = self.resume(control)
        self.assertTrue(result['success'])
        self.assertEqual(result['pid'], 123)
        self.assertEqual(result['venues'], self.execution['venues'])
        self.assertFalse(self.console.stop_path.exists())
        self.assertEqual(before, [p.read_bytes() for p in (self.path, self.console.state_path)])
        spawn.assert_not_called()

    def test_live_scope_queues_bound_request_without_clearing_stop_or_changing_positions(self):
        self.monitor['runtime_scope'] = dict(version=1)
        self.write_evidence()
        before = [p.read_bytes() for p in (self.path,self.console.state_path,self.console.stop_path)]
        with patch('src.execution.console.subprocess.Popen') as spawn:
            result = self.console.request_scope(123,self.execution['started_at'],['lighter','arcus'],['BTC','ETH'])
        self.assertTrue(result['success'])
        request = json.loads(self.path.with_suffix('.scope-request.json').read_text())
        self.assertEqual(request['pid'],123)
        self.assertEqual(request['worker_started_at'],self.monitor['started_at'])
        self.assertEqual(request['symbols'],['BTC','ETH'])
        self.assertEqual(before,[p.read_bytes() for p in (self.path,self.console.state_path,self.console.stop_path)])
        spawn.assert_not_called()

    def test_live_scope_refuses_old_or_different_worker(self):
        for pid,started in ((999,self.execution['started_at']),(123,'stale'),(True,self.execution['started_at'])):
            self.assertFalse(self.console.request_scope(pid,started,['lighter','arcus'],['BTC'])['success'])
        result = self.console.request_scope(123,self.execution['started_at'],['lighter','arcus'],['BTC'])
        self.assertFalse(result['success'])
        self.assertIn('旧进程',result['message'])
        self.assertFalse(self.path.with_suffix('.scope-request.json').exists())

    def test_new_stop_wins_over_stale_browser_click(self):
        control = self.control()
        self.console.request_stop()
        stamp = self.console.stop_path.stat()
        os.utime(self.console.stop_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
        generation = stop_generation(self.console.stop_path)
        self.assertFalse(self.resume(control)['success'])
        self.assertEqual(stop_generation(self.console.stop_path), generation)

    def test_different_run_and_boolean_pid_cannot_resume(self):
        control = self.control()
        for field, value in (('pid', 999), ('pid', True), ('started_at', 'another-run')):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.resume({**control, field:value})['success'])
                self.assertTrue(self.console.stop_path.exists())

    def test_stop_changed_during_final_process_check_is_preserved(self):
        control = self.control()
        def process_check(_):
            stamp = self.console.stop_path.stat()
            os.utime(self.console.stop_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
            return True
        with patch.object(self.console, 'status', return_value={**self.execution, 'stop_requested':True}), \
                patch.object(self.console, '_worker_alive', side_effect=process_check):
            self.assertFalse(self.resume(control)['success'])
        self.assertTrue(self.console.stop_path.exists())

    def test_unhealthy_or_incomplete_monitor_never_clears_stop(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        cases = [dict(worker_pid=999), dict(updated_at=old), dict(running=False),
                 dict(entry_paused=True), dict(stop_reason='halted'), dict(entry_limits={}),
                 dict(positions=[])]
        row = self.monitor['positions'][0]
        for delta in (dict(position_status='pending'), dict(account_status='unknown'),
                      dict(status='closing'), dict(error='unavailable'),
                      dict(last_success_at=old), dict(id='another-hedge')):
            cases.append(dict(positions=[{**row, **delta}]))
        baseline = dict(self.monitor)
        for delta in cases:
            with self.subTest(delta=delta):
                self.monitor = {**baseline, **delta}
                self.write_evidence()
                self.assertFalse(self.control()['available'])
                self.assertTrue(self.console.stop_path.exists())

    def test_pending_halted_and_flat_local_state_cannot_resume(self):
        baseline = dict(self.state)
        for delta in (dict(halted_reason='uncertain send'), dict(positions=[]),
                      dict(positions=[dict(id='hedge1', status='pending')]),
                      dict(positions=[dict(id='hedge1', status='closed')])):
            with self.subTest(delta=delta):
                self.state = {**baseline, **delta}
                self.write_evidence()
                self.assertFalse(self.control()['available'])

    def test_close_only_single_run_or_exited_worker_cannot_resume(self):
        control = self.control()
        for delta in (dict(close_only=True), dict(mode='wait_once'), dict(running=False)):
            with self.subTest(delta=delta):
                with patch.object(self.console, 'status', return_value={**self.execution, 'stop_requested':True, **delta}):
                    self.assertFalse(self.resume(control)['success'])
        with patch.object(self.console, 'status', return_value={**self.execution, 'stop_requested':True}), \
                patch.object(self.console, '_worker_alive', return_value=False):
            self.assertFalse(self.resume(control)['success'])
        self.assertTrue(self.console.stop_path.exists())


if __name__ == '__main__':
    unittest.main()
