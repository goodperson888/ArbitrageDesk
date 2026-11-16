"""Bounded caches for durable history projections, never for live marks."""
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path


def file_version(path):
    try:
        s = Path(path).stat()
        return (s.st_ino, s.st_mtime_ns, s.st_size)
    except FileNotFoundError:
        return None


def database_version(path):
    return (file_version(path), file_version(str(path) + '-wal'))


def evidence_version(data_dir):
    """Ignore quote/heartbeat events; only receipts affect base accounting.

    The event/id index makes these three last-id lookups independent of the
    potentially large stream of rejected candidates and stage messages.
    """
    path = data_dir / 'hedge_events.sqlite'
    receipts = None
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2) as db:
                receipts = (path.stat().st_ino, tuple(
                    db.execute('SELECT MAX(id) FROM events WHERE event=?', (event,)).fetchone()[0]
                    for event in ('entry_receipt','close_receipt','closed_pending_funding')))
        except sqlite3.Error:
            # A legacy/damaged event archive still invalidates the projection;
            # its existing display error handling remains responsible for it.
            receipts = database_version(path)
    return (file_version(data_dir / 'platform-order-history.json'),
            database_version(data_dir / 'execution_orders.sqlite'), receipts)


class ProjectionCache:
    def __init__(self, capacity=32):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.values = OrderedDict()

    def get(self, identity, version, build):
        # Singleflight: concurrent page requests cannot rebuild the same book.
        with self.lock:
            cached = self.values.get(identity)
            if cached is not None and cached[0] == version:
                self.values.move_to_end(identity)
                return cached[1]
            value = build()
            self.values[identity] = (version, value)
            self.values.move_to_end(identity)
            while len(self.values) > self.capacity:
                self.values.popitem(last=False)
            return value


def enrich_history(overview, data_dir, cache):
    """Only rebuild diagnostics for positions whose evidence has changed."""
    import copy
    import json
    from src.web.close_diagnostics import attach_close_diagnostics
    from src.web.rollback_explanation import attach_rollback_explanations
    from contextlib import closing
    rows = [row for collection in ('open_positions','closed_positions')
            for row in overview.get(collection, [])]
    latest_by_kind = {True: {}, False: {}}
    for rollback, name in ((True,'hedge_events.sqlite'),(False,'close_diagnostics.sqlite')):
        selected = [r for r in rows if (r.get('hedge_status') == 'single_leg_recovered') == rollback]
        path = data_dir / name
        if not selected or not path.exists():
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            for row in selected:
                if rollback:
                    versions = []
                    for key, value in (('position_id',row.get('id')), ('execution_id',row.get('execution_id'))):
                        if value:
                            expression = "json_extract(detail,'$." + key + "')"
                            versions.append(db.execute('SELECT MAX(id) FROM events WHERE json_valid(detail) AND '
                                + expression + ' IS NOT NULL AND ' + expression + '=?', (value,)).fetchone()[0])
                    latest = tuple(versions)
                else:
                    latest = db.execute('SELECT MAX(id) FROM close_decisions WHERE position_id=?',
                                        (row.get('id'),)).fetchone()[0]
                latest_by_kind[rollback][row.get('id')] = latest
    order_version = database_version(data_dir/'execution_orders.sqlite')
    # Close outcome explanations also depend on terminal/residual evidence.
    # Use the existing position index; unrelated quote events do not rebuild it.
    from src.web.close_outcome import CLOSE_VERSION_SQL
    close_versions = {}
    event_path = data_dir/'hedge_events.sqlite'
    if event_path.exists():
        with closing(sqlite3.connect(event_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            for row in rows:
                if row.get('id') and row.get('hedge_status') != 'single_leg_recovered':
                    close_versions[row['id']] = db.execute(CLOSE_VERSION_SQL, (row['id'],)).fetchone()[0]
    for collection in ('open_positions', 'closed_positions'):
        for row in overview.get(collection, []):
            rollback = row.get('hedge_status') == 'single_leg_recovered'
            field = 'rollback_explanation' if rollback else 'close_diagnostics'
            latest = latest_by_kind[rollback].get(row.get('id'))
            # Explanations for open rows can include the latest estimated PnL;
            # they stay live. Closed rows normally hit this bounded cache.
            source = json.dumps(row, sort_keys=True, default=str)
            version = (latest, source, order_version if rollback else close_versions.get(row.get('id')))
            def build():
                book = {collection: [copy.deepcopy(row)]}
                (attach_rollback_explanations if rollback else attach_close_diagnostics)(book, data_dir)
                return book[collection][0].get(field)
            value = cache.get((str(data_dir.resolve()), row.get('id'), field), version, build)
            if value is not None:
                row[field] = copy.deepcopy(value)
