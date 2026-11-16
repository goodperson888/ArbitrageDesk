"""Portable service supervisor. Startup never enables trading or resets STOP."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.execution.journal import execution_lease


def prepare(root):
    for directory in ('config', 'data', 'logs', '.run'):
        (root / directory).mkdir(exist_ok=True)
    for source, target in (('config.example.yaml', 'config.yaml'),
                           ('variational-execution.example.json', 'variational-execution.local.json')):
        destination = root / 'config' / target
        if not destination.exists():
            shutil.copyfile(root / 'config' / source, destination)


def read_state(root):
    try:
        return json.loads((root / '.run/windows-services.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_state(root, state):
    path = root / '.run/windows-services.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(path)


def owned_process(record, root):
    import psutil
    try:
        process = psutil.Process(record['pid'])
        if abs(process.create_time() - record['created_at']) > .01:
            return None
        if Path(process.cwd()).resolve() != root.resolve():
            return None
        command = process.cmdline()
        role = record['role']
        if role == 'web':
            valid = any(Path(arg).resolve() == root / 'src/web/app.py' for arg in command[1:])
        elif role == 'api':
            valid = 'uvicorn' in command and 'src.api_v2.app:app' in command
        elif role == 'launcher':
            valid = any(Path(arg).resolve() == root / 'scripts/windows_launcher.py' for arg in command[1:])
        else:
            valid = False
        return process if valid else None
    except (psutil.Error, OSError, KeyError, TypeError):
        return None


def terminate_services(root, state):
    """Terminate only identity-verified services and their verified descendants."""
    import psutil
    (root / 'data/STOP_EXECUTION').touch()
    processes = []
    for record in state.get('services', []):
        parent = owned_process(record, root)
        if parent is None:
            continue
        try:
            processes.extend(parent.children(recursive=True))
        except psutil.Error:
            pass
        processes.append(parent)
    for process in processes:
        try:
            process.terminate()
        except psutil.Error:
            pass
    _, remaining = psutil.wait_procs(processes, timeout=5)
    for process in remaining:
        try:
            process.kill()
        except psutil.Error:
            pass
    _, remaining = psutil.wait_procs(remaining, timeout=3)
    return not remaining


def stop(root):
    state = read_state(root)
    if not state:
        print('没有 Windows 启动记录。')
        return 0
    (root / 'data/STOP_EXECUTION').touch()
    request = root / '.run/windows-stop.request'
    request.write_text(state['run_id'], encoding='utf-8')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and owned_process(state['supervisor'], root):
        time.sleep(.2)
    if owned_process(state['supervisor'], root):
        print('服务仍在结束中，请查看 logs/windows-launcher.log。')
        return 1
    # Recover only recorded, identity-verified descendants if the supervisor died.
    stopped = terminate_services(root, state)
    print('已停止服务；停止不代表平仓。' if stopped else '仍有服务未退出，请检查日志。')
    return 0 if stopped else 1


def start(root, open_browser=True):
    import psutil
    prepare(root)
    with execution_lease(root / '.run/windows-launcher.lock'):
        old = read_state(root)
        if any(owned_process(item, root) for item in old.get('services', [])):
            raise RuntimeError('已有服务，请先双击 stop-windows.cmd。')
        for port in (5001, 5002):
            with socket.socket() as probe:
                if os.name == 'nt':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', port))
        state = {'run_id': uuid.uuid4().hex, 'services': [], 'supervisor':
                 {'pid': os.getpid(), 'created_at': psutil.Process().create_time(), 'role': 'launcher'}}
        stopped = threading.Event()
        for name in ('SIGINT', 'SIGTERM'):
            signal.signal(getattr(signal, name), lambda *_: stopped.set())
        env = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
        children = []
        try:
            for role, arguments in (
                ('api', ['-m', 'uvicorn', 'src.api_v2.app:app', '--host', '127.0.0.1', '--port', '5002']),
                ('web', [str(root / 'src/web/app.py')])):
                with (root / ('logs/windows-' + role + '.log')).open('ab') as log:
                    child = subprocess.Popen([sys.executable, '-B', *arguments], cwd=root,
                        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                children.append(child)
                state['services'].append({'pid': child.pid, 'role': role,
                                          'created_at': psutil.Process(child.pid).create_time()})
                write_state(root, state)
            print('服务启动中：http://127.0.0.1:5001 · 实盘需在页面手动开启。', flush=True)
            launched = False
            while not stopped.wait(.2):
                if any(child.poll() is not None for child in children):
                    raise RuntimeError('服务退出，请查看 logs/windows-web.log 和 windows-api.log。')
                try:
                    if (root / '.run/windows-stop.request').read_text(encoding='utf-8') == state['run_id']:
                        break
                except FileNotFoundError:
                    pass
                if open_browser and not launched:
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1', 5001)) == 0:
                            webbrowser.open('http://127.0.0.1:5001/')
                            launched = True
        finally:
            terminate_services(root, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    args = parser.parse_args()
    if args.action == 'stop':
        return stop(ROOT)
    if args.action == 'status':
        state = read_state(ROOT)
        print(json.dumps({item['role']: bool(owned_process(item, ROOT))
                          for item in state.get('services', [])}))
        return 0
    start(ROOT)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
