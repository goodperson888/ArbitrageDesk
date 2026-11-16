import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.common.market_data import utc_now
from src.web import app as web


class PositionMonitorWebTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'data').mkdir()
        self.console_path = self.root / 'data/console_execution.json'
        self.console_path.write_text(json.dumps({'pid': 123, 'venues': ['lighter', 'arcus']}))
        self.state_path = self.root / 'data/hedge_state_lighter_arcus.json'
        self.state_path.write_text(json.dumps({'positions': [
            {'status': 'open'}, {'status': 'reconcile_required'}, {'status': 'closed'}]}))
        self.monitor_path = self.state_path.with_suffix('.monitor.json')
        self.monitor_path.write_text(json.dumps({'worker_pid': 123, 'running': True,
            'updated_at': utc_now(), 'positions': [{'symbol': 'SUI'}]}))
        for name, value in {'ROOT': self.root,
                            'execution_console': SimpleNamespace(state_path=self.console_path)}.items():
            patcher = patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = web.app.test_client()

    def test_status_reads_only_local_files_and_matches_worker_pid(self):
        with patch.object(web, 'ensure_initialized', side_effect=AssertionError('exchange initialization')):
            response = self.client.get('/api/position-monitor')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['position_count'], 2)
        self.assertFalse(data['monitor']['heartbeat_stale'])
        self.console_path.write_text(json.dumps({'pid': 456, 'venues': ['lighter', 'arcus']}))
        self.assertIsNone(self.client.get('/api/position-monitor').get_json()['monitor'])

    def test_three_platform_stopped_worker_exposes_reason_and_position_count(self):
        self.console_path.write_text(json.dumps(dict(pid=123,venues=['lighter','lighter_rh','arcus'],
            latest_event=dict(detail=dict(reason='账户持仓与程序记录不一致')))))
        path=self.root/'data/hedge_state_lighter_lighter_rh_arcus.json'
        path.write_text(json.dumps({'positions':[{'status':'reconcile_required'}]}))
        path.with_suffix('.monitor.json').write_text(json.dumps(dict(worker_pid=123,running=False,
            updated_at=utc_now(),positions=[dict(symbol='BMNR',position_status='reconcile_required')])) )
        data=self.client.get('/api/position-monitor').get_json()
        self.assertEqual(data['position_count'],1)
        self.assertEqual(data['monitor']['heartbeat_status'],'stopped')
        self.assertIn('账户持仓与程序记录不一致',data['monitor']['stop_reason'])

    def test_stream_emits_framed_json_refreshes_and_closes(self):
        with patch.object(web.time, 'sleep') as sleep:
            response = self.client.get('/api/position-monitor/stream', buffered=False)
            try:
                frames = iter(response.response)
                frame = next(frames).decode()
                self.assertTrue(frame.endswith('\n\n'))
                self.assertEqual(json.loads(frame.removeprefix('data: '))['position_count'], 2)
                self.state_path.write_text(json.dumps({'positions': []}))
                updated = json.loads(next(frames).decode().removeprefix('data: '))
                self.assertEqual(updated['position_count'], 0)
                sleep.assert_called_once_with(1)
                self.assertEqual(response.mimetype, 'text/event-stream')
                self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
            finally:
                response.close()

    def test_stream_rejects_remote_access_and_handles_bad_state(self):
        self.assertEqual(self.client.get('/api/position-monitor/stream',
                         base_url='http://evil.example').status_code, 403)
        self.console_path.write_text('{')
        self.assertEqual(self.client.get('/api/position-monitor').get_json()['error'], '监控状态暂不可读')
