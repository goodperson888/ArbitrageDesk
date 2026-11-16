"""Allowlisted portable packaging; never copy local credentials or trading state."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'dist/ArbitrageDesk'


def stage(root=ROOT, package=PACKAGE):
    if package.exists():
        raise ValueError('Package directory already exists; remove dist/ArbitrageDesk before rebuilding')
    package.mkdir(parents=True)
    # Deliberate source allowlist, independent of Git ignore rules.
    for directory in ('src', 'scripts', 'browser_extension'):
        shutil.copytree(root / directory, package / directory,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log', '*.har', '.DS_Store', '*.local.*'))
    for name in ('main.py', 'start-windows.cmd', 'stop-windows.cmd', 'README.md',
                 'requirements-windows.txt', 'requirements-runtime.txt', 'requirements-execution.txt'):
        shutil.copyfile(root / name, package / name)
    (package / 'config').mkdir()
    for name in ('config.example.yaml', 'variational-execution.example.json'):
        shutil.copyfile(root / 'config' / name, package / 'config' / name)
    # Recreate the downloadable read-only extension from tracked source.
    static = package / 'src/web/static'
    if static.is_dir():
        shutil.make_archive(str(static / 'variational-quote-bridge'), 'zip',
                            root_dir=package / 'browser_extension')
    (package / 'docs').mkdir()
    shutil.copyfile(root / 'docs/windows-package.md', package / 'docs/windows-package.md')


def configure(package=PACKAGE):
    files = list((package / 'python').glob('python*._pth'))
    if len(files) != 1:
        raise ValueError('Expected one official embedded Python path file')
    # Run source scripts and verifiers from the same portable interpreter.
    files[0].write_text('python312.zip\n.\nLib/site-packages\n..\nimport site\n', encoding='utf-8')
    profile = json.loads((package / 'config/variational-execution.example.json').read_text(encoding='utf-8'))
    if profile.get('live_enabled') is not False or profile.get('account_verified') is not False:
        raise ValueError('Example profile must disable trading')


def archive(package=PACKAGE):
    forbidden = [p for p in package.rglob('*') if p.is_file() and (
        p.name == '.env' or p.name.endswith(('.sqlite', '.db', '.har', '.jsonl', '.local.json'))
        or p.relative_to(package).parts[0] in ('data', 'logs', '.run'))]
    if forbidden:
        raise ValueError('Runtime or private files found in package')
    return shutil.make_archive(str(package.parent / 'ArbitrageDesk-windows-x64'), 'zip',
                               root_dir=package.parent, base_dir=package.name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'configure', 'archive'))
    args = parser.parse_args()
    if args.action == 'stage':
        stage()
    elif args.action == 'configure':
        configure()
    else:
        print(archive())
