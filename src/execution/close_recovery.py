"""Resolve only provably unsent legacy closes during fresh account recovery."""
from contextlib import closing
import sqlite3

from src.execution.orders import decimal


def _intent_absent(path, venue, client_id):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            # Any account with this identity is conservatively ambiguous.
            return db.execute('SELECT 1 FROM orders WHERE venue=? AND client_id=? LIMIT 1',
                              (venue, client_id)).fetchone() is None
    except sqlite3.Error:
        return False


def _reset_unsent_cycle(position, journal_path, checked_at):
    """A cycle that never reached either transport consumes no send attempts."""
    results = position.get('close_order_results') or {}
    if not position.get('close_attempts') or not any(r.get('recovery_evidence') == 'submission_deadline_before_journal'
               for r in results.values()):
        return
    for leg in ('variational', 'lighter'):
        result = results.get(leg) or {}
        venue = position.get(leg + '_venue')
        if (venue not in ('arcus', 'lighter', 'lighter_rh') or
                result.get('confirmed') is not True or result.get('status') != 'rejected' or
                not result.get('client_order_id') or result.get('order_id') or
                not result.get('requested_size') or not position.get('entry_size_' + leg) or
                decimal(result.get('filled_size') or 0) != 0 or
                (position.get('close_receipt_order_ids') or {}).get(leg) or
                decimal(result.get('requested_size'), True) != decimal(position.get(leg + '_size'), True) or
                decimal(position.get('entry_size_' + leg), True) != decimal(position.get(leg + '_size'), True) or
                not _intent_absent(journal_path, venue, result['client_order_id'])):
            return
    for leg, result in results.items():
        result.update(submitted=False, unsent_attempts=(position.get('close_attempts') or {}).get(leg, 0),
                      cycle_reset_at=checked_at, previous_close_started_time=position.get('close_started_time'))
    # The original identities/evidence remain in close_order_results. A new
    # profitable exit must use new client ids and a new execution start time.
    position.update(close_attempts={}, close_started_time=None, close_reason=None)


def reconcile_unsent_closes(root, positions, checked_at):
    """Call only after all remaining positions and active orders match.

    Older Arcus adapters raised the submission deadline before reserving an
    intent. An absent durable intent plus that specific failure proves no
    transport ran. Account quantities alone cannot prove an order was unsent.
    A missing/unreadable journal, any existing intent, or another error keeps
    the close unknown. No network reads or order operations occur here.
    """
    journal_path = root / 'data/execution_orders.sqlite'
    for position in positions:
        if position.get('status') == 'closed':
            continue
        for leg in ('variational', 'lighter'):
            result = (position.get('close_order_results') or {}).get(leg) or {}
            if (position.get(leg + '_venue') != 'arcus' or
                    result.get('confirmed') is True or result.get('status') != 'unknown' or
                    result.get('error_class') != 'SubmissionWindowExpired' or
                    not result.get('client_order_id') or result.get('order_id') or
                    decimal(result.get('filled_size') or 0) != 0 or
                    (position.get('close_receipt_order_ids') or {}).get(leg) or
                    decimal(result.get('requested_size'), True) != decimal(position.get(leg + '_size'), True)):
                continue
            if _intent_absent(journal_path, 'arcus', result['client_order_id']):
                result.update(confirmed=True, status='rejected', filled_size=0, submitted=False,
                              recovery_evidence='submission_deadline_before_journal',
                              reconciled_at=checked_at)
        _reset_unsent_cycle(position, journal_path, checked_at)


def validate_resume_closes(positions):
    """A paired account match must not erase an ambiguous close intent."""
    for position in positions:
        if position.get('status') == 'closed':
            continue
        for leg in ('variational', 'lighter'):
            if decimal(position.get(leg + '_size') or 0) <= 0:
                continue
            attempts = (position.get('close_attempts') or {}).get(leg, 0)
            result = (position.get('close_order_results') or {}).get(leg) or {}
            if (attempts or result) and (result.get('confirmed') is not True or
                    result.get('status') not in ('filled', 'partial', 'canceled', 'rejected')):
                raise ValueError('上一笔平仓终态未确认，禁止恢复自动下单；请核对原平仓订单')
