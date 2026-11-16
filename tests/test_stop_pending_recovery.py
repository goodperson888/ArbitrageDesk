import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.execution.console import ExecutionConsole
from src.execution.worker import ExecutionWorker
from src.web.diagnostics import Diagnostics


class PendingStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.position = SimpleNamespace(id='original-position', status='reconcile_required')
        self.strategy = SimpleNamespace(
            positions=[self.position], halted_reason=None, live_blockers=lambda: [],
            history=Mock(), reconcile_open_positions=Mock(return_value='ok'),
            recover_single_leg_positions=Mock(return_value='waiting'),
            close_reason=Mock(return_value=None), close_position=Mock(),
            execute_arbitrage=Mock(), get_strategy_status=lambda: {'positions': 1})
        self.worker = ExecutionWorker(self.strategy, ['PUMP'], self.root/'worker.lock',
                                      self.root/'STOP', live=True)
        self.worker.kill_path.touch()
        self.worker.stop_event = Mock()
        self.worker.stop_event.is_set.return_value = False

    def test_pending_position_exits_without_polling_forever_or_claiming_flat(self):
        result = self.worker.run(max_cycles=3)
        self.assertTrue(result['stopped'])
        self.assertIn('仍需核对', result['note'])
        self.assertNotIn('已完成平仓', result['note'])
        self.strategy.history.append.assert_called_once()
        self.assertEqual(self.strategy.history.append.call_args.args[0],
                         'execution_stop_requires_reconcile')
        self.strategy.close_position.assert_not_called()
        self.strategy.execute_arbitrage.assert_not_called()
        self.strategy.reconcile_open_positions.assert_not_called()
        self.worker.stop_event.wait.assert_not_called()
        self.assertEqual(self.position.status, 'reconcile_required')
        self.assertTrue(self.worker.kill_path.exists())

    def test_halted_stop_exits_before_attempting_repair(self):
        self.position.status = 'open'
        self.strategy.halted_reason = '订单回执待核对'
        self.worker.run(max_cycles=3)
        self.strategy.recover_single_leg_positions.assert_not_called()
        self.strategy.close_position.assert_not_called()
        self.worker.stop_event.wait.assert_not_called()

    def test_healthy_open_position_still_waits_for_normal_close(self):
        self.position.status = 'open'
        result = self.worker.run(max_cycles=2)
        self.assertEqual(self.strategy.close_reason.call_count, 2)
        self.worker.stop_event.wait.assert_called_once()
        self.strategy.history.append.assert_not_called()
        self.assertNotIn('已完成平仓', result['note'])

    def test_stop_during_close_waits_until_call_returns_then_preserves_pending(self):
        self.worker.kill_path.unlink()
        self.position.status = 'open'
        self.strategy.close_reason.return_value = '价差收敛'
        sequence = []

        def close(position, **kwargs):
            sequence.append('close-start')
            self.worker.kill_path.touch()
            position.status = 'reconcile_required'
            sequence.append('close-finished')

        self.strategy.close_position.side_effect = close
        self.strategy.history.append.side_effect = lambda *a, **k: sequence.append(a[0])
        self.worker.run(max_cycles=3)
        self.assertEqual(sequence, ['close-start', 'close-finished',
                                    'execution_stop_requires_reconcile'])
        self.strategy.close_position.assert_called_once()
        self.worker.stop_event.wait.assert_not_called()

    def test_gone_worker_with_stop_unlocks_readonly_controls_without_flat_claim(self):
        console = ExecutionConsole(self.root, Diagnostics(self.root/'diagnostics.sqlite'))
        console._save({'state': 'running', 'pid': 123,
                       'started_at': '2026-11-03T00:00:00+00:00',
                       'venues': ['lighter', 'arcus']})
        console.stop_path.touch()
        with patch.object(console, '_worker_alive', return_value=True), \
                patch.object(console, '_maybe_start_auto_reconcile'):
            self.assertTrue(console.status()['running'])
        with patch.object(console, '_worker_alive', return_value=False), \
                patch.object(console, '_maybe_start_auto_reconcile'):
            result = console.status()
        self.assertFalse(result['running'])
        self.assertEqual(result['state'], 'finished')
        self.assertTrue(result['stop_requires_reconcile'])
        self.assertFalse(result['stop_completed'])
        self.assertTrue(result['stop_requested'])
        self.assertIn('仍需核对', result['note'])
        self.assertTrue(json.loads(console.state_path.read_text())['stop_requires_reconcile'])
        console.stop_path.unlink()
        with patch.object(console, '_maybe_start_auto_reconcile'):
            result = console.status()
        self.assertFalse(result['stop_requested'])
        self.assertIn('需手动开启实盘', result['note'])
        self.assertNotIn('停止保护已保留', result['note'])

    def test_legacy_web_stale_failure_is_recovered_after_stop(self):
        console = ExecutionConsole(self.root, Diagnostics(self.root/'diagnostics.sqlite'))
        console._save({'state': 'failed', 'pid': 123,
                       'started_at': '2026-11-03T00:00:00+00:00',
                       'venues': ['lighter', 'arcus']})
        console.stop_path.touch()
        with patch.object(console, '_worker_alive', return_value=False), \
                patch.object(console, '_maybe_start_auto_reconcile'):
            result = console.status()
        self.assertFalse(result['running'])
        self.assertEqual(result['state'], 'finished')
        self.assertTrue(result['stop_requires_reconcile'])
        self.assertFalse(result['stop_completed'])

    def test_completed_stop_no_longer_claims_marker_is_retained_after_reset(self):
        console = ExecutionConsole(self.root, Mock())
        console._save({'state':'finished', 'running':False, 'stop_completed':True,
                       'note':'停止标记保留，重新启动前需核对账户并解除停止'})
        with patch.object(console, '_maybe_start_auto_reconcile'):
            result = console.status()
        self.assertFalse(result['stop_requested'])
        self.assertIn('停止标记已解除', result['note'])
        self.assertNotIn('停止标记保留', result['note'])
