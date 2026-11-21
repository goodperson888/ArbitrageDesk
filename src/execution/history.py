"""Append-only local hedge events. Payloads are allowlisted and never contain keys."""
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from src.common.market_data import utc_now

_execution_context = ContextVar('trade_history_execution_context', default=None)
_event_buffer = ContextVar('trade_history_event_buffer', default=None)
_logger = logging.getLogger(__name__)
# Only observations with no effect on exposure or order recovery may be lost.
OPTIONAL_DIAGNOSTIC_EVENTS = frozenset((
    'position_reconcile_failed', 'entry_wait_monitor_failed',
    'stage_started', 'stage_completed', 'stage_failed', 'quote_evaluation', 'blocked'))

FIELDS = {'symbol', 'direction', 'quantity', 'cap_usdc', 'reason', 'position_id',
          'target_leverage', 'effective_leverages', 'leverage_setting_sources',
          'requested_size', 'confirmed', 'submitted',
          'venue', 'status', 'filled_size', 'average_price', 'fees_usdc', 'funding_usdc',
          'net_edge_bps', 'estimated_entry_edge_usdc', 'variational_notional', 'lighter_notional', 'order_id',
          'fee_basis', 'fee_policy_url', 'stage', 'elapsed_ms', 'location',
          'gross_edge_bps', 'discovery_gross_bps', 'slippage_budget_bps', 'min_edge_bps', 'execution_id',
          'execution_total_ms', 'actual_spread_bps', 'attempt', 'observed_price',
          'protected_bound', 'price_budget_bound', 'price_movement_bps',
          'hedge_edge_usdc', 'hedge_min_net_bps', 'unwind_pnl_estimate_usdc',
          'observed_net_edge_bps',
          'execution_window_samples', 'execution_window_ms', 'second_reference_price',
          'projection_decay_bps', 'recent_window_failures',
          'movement_bps', 'hedge_reserve_bps', 'adverse_second_price',
          'projected_net_edge_usdc', 'projected_net_edge_bps', 'minimum_net_bps', 'minimum_profit_usdc',
          'first_submit_ms', 'first_receipt_processing_ms',
          'first_receipt_to_second_quote_ms', 'second_quote_ms',
          'second_decision_ms', 'second_submit_ms',
          'first_receipt_to_second_send_ms',
          'entry_reserve_bps', 'base_min_edge_bps', 'reason_code', 'constraint_failures',
          'rollback_count', 'paused_until', 'route', 'decision',
          'grid_cycle_id', 'grid_layer', 'grid_entry_net_bps', 'grid_anchor_bps',
          'edge_basis', 'opening_edge_bps', 'target_context', 'target_projection', 'target_entry_net_bps',
          'remaining_venue', 'combined_pnl_usdc', 'pnl_bps'}
FIELDS.add('preparation')
FIELDS.update(('requested_quantity', 'book_utilization_percent', 'paired_depth_quantity', 'depth_quantity_limit'))
FIELDS.update(('raw_status', 'time_in_force', 'limit_price', 'platform_reason_code',
               'platform_reason', 'platform_reason_reported', 'evidence_source'))
FIELDS.update(('receipt_source', 'receipt_path', 'receipt_lookup_ms',
               'receipt_ws_ready', 'receipt_rest_started', 'receipt_ws_grace_ms'))


class TradeHistory:
    def __init__(self, path):
        self.path = Path(path)
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._observation_store_seen = False
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_warned_at = None
        self.diagnostic_storage = {}

    def prepare_indexes(self):
        """One-time migration at service startup, outside order submission.

        Read-only history views do not call this. Missing JSON identifiers stay
        out of the small partial indexes, including malformed legacy payloads.
        """
        with self._schema_lock:
            if self._schema_ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp TEXT, event TEXT, detail TEXT)')
                db.execute('CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event,id)')
                for key in ('position_id', 'execution_id'):
                    expression = "json_extract(detail,'$." + key + "')"
                    db.execute('CREATE INDEX IF NOT EXISTS idx_events_' + key + ' ON events('
                               + expression + ',id) WHERE json_valid(detail) AND ' + expression + ' IS NOT NULL')
            self._schema_ready = True

    @staticmethod
    def _event_row(event, fields):
        from src.execution.preparation import is_preparing
        if is_preparing():
            fields.setdefault('preparation', True)
        context = _execution_context.get()
        if context:
            fields.setdefault('execution_id', context['id'])
        detail = {k: v for k, v in fields.items() if k in FIELDS}
        return (utc_now(), event, json.dumps(detail, ensure_ascii=False, allow_nan=False))

    def append(self, event, **fields):
        row = self._event_row(event, fields)
        buffer = _event_buffer.get()
        if buffer is not None and buffer[0] == self.path:
            buffer[1].append(row)
            return
        if event in OPTIONAL_DIAGNOSTIC_EVENTS:
            return self._append_observations([row], initialize=True)
        self._append_rows([row])

    def append_diagnostic(self, event, **fields):
        """Try once without waiting for SQLite; never used for trade evidence.

        The normal history is prepared at startup. Do not create/migrate it on
        an account-read failure, and do not let its locks stop the monitor.
        """
        if event not in OPTIONAL_DIAGNOSTIC_EVENTS:
            raise ValueError('Only observational diagnostics may use this path')
        row = self._event_row(event, fields)
        return self._append_observations([row])

    def _append_observations(self, rows, initialize=False):
        """Zero SQLite lock wait for observations; trade evidence stays durable."""
        try:
            # Standalone callers may start with no database. Existing stores
            # never migrate indexes on this path, including a busy worker.
            if initialize and not self._observation_store_seen:
                self._observation_store_seen = True
                if not self.path.exists():
                    self.prepare_indexes()
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw',
                                         uri=True, timeout=0)) as db, db:
                db.executemany('INSERT INTO events(timestamp,event,detail) VALUES(?,?,?)', rows)
            return True
        except (sqlite3.Error, OSError) as exc:
            # Never expose SDK/request data or arbitrary exception messages.
            reason = {
                'database is locked': 'sqlite_busy',
                'database table is locked': 'sqlite_locked',
                'database or disk is full': 'sqlite_full',
                'attempt to write a readonly database': 'sqlite_readonly',
                'disk I/O error': 'sqlite_io',
                'database disk image is malformed': 'sqlite_corrupt',
            }.get(str(exc), type(exc).__name__)
            now = time.monotonic()
            with self._diagnostic_lock:
                self.diagnostic_storage = dict(skipped_rows=self.diagnostic_storage.get('skipped_rows', 0) + len(rows),
                    last_error=reason, last_error_at=utc_now())
                warn = self._diagnostic_warned_at is None or now - self._diagnostic_warned_at >= 30
                if warn:
                    self._diagnostic_warned_at = now
            if warn:
                _logger.warning('Optional execution diagnostic skipped (%s); position monitoring continues', reason)
            return False

    def _append_rows(self, rows):
        if not rows:
            return
        self.prepare_indexes()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp TEXT, event TEXT, detail TEXT)')
            db.executemany('INSERT INTO events(timestamp,event,detail) VALUES(?,?,?)', rows)

    @contextmanager
    def buffered(self):
        """Batch read-only preparation diagnostics after calculation finishes.

        Not used around orders: durable order intents/receipts retain their
        original synchronous writes. Child quote threads inherit this context.
        """
        rows = []
        token = _event_buffer.set((self.path, rows))
        try:
            yield
        finally:
            _event_buffer.reset(token)
            if rows and all(row[1] in OPTIONAL_DIAGNOSTIC_EVENTS for row in rows):
                self._append_observations(rows, initialize=True)
            else:
                self._append_rows(rows)

    @contextmanager
    def execution_scope(self, execution_id):
        """Attach one stable id and monotonic start time to one execution attempt."""
        token = _execution_context.set({'id': str(execution_id), 'started': time.monotonic()})
        try:
            yield
        finally:
            _execution_context.reset(token)

    @staticmethod
    def current_execution_id():
        context = _execution_context.get()
        return context.get('id') if context else None

    @staticmethod
    def current_elapsed_ms():
        context = _execution_context.get()
        if not context:
            return None
        return round((time.monotonic() - context['started']) * 1000)

    def recent(self, limit=100, offset=0):
        if not self.path.exists():
            return []
        try:
            limit = min(200, max(1, int(limit)))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            limit, offset = 100, 0
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=10)) as db:
            rows = db.execute(
                'SELECT id,timestamp,event,detail FROM events ORDER BY id DESC LIMIT ? OFFSET ?',
                (limit, offset)).fetchall()
        return [dict(id=i, timestamp=t, event=e, detail=json.loads(d)) for i, t, e, d in rows]

    def count(self):
        if not self.path.exists():
            return 0
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=10)) as db:
            return int(db.execute('SELECT COUNT(*) FROM events').fetchone()[0])

    def close_timings(self, position_ids):
        """Return first close receipt and terminal close event timestamps."""
        wanted = {str(item) for item in position_ids if item}
        if not wanted or not self.path.exists():
            return {}
        result = {}
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            rows = db.execute(
                "SELECT timestamp,event,detail FROM events "
                "WHERE event IN ('close_receipt','closed_pending_funding') ORDER BY id"
            ).fetchall()
        for timestamp, event, detail in rows:
            try:
                payload = json.loads(detail)
                position_id = str(payload.get('position_id') or '')
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if position_id not in wanted:
                continue
            item = result.setdefault(position_id, {})
            if event == 'close_receipt':
                item.setdefault('first_receipt_at', timestamp)
                item['last_receipt_at'] = timestamp
            else:
                item['terminal_at'] = timestamp
        return result

    def receipt_orders(self, position_ids):
        """Return platform order ids grouped by local hedge position."""
        wanted = {str(item) for item in position_ids if item}
        if not wanted or not self.path.exists():
            return {}
        result = {}
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            rows = db.execute(
                "SELECT event,detail FROM events "
                "WHERE event IN ('entry_receipt','close_receipt') ORDER BY id"
            ).fetchall()
        for event, detail in rows:
            try:
                payload = json.loads(detail)
                position_id = str(payload.get('position_id') or '')
                order_id = payload.get('order_id')
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if position_id not in wanted or not order_id:
                continue
            item = result.setdefault(position_id, {'entry': [], 'close': [], 'entry_by_venue': {}, 'close_by_venue': {}})
            bucket = 'entry' if event == 'entry_receipt' else 'close'
            if str(order_id) not in item[bucket]:
                item[bucket].append(str(order_id))
            venue = payload.get('venue')
            if venue:
                scoped = item[bucket + '_by_venue'].setdefault(venue, [])
                if str(order_id) not in scoped:
                    scoped.append(str(order_id))
        return result

    def receipt_prices(self, position_ids):
        """Return VWAP close prices recoverable from durable close receipts."""
        wanted = {str(item) for item in position_ids if item}
        if not wanted or not self.path.exists():
            return {}
        result = {}
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            rows = db.execute(
                "SELECT detail FROM events WHERE event='close_receipt' ORDER BY id"
            ).fetchall()
        for (detail,) in rows:
            try:
                payload = json.loads(detail)
                position_id = str(payload.get('position_id') or '')
                venue = str(payload.get('venue') or '')
                price = float(payload.get('average_price'))
                size = float(payload.get('filled_size') or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if position_id not in wanted or not venue or not price > 0 or not size > 0:
                continue
            venue_rows = result.setdefault(position_id, {}).setdefault(venue, [0.0, 0.0])
            venue_rows[0] += price * size
            venue_rows[1] += size
        return {
            position_id: {
                venue: total / size for venue, (total, size) in venues.items() if size > 0
            }
            for position_id, venues in result.items()
        }
