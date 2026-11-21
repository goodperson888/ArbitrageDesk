"""Small, optional accounting reads; never acquire the trading runtime lock."""
import json
import sqlite3
from pathlib import Path

from src.execution.history_recovery import recover_leg
from src.execution.position_monitor import read_position_monitor


POSITION_FIELDS = ('id', 'symbol', 'status', 'accounting_pending', 'variational_venue',
    'lighter_venue', 'variational_side', 'lighter_side', 'entry_size_variational',
    'entry_size_lighter', 'entry_order_ids', 'entry_order_ids_by_venue',
    'close_order_ids_by_venue', 'grid_cycle_id', 'funding_settlements')


def read_allowed(root):
    """Fail closed on stale monitoring; recheck before each optional request."""
    root = Path(root)
    try:
        path = root / 'data/console_execution.json'
        if not path.exists():
            return True
        state = json.loads(path.read_text())
        if state.get('state') in ('finished', 'failed', 'idle'):
            return True
        if state.get('state') != 'running':
            return False
        venues = state.get('venues') or []
        if len(venues) < 2 or any(v not in ('lighter', 'lighter_rh', 'arcus') for v in venues):
            return False
        monitor = read_position_monitor(root / ('data/hedge_state_' + '_'.join(venues) + '.monitor.json'), state.get('pid'))
        if (not monitor or monitor.get('heartbeat_stale') or monitor.get('stop_reason') or
                any(row.get('phase') == 'closing' or row.get('position_status') != 'open'
                    for row in monitor.get('positions', []))):
            return False
        journal = root / 'data/execution_orders.sqlite'
        if not journal.exists():
            return False
        with sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1) as db:
            for status, result in db.execute("SELECT state,result FROM orders WHERE state IN ('submitting','unknown') LIMIT 20"):
                if status == 'submitting' or json.loads(result or '{}').get('confirmed') is not True:
                    return False
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def fee_known(row):
    return (row.get('fees_usdc') is not None and row.get('fee_estimated') is not True and
            row.get('fee_source') != 'account_rate_estimate')


def pending_jobs(position, cache):
    """Plan only missing evidence for fully matched, closed legs."""
    if position.get('status') != 'closed' or not position.get('accounting_pending'):
        return []
    orders = cache.get('rows', [])
    index = {(r['venue'], r['order_id']): r for r in orders}
    complete = {tuple(item) for item in cache.get('complete_markets', [])}
    jobs = []
    for leg in ('variational', 'lighter'):
        venue = position.get(leg + '_venue')
        if venue not in ('lighter', 'lighter_rh', 'arcus'):
            continue
        fill = recover_leg(position, leg, orders)
        if not fill or not fill['terminal'] or ((venue, position['symbol']) not in complete and not fill['identity_matched']):
            continue
        for order_id in fill['entry_order_ids'] + fill['order_ids']:
            row = index[venue, order_id]
            if not fee_known(row) or not row.get('first_fill_at'):
                jobs.append(dict(kind='fills', venue=venue, symbol=position['symbol'], order=row))
        covered = any(r.get('complete') is True and r.get('venue') == venue and
            r.get('symbol') == position['symbol'] and r['start'] <= fill['entry_timestamp'] and
            r['end'] >= fill['timestamp'] for r in cache.get('funding_intervals', []))
        if fill['fill_times_verified'] and not covered and not position.get('grid_cycle_id'):
            jobs.append(dict(kind='funding', venue=venue, symbol=position['symbol'],
                             start=fill['entry_timestamp'], end=fill['timestamp']))
    return jobs
