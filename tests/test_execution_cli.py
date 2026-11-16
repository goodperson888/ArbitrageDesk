"""Exercise CLI startup without opening accounts or submitting orders."""
import contextlib
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class ExecutionCliRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name) / 'profile.json'
        self.profile.write_text(json.dumps({'live_enabled': True, 'symbols': ['CASHCAT']}))

    def invoke(self, *arguments):
        argv = ['execution_cli.py', '--profile', str(self.profile), *arguments]
        with patch.object(sys, 'argv', argv), \
                patch('src.common.config_loader.ConfigLoader'), \
                contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'scripts/execution_cli.py'), run_name='__main__')

    def test_live_start_reaches_locked_recovery_for_existing_hedge_in_both_modes(self):
        for venues in ('lighter,arcus', 'lighter,lighter_rh,arcus'):
            for mode in ([], ['--close-only']):
                with self.subTest(venues=venues, mode=mode):
                    def blockers(profile, **options):
                        return [] if options.get('recover_at_start') else ['存在未完成持仓']

                    with patch('src.execution.runtime.runtime_blockers', side_effect=blockers) as gate, \
                            patch('src.execution.runtime.run_runtime', return_value={'stopped': True}) as runtime:
                        self.invoke('--run', '--venues', venues, *mode)
                    self.assertTrue(gate.call_args.kwargs['recover_at_start'])
                    self.assertTrue(gate.call_args.kwargs['require_enabled'])
                    runtime.assert_called_once()
                    self.assertEqual(runtime.call_args.kwargs['venues'], venues.split(','))
                    self.assertEqual(runtime.call_args.kwargs['close_only'], bool(mode))

    def test_read_only_status_keeps_pending_position_gate_and_never_starts_runtime(self):
        with patch('src.execution.runtime.runtime_blockers', return_value=['存在未完成持仓']) as gate, \
                patch('src.execution.runtime.run_runtime') as runtime:
            self.invoke('--venues', 'lighter,arcus')
        self.assertNotIn('recover_at_start', gate.call_args.kwargs)
        runtime.assert_not_called()

    def test_follow_saved_flag_is_explicitly_forwarded_to_runtime(self):
        with patch('src.execution.runtime.runtime_blockers', return_value=[]), \
                patch('src.execution.runtime.run_runtime', return_value={}) as runtime:
            self.invoke('--run', '--venues', 'lighter,arcus', '--follow-saved-cap')
            self.assertIs(runtime.call_args.kwargs['follow_saved_cap'], True)

    def test_unrelated_startup_blocker_still_prevents_runtime(self):
        with patch('src.execution.runtime.runtime_blockers', return_value=['账户鉴权未通过']), \
                patch('src.execution.runtime.run_runtime') as runtime:
            with self.assertRaisesRegex(SystemExit, '账户鉴权未通过'):
                self.invoke('--run', '--venues', 'lighter,arcus')
        runtime.assert_not_called()


if __name__ == '__main__':
    unittest.main()
