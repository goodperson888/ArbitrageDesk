"""Explain entry rollbacks from local evidence, without exchange requests."""
import json
import math
import sqlite3
from datetime import datetime
from src.execution.telemetry import safe_reason
from src.execution.window_risk import REASON_LABELS


VENUE_NAMES = {'lighter': 'Lighter', 'lighter_rh': 'Lighter RH', 'arcus': 'Arcus',
               'variational': 'Variational', 'standx': 'StandX'}
EVENTS = ('planned', 'entry_requoted', 'entry_receipt', 'hedge_decision',
          'hedge_leg_skipped', 'accounting_pending', 'entry_rollback', 'blocked',
          'stage_started', 'close_receipt', 'platform_order_observed')


def number(value):
    try:
        result = float(value) if value is not None and not isinstance(value, bool) else None
        return result if result is not None and math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def display(value, unit=''):
    value = number(value)
    return '未知' if value is None else f'{value:.10g}{unit}'


def elapsed(start, end):
    if not start or not end:
        return None
    a, b = (number(item.get('execution_total_ms')) for item in (start, end))
    if a is not None and b is not None:
        return b - a if b >= a else None
    try:
        delta = (datetime.fromisoformat(end['_timestamp'].replace('Z', '+00:00')) -
                 datetime.fromisoformat(start['_timestamp'].replace('Z', '+00:00'))).total_seconds() * 1000
        return round(delta, 3) if delta >= 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def price_guard(row, decision, order):
    reference = number(decision.get('second_reference_price'))
    observed = number(decision.get('observed_price'))
    budget_bound = number(decision.get('price_budget_bound'))
    limit = number(order.get('limit_price')) or number(order.get('price'))
    side = row.get('lighter_side')
    is_buy = side in ('buy', 'long') if side in ('buy', 'long', 'sell', 'short') else None
    def distance(a, b):
        if is_buy is None or not a or not b or a <= 0 or b <= 0:
            return None
        return (b / a - 1 if is_buy else 1 - b / a) * 10000
    used = distance(reference, observed)
    return dict(reference_price=reference, observed_price=observed,
                budget_bound=budget_bound, submitted_limit=limit,
                budget_bps=distance(reference, budget_bound),
                consumed_bps=max(0., used) if used is not None else None,
                remaining_budget_bps=distance(observed, budget_bound),
                remaining_limit_bps=distance(observed, limit))


def explain(row, events, orders):
    plan = next((d for e, d in reversed(events) if e in ('planned', 'entry_requoted')), {})
    decision = next((d for e, d in reversed(events) if e == 'hedge_decision'), {})
    skipped = next((d for e, d in reversed(events) if e == 'hedge_leg_skipped'), {})
    cause = next((d.get('reason') for e, d in reversed(events)
                  if e in ('entry_rollback', 'hedge_leg_skipped', 'accounting_pending', 'blocked')
                  and d.get('reason')), None)
    lines, legs, receipts, order_evidence = [], [], [], []
    for index, leg in enumerate(('variational', 'lighter')):
        venue = row.get(leg + '_venue')
        receipt = next((d for e, d in reversed(events)
                        if e == 'entry_receipt' and d.get('venue') == venue), {})
        order = orders.get((row['id'] + ('-v' if index == 0 else '-l'), venue), {})
        evidence = {**order, **{k: v for k, v in receipt.items() if v is not None}}
        observed_order = next((d for e, d in reversed(events) if e == 'platform_order_observed'
            and d.get('venue') == venue and d.get('order_id') == evidence.get('order_id')
            and d.get('order_id')), {})
        evidence.update({k: v for k, v in observed_order.items() if v is not None})
        receipts.append(receipt)
        order_evidence.append(evidence)
        filled = number(receipt.get('filled_size'))
        if filled is None:
            filled = number(order.get('filled_size'))
        if filled is None:
            filled = number(row.get('entry_size_' + leg))
        requested = number(receipt.get('requested_size'))
        if requested is None:
            requested = number(order.get('size'))
        # A confirmed first fill sets the second leg's intended hedge size.
        planned = number(plan.get('quantity')) if index == 0 else legs[0]['filled_size']
        price = number(receipt.get('average_price')) if filled and filled > 0 else None
        if price is None and filled and filled > 0:
            price = number(row.get('entry_price_' + leg))
        order_id = receipt.get('order_id') or order.get('order_id')
        reason = order.get('reason') or receipt.get('reason')
        submitted = receipt.get('submitted')
        if submitted is None:
            submitted = order.get('submitted')
        if index == 1 and skipped and not receipt and not order:
            submission = '未提交（保护拦截）'
            requested = 0
        elif order_id or (filled is not None and filled > 0):
            submission = '平台已接单' + ('，0 成交' if filled == 0 else '')
        elif submitted is False:
            submission = '未提交（下单检查未通过）'
        elif submitted is True:
            submission = '已发送订单，平台未接单' if receipt.get('status') == 'rejected' else '已发送订单，平台接单情况待核对'
        elif receipt or order:
            submission = ('未提交' if reason and ('未提交' in reason or '未发送' in reason)
                          else '有下单调用记录，平台接单未知')
            submission += '（' + {'rejected': '拒绝回执', 'canceled': '取消回执',
                                 'unknown': '回执未知'}.get(receipt.get('status'), '需核对') + '）'
        else:
            submission = '提交情况未知（旧记录缺失）'
        info = dict(venue=venue, planned_size=planned, requested_size=requested,
                    filled_size=filled, average_price=price, submission=submission, reason=reason,
                    status=evidence.get('status'), raw_status=evidence.get('raw_status'),
                    time_in_force=evidence.get('time_in_force'),
                    platform_reason=evidence.get('platform_reason'),
                    platform_reason_reported=evidence.get('platform_reason_reported'))
        legs.append(info)
        side = '买入' if row.get(leg + '_side') in ('long', 'buy') else '卖出'
        lines.append(f'第{index + 1}腿 {VENUE_NAMES.get(venue, venue or "未知平台")} {side}：'
                     f'计划 {display(planned)}；下单数量 {display(requested)}；'
                     f'成交 {display(filled)} @ {display(price)}；{submission}'
                     + (f'；原因：{reason}' if reason else ''))
        if evidence.get('status') or evidence.get('raw_status'):
            raw = evidence.get('raw_status') or '未保存'
            tif = evidence.get('time_in_force') or '未保存'
            lines.append(f'第{index + 1}腿平台回执：{evidence.get("status") or "未知"}；'
                         f'原始状态 {raw}；指令 {tif}')
            if evidence.get('status') in ('canceled', 'rejected'):
                lines.append('平台取消/拒绝原因：' + (evidence.get('platform_reason') or
                    ('未提供' if evidence.get('platform_reason_reported') is False else '旧记录未保存，无法确认平台是否提供')))
            if observed_order:
                lines.append('原始回执证据来源：平台只读补查（' + str(observed_order.get('_timestamp', '时间未知')) + '）')
        recovered = {}
        for event_index, (event, detail) in enumerate(events):
            if (event == 'close_receipt' and detail.get('venue') == venue
                    and detail.get('confirmed') is True and (number(detail.get('filled_size')) or 0) > 0):
                recovered[detail.get('order_id') or event_index] = detail
        info['rollback_filled_size'] = sum(float(d['filled_size']) for d in recovered.values()) if recovered else None
        if recovered:
            qty = info['rollback_filled_size']
            prices = [(number(d.get('average_price')), float(d['filled_size'])) for d in recovered.values()]
            average = sum(p * q for p, q in prices) / qty if all(p is not None for p, _ in prices) else None
            lines.append(f'第{index + 1}腿回补平仓成交：{display(qty)} @ {display(average)}')
    if not cause:
        second = next((d for e, d in reversed(events) if e == 'entry_receipt'
                       and d.get('venue') == row.get('lighter_venue')), {})
        receipt_cause = None
        if second.get('status') in ('rejected', 'canceled', 'partial') and legs[1]['filled_size'] is not None:
            state = {'rejected': '拒绝', 'canceled': '取消', 'partial': '部分成交'}[second['status']]
            receipt_cause = f'第二腿{state}回执，成交 {display(legs[1]["filled_size"])}，未形成等量对冲'
        cause = (legs[1]['reason'] or second.get('reason') or receipt_cause
                 or row.get('close_reason') or '旧记录未保存具体触发原因')
    lines.append('回补原因：' + cause)
    category = next((d.get('reason_code') for e, d in reversed(events) if d.get('reason_code')), None)
    category = row.get('rollback_category') or category
    if category in REASON_LABELS:
        lines.append('回补分类：' + REASON_LABELS[category])
    failures = decision.get('constraint_failures') or skipped.get('constraint_failures') or []
    if failures:
        lines.append('未通过条件：' + '、'.join(REASON_LABELS[c] for c in failures if c in REASON_LABELS))
    if number(decision.get('execution_window_ms')) is not None:
        lines.append('首腿提交至第二腿复核：' + display(decision['execution_window_ms'], ' ms'))
    target_return = decision.get('edge_basis', plan.get('edge_basis')) == 'target_return' or bool(row.get('entry_target'))
    lines.append(('计划预计净回归空间：' if target_return else '计划净价差：') + display(plan.get('net_edge_bps'), ' bps'))
    target = decision.get('target_context') or plan.get('target_context') or row.get('entry_target') or {}
    if target_return:
        lines.append('冻结回归目标：' + ('历史中心 ' if target.get('target_mode') == 'center' else '零目标 ')
                     + display(target.get('target_bps'), ' bps'))
    observed = number(decision.get('observed_price'))
    if observed is None:
        observed = number(skipped.get('observed_price'))
    protected = number(decision.get('protected_bound'))
    if protected is None:
        protected = number(skipped.get('protected_bound'))
    first = legs[0]['average_price']
    gross = None
    if first and observed and first > 0 and observed > 0:
        sell, buy = (first, observed) if row.get('variational_side') in ('short', 'sell') else (observed, first)
        gross = (sell - buy) / max(sell, buy) * 10000
    lines.append('第二腿决策时毛价差：' + display(gross, ' bps') + '（首腿成交价 / 次腿当时报价）')
    lines.append(('保护限价下预计净回归空间：' if target_return else '保护限价下对冲净价差：') + display(decision.get('net_edge_bps'), ' bps')
                 + '；门槛 ' + display(decision.get('hedge_min_net_bps'), ' bps'))
    if number(decision.get('observed_net_edge_bps')) is not None:
        lines.append(('当时报价下预计净回归空间：' if target_return else '当时报价下对冲净价差：') + display(decision.get('observed_net_edge_bps'), ' bps')
                     + ('（首腿实际成交价及费用 / 次腿当时报价及费率；包含目标平仓成本与资金费预算）'
                        if target_return else '（首腿实际成交价及费用 / 次腿当时报价及费率；未包含后续平仓成本）'))
    lines.append('第二腿当时报价 ' + display(observed) + '；保护限价 ' + display(protected))
    guard = price_guard(row, decision, order_evidence[1])
    if guard['reference_price'] is not None:
        lines.append('第二腿原参考价 ' + display(guard['reference_price']) +
                     '；价格预算边界 ' + display(guard['budget_bound']))
        lines.append('价格预算：原预算 ' + display(guard['budget_bps'], ' bps') +
                     '；复核时已消耗 ' + display(guard['consumed_bps'], ' bps') +
                     '；复核时剩余 ' + display(guard['remaining_budget_bps'], ' bps'))
    if guard['submitted_limit'] is not None:
        sent = bool(order_evidence[1].get('order_id') or order_evidence[1].get('submitted') is True)
        lines.append(('第二腿实际提交限价 ' if sent else '第二腿拟提交限价 ') + display(guard['submitted_limit']) +
                     '；按价格精度取整后可容许不利变动 ' + display(guard['remaining_limit_bps'], ' bps'))
    if order_evidence[1].get('quote_updated_at'):
        lines.append('第二腿所用行情时间：' + str(order_evidence[1]['quote_updated_at']))
    timings = {}
    decision_index = next((i for i in reversed(range(len(events))) if events[i][0] == 'hedge_decision'), -1)
    receipt_index = next((i for i in reversed(range(len(events))) if events[i][0] == 'entry_receipt'
                          and events[i][1].get('venue') == row.get('lighter_venue')), -1)
    if decision.get('decision') == 'hedge' and receipt_index > decision_index >= 0:
        label = VENUE_NAMES.get(row.get('lighter_venue'), row.get('lighter_venue') or '未知平台')
        send = next((d for e, d in events[decision_index + 1:receipt_index]
                     if e == 'stage_started' and d.get('stage') == label + ' 签名发送'), {})
        first = receipts[0] if receipts[0].get('confirmed') is True and (legs[0]['filled_size'] or 0) > 0 else {}
        timings = dict(first_receipt_to_second_send_ms=elapsed(first, send),
                       second_recheck_to_send_ms=elapsed(decision, send),
                       second_send_to_receipt_ms=elapsed(send, receipts[1]))
        for key, title in (('first_receipt_to_second_send_ms', '首腿确认→第二腿发送'),
                           ('second_recheck_to_send_ms', '第二腿复核→发送'),
                           ('second_send_to_receipt_ms', '第二腿发送→终态回执')):
            if timings[key] is not None:
                lines.append(title + '：' + display(timings[key], ' ms'))
    if any(value is not None for value in timings.values()):
        lines.append('耗时来自同一执行的单调时钟；旧记录按事件时间还原，包含本地记录和检查开销。')
    # New executions persist the hot-path timings directly.  Prefer these
    # measurements over reconstructing them from SQLite event timestamps,
    # while retaining the reconstruction above for older records.
    explicit_timing = (
        ('first_submit_ms', '首腿提交/终态回执'),
        ('first_receipt_processing_ms', '首腿回执处理'),
        ('first_receipt_to_second_quote_ms', '首腿确认→第二腿报价开始'),
        ('second_quote_ms', '第二腿报价读取'),
        ('second_decision_ms', '第二腿本地保护决策'),
        ('first_receipt_to_second_send_ms', '首腿确认→第二腿发送'),
        ('second_submit_ms', '第二腿提交/终态回执'),
    )
    second_receipt = receipts[1] if len(receipts) > 1 else {}
    timing_source = {key: decision.get(key) if decision.get(key) is not None
                     else second_receipt.get(key) for key, _ in explicit_timing}
    for key, title in explicit_timing:
        value = number(timing_source.get(key))
        if value is not None:
            lines.append(title + '：' + display(value, ' ms'))
    if decision.get('decision') == 'hedge':
        lines.append('最后一次对冲评估允许提交；不能据此认定后续回补由价差恶化触发。')
    if legs[1]['status'] == 'canceled' and legs[1]['filled_size'] == 0:
        lines.append('分析：价格余量和耗时可用于排查 IOC 零成交；未记录撮合瞬间盘口，不能确定是价格变化还是深度不足。')
    lines.append('回补指撤销未成对敞口；决策报价和保护限价估算不等于最终结算盈亏。未知项表示缺少历史证据。')
    return dict(legs=legs, reason=cause, reason_code=category, observed_gross_bps=gross,
                decision=decision.get('decision'), price_guard=guard, timings=timings, text='\n'.join(lines))


def attach_rollback_explanations(overview, data_dir):
    rows = [row for key in ('open_positions', 'closed_positions') for row in overview.get(key, [])
            if row.get('hedge_status') == 'single_leg_recovered' and row.get('id')]
    if not rows:
        return
    by_position, by_execution, orders = {}, {}, {}
    event_path = data_dir / 'hedge_events.sqlite'
    order_path = data_dir / 'execution_orders.sqlite'
    # Batched, read-only local reads; independent of the events page and REST limits.
    if event_path.exists():
        with sqlite3.connect(event_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            for start in range(0, len(rows), 150):
                batch = rows[start:start + 150]
                ids = [r['id'] for r in batch]
                executions = [r['execution_id'] for r in batch if r.get('execution_id')]
                # Separate indexed identifier lookups avoid evaluating JSON for
                # every historical blocked opportunity. UNION deduplicates rows
                # carrying both identifiers before preserving event order.
                queries, params = [], []
                for key, identifiers in (('position_id', ids), ('execution_id', executions)):
                    if not identifiers:
                        continue
                    expression = "json_extract(detail,'$." + key + "')"
                    queries.append('SELECT id,timestamp,event,detail FROM events WHERE json_valid(detail) AND '
                        + expression + ' IS NOT NULL AND ' + expression + ' IN ('
                        + ','.join('?' for _ in identifiers) + ') AND event IN ('
                        + ','.join('?' for _ in EVENTS) + ')')
                    params.extend((*identifiers, *EVENTS))
                query = ' UNION '.join(queries) + ' ORDER BY id'
                for event_id, timestamp, event, raw in db.execute(query, params):
                    d = json.loads(raw)
                    d.update(_timestamp=timestamp, _event_id=event_id)
                    if d.get('position_id'):
                        by_position.setdefault(d['position_id'], []).append((event, d))
                    else:
                        by_execution.setdefault(d.get('execution_id'), []).append((event, d))
    if order_path.exists():
        with sqlite3.connect(order_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            for start in range(0, len(rows), 150):
                ids = [r['id'] + suffix for r in rows[start:start + 150] for suffix in ('-v', '-l')]
                for venue, client, raw, result in db.execute(
                        'SELECT venue,client_id,intent,result FROM orders WHERE client_id IN ('
                        + ','.join('?' for _ in ids) + ')', ids):
                    intent, receipt = json.loads(raw), json.loads(result or '{}')
                    reason = safe_reason(ValueError(receipt['error'])) if receipt.get('error') else None
                    if reason and '检查未通过，详见阶段和代码位置' in reason:
                        reason = None
                    orders[(client, venue)] = {**{k: receipt.get(k) for k in (
                        'order_id', 'submitted', 'status', 'filled_size', 'raw_status', 'limit_price',
                        'platform_reason', 'platform_reason_code', 'platform_reason_reported')},
                        'size': intent.get('size'), 'price': intent.get('price'),
                        'time_in_force': receipt.get('time_in_force') or intent.get('time_in_force'),
                        'quote_updated_at': intent.get('quote_updated_at'), 'reason': reason}
    for row in rows:
        events = by_execution.get(row.get('execution_id'), []) + by_position.get(row['id'], [])
        events.sort(key=lambda item: item[1]['_event_id'])
        row['rollback_explanation'] = explain(row, events, orders)
