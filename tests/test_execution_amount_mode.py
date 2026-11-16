"""Explicit follow mode does not silently remove legacy launch ceilings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.common.config_loader import ConfigLoader
from src.execution.console import ExecutionConsole
from src.execution.runtime import run_runtime


class AmountModeTests(unittest.TestCase):
    def test_console_command_binds_mode_and_rejects_non_boolean(self):
        fixed = ExecutionConsole._policy(12)
        follow = ExecutionConsole._policy(12, follow_saved_cap=True)
        self.assertNotIn('follow_saved_cap', fixed)
        self.assertIn('--follow-saved-cap', ExecutionConsole._policy_command([], follow))
        self.assertNotIn('--follow-saved-cap', ExecutionConsole._policy_command([], fixed))
        for value in (1, 'true', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '跟随设置'):
                ExecutionConsole._policy(12, follow_saved_cap=value)

    def test_runtime_only_explicit_follow_mode_removes_launch_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config').mkdir()
            path, profile = root/'config/config.yaml', root/'config/profile.json'
            path.write_text('strategy:\n  trading:\n    position_size: 12\n')
            profile.write_text(json.dumps(dict(symbol='BTC', venues=['lighter', 'arcus'])))
            for follow in (False, True):
                config = ConfigLoader(path)
                with self.subTest(follow=follow), \
                        patch('src.execution.runtime.ConfigLoader', return_value=config), \
                        patch('src.execution.runtime.runtime_blockers', return_value=[]), \
                        patch('src.execution.runtime._run_pair_verified', return_value={}) as run:
                    run_runtime(path, profile, max_notional=8, follow_saved_cap=follow)
                    strategy = run.call_args.args[0].config['strategy']
                    self.assertEqual(strategy.get('execution_notional_cap'), None if follow else 8)
                    self.assertEqual(strategy['trading']['position_size'], 12)
            with patch('src.execution.runtime.runtime_blockers', return_value=[]), \
                    patch('src.execution.runtime._run_pair_verified') as run:
                with self.assertRaisesRegex(ValueError, '不能提高'):
                    run_runtime(path, profile, max_notional=13, follow_saved_cap=True)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
