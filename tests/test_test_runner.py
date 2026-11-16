"""The selector must neither omit new tests from full nor run live diagnostics."""
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('regression_runner', ROOT / 'scripts/run_tests.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class TestRunnerTests(unittest.TestCase):
    def test_full_automatically_includes_all_backend_and_frontend_files(self):
        expected = {p for p in (ROOT / 'tests').glob('test_*') if p.suffix in ('.py', '.js')}
        self.assertEqual(set(runner.select_tests('full')), expected)

    def test_every_scope_has_existing_tests_and_quick_is_smaller(self):
        for name in runner.SCOPES:
            self.assertTrue(runner.select_tests(name), name)
        self.assertLess(len(runner.select_tests('quick')), len(runner.select_tests('full')) / 5)
        self.assertTrue(all(p.suffix == '.js' for p in runner.select_tests('frontend')))

    def test_custom_selection_deduplicates_without_loading_test_modules(self):
        with patch.object(runner.unittest.TestLoader, 'loadTestsFromName') as load:
            selected = runner.select_tests('custom', ['tests/test_sizing.py'] * 2)
        self.assertEqual(selected, [ROOT / 'tests/test_sizing.py'])
        load.assert_not_called()

    def test_rejects_root_diagnostics_missing_files_and_escape_paths(self):
        for name in ('test_place_order.py', 'scripts/arcus_round_trip_test.py',
                     'tests/test_missing.py', 'tests/../test_lighter.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runner.select_tests('custom', [name])

    def test_unknown_group_pattern_cannot_silently_select_nothing(self):
        with patch.dict(runner.SCOPES, broken=('test_does_not_exist.py',)):
            with self.assertRaises(ValueError):
                runner.select_tests('broken')

    def test_frontend_failures_and_timeouts_are_not_success(self):
        files = [ROOT / 'tests/test_capture.js']
        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO), patch.object(runner.subprocess, 'run', return_value=Mock(
                returncode=1, stdout='', stderr='assertion failed')):
            self.assertFalse(runner.run_javascript(files, 'node')[0]['passed'])
        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO), patch.object(runner.subprocess, 'run', side_effect=runner.subprocess.TimeoutExpired('node', 60)):
            self.assertFalse(runner.run_javascript(files, 'node')[0]['passed'])

    def test_empty_backend_suite_is_not_success(self):
        with self.assertRaises(ValueError):
            runner.run_python([])

    def test_list_never_imports_or_executes_tests_or_writes_reports(self):
        with patch.object(runner, 'run_python') as backend, patch.object(runner, 'run_javascript') as frontend, patch.object(runner, 'write_report') as report, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(runner.main(['--scope', 'full', '--list']), 0)
        backend.assert_not_called()
        frontend.assert_not_called()
        report.assert_not_called()

    def test_frontend_failure_propagates_to_command_exit_and_report(self):
        with patch.object(runner.shutil, 'which', return_value='node'), patch.object(runner, 'run_javascript', return_value=[{'passed': False}]), patch.object(runner, 'write_report') as report, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(runner.main(['--scope', 'frontend']), 1)
        self.assertFalse(report.call_args[0][0]['passed'])

    def test_missing_node_cannot_silently_skip_frontend_tests(self):
        with patch.object(runner.shutil, 'which', return_value=None), patch.object(runner, 'run_python') as backend, patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as stopped:
                runner.main(['--scope', 'full'])
        self.assertEqual(stopped.exception.code, 2)
        backend.assert_not_called()

    def test_report_write_isolated_from_application_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runner, 'ROOT', Path(folder)):
            destination = runner.write_report({'passed': False, 'scope': 'custom'})
            self.assertEqual(destination, Path(folder) / '.run/test-results/last.json')
            self.assertIn('false', destination.read_text())
            self.assertEqual(list(destination.parent.iterdir()), [destination])
