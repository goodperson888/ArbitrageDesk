#!/usr/bin/env python3
"""Run one explicit Arcus-only open -> reduce-only close smoke test.

This is intentionally separate from the hedge worker.  It never runs unless
the caller supplies --confirm, and it refuses to start when Arcus already has
the selected market position or an active order.
"""
import argparse
import json
import os
import sys
import math
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter
from src.common.config_loader import ConfigLoader
from src.common.market_data import age_seconds
from src.execution.journal import OrderJournal, execution_lease
from src.arcus.round_trip import run_round_trip


def save_report(report):
    path = ROOT / 'data/arcus-round-trip-test.json'
    fd, name = tempfile.mkstemp(prefix='.arcus-test-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), 0o600) if hasattr(os, 'fchmod') else None
            json.dump(report, out, ensure_ascii=False, indent=2, default=str)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _arcus_values(root):
    values = dotenv_values(root / '.env')
    required = ('ARCUS_WALLET_ADDRESS', 'ARCUS_ACCOUNT_INDEX', 'ARCUS_API_SIGNING_KEY')
    if any(not values.get(key) for key in required):
        raise ValueError('Arcus 本机凭据未配置')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='BABA', help='Arcus 市场基础符号')
    parser.add_argument('--notional', type=float, default=0,
                        help='开仓名义金额；0 表示按 Arcus 最小金额上浮 5%%')
    parser.add_argument('--hold-seconds', type=float, default=0,
                        help='开仓成交后等待秒数，默认立即进入平仓')
    parser.add_argument('--confirm', action='store_true',
                        help='确认提交真实 Arcus 订单；没有此参数只显示用法')
    parser.add_argument('--check-only', action='store_true', help='只核对账户、金额与同数量报价，不下单')
    args = parser.parse_args()
    if not args.confirm and not args.check_only:
        raise SystemExit('这是实时下单测试；请明确追加 --confirm 后再运行。')
    if (not math.isfinite(args.notional) or not math.isfinite(args.hold_seconds) or
            args.notional < 0 or args.hold_seconds < 0):
        raise SystemExit('金额和等待秒数必须为非负数')

    root = ROOT
    values = _arcus_values(root)
    try:
        evidence = json.loads((root / 'data/platform-verification.json').read_text())['arcus']
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError('Arcus 费率证据缺失，请先做只读鉴权')
    fee_age = age_seconds(evidence.get('checked_at'))
    if evidence.get('authenticated') is not True or fee_age is None or not 0 <= fee_age <= 300:
        raise ValueError('Arcus 只读鉴权或费率证据已过期，请先重新验证')
    fee_bps = float(evidence.get('taker_fee_bps'))
    config = ConfigLoader(root / 'config/config.yaml')
    client = ArcusClient(
        values['ARCUS_WALLET_ADDRESS'], int(values['ARCUS_ACCOUNT_INDEX']),
        values['ARCUS_API_SIGNING_KEY'],
        base_url=config.get('arcus.api.base_url', 'https://api.arcus.xyz'),
        ws_url=config.get('arcus.api.ws_url', 'wss://api.arcus.xyz/v1/ws'),
        proxy_url=config.get('arcus.api.proxy_url', config.get('common.network.proxy_url')),
        order_timeout=config.get('arcus.execution.order_timeout_seconds', 15),
    )
    journal = OrderJournal(root / 'data/execution_orders.sqlite')
    try:
        adapter = ArcusExecutionAdapter(client, journal, values['ARCUS_WALLET_ADDRESS'],
                                         enabled=True, fee_bps=fee_bps)
        with execution_lease(root / 'data/runtime.lock'):
            result = run_round_trip(adapter, args.symbol.upper().removesuffix('-USD'),
                                    args.notional, config.get('strategy.trading.position_size'),
                                    hold_seconds=args.hold_seconds, check_only=args.check_only,
                                    persist=save_report)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if result['success'] else 3
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'success': False, 'orders_submitted': False,
                          'error': type(exc).__name__ + '：' + str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
