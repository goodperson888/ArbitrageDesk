#!/usr/bin/env python3
"""Read-only recovery for a signed venue pair; never sends an order."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.execution.reconcile_pair import reconcile_pair
from src.execution.reset_stop import reset_stop
from src.execution.reset_stop import reconcile_unknown_orders
from src.execution.journal import execution_lease


def reconcile_and_reset(root, venues, state_venues=None):
    report = {}

    def verify():
        report.update(reconcile_pair(root, venues, state_venues=state_venues)
                      if state_venues else reconcile_pair(root, venues))
        return report.get('success') is True and report.get('orders_submitted') is False

    if (root / 'data/STOP_EXECUTION').exists():
        # Capture the stop marker before network reads. A new stop request
        # during reconciliation must survive; the shared reset also checks
        # unresolved order intents and holds the execution lease throughout.
        reset_stop(root, verify,
                   reconcile_orders=lambda: (reconcile_unknown_orders(root, report.get('checked_venues') or venues)
                                              if report.get('accounts_flat') is True else 0))
    else:
        with execution_lease(root / 'data/runtime.lock'):
            verify()
            # There may be no STOP_EXECUTION marker after a child process has
            # exited normally.  The fresh flat-account proof still makes
            # journal rows with unknown terminal accounting safe to close;
            # otherwise the next candidate remains blocked by the old intent.
            if (report.get('success') is True and report.get('orders_submitted') is False and
                    report.get('accounts_flat') is True):
                report['reconciled_orders'] = reconcile_unknown_orders(root, report.get('checked_venues') or venues)
    if not report:
        raise ValueError('账户核对未完成')
    if report.get('success') is True:
        if report.get('resume_ready'):
            report['message'] = '两平台账户已核对并匹配本地未平仓记录，停止已解除；请重新只读检查后恢复监控，实盘仍需手动开启。'
        else:
            report['message'] = '原执行平台账户已核对空仓，停止已解除；请重新只读检查，实盘需手动开启。'
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--venues', required=True)
    parser.add_argument('--state-venues', help='原工作进程的完整平台范围，仅选择对应本地记录')
    args = parser.parse_args()
    report = {'success': False, 'orders_submitted': False, 'stage': '只读核对平台账户'}
    try:
        report.update(reconcile_and_reset(ROOT, [x for x in args.venues.split(',') if x],
            [x for x in args.state_venues.split(',') if x] if args.state_venues else None))
    except Exception as exc:
        # Echo only bounded, non-sensitive failure classes. Never expose SDK
        # responses, account data, URLs with credentials, or private keys.
        message = str(exc)
        safe = {
            '已有执行进程持有锁',
            '存在未核对订单；请先核对成交回执',
            '未确认两平台账户空仓且本次市场无挂单',
            '需要选择两个不同平台',
            '当前只读恢复支持 Lighter 主网与 Lighter RH 组合',
            '当前只读恢复支持 Lighter 主网、Lighter RH 与 Arcus 组合',
            '原执行范围与核对平台不匹配',
            '未找到该平台组合的本地执行状态',
            '原持仓平台与本次执行平台不匹配',
            '本地有未核对持仓或停机原因；不能通过解除停止清除',
            '上一笔平仓终态未确认，禁止恢复自动下单；请核对原平仓订单',
            '原持仓双腿数量不匹配，保留待核对',
            '账户持仓与已记录各组持仓不匹配',
            '账户持仓方向或数量不匹配',
            '账户持仓市场未匹配',
            '原组合已无对应持仓，但管理范围内有其他敞口，请先核对账户',
        }
        if message.startswith('行情请求失败: ') or message.startswith('本地代理不可用或直连失败（'):
            reason = message
        elif message in safe or '凭据未配置' in message or '账户快照' in message or '仍有持仓或挂单' in message or '仍有活动订单' in message:
            reason = message
        else:
            reason = type(exc).__name__ + '：账户只读核对未完成'
        report['message'] = report['stage'] + '未通过：' + reason + '；停止状态保留，未发送订单。'
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
