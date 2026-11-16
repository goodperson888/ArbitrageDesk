#!/usr/bin/env python3
"""Reproduce quantity quotes and sizing; never call an execution worker."""
import json
import argparse
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.common.config_loader import ConfigLoader
from src.execution.runtime import _run_verified, _run_pair_verified, runtime_blockers
from src.execution.journal import execution_lease
from src.execution.orders import decimal
from src.execution.telemetry import safe_reason
from src.execution.history import TradeHistory

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('cap')
parser.add_argument('symbol')
parser.add_argument('--venues', help='两个签名平台，逗号分隔；省略时复测 Variational/Lighter')
args=parser.parse_args()
start=time.monotonic()
result={'ready':False,'orders_submitted':False}
try:
    config=ConfigLoader(ROOT/'config/config.yaml')
    profile=json.loads((ROOT/'config/variational-execution.local.json').read_text())
    cap=decimal(args.cap,True)
    symbol=args.symbol.strip().upper().removesuffix('-PERP')
    venues=args.venues.split(',') if args.venues is not None else None
    if (not symbol or symbol=='ALL' or
            (venues is None and profile['symbol']!=symbol) or
            cap>decimal(config.get('strategy.trading.position_size'),True)):
        raise ValueError('invalid_preview_parameters')
    if venues is not None:
        profile.update(venues=venues,symbol=symbol,symbols=[symbol])
    config.config['strategy']['trading']['position_size']=float(cap)
    blockers=runtime_blockers(profile,require_enabled=False)
    if blockers:
        result['message']='；'.join(blockers)
    else:
        profile['live_enabled']=True  # In-memory adapter readiness only; plan-only branch cannot execute.
        with execution_lease(ROOT/'data/runtime.lock'):
            preview=_run_pair_verified if venues is not None else _run_verified
            result=preview(config,profile,ROOT,plan_only=True)
        if result.get('ready'):
            result['message']='数量报价及净价差测算通过；这不是下单，正式执行仍需重新检查。'
            if venues is not None:
                plan=result['plan']
                names={'lighter':'Lighter 主网','lighter_rh':'Lighter RH','arcus':'Arcus','standx':'StandX'}
                result['message']=(
                    f"只读计划：{plan['quantity']:g} {symbol}，"
                    f"买 {names[plan['buy_venue']]} @ {plan['buy_price']:g}，"
                    f"卖 {names[plan['sell_venue']]} @ {plan['sell_price']:g}；"
                    f"入场扣费后价差 {plan['net_edge_bps']/100:.4f}%"
                    f"（约 {plan['estimated_entry_edge_usdc']:.6f} USDC），未计平仓成本与资金费。"
                    '未提交订单，正式执行会重新检查。')
except Exception as exc:
    result['message']=safe_reason(exc)+'；本次为只读计划复测，未提交订单。'
result['elapsed_ms']=round((time.monotonic()-start)*1000)
TradeHistory(ROOT/'data/hedge_events.sqlite').append('plan_preview',symbol=args.symbol,reason=result['message'],elapsed_ms=result['elapsed_ms'])
print(json.dumps(result,ensure_ascii=False))
