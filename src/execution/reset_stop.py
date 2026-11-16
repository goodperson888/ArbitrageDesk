"""Explicit stop reset; preserve unresolved trading state and concurrent stops."""
import json
import sqlite3
from src.common import file_lock as fcntl
from contextlib import contextmanager
from pathlib import Path
from src.execution.journal import execution_lease
from src.common.market_data import utc_now


@contextmanager
def stop_guard(root):
    path=Path(root)/'data/stop-control.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def reconcile_unknown_orders(root, venues):
    """Close only ambiguous journal rows after a fresh flat-account check.

    A row with ``unknown`` state means the venue response did not include
    complete accounting evidence (usually fees).  It must never be retried,
    but once the selected accounts have been freshly observed flat with no
    active orders it is safe to mark the local intent as reconciled.  The
    original receipt is retained for audit; no order is cancelled or sent.
    """
    names = tuple(dict.fromkeys(str(v).strip() for v in (venues or []) if str(v).strip()))
    if not names:
        return 0
    journal = Path(root) / 'data/execution_orders.sqlite'
    if not journal.exists():
        return 0
    changed = 0
    with sqlite3.connect(str(journal), timeout=10) as db:
        rows = db.execute(
            "SELECT seq, result FROM orders WHERE venue IN ({}) AND state='unknown'".format(
                ','.join('?' for _ in names)), names).fetchall()
        for seq, raw in rows:
            try:
                result = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                result = {}
            result.update({
                'reconciled': True,
                'reconciled_at': utc_now(),
                'reconciliation_note': 'fresh selected venue account snapshots were flat with no active orders; fee evidence remains unknown',
            })
            db.execute("UPDATE orders SET state='terminal', result=? WHERE seq=? AND state='unknown'",
                       (json.dumps(result, ensure_ascii=False, allow_nan=False), seq))
            changed += 1
    return changed


def reset_stop(root, verify_accounts, reconcile_orders=None):
    root = Path(root)
    stop = root/'data/STOP_EXECUTION'
    with execution_lease(root/'data/runtime.lock'):
        if not stop.exists():
            return
        stamp = stop.stat()
        state_path = root/'data/hedge_state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get('halted_reason') or any(p['status'] != 'closed' for p in state['positions']):
                raise ValueError('本地有未核对持仓或停机原因；不能通过解除停止清除')
        if verify_accounts() is not True:
            raise ValueError('未确认两平台账户空仓且本次市场无挂单')
        if reconcile_orders is not None:
            reconcile_orders()
        journal = root/'data/execution_orders.sqlite'
        if journal.exists():
            with sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True) as db:
                if db.execute("SELECT 1 FROM orders WHERE state != 'terminal' LIMIT 1").fetchone():
                    raise ValueError('存在未核对订单；请先核对成交回执')
        with stop_guard(root):
            latest = stop.stat()
            if (stamp.st_ino,stamp.st_mtime_ns) != (latest.st_ino,latest.st_mtime_ns):
                raise ValueError('检查期间收到新的停止请求，已保留停止状态')
            stop.unlink()
