"""Exercise shutdown with fake PIDs and read-only identity checks on owned fixtures."""
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ServiceShutdownTests(unittest.TestCase):
    def test_real_unicode_processes_are_identified_under_c_locale(self):
        root=Path(__file__).resolve().parents[1]
        prefix=(root/'start.sh').read_text().split('\ncase "${1:-start}"')[0]
        shells=['/bin/bash']
        if Path('/opt/homebrew/bin/bash').is_file():
            shells.append('/opt/homebrew/bin/bash')
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp).resolve()/'可验证'
            (directory/'src/web').mkdir(parents=True)
            (directory/'scripts').mkdir()
            sleeper='import time\ntime.sleep(60)\n'
            for file in ('src/web/app.py','scripts/execution_cli.py','uvicorn.py'):
                (directory/file).write_text(sleeper)
            commands=[
                [sys.executable,'-B',str(directory/'src/web/app.py')],
                [sys.executable,'-m','uvicorn','src.api_v2.app:app','--port','5002'],
                [sys.executable,'-B',str(directory/'scripts/execution_cli.py')],
                [sys.executable,'-B','scripts/execution_cli.py'],
            ]
            children=[]
            try:
                for command in commands:
                    children.append(subprocess.Popen(command,cwd=directory,
                                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
                web,api,absolute_worker,relative_worker=[child.pid for child in children]
                source=re.sub(r'^ROOT_DIR=.*$', 'ROOT_DIR='+shlex.quote(str(directory)),
                              prefix,count=1,flags=re.M)
                script=directory/'check.sh'
                script.write_text(source+f'''
[ "$(process_cwd {api})" = "$ROOT_DIR" ]
case "$(process_command {web})" in *"$ROOT_DIR/src/web/app.py"*) ;; *) exit 9 ;; esac
matches_service "行情面板" {web}
matches_service "统一只读 API" {api}
[[ $'\\n'"$(service_pids "行情面板")"$'\\n' == *$'\\n'{web}$'\\n'* ]]
[[ $'\\n'"$(service_pids "统一只读 API")"$'\\n' == *$'\\n'{api}$'\\n'* ]]
workers="$(execution_pids)"
[[ $'\\n'"$workers"$'\\n' == *$'\\n'{absolute_worker}$'\\n'* ]]
[[ $'\\n'"$workers"$'\\n' == *$'\\n'{relative_worker}$'\\n'* ]]
[ "$LC_ALL" = C ]
printf 'unicode-identity-ok\\n'
''')
                for shell in shells:
                    with self.subTest(shell=shell):
                        result=subprocess.run([shell,str(script)],env=dict(os.environ,LC_ALL='C'),
                                              capture_output=True,text=True,timeout=15)
                        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                        self.assertIn('unicode-identity-ok',result.stdout)
            finally:
                for child in children:
                    child.terminate()
                for child in children:
                    child.wait(timeout=5)

    def test_legacy_launchers_forward_stop_without_starting_services(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('start_web.sh','start_api_v2.sh','start_all.sh','启动行情面板.command'):
            with self.subTest(launcher=name), tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp)
                launcher=directory/name
                launcher.write_text((root/name).read_text())
                unified=directory/'start.sh'
                unified.write_text('#!/bin/bash\nprintf "unified:%s\\n" "$*"\n')
                unified.chmod(0o755)
                # First execution of a newly created script on macOS can
                # exceed 3s; this checks delegation, not startup latency.
                result=subprocess.run(['/bin/bash',str(launcher),'stop'],
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.strip(),'unified:stop')

    def run_shell(self, body):
        source=(Path(__file__).resolve().parents[1]/'start.sh').read_text()
        source=source.split('\ncase "${1:-start}"')[0]
        with tempfile.TemporaryDirectory() as tmp:
            source=re.sub(r'^ROOT_DIR=.*$', 'ROOT_DIR='+shlex.quote(tmp), source, count=1, flags=re.M)
            script=Path(tmp)/'shutdown.sh'
            script.write_text(source+'\n'+body)
            return subprocess.run(['/bin/bash',str(script)],capture_output=True,text=True,timeout=3)

    def test_failure_of_web_cleanup_does_not_skip_api_cleanup(self):
        result=self.run_shell('''
stop_execution() { return 0; }
stop_one() { printf '%s\\n' "$1"; [ "$1" != "行情面板" ]; }
set +e
stop_all
exit "$?"
''')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertIn('行情面板',result.stdout)
        self.assertIn('统一只读 API',result.stdout)

    def test_unresolved_execution_prevents_full_shutdown(self):
        result=self.run_shell('''
stop_execution() { return 1; }
stop_one() { printf 'should-not-run'; }
set +e
stop_all
exit "$?"
''')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertNotIn('should-not-run',result.stdout)

    def test_verified_readonly_service_is_killed_after_grace_timeout(self):
        result=self.run_shell('''
alive=1
read_pid() { printf 777; }
pid_alive() { [ "$alive" -eq 1 ]; }
matches_service() { return 0; }
kill() { printf 'fake-signal:%s\\n' "$*"; if [ "$1" = '-KILL' ]; then alive=0; fi; }
sleep() { :; }
stop_service_pid "统一只读 API" 777
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('fake-signal:-KILL 777',result.stdout)
        self.assertIn('已停止',result.stdout)

    def test_system_lsof_is_available_without_sbin_on_path(self):
        if not Path('/usr/sbin/lsof').is_file():
            self.skipTest('macOS system lsof check')
        result=self.run_shell('''
PATH=/usr/bin:/bin
resolved="$(lsof_path)"
[ -x "$resolved" ]
[ "$(process_cwd "$$")" = "$(pwd -P)" ]
printf 'system-lsof-ok\\n'
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('system-lsof-ok',result.stdout)

    def test_missing_pid_and_unknown_listener_are_not_reported_as_stopped(self):
        result=self.run_shell('''
read_pid() { :; }
listener_pid() { :; }
service_pids() { :; }
port_open() { return 0; }
set +e
stop_one "行情面板" "$WEB_PID_FILE" 5001
exit "$?"
''')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertIn('仍在监听',result.stdout)

    def test_all_verified_service_processes_are_stopped_without_pid_file(self):
        result=self.run_shell('''
read_pid() { :; }
listener_pid() { :; }
dead=' '
pid_alive() {
    [ -n "$1" ] || return 1
    case "$dead" in *" $1 "*) return 1 ;; esac
    return 0
}
service_pids() {
    for item in 777 778; do
        if pid_alive "$item"; then printf '%s\\n' "$item"; fi
    done
}
matches_service() { return 0; }
kill() { printf 'fake-signal:%s\\n' "$*"; dead="$dead$1 "; }
port_open() { pid_alive 777 || pid_alive 778; }
sleep() { :; }
stop_one "行情面板" "$WEB_PID_FILE" 5001
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('fake-signal:777',result.stdout)
        self.assertIn('fake-signal:778',result.stdout)

    def test_final_verification_rejects_port_that_remains_open(self):
        result=self.run_shell('''
stop_execution() { return 0; }
stop_one() { return 0; }
execution_pids() { :; }
service_pids() { :; }
port_open() { [ "$1" = 5001 ]; }
set +e
stop_all
exit "$?"
''')
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertIn('5001',result.stdout)

    def test_final_verification_requires_no_processes_or_open_ports(self):
        result=self.run_shell('''
stop_execution() { return 0; }
stop_one() { return 0; }
execution_pids() { :; }
service_pids() { :; }
port_open() { return 1; }
stop_all
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('全停已确认',result.stdout)

    def test_relative_service_command_must_belong_to_this_project(self):
        result=self.run_shell('''
process_command() { printf 'python -m uvicorn src.api_v2.app:app --port 5002'; }
process_cwd() { printf '/another-project'; }
if matches_service "统一只读 API" 777; then exit 9; fi
process_cwd() { printf '%s' "$ROOT_DIR"; }
matches_service "统一只读 API" 777
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_execution_process_scan_splits_pid_and_command_and_checks_scope(self):
        result=self.run_shell('''
ps() {
    printf '  101 /usr/bin/python3 -B %s/scripts/execution_cli.py --run\\n' "$ROOT_DIR"
    printf '  102 /usr/bin/python3 scripts/execution_cli.py --run\\n'
    printf '  103 /usr/bin/python3 scripts/execution_cli.py --run\\n'
    printf '  104 /bin/zsh -c python scripts/execution_cli.py --run\\n'
    printf '  105 /usr/bin/python3 /another-project/scripts/execution_cli.py --run\\n'
    printf '  106 /usr/bin/python3 %s/scripts/execution_cli.py.backup --run\\n' "$ROOT_DIR"
    printf '  107 /usr/bin/python3 other.py --note scripts/execution_cli.py\\n'
}
process_cwd() {
    if [ "$1" = 102 ]; then printf '%s' "$ROOT_DIR"; else printf '/another-project'; fi
}
execution_pids
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.splitlines(),['101','102'])

    def test_execution_stop_signals_verified_worker_and_preserves_marker(self):
        result=self.run_shell('''
alive=1
execution_pids() { if [ "$alive" -eq 1 ]; then printf '777\\n'; fi; }
pid_alive() { [ "$alive" -eq 1 ]; }
process_command() { printf 'python -B %s/scripts/execution_cli.py --run' "$ROOT_DIR"; }
kill() { printf 'fake-signal:%s\\n' "$*"; alive=0; }
sleep() { :; }
stop_execution
[ -e "$EXEC_STOP_MARKER" ]
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('fake-signal:777',result.stdout)
        self.assertIn('实盘执行已停止',result.stdout)
        self.assertNotIn('-KILL',result.stdout)


if __name__=='__main__':
    unittest.main()
