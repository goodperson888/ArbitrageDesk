"""Read-only matching of actual platform fills to a persisted hedge."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import sqlite3
from pathlib import Path


def timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.replace('.', '', 1).isdigit():
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.astimezone(timezone.utc).isoformat()
        seconds = float(value)
        if seconds > 1e17:
            seconds /= 1e9
        elif seconds > 1e14:
            seconds /= 1e6
        elif seconds > 1e11:
            seconds /= 1e3
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def normalized_order(venue, symbol, row):
    """Never confuse an order's protective limit with its execution VWAP."""
    if venue == 'arcus':
        size = Decimal(str(row.get('filledSize') or 0))
        price = row.get('avgFillPrice') or row.get('averagePrice') or row.get('avgPrice')
        order_id, side = row.get('orderId'), str(row.get('side') or '').lower()
        stamp = timestamp(row.get('updatedAt') or row.get('createdAt'))
        client_id = row.get('clientId')
        reduce_only = row.get('reduceOnly') is True
    else:
        size = Decimal(str(row.get('filled_base_amount') or 0))
        quote = Decimal(str(row.get('filled_quote_amount') or 0))
        price = str(quote / size) if size > 0 and quote > 0 else None
        order_id = row.get('order_id') or row.get('order_index')
        side = 'sell' if row.get('is_ask') is True else 'buy' if row.get('is_ask') is False else ''
        stamp = timestamp(row.get('transaction_time') or row.get('updated_at') or row.get('timestamp'))
        client_id = row.get('client_order_id')
        reduce_only = row.get('reduce_only') is True
    if not order_id or not size.is_finite() or size <= 0 or not price or side not in ('buy', 'sell'):
        return None
    price = Decimal(str(price))
    if not price.is_finite() or price <= 0:
        return None
    return {'venue': venue, 'symbol': symbol, 'order_id': str(order_id),
            'client_order_id': str(client_id) if client_id is not None else None,
            'filled_size': str(size), 'average_price': str(price), 'side': side,
            'reduce_only': reduce_only, 'timestamp': stamp,
            'status': str(row.get('status') or '').lower()}


def recover_leg(position, leg, orders):
    venue, symbol = position.get(leg + '_venue'), position['symbol']
    quantity = Decimal(str(position.get('entry_size_' + leg) or 0))
    if quantity <= 0:
        return None
    raw_side = position.get(leg + '_side')
    if raw_side not in ('long', 'short', 'buy', 'sell'):
        return None
    side = 'buy' if raw_side in ('long', 'buy') else 'sell'
    rows = [r for r in orders if r['venue'] == venue and r['symbol'] == symbol and r.get('timestamp')]
    identities = set(map(str, position.get('entry_order_ids', [])))
    if position.get('entry_order_ids_by_venue') or position.get('grid_cycle_id'):
        identities = set(map(str, (position.get('entry_order_ids_by_venue') or {}).get(venue, [])))
    entries = [r for r in rows if not r['reduce_only'] and r['side'] == side and
               (r['order_id'] in identities or str(r.get('client_order_id') or '').startswith(position['id'] + '-'))]
    if not entries or sum(Decimal(r['filled_size']) for r in entries) != quantity:
        return None
    start = max(r['timestamp'] for r in entries)
    later_entries = [r['timestamp'] for r in rows if not r['reduce_only'] and r['timestamp'] > start]
    end = min(later_entries) if later_entries else None
    close_ids = set(map(str, (position.get('close_order_ids_by_venue') or {}).get(venue, [])))
    client_prefix = ''.join(ch for ch in str(position['id']) if ch.isalnum())[:16] + '-c-' + venue.replace('_','')[:3] + '-'
    linked_closes = [r for r in rows if r['reduce_only'] and r['side'] != side and r['timestamp'] >= start and
                     (r['order_id'] in close_ids or str(r.get('client_order_id') or '').startswith(client_prefix))]
    explicitly_matched = bool(linked_closes) and sum(Decimal(r['filled_size']) for r in linked_closes) == quantity
    if position.get('grid_cycle_id'):
        candidates = linked_closes
    elif explicitly_matched:
        candidates = linked_closes
    else:
        candidates = [r for r in rows if r['reduce_only'] and r['side'] != side and
                      r['timestamp'] > start and (not end or r['timestamp'] < end)]
    # Full quantity must reconcile without choosing a convenient subset.
    # Extra closes/overlapping openings therefore stay unpaired for review.
    if not candidates or sum(Decimal(r['filled_size']) for r in candidates) != quantity:
        return None
    price = sum(Decimal(r['filled_size']) * Decimal(r['average_price']) for r in candidates) / quantity
    entry_price = sum(Decimal(r['filled_size']) * Decimal(r['average_price']) for r in entries) / quantity
    all_orders = entries + candidates
    fees_known = all(r.get('fees_usdc') is not None and r.get('fee_estimated') is not True and
                     r.get('fee_source') != 'account_rate_estimate' for r in all_orders)
    fees = sum(Decimal(r['fees_usdc']) for r in all_orders) if fees_known else None
    gross = (price - entry_price) * quantity * (1 if side == 'buy' else -1)
    # Cross-check Arcus's fee-inclusive closedPnl against fill prices. This
    # also rejects a different cost basis from an overlapping account position.
    if not position.get('grid_cycle_id') and fees_known and all(r.get('fee_inclusive_pnl_usdc') is not None for r in all_orders):
        reported = sum(Decimal(r['fee_inclusive_pnl_usdc']) for r in all_orders)
        if abs(reported - (gross - fees)) > Decimal('0.00000002'):
            fees_known, fees = False, None
    return {'average_price': float(price), 'filled_size': float(quantity),
            'order_ids': [r['order_id'] for r in candidates],
            'entry_order_ids': [r['order_id'] for r in entries],
            'entry_timestamp': min(r.get('first_fill_at') or r['timestamp'] for r in entries),
            'fill_times_verified': all(r.get('first_fill_at') for r in all_orders),
            'close_time_verified': all(r.get('first_fill_at') for r in candidates),
            'terminal': all(r.get('status') in ('filled', 'canceled', 'cancelled', 'rejected', 'expired') for r in all_orders),
            'identity_matched': explicitly_matched,
            'gross_price_pnl_usdc': str(gross),
            'fees_usdc': str(fees) if fees_known else None,
            'missing_fees': [('开仓' if r in entries else '平仓') + '手续费（订单 ' + r['order_id'] + '）'
                             for r in all_orders if r.get('fees_usdc') is None or
                             r.get('fee_estimated') is True or r.get('fee_source') == 'account_rate_estimate'] or
                            ([] if fees_known else ['成交账单与成本基准不一致']),
            'timestamp': max(r['timestamp'] for r in candidates), 'source': 'platform_order_history'}


def settle_history(position, recovered, funding):
    gross, known_fees, known_funding = Decimal(0), Decimal(0), Decimal(0)
    missing, legs = [], {}
    fee_venues, funding_venues = [], []
    for leg in ('variational', 'lighter'):
        venue = position.get(leg + '_venue')
        if not Decimal(str(position.get('entry_size_' + leg) or 0)):
            continue
        fill = recovered.get(leg)
        if not fill:
            missing.append(str(venue) + ' 开平仓成交尚未完整匹配')
            continue
        gross += Decimal(fill['gross_price_pnl_usdc'])
        fees = fill['fees_usdc']
        if fees is None:
            fee_venues.append(venue)
            missing.extend(str(venue) + ' ' + note for note in fill['missing_fees'])
        else:
            known_fees += Decimal(fees)
        interval = next((r for r in funding if fill['fill_times_verified'] and r.get('complete') is True and r['venue'] == venue and
                         r['symbol'] == position['symbol'] and r['start'] <= fill['entry_timestamp'] and
                         r['end'] >= fill['timestamp']), None)
        paid = None
        allocated = (position.get('funding_settlements') or {}).get(leg, {})
        if position.get('grid_cycle_id') and allocated.get('allocation') in ('layer_quantity_at_settlement', 'order_receipt'):
            paid = Decimal(str(allocated['funding_usdc']))
            known_funding += paid
        elif position.get('grid_cycle_id') or interval is None:
            funding_venues.append(venue)
            missing.append(str(venue) + ' 持仓期间资金费未完整读取')
        else:
            paid = sum((Decimal(r['payment_usdc']) for r in interval['payments']
                        if fill['entry_timestamp'] <= r['timestamp'] <= fill['timestamp']), Decimal(0))
            known_funding += paid
        legs[leg] = {'venue': venue, 'gross_price_pnl_usdc': float(Decimal(fill['gross_price_pnl_usdc'])),
                     'fees_usdc': float(Decimal(fees)) if fees is not None else None,
                     'funding_usdc': float(paid) if paid is not None else None}
    # A partial subtotal is useful evidence, but must never enter net totals.
    complete_fills = all(not position.get('entry_size_' + leg) or leg in recovered
                         for leg in ('variational', 'lighter'))
    position.update(accounting_pending=bool(missing), pending_fee_venues=fee_venues,
                    pending_funding_venues=funding_venues, accounting_legs=legs,
                    accounting_missing=missing, accounting_note='；'.join(missing) if missing else
                    '按实际开平仓成交、已核验手续费及各腿实际持仓期间资金费核算',
                    gross_price_pnl_usdc=float(gross) if complete_fills else None,
                    known_fees_usdc=float(known_fees), known_funding_usdc=float(known_funding),
                    accounting_subtotal_usdc=float(gross - known_fees + known_funding) if complete_fills else None,
                    realized_pnl_usdc=float(gross - known_fees + known_funding) if not missing else None)


def apply_close_history(book, data_dir):
    try:
        cache = json.loads((Path(data_dir) / 'platform-order-history.json').read_text())
    except (OSError, ValueError):
        cache = {}
    orders = cache.get('rows', [])
    from src.execution.history_receipts import receipt_evidence, merge_receipts
    try:
        orders = merge_receipts(orders, receipt_evidence(data_dir))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        pass  # A damaged receipt archive cannot invalidate platform evidence.
    complete = {tuple(item) for item in cache.get('complete_markets', [])}
    promoted = []
    for position in book.get('closed_positions', []) + book.get('open_positions', []):
        was_open = position.get('status') != 'closed' and position in book.get('open_positions', [])
        recovered = {}
        accounting = {}
        fill_times = {}
        changed_fills = False
        for leg in ('variational', 'lighter'):
            fill = recover_leg(position, leg, orders)
            if fill and (position.get(leg + '_venue'), position['symbol']) not in complete and not fill['identity_matched']:
                fill = None
            if fill:
                changed_fills |= position.get('close_price_' + leg) != fill['average_price']
                fill_times[leg] = fill['timestamp']
                accounting[leg] = fill
            if fill:
                position['close_price_' + leg] = fill['average_price']
                position['close_order_ids'] = list(dict.fromkeys(position.get('close_order_ids', []) + fill['order_ids']))
                recovered[leg] = fill
        if not accounting:
            continue
        position['recovered_close_fills'] = recovered
        position['close_fill_times'] = fill_times
        position['close_fill_time_sources'] = {leg: 'exchange_fill' if fill['close_time_verified'] else 'receipt_received'
                                               for leg, fill in accounting.items()}
        active_legs = [leg for leg in ('variational', 'lighter') if position.get('entry_size_' + leg)]
        fully_matched = bool(active_legs) and all(leg in accounting and accounting[leg]['terminal'] for leg in active_legs)
        position['platform_history_checked_at'] = cache.get('checked_at')
        position['platform_history_status'] = 'matched' if fully_matched else 'partial'
        if was_open and not fully_matched:
            # One leg's matched history cannot prove the other leg closed.
            continue
        if fully_matched and not position.get('closed_time'):
            position['closed_time'] = datetime.fromisoformat(max(fill_times.values())).astimezone().isoformat()
        if was_open:
            position.update(local_status=position['status'], status='closed', partially_closed=False,
                            history_recovered=True, local_state_requires_reconcile=True,
                            variational_size=0.0, lighter_size=0.0,
                            unrealized_pnl_usdc=None, current_spread_bps=None)
            position['close_reason'] = (position.get('close_reason') or '平台成交补齐') + '；已按平台成交补齐（含手动平仓）'
            promoted.append(position)
        if position.get('closed_time') and position.get('close_started_time'):
            start, end = timestamp(position['close_started_time']), timestamp(position['closed_time'])
            if start and end and end >= start:
                position['close_elapsed_ms'] = round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)
                position['close_elapsed_source'] = 'exit_started_to_final_fill'
        candidate = dict(position)
        settle_history(candidate, accounting, cache.get('funding_intervals', []))
        # A refresh for one pair can also return orders belonging to another
        # pair on the shared venue. Partial new evidence must not erase that
        # other pair's already settled ledger.
        needs_settlement = bool(changed_fills or position.get('accounting_pending') or
                                position.get('pending_fee_venues') or position.get('pending_funding_venues') or
                                position.get('realized_pnl_usdc') is None)
        if needs_settlement or not candidate['accounting_pending']:
            position.update(candidate)
        a, b = position.get('close_price_variational'), position.get('close_price_lighter')
        if all(isinstance(p, (int, float)) and math.isfinite(p) and p > 0 for p in (a, b)):
            buy, sell = (a, b) if position['variational_side'] in ('long', 'buy') else (b, a)
            position['close_spread_bps'] = (sell - buy) / max(buy, sell) * 10000
    for position in promoted:
        book['open_positions'].remove(position)
        book.setdefault('closed_positions', []).append(position)
