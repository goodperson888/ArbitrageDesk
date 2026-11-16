"""Reuse durable, confirmed fills when an account history endpoint is down."""
import json
import math
import sqlite3
from pathlib import Path

from src.execution.history_recovery import timestamp


def historical_market_id(path, venue, account, symbol):
    """Use the original signed market identity, scoped to this account/deployment."""
    if not Path(path).exists():
        return None
    ids = set()
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        for (text,) in db.execute('SELECT intent FROM orders WHERE venue=? AND account=?', (venue, str(account))):
            intent = json.loads(text)
            value = intent.get('market_index')
            if intent.get('symbol') == symbol and type(value) is int and value >= 0:
                ids.add(value)
    if len(ids) > 1:
        raise ValueError('历史品种对应多个市场编号，需分别核对')
    return next(iter(ids)) if ids else None


def receipt_evidence(data_dir):
    root = Path(data_dir)
    journal, events = root / 'execution_orders.sqlite', root / 'hedge_events.sqlite'
    if not journal.exists() or not events.exists():
        return []
    observed = {}
    with sqlite3.connect(events.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        for at, detail in db.execute("SELECT timestamp,detail FROM events WHERE event IN ('entry_receipt','close_receipt') ORDER BY id"):
            row = json.loads(detail)
            key = (row.get('venue'), str(row.get('order_id') or ''))
            if row.get('position_id') and row.get('filled_size', 0) > 0:
                observed.setdefault(key, timestamp(at))
    result = []
    with sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        for venue, client_id, intent_text, receipt_text in db.execute('SELECT venue,client_id,intent,result FROM orders'):
            intent, receipt = json.loads(intent_text), json.loads(receipt_text or '{}')
            at = observed.get((venue, str(receipt.get('order_id') or '')))
            if not at or receipt.get('confirmed') is not True or receipt.get('status') not in ('filled', 'partial', 'canceled'):
                continue
            try:
                size, price = float(receipt['filled_size']), float(receipt['average_price'])
                if not all(math.isfinite(value) and value > 0 for value in (size, price)):
                    continue
                side = intent['side']
                if side not in ('buy', 'sell'):
                    continue
                row = dict(venue=venue, symbol=intent['symbol'], order_id=str(receipt['order_id']),
                           client_order_id=client_id, side=side, filled_size=str(size),
                           average_price=str(price), reduce_only=intent.get('reduce_only') is True,
                           timestamp=at, status='canceled' if receipt['status'] == 'partial' else receipt['status'],
                           source='confirmed_order_receipt')
                if receipt.get('fees_verified') is True:
                    try:
                        fee = float(receipt['fees_usdc'])
                        if math.isfinite(fee):
                            row.update(fees_usdc=str(fee),
                                fee_source=receipt.get('fee_source') or 'verified_order_receipt',
                                fee_estimated=receipt.get('fee_estimated') is True)
                    except (KeyError, TypeError, ValueError):
                        pass
                # Receipt arrival is NOT the exchange fill time. Do not mark
                # first_fill_at verified or settle a funding window from it.
                result.append(row)
            except (KeyError, TypeError, ValueError):
                continue
    return result


def merge_receipts(orders, receipts):
    merged = {(row['venue'], row['order_id']): dict(row) for row in orders}
    for row in receipts:
        key = (row['venue'], row['order_id'])
        prior = merged.get(key)
        if prior is None:
            merged[key] = row
        elif all(str(prior.get(field)) == str(row.get(field)) for field in ('symbol', 'side', 'reduce_only')) and all(
                float(prior[field]) == float(row[field]) for field in ('filled_size', 'average_price')):
            if prior.get('fees_usdc') is None and row.get('fees_usdc') is not None:
                prior.update(fees_usdc=row['fees_usdc'], fee_source=row['fee_source'])
    return list(merged.values())
