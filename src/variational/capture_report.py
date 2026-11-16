"""Read-only evidence report. Historical observations never authorize execution."""
import json
from collections import Counter
from pathlib import Path
from src.common.market_data import age_seconds
from decimal import Decimal, InvalidOperation


def matched_fill(request, quote, orders, trades):
    """Evidence check against the observed request→RFQ→order/trade contract."""
    if not quote or len(orders) != 1 or not trades:
        return False
    order = orders[0]
    try:
        size = Decimal(str(quote['qty']))
        if not size.is_finite() or size <= 0 or Decimal(str(order['qty'])) != size:
            return False
        if order.get('status') != 'cleared' or order.get('clearing_status') != 'success_trades_booked_into_pool':
            return False
        if order.get('symbol') != quote.get('symbol') or order.get('side') != request.get('side'):
            return False
        if not all(order.get(k) for k in ('company_digest','pool_location_digest')):
            return False
        total, notional = Decimal(0), Decimal(0)
        for trade in trades:
            if any(trade.get(k) != order.get(k) for k in ('company_digest','pool_location_digest','symbol','side')):
                return False
            if not trade.get('source_quote_digest') or trade.get('status') != 'confirmed':
                return False
            quantity, price = Decimal(str(trade['qty'])), Decimal(str(trade['price']))
            if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
                return False
            total += quantity; notional += quantity * price
        return total == size and notional / total == Decimal(str(order['price']))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def read_events(path, max_bytes=1_000_000):
    path = Path(path)
    if not path.exists():
        return [], False, 0
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        start = max(0, size - max_bytes)
        stream.seek(start)
        if start:
            stream.readline()  # Ignore a partial first line.
        lines = stream.read(max_bytes).splitlines()
    events, invalid = [], 0
    for line in lines:
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError()
            events.append(event)
        except (ValueError, UnicodeError):
            invalid += 1
    return events, start > 0, invalid


def report(path):
    events, truncated, invalid = read_events(path)
    requests, responses, orders, trades, quotes = {}, {}, {}, {}, {}
    settlements = {}
    evidence = {}
    latest_position = None
    for event in events:
        capture = event.get('capture_id')
        kind = event.get('kind')
        if kind == 'quote_response' and event.get('quote_id_digest'):
            quotes[event['quote_id_digest']] = event
        if capture and kind == 'submit_request':
            requests[capture] = event
        if capture and kind == 'submit_response':
            responses[capture] = event
        if kind not in ('snapshot_response', 'settlement_response') or not 200 <= event.get('http_status', 0) < 300:
            continue
        if event.get('endpoint') == '/api/positions' and event.get('schema_version') == 2:
            latest_position = event
        for row in event.get('rows', []):
            if not isinstance(row, dict):
                continue
            order_id = row.get('order_id_digest')
            trade_id = row.get('id_digest')
            if event.get('endpoint') == '/api/orders/v2' and order_id:
                orders[order_id] = row
            if event.get('endpoint') == '/api/trades' and trade_id:
                trades[trade_id] = row
            if event.get('endpoint') == '/api/transfers':
                identity = row.get('id_digest') or row.get('rfq_id_digest') or str(len(settlements))
                settlements[identity] = {**row, 'observed_at': event.get('observed_at'),
                                         'complete': event.get('complete') is True}
            if row.get('status') in ('cleared', 'confirmed'):
                # Legacy rows lack identities. Deduplicate display observations only;
                # never count them as distinct orders or sum their filled quantities.
                key = (event.get('endpoint'), row.get('symbol'), row.get('side'), row.get('qty'), row.get('price'))
                evidence[key] = {k: row.get(k) for k in ('symbol', 'side', 'qty', 'price', 'status', 'clearing_status')}
                evidence[key]['observed_at'] = event.get('observed_at')
                evidence[key]['identity_available'] = bool(order_id or trade_id)
    chains = []
    for capture, request in requests.items():
        response = responses.get(capture, {})
        rfq, quote = response.get('rfq_id_digest'), request.get('quote_id_digest')
        matched_orders = [row for row in orders.values() if rfq and row.get('rfq_id_digest') == rfq]
        matched_trades = [row for row in trades.values() if rfq and quote and
                          row.get('source_rfq_digest') == rfq]
        confirmed = [row for row in matched_trades if row.get('status') == 'confirmed']
        # This is linkage evidence, not a receipt: account binding, amount, side,
        # terminal quantities and completeness remain the execution reader's job.
        chains.append({'observed_at': request.get('observed_at'), 'side': request.get('side'),
                       'reduce_only': request.get('is_reduce_only'), 'rfq_received': bool(rfq),
                       'orders_linked': len(matched_orders), 'trades_linked': len(confirmed),
                       'quote_observed': quote in quotes,
                       'correlation': '提交报价 → 提交响应 RFQ → 订单/成交 RFQ；成交 source_quote 为独立编号',
                       'linked': bool(200 <= response.get('http_status', 0) < 300 and
                                      matched_fill(request, quotes.get(quote), matched_orders, matched_trades))})
    position_age = age_seconds(latest_position.get('observed_at')) if latest_position else None
    position_valid = bool(latest_position and latest_position.get('complete') is True and
                          position_age is not None and 0 <= position_age <= 10)
    latest = events[-1].get('observed_at') if events else None
    warnings = []
    if events and not any(e.get('schema_version') == 2 for e in events):
        warnings.append('已有录制为旧版：订单关联编号缺失，空持仓记录不能证明账户已平仓。更新扩展并重新配对、刷新官网即可继续采集。')
    if not any(c['linked'] for c in chains):
        warnings.append('尚无报价 → 提交 → 订单 → 成交的完整编号关联证据。')
    if not position_valid:
        warnings.append('当前持仓未知：需要新鲜且完整的持仓响应；历史平仓不代表当前空仓。')
    settlement_rows = list(settlements.values())[-20:]
    confirmed_trade_rfqs = {(row.get('company_digest'), row.get('source_rfq_digest')) for row in trades.values()
                            if row.get('status') == 'confirmed' and row.get('source_rfq_digest') and row.get('company_digest')}
    for row in settlement_rows:
        rfq = row.get('rfq_id_digest') or row.get('source_rfq_digest')
        row['linked_to_confirmed_trade'] = bool(row.get('status') == 'confirmed' and rfq and
            (row.get('company_digest'), rfq) in confirmed_trade_rfqs)
        if row['linked_to_confirmed_trade'] and row.get('asset') == 'USDC' and row.get('qty') is not None:
            kind = row.get('transfer_type')
            if kind in ('realized_pnl', 'funding'):
                row[kind+'_usdc'] = row['qty']
    has_settlement_values = any(any(row.get(key) is not None for key in
                                    ('fee', 'fee_amount', 'funding_fee', 'funding_payment',
                                     'funding_amount', 'settled_funding')) for row in settlement_rows)
    if not has_settlement_values:
        warnings.append('未捕获到可核对的已结算手续费/资金费字段；毛价差不能视为净收益。')
    return {'window_events': len(events), 'window_truncated': truncated, 'invalid_lines': invalid,
            'latest_at': latest, 'age_seconds': age_seconds(latest),
            'endpoints': dict(Counter(e.get('endpoint') for e in events if e.get('endpoint'))),
            'evidence': list(evidence.values())[-12:], 'chains': chains[-12:],
            'settlements': settlement_rows, 'settlement_values_observed': has_settlement_values,
            'position': {'known': position_valid, 'observed_at': latest_position.get('observed_at') if latest_position else None,
                         'rows': latest_position.get('rows', []) if position_valid else []},
            'warnings': warnings, 'live_verified': False,
            'note': '手动观测证据，不是本程序双腿执行回执；旧版相同价格数量的观测仅合并展示，不统计成交次数或盈亏。'}
