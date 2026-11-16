"""Best-effort close snapshots, isolated from durable order-intent writes."""
import atexit
import json
import math
import queue
import sqlite3
import threading
import time
from pathlib import Path

from src.common.market_data import utc_now


QUOTE_FIELDS = ('bid', 'ask', 'bid_vwap', 'ask_vwap', 'updated_at', 'source', 'fee_bps',
                'received_at', 'exchange_at', 'time_basis', 'market_id', 'offset', 'nonce')
SCALARS = frozenset((
    'decision_id', 'position_id', 'execution_id', 'symbol', 'reason', 'observed_at',
    'mode', 'target_mode', 'effective_target', 'basis', 'tolerance_bps',
    'center_bps', 'center_samples', 'center_window_seconds', 'directional_spread_bps',
    'target_bps', 'target_frozen_at', 'edge_basis',
    'absolute_spread_bps', 'entry_spread_bps', 'reduction_bps', 'distance_to_center_bps',
    'convergence_ok', 'venue', 'position_side', 'close_side', 'size', 'mid',
    'reference_price', 'bound', 'slippage_bps', 'net_pnl_usdc', 'net_pnl_bps',
    'entry_fees_usdc', 'funding_usdc', 'minimum_usdc', 'minimum_bps', 'allowed',
    'reference_net_pnl_usdc', 'movement_cost_usdc', 'closing_fee_usdc', 'closing_fee_source',
    'protective_exit', 'held_seconds', 'max_hold_seconds',
) + QUOTE_FIELDS)
NESTED = frozenset(('trigger', 'pre_submit', 'legs', 'variational', 'lighter'))


def _clean(value, depth=0):
    if isinstance(value, dict) and depth < 5:
        return {key: _clean(item, depth + 1) for key, item in value.items()
                if key in SCALARS or key in NESTED}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:192]
    return None


def quote_snapshot(quote):
    return _clean({key: quote[key] for key in QUOTE_FIELDS if key in quote})


class DiagnosticWriter:
    """One bounded queue; the caller never opens SQLite or waits for the writer."""
    def __init__(self, capacity=256):
        self.queue = queue.Queue(maxsize=capacity)
        self.written = self.dropped = self.failed = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name='close-diagnostics')
        self.thread.start()

    def submit(self, path, snapshot):
        try:
            self.queue.put_nowait((path, snapshot))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _write(self, path, snapshot):
        # This database has no order intents or receipts: a slow diagnostic
        # writer cannot acquire their SQLite write lock.
        if not path.parent.exists():
            raise OSError('diagnostic directory unavailable')
        with sqlite3.connect(path, timeout=.05) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('CREATE TABLE IF NOT EXISTS close_decisions ('
                       'id INTEGER PRIMARY KEY, position_id TEXT, observed_at TEXT, detail TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS close_decisions_position '
                       'ON close_decisions(position_id,id DESC)')
            db.execute('INSERT INTO close_decisions(position_id,observed_at,detail) VALUES(?,?,?)',
                       (snapshot['position_id'], snapshot['observed_at'],
                        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))

    def _run(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                self._write(*item)
                self.written += 1
            except Exception:
                # Optional evidence must never change a trading decision.
                self.failed += 1
            finally:
                self.queue.task_done()

    def flush(self, timeout=.25):
        """Shutdown/tests only. Never called by quote, submit or receipt paths."""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.002)
        return self.queue.unfinished_tasks == 0

    def close(self, timeout=.25):
        if self.flush(timeout):
            self.queue.put_nowait(None)
            self.thread.join(timeout)


_writer = None
_writer_lock = threading.Lock()


def shared_writer():
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = DiagnosticWriter()
            atexit.register(_writer.flush)
        return _writer


class CloseDiagnostics:
    def __init__(self, path, writer=None):
        self.path = Path(path)
        # Start the shared writer at strategy construction, never on first send.
        try:
            self.writer = writer if writer is not None else shared_writer()
        except Exception:
            self.writer = None

    def record(self, **snapshot):
        try:
            if self.writer is None:
                return False
            snapshot.setdefault('observed_at', utc_now())
            return self.writer.submit(self.path, _clean(snapshot))
        except Exception:
            return False


def read_close_decisions(path, position_ids, limit=4):
    """Indexed local reads for the dashboard; no platform queries or full scans."""
    path = Path(path)
    if not path.exists():
        return {}
    result = {}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05) as db:
        for position_id in set(position_ids):
            rows = db.execute('SELECT detail FROM close_decisions WHERE position_id=? '
                              'ORDER BY id DESC LIMIT ?', (position_id, limit)).fetchall()
            if rows:
                result[position_id] = [json.loads(row[0]) for row in rows]
    return result
