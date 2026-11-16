"""Explain residual exits from bounded local evidence, outside execution."""
import json
import sqlite3
from contextlib import closing

from src.web.rollback_explanation import VENUE_NAMES, display, number


EVENT_FILTER = "event IN ('close_receipt','close_residual_retry')"
POSITION_FILTER = "json_valid(detail) AND json_extract(detail,'$.position_id') IS NOT NULL AND json_extract(detail,'$.position_id')=?"
RECENT_EVENTS = 'SELECT id,timestamp,event,detail FROM events WHERE ' + POSITION_FILTER + ' ORDER BY id DESC LIMIT 256'
CLOSE_EVENTS_SQL = 'SELECT timestamp,event,detail FROM (' + RECENT_EVENTS + ') WHERE ' + EVENT_FILTER + ' ORDER BY id DESC LIMIT 64'
CLOSE_VERSION_SQL = 'SELECT MAX(id) FROM (' + RECENT_EVENTS + ') WHERE ' + EVENT_FILTER


def read_close_events(path, position_ids):
    if not path.exists() or not position_ids:
        return {}
    result = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
        for identity in set(position_ids):
            # Bound the inspected position events as well as returned receipts:
            # filtering first can scan months of quote diagnostics for no match.
            rows = db.execute(CLOSE_EVENTS_SQL, (identity,)).fetchall()
            result[identity] = [(stamp, event, json.loads(detail)) for stamp, event, detail in reversed(rows)]
    return result


def close_outcome(row, decisions, events):
    receipts, seen = {}, set()
    residual = any(event == 'close_residual_retry' for _, event, _ in events)
    for stamp, event, receipt in events:
        if event != 'close_receipt' or receipt.get('confirmed') is not True:
            continue
        venue = receipt.get('venue')
        order_id = receipt.get('order_id')
        if order_id:
            key = (venue, str(order_id))
            if key in seen:
                continue
            seen.add(key)
        receipts.setdefault(venue, []).append((stamp, receipt))
    # Repeated orders can also be two zero-fill batches followed by a fresh
    # profitable attempt. Only an explicit residual decision proves cleanup.
    multiple = any(len({str(r.get('order_id')) for _, r in rows if r.get('order_id')}) > 1
                   for rows in receipts.values())
    allowed = next((d.get('pre_submit') for d in decisions
                    if (d.get('pre_submit') or {}).get('allowed')), {}) or {}
    estimated = number(allowed.get('net_pnl_usdc'))
    realized = number(row.get('realized_pnl_usdc'))
    closed = row.get('status') == 'closed'
    summary = ('补平后亏损' if closed and realized is not None and realized < 0 else
               '含残余补平' if closed else '残余仓位处理中') if residual else ''
    if not summary and multiple:
        summary = '多次尝试后亏损' if closed and realized is not None and realized < 0 else '多次平仓尝试'
    if not summary and closed and estimated is not None and estimated > 0 and realized is not None and realized < 0:
        summary = '成交后收益转负'
    lines = []
    if estimated is not None:
        lines.append('发送前保护限价估算 ' + display(estimated, ' USDC') + '；最终净盈亏 '
                     + display(realized, ' USDC') + '。估算仅覆盖当次限价尝试，不保证后续补平收益。')
    if residual:
        lines.append('双腿平仓未一次完成，随后按新报价只减仓补平剩余敞口；补平不再等待整组盈利。')
    for leg in ('variational', 'lighter'):
        venue = row.get(leg + '_venue')
        rows = receipts.get(venue, [])
        if not rows:
            continue
        name = VENUE_NAMES.get(venue, venue or leg)
        for index, (stamp, receipt) in enumerate(rows):
            filled = number(receipt.get('filled_size'))
            lines.append(name + ' 平仓记录' + str(index+1)
                         + '：' + str(receipt.get('status') or '终态未知')
                         + '；成交 ' + display(filled) + (' @ ' + display(receipt.get('average_price')) if filled else '')
                         + '；' + stamp)
        plan = (allowed.get('legs') or {}).get(leg, {})
        actual = number(row.get('close_price_' + leg))
        bound = number(plan.get('bound'))
        qty = number(row.get('entry_size_' + leg))
        if actual and bound and qty and plan.get('close_side') in ('buy', 'sell'):
            adverse = (actual - bound) * qty * (1 if plan['close_side'] == 'buy' else -1)
            lines.append(name + ' 最终均价 ' + display(actual) + '；触发时保护限价 ' + display(bound)
                         + '；相对该限价的成交损益影响 ' + display(-adverse, ' USDC'))
    return dict(summary=summary, residual_retry=residual, multiple_attempts=multiple, estimated_net_pnl_usdc=estimated,
                realized_pnl_usdc=realized, text='\n'.join(lines))
