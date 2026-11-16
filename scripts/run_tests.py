#!/usr/bin/env python3
"""Run bounded, explicit offline regression scopes; never discover root diagnostics."""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
SCOPES = {
    'quick': ('test_test_runner.py', 'test_sizing.py', 'test_hedge_safety.py',
              'test_channel_strategy.py', 'test_market_data.py'),
    'frontend': ('test_*.js',),
    'web': ('test_*.js', 'test_web.py', 'test_operations.py',
            'test_execution_console.py', 'test_position_monitor_web.py',
            'test_strategy_preview.py', 'test_pair_preview.py'),
    'strategy': ('test_channel*.py', 'test_target_return.py', 'test_shared_reversion.py',
                 'test_spread_grid.py', 'test_profit_exit.py', 'test_funding_forecast.py',
                 'test_entry_cost_and_once.py', 'test_entry_window_risk.py',
                 'test_opportunity*.py', 'test_reversion_recency.py',
                 'test_center_statistics.py', 'test_round_trip_preview.py'),
    'market': ('test_market*.py', 'test_live_feeds.py', 'test_quote_stream_freshness.py',
               'test_*discovery*.py', 'test_catalog*.py', 'test_arcus_adapter.py',
               'test_arcus_pricing.py', 'test_arcus_quote_clock.py',
               'test_arcus_subscription_limits.py', 'test_passive_bridge.py',
               'test_route_discovery.py'),
    'history': ('test_history*.py', 'test_close_diagnostics.py', 'test_close_outcome.py',
                'test_execution_book.py', 'test_manual_events.py',
                'test_rollback_explanation.py'),
    'services': ('test_service_shutdown.py', 'test_reload_web.py',
                 'test_reset_stop.py', 'test_browser_launcher.py'),
    'full': ('test_*.py', 'test_*.js'),
}


def select_tests(scope, files=(), root=ROOT):
    """Only direct regression files in tests/ are executable through this entry."""
    directory = root / 'tests'
    available = sorted(p for p in directory.glob('test_*')
                       if p.is_file() and p.suffix in ('.py', '.js'))
    if files:
        selected = set()
        for name in files:
            candidate = Path(name)
            path = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if path.parent != directory.resolve() or path not in available:
                raise ValueError('只能选择 tests/ 下现存的 test_*.py 或 test_*.js：' + name)
            selected.add(path)
        return sorted(selected)
    patterns = SCOPES[scope]
    for pattern in patterns:
        if not any(fnmatch.fnmatch(p.name, pattern) for p in available):
            raise ValueError('测试分组引用了不存在的文件：' + pattern)
    selected = [p for p in available if any(fnmatch.fnmatch(p.name, x) for x in patterns)]
    if not selected:
        raise ValueError('没有选中测试，不能视为验证通过')
    return selected


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []

    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append({'test': test.id(),
                             'seconds': time.perf_counter() - self.started})
        super().stopTest(test)


def run_python(files):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    imports = []
    for path in files:
        started = time.perf_counter()
        suite.addTests(loader.loadTestsFromName(path.stem))
        imports.append({'file': path.name, 'seconds': time.perf_counter() - started})
    if not suite.countTestCases():
        raise ValueError('选中的后端文件没有测试用例，不能视为验证通过')
    result = unittest.TextTestRunner(verbosity=1, resultclass=TimedResult).run(suite)
    return {'passed': result.wasSuccessful(), 'count': result.testsRun,
            'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'imports': imports,
            'slowest': sorted(result.timings, key=lambda x: x['seconds'], reverse=True)[:10]}


def run_javascript(files, node):
    results = []
    for path in files:
        started = time.perf_counter()
        try:
            process = subprocess.run([node, str(path)], cwd=str(ROOT),
                                     capture_output=True, text=True, timeout=60)
            passed = process.returncode == 0
            if not passed:
                print((process.stdout + process.stderr)[-12000:], file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            passed = False
            print('{}: {}'.format(path.name, error), file=sys.stderr)
        results.append({'file': path.name, 'passed': passed,
                        'seconds': time.perf_counter() - started})
        print('{} {}'.format('PASS' if passed else 'FAIL', path.name), flush=True)
    return results


def write_report(report):
    folder = ROOT / '.run' / 'test-results'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'last.json'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(folder),
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
        os.replace(str(temporary), str(destination))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--scope', choices=SCOPES, help='默认 quick；完整验收使用 full')
    choice.add_argument('--tests', nargs='+', metavar='tests/test_file', help='只运行指定回归文件')
    parser.add_argument('--list', action='store_true', help='只列出文件，不加载或运行测试')
    args = parser.parse_args(argv)
    scope = args.scope or ('custom' if args.tests else 'quick')
    try:
        files = select_tests(scope, args.tests or ())
    except ValueError as error:
        parser.error(str(error))
    python_files = [p for p in files if p.suffix == '.py']
    js_files = [p for p in files if p.suffix == '.js']
    print('验证范围：{} · 后端 {} 个文件 · 前端 {} 个文件'.format(
        scope, len(python_files), len(js_files)), flush=True)
    if scope != 'full':
        print('这是局部检查；成交、资金、平仓、恢复或跨模块改动完成后需运行 --scope full。', flush=True)
    if args.list:
        for path in files:
            print(path.relative_to(ROOT))
        return 0
    node = shutil.which(os.environ.get('NODE', 'node')) if js_files else None
    if js_files and not node:
        parser.error('找不到 Node.js；请配置 PATH 或 NODE，前端测试未执行')
    os.chdir(str(ROOT))
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(TESTS), str(ROOT)]
    started = time.perf_counter()
    report = {'scope': scope, 'files': [str(p.relative_to(ROOT)) for p in files],
              'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    try:
        backend = run_python(python_files) if python_files else {'passed': True, 'count': 0}
    except Exception as error:
        print('后端测试运行失败：{}'.format(error), file=sys.stderr)
        backend = {'passed': False, 'count': 0, 'runner_error': type(error).__name__}
    frontend = run_javascript(js_files, node)
    report.update(backend=backend, frontend=frontend,
                  seconds=time.perf_counter() - started,
                  passed=backend['passed'] and all(item['passed'] for item in frontend))
    try:
        destination = write_report(report)
    except OSError as error:
        print('无法保存测试报告：{}'.format(error), file=sys.stderr)
        return 1
    print('{} · 后端 {} 项 · 前端 {} 个套件 · {:.2f} 秒'.format(
        '通过' if report['passed'] else '失败', backend['count'], len(frontend), report['seconds']))
    for item in backend.get('slowest', [])[:3]:
        print('耗时 {:.3f}s {}'.format(item['seconds'], item['test']))
    print('耗时报告：{}'.format(destination))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
