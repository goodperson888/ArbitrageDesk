#!/usr/bin/env python3
"""Exercise both execution adapters against controlled local fixtures; no network."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    print('离线执行演练：使用测试账户和合成回执，不连接浏览器、不签名、不发送订单。', flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern='test_execution.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
