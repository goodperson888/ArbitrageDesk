"""Read-only aggregation of persisted executions and unmatched fill receipts."""
import json
import math
import sqlite3
from itertools import permutations
from datetime import datetime


VENUES = ('variational', 'lighter', 'lighter_rh', 'standx', 'arcus')
MULTI_SCOPE_FILES = {f"hedge_state_{'_'.join(scope)}.json": scope
                     for size in range(3, len(VENUES)+1) for scope in permutations(VENUES, size)}


def pair_sources(data_dir, selected=None):
    """Use known venue names, including underscores, instead of splitting filenames."""
    for first in VENUES:
        for second in VENUES:
            if first == second or (selected and set(selected) != {first, second}):
                continue
            path = data_dir / f'hedge_state_{first}_{second}.json'
            if path.is_file():
                yield path, [first, second]
    for path in sorted(data_dir.glob('hedge_state_*.json')):
        scope = MULTI_SCOPE_FILES.get(path.name)
        if not scope:
            continue
        try:
            rows = json.loads(path.read_text()).get('positions', [])
            pairs = set()
            for row in rows:
                pair = (row.get('variational_venue'), row.get('lighter_venue'))
                if (all(venue in scope for venue in pair) and pair[0] != pair[1] and
                        (not selected or set(pair) == set(selected))):
                    pairs.add(pair)
            for pair in sorted(pairs):
                yield path, list(pair)
        except (OSError, ValueError, TypeError, AttributeError):
            continue


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def merge_books(books, today=None):
    today = today or datetime.now().astimezone().date()
    result = {'open_positions': [], 'closed_positions': []}
    seen = set()
    for book, venues, source in books:
        for collection in result:
            for original in book.get(collection, []):
                row = dict(original)
                identity = row.get('id')
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                row['variational_venue'] = row.get('variational_venue') or venues[0]
                row['lighter_venue'] = row.get('lighter_venue') or venues[1]
                row['history_source'] = source
                # A local flat reconciliation proves no remaining exposure;
                # it cannot turn missing close fills into settled profit.
                missing = []
                if collection == 'closed_positions':
                    for leg in ('variational', 'lighter'):
                        if (row.get('entry_size_' + leg) or 0) > 0:
                            price = row.get('close_price_' + leg)
                            if not number(price) or price <= 0:
                                missing.append(row[leg + '_venue'] + ' 平仓成交价缺失')
                    if not number(row.get('realized_pnl_usdc')):
                        missing.append('最终盈亏缺失')
                pending = bool(missing or row.get('accounting_pending') or
                               row.get('pending_fee_venues') or row.get('pending_funding_venues'))
                row['accounting_pending'] = pending
                row['accounting_note'] = '；'.join(filter(None, [row.get('accounting_note'), *missing]))
                if collection == 'closed_positions' and pending:
                    row['realized_pnl_usdc'] = None
                result[collection].append(row)
    for collection in result:
        result[collection].sort(key=lambda p: p.get('closed_time') or p.get('open_time') or '', reverse=True)
    opened, closed = result['open_positions'], result['closed_positions']
    daily = []
    for row in closed:
        try:
            stamp = datetime.fromisoformat(row.get('closed_time') or row.get('open_time'))
            if stamp.tzinfo:
                stamp = stamp.astimezone()
            if stamp.date() == today:
                daily.append(row)
        except (TypeError, ValueError):
            pass
    known_daily = sum(p['realized_pnl_usdc'] for p in daily if number(p.get('realized_pnl_usdc')))
    daily_missing = sum(not number(p.get('realized_pnl_usdc')) for p in daily)
    realized = None if daily_missing else known_daily
    mark_complete = all(number(p.get('unrealized_pnl_usdc')) for p in opened)
    unrealized = sum(p['unrealized_pnl_usdc'] for p in opened) if mark_complete else None
    result['summary'] = {
        'open_count': len(opened), 'closed_count': len(closed),
        'paired_closed_count': sum(p.get('hedge_status') == 'paired' for p in closed),
        'single_leg_recovered_count': sum(p.get('hedge_status') == 'single_leg_recovered' for p in closed),
        'pending_accounting_count': sum(p['accounting_pending'] for p in opened + closed),
        'realized_pnl_usdc': realized, 'known_daily_realized_pnl_usdc': known_daily,
        'daily_pending_count': daily_missing,
        'estimated_unrealized_pnl_usdc': unrealized,
        'estimated_total_pnl_usdc': realized + unrealized if realized is not None and unrealized is not None else None,
        'realized_position_rows': sum(p['realized_pnl_usdc'] for p in closed if number(p.get('realized_pnl_usdc'))),
        'mark_complete': mark_complete,
    }
    cycles = {}
    for row in opened + closed:
        cycle_id = row.get('grid_cycle_id')
        if cycle_id:
            cycles.setdefault(cycle_id, []).append(row)
    result['grid_cycles'] = []
    for cycle_id, rows in cycles.items():
        live = [p for p in rows if p in opened]
        done = [p for p in rows if p in closed]
        realized = (sum(p['realized_pnl_usdc'] for p in done)
                    if all(number(p.get('realized_pnl_usdc')) for p in done) else None)
        unrealized = (sum(p['unrealized_pnl_usdc'] for p in live)
                      if all(number(p.get('unrealized_pnl_usdc')) for p in live) else None)
        result['grid_cycles'].append({'id':cycle_id, 'symbol':rows[0]['symbol'],
            'venues':[rows[0]['variational_venue'],rows[0]['lighter_venue']],
            'active_layers':len(live),'used_layers':len(rows),
            'realized_pnl_usdc':realized,'estimated_unrealized_pnl_usdc':unrealized,
            'estimated_total_pnl_usdc':realized+unrealized if realized is not None and unrealized is not None else None})
    return result


def unmatched_fills(data_dir, books, venues=None):
    """Journal fills are orders, never fabricated pairs or settled round trips."""
    linked = set()
    for book, _, _ in books:
        for row in book.get('open_positions', []) + book.get('closed_positions', []):
            for field in ('entry_order_ids', 'close_order_ids'):
                linked.update((venue, str(value)) for value in row.get(field, [])
                              for venue in (row.get('variational_venue'), row.get('lighter_venue')) if venue)
    times = {}
    try:
        report = json.loads((data_dir / 'arcus-round-trip-test.json').read_text())
        for key, stamp in [('open', 'started_at'), ('close', 'finished_at')]:
            receipt = report.get(key) or {}
            if receipt.get('order_id'):
                times[('arcus', str(receipt['order_id']))] = report.get(stamp)
    except (OSError, ValueError, TypeError):
        pass
    path = data_dir / 'execution_orders.sqlite'
    if not path.exists():
        return []
    rows = []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        for seq, venue, client_id, intent_text, receipt_text in db.execute(
                'SELECT seq,venue,client_id,intent,result FROM orders ORDER BY seq DESC'):
            if venues and venue not in venues:
                continue
            intent, receipt = json.loads(intent_text), json.loads(receipt_text or '{}')
            order_id = str(receipt.get('order_id') or '')
            filled = receipt.get('filled_size')
            if not number(filled) or filled <= 0 or (venue, order_id) in linked:
                continue
            # Open positions may not have receipt ids in their overview yet.
            if any(client_id.startswith(str(p.get('id')) + '-') for book, _, _ in books
                   for p in book.get('open_positions', []) if p.get('id')):
                continue
            rows.append({'seq': seq, 'venue': venue, 'symbol': intent.get('symbol'),
                         'side': intent.get('side'), 'reduce_only': intent.get('reduce_only', False),
                         'status': receipt.get('status'), 'filled_size': filled,
                         'average_price': receipt.get('average_price'), 'fees_usdc': receipt.get('fees_usdc'),
                         'order_id': order_id or None, 'timestamp': times.get((venue, order_id)),
                         'source': 'arcus_round_trip' if client_id.startswith(('arcus-open-', 'arcus-close-')) else 'order_journal'})
    return rows
