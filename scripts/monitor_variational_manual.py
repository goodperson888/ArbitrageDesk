#!/usr/bin/env python3
"""Tail redacted Variational observations produced by the local browser bridge."""
import argparse
import json
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', default='data/variational-manual-events.jsonl')
    parser.add_argument('--seconds', type=int, default=300)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 3600:
        raise SystemExit('监控时长必须在 10–3600 秒之间')
    path = Path(args.file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with path.open('r', encoding='utf-8') as stream:
        stream.seek(0, 2)
        print('手动观测监控已启动：只读取脱敏事件，不连接浏览器、不点击下单。', flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            line = stream.readline()
            if line:
                try:
                    print(json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True), flush=True)
                except json.JSONDecodeError:
                    print('{"event":"invalid_local_record"}', flush=True)
            else:
                time.sleep(0.25)
    print('手动观测监控结束；请以 Variational 页面订单/交易历史和仓位为最终依据。', flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('手动观测监控已停止；浏览器保持打开。')
