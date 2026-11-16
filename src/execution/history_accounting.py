"""Read-only accounting evidence. Missing fees or pagination are never zero."""
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
import json
import sqlite3

from src.execution.orders import decimal
from src.execution.history_recovery import timestamp


def arcus_pages(client, path, field, start_us, end_us, limit=1000):
    """Inclusive timestamp paging, with overlap and identity deduplication.

    A full page at one timestamp cannot prove completeness. Fail rather than
    skipping that timestamp and silently dropping fills or funding payments.
    """
    rows, seen, upper = [], set(), int(end_us)
    time_key = 'createdAt' if field == 'fills' else 'time'
    for _ in range(100):
        page = client._get(path, {'address': client.wallet_address,
                                  'accountIndex': client.account_index,
                                  'from': int(start_us), 'to': upper, 'limit': limit})
        items = page.get(field)
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError('Arcus 账单分页无效')
        for row in items:
            at = int(row[time_key])
            if not start_us <= at <= upper:
                raise ValueError('Arcus 账单时间越界')
            key = str(row['tradeId']) if field == 'fills' else (str(row['marketId']), at)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if len(items) < limit:
            return rows
        boundary = min(int(r[time_key]) for r in items)
        if boundary >= upper:
            raise ValueError('Arcus 同时刻账单超出分页上限')
        upper = boundary
    raise ValueError('Arcus 账单未完整读取')


def enrich_arcus_orders(orders, fills):
    grouped = defaultdict(list)
    for fill in fills:
        grouped[str(fill['orderId'])].append(fill)
    for order in orders:
        matches = grouped.get(order['order_id'], [])
        if not matches or any(r.get('marketDisplayName') != order['symbol'] + '-USD' or
                              str(r.get('side', '')).lower() != order['side'] for r in matches):
            continue
        size = sum(decimal(r['size'], True) for r in matches)
        if size != decimal(order['filled_size'], True):
            continue
        price = sum(decimal(r['size'], True) * decimal(r['price'], True) for r in matches) / size
        if abs(price - decimal(order['average_price'], True)) > Decimal('0.00000001') * max(price, 1):
            continue
        first, last = min(int(r['createdAt']) for r in matches), max(int(r['createdAt']) for r in matches)
        order.update(average_price=str(price), first_fill_at=timestamp(first),
                     timestamp=timestamp(last), fill_ids=[str(r['tradeId']) for r in matches])
        if any(r.get('liquidation') or r.get('isLiquidation') or r.get('isLiquidated') for r in matches):
            continue
        if all(r.get('fee') is not None for r in matches):
            order.update(fees_usdc=str(sum(decimal(r['fee']) for r in matches)),
                         fee_source='arcus_fills')
        if all(r.get('closedPnl') is not None for r in matches):
            # Arcus closedPnl already includes this fill's fee.
            order['fee_inclusive_pnl_usdc'] = str(sum(decimal(r['closedPnl']) for r in matches))


def journal_fees(path, orders):
    """Only reuse a receipt for the same venue, ID, full size and VWAP."""
    if not path.exists():
        return
    index = {(o['venue'], o['order_id']): o for o in orders}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        for venue, intent_text, result_text in db.execute('SELECT venue,intent,result FROM orders'):
            receipt = json.loads(result_text or '{}')
            order = index.get((venue, str(receipt.get('order_id') or '')))
            if not order or order.get('fees_usdc') is not None or receipt.get('fees_verified') is not True or receipt.get('fees_usdc') is None:
                continue
            intent = json.loads(intent_text)
            if (intent.get('symbol') != order['symbol'] or intent.get('side') != order['side'] or
                    decimal(receipt.get('filled_size')) != decimal(order['filled_size']) or
                    decimal(receipt.get('average_price')) != decimal(order['average_price'])):
                continue
            order.update(fees_usdc=str(decimal(receipt['fees_usdc'])),
                         fee_source=receipt.get('fee_source') or 'verified_order_receipt',
                         fee_estimated=receipt.get('fee_estimated') is True,
                         fee_basis=receipt.get('fee_basis'))


def enrich_lighter_order(order, trades, account_index, market_id):
    matches, seen = [], set()
    side = 'ask' if order['side'] == 'sell' else 'bid'
    for row in trades:
        identity = str(row['trade_id'])
        if identity in seen:
            continue
        seen.add(identity)
        if (str(row.get(side + '_id')) == order['order_id'] and
                row.get(side + '_account_id') == account_index and row.get('market_id') == market_id):
            matches.append(row)
    if not matches or sum(decimal(r['size'], True) for r in matches) != decimal(order['filled_size']):
        return
    price = sum(decimal(r['size']) * decimal(r['price'], True) for r in matches) / decimal(order['filled_size'])
    if abs(price - decimal(order['average_price'])) > Decimal('0.00000001') * max(price, 1):
        return
    stamps = [timestamp(r['timestamp']) for r in matches]
    if not all(stamps):
        return
    order.update(first_fill_at=min(stamps), timestamp=max(stamps), fill_ids=[str(r['trade_id']) for r in matches])
    # Lighter's nonzero integer fee units require a separate conversion; an
    # omitted field provides no evidence of zero fees.
    if all(all(r.get(k) == 0 for k in
               (('maker_fee', 'integrator_maker_fee') if bool(r['is_maker_ask']) == (side == 'ask')
                else ('taker_fee', 'integrator_taker_fee'))) for r in matches):
        order.update(fees_usdc='0', fee_source='lighter_trade_zero_fees')


def epoch_ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def funding_record(venue, symbol, start, end, rows, value_key, time_key):
    payments, seen = [], set()
    for row in rows:
        stamp = timestamp(row[time_key])
        if not stamp or not start <= stamp <= end:
            raise ValueError('资金费时间范围不匹配')
        key = (stamp, str(row.get('market_id', row.get('marketId', symbol))))
        if key in seen:
            raise ValueError('重复资金费记录')
        seen.add(key)
        payments.append({'timestamp': stamp, 'payment_usdc': str(decimal(row[value_key]))})
    return {'venue': venue, 'symbol': symbol, 'start': start, 'end': end,
            'complete': True, 'payments': payments}


def merge_evidence(previous, rows, complete, funding):
    """Keep proven historic facts across a partial endpoint outage."""
    refreshed = {tuple(m) for m in complete}
    old = {(r['venue'], r['order_id']): r for r in previous.get('rows', [])}
    # An exchange's retention window may shrink. Absence from a later page
    # cannot erase an immutable, previously verified historical fill.
    combined = dict(old)
    for row in rows:
        if (row['venue'], row['symbol']) not in refreshed:
            continue
        key = (row['venue'], row['order_id'])
        prior = old.get(key, {})
        if all(prior.get(k) == row.get(k) for k in ('symbol', 'side', 'filled_size', 'average_price', 'reduce_only')):
            retained_fill_time = not row.get('first_fill_at') and prior.get('first_fill_at')
            for field in ('fees_usdc', 'fee_source', 'fee_basis', 'fee_inclusive_pnl_usdc', 'first_fill_at', 'fill_ids'):
                if row.get(field) is None and prior.get(field) is not None:
                    row[field] = prior[field]
            if retained_fill_time:
                row['timestamp'] = prior['timestamp']
        combined[key] = row
    intervals = [r for r in previous.get('funding_intervals', []) if not any(
        new['venue'] == r['venue'] and new['symbol'] == r['symbol'] and
        new['start'] <= r['start'] and new['end'] >= r['end'] for new in funding)] + funding
    return {'rows': list(combined.values()), 'funding_intervals': intervals,
            'complete_markets': sorted(refreshed | {tuple(m) for m in previous.get('complete_markets', [])})}
