"""Offline package import, template and native DLL check. Never starts trading."""
import argparse
import ctypes
from pathlib import Path
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if root.name != 'ArbitrageDesk' or root.parent.name != 'dist':
        raise ValueError('Smoke check only accepts a disposable dist/ArbitrageDesk package')
    if not args.child:
        import subprocess
        import shutil
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 '--root', str(root), '--child'], timeout=60)
        # Imported SQLite writers must exit before removing files on Windows.
        for name in ('data', 'logs', '.run'):
            if (root / name).exists():
                shutil.rmtree(root / name)
        raise SystemExit(result.returncode)
    sys.path.insert(0, str(root))
    import os
    os.chdir(root)
    with patch('socket.socket.connect', side_effect=AssertionError('Offline smoke: network forbidden')):
        from src.common.config_loader import ConfigLoader
        from src.execution.runtime import pair_runtime_blockers
        from src.execution.console import ExecutionConsole
        from src.web.app import app
        from src.api_v2.app import app as api
        from src.variational.quote_bridge import QuoteBridge
        from src.common.platform_support import execution_python
        import lighter
        QuoteBridge(root / 'data/windows-smoke-token')
        assert execution_python(root).is_file()
        assert ConfigLoader(root / 'config/config.example.yaml').get('strategy.mode') == 'monitor'
        with app.app_context():
            html = app.jinja_env.get_template('index.html').render(
                local_token='offline-smoke', symbols=['BTC', 'ETH'], notional_cap=1,
                max_total_position_notional=0)
            assert 'execution-check' in html and 'live-arm' in html
        assert api.title
        library = Path(lighter.__file__).parent / 'signers/lighter-signer-windows-amd64.dll'
        if sys.platform == 'win32':
            ctypes.CDLL(str(library))
    print('Portable imports, template, bridge token and Windows signer DLL passed offline.')


if __name__ == '__main__':
    main()
