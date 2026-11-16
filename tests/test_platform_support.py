"""Offline portability regressions; also executed by the Windows package job."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.common.platform_support import execution_python, process_alive, open_private_read
from src.execution.journal import execution_lease


class PlatformSupportTests(unittest.TestCase):
    def test_worker_liveness_query_never_terminates_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        try:
            self.assertTrue(process_alive(child.pid))
            self.assertIsNone(child.poll())
        finally:
            child.communicate(timeout=5)
        self.assertFalse(process_alive(child.pid))

    def test_lease_excludes_another_process_then_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '锁.lock'
            program = ('import sys; from src.execution.journal import execution_lease; '
                       '\ntry:\n with execution_lease(sys.argv[1]): pass'
                       '\nexcept RuntimeError: sys.exit(7)')
            command = [sys.executable, '-c', program, str(path)]
            with execution_lease(path):
                self.assertEqual(subprocess.run(command, cwd=ROOT, timeout=5).returncode, 7)
            self.assertEqual(subprocess.run(command, cwd=ROOT, timeout=5).returncode, 0)

    def test_execution_python_keeps_posix_path_and_supports_portable_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('src.common.platform_support.sys.platform', 'win32'):
                self.assertEqual(execution_python(root), root / '.venv-execution/Scripts/python.exe')
                (root / 'python').mkdir()
                (root / 'python/python.exe').touch()
                self.assertEqual(execution_python(root), root / 'python/python.exe')
            with patch('src.common.platform_support.sys.platform', 'darwin'):
                self.assertEqual(execution_python(root), root / '.venv-execution/bin/python')

    def test_private_read_preserves_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_text('private', encoding='utf-8')
            with os.fdopen(open_private_read(path)) as stream:
                self.assertEqual(stream.read(), 'private')

    def test_source_package_never_copies_local_data(self):
        from scripts.build_windows import stage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('src', 'scripts', 'browser_extension', 'config', 'data', 'logs'):
                (root / name).mkdir()
            for name in ('main.py', 'start-windows.cmd', 'stop-windows.cmd', 'README.md',
                         'requirements-windows.txt', 'requirements-runtime.txt', 'requirements-execution.txt'):
                (root / name).write_text('example', encoding='utf-8')
            for name in ('config.example.yaml', 'variational-execution.example.json'):
                (root / 'config' / name).write_text('example', encoding='utf-8')
            (root / 'docs').mkdir()
            (root / 'docs/windows-package.md').write_text('example', encoding='utf-8')
            for name in ('.env', 'config/config.yaml', 'config/variational-execution.local.json',
                         'data/positions.sqlite', 'logs/secret.log'):
                (root / name).write_text('DO_NOT_SHIP', encoding='utf-8')
            package = root / 'output'
            stage(root, package)
            self.assertFalse((package / '.env').exists())
            self.assertFalse((package / 'data').exists())
            self.assertFalse((package / 'config/config.yaml').exists())
            self.assertTrue((package / 'config/config.example.yaml').is_file())

    @unittest.skipUnless(importlib.util.find_spec('psutil'), 'psutil installed by Windows CI')
    def test_stop_rejects_reused_pid_and_terminates_verified_service_tree(self):
        import psutil
        from scripts.windows_launcher import owned_process, terminate_services
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = root / 'src/web/app.py'
            script.parent.mkdir(parents=True)
            (root / 'data').mkdir()
            script.write_text('import sys; sys.stdin.read()', encoding='utf-8')
            child = subprocess.Popen([sys.executable, str(script)], cwd=root, stdin=subprocess.PIPE)
            try:
                record = {'role': 'web', 'pid': child.pid, 'created_at': psutil.Process(child.pid).create_time()}
                self.assertIsNone(owned_process({**record, 'created_at': record['created_at'] - 100}, root))
                self.assertIsNotNone(owned_process(record, root))
                self.assertTrue(terminate_services(root, {'services': [record]}))
                child.wait(timeout=5)
                self.assertTrue((root / 'data/STOP_EXECUTION').exists())
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
