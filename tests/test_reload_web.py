"""Exercise the dashboard-only command without running or stopping services."""
import subprocess
import tempfile
import unittest
from pathlib import Path


class ReloadWebTests(unittest.TestCase):
    def test_reload_only_stops_and_starts_dashboard(self):
        source = (Path(__file__).resolve().parents[1]/'start.sh').read_text()
        definitions, separator, dispatch = source.partition('case "${1:-start}" in')
        self.assertTrue(separator)
        stubs = '''
ensure_runtime() { :; }
stop_execution() { echo 'ERROR execution stopped'; exit 99; }
stop_all() { echo 'ERROR project stopped'; exit 99; }
start_all() { echo 'ERROR project started'; exit 99; }
stop_one() { printf 'stop:%s:%s\\n' "$1" "$3"; }
start_web() { echo 'start:web'; }
'''
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder)/'start.sh'
            marker = Path(folder)/'data/STOP_EXECUTION'
            marker.parent.mkdir()
            marker.write_text('existing user stop')
            script.write_text(definitions+stubs+separator+dispatch)
            result = subprocess.run(['bash', str(script), 'reload-web'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['stop:行情面板:5001', 'start:web'])
            self.assertEqual(marker.read_text(), 'existing user stop')
