"""One-second sampled observations persisted locally for chart reloads."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone


class HistoryStore:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS observations (symbol TEXT, ts INTEGER, payload TEXT, PRIMARY KEY(symbol, ts))')
            db.execute('CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(ts)')
        self.last_cleanup = 0

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def append(self, symbol, payload):
        now = int(time.time())
        with self.lock, self._connect() as db:
            db.execute('INSERT OR REPLACE INTO observations VALUES (?, ?, ?)',
                       (symbol, now, json.dumps(payload, allow_nan=False)))
            if now - self.last_cleanup > 300:
                db.execute('DELETE FROM observations WHERE ts < ?', (now - 86400,))
                self.last_cleanup = now

    def read(self, symbol, seconds=3600, limit=1800):
        now = int(time.time())
        with self.lock, self._connect() as db:
            rows = db.execute('SELECT ts, payload FROM observations WHERE symbol=? AND ts>=? ORDER BY ts',
                              (symbol, now-seconds)).fetchall()
        items, previous = [], None
        for timestamp, payload in rows:
            if previous is not None and timestamp - previous > 5:
                # Service downtime is a real gap, even in the cached reference view.
                items.append({'timestamp': datetime.fromtimestamp(previous + 1, timezone.utc).isoformat(),
                              'gap': True})
            items.append(json.loads(payload))
            previous = timestamp
        # Preserve extrema AND null gaps in each bucket; avoid hiding brief spikes.
        if len(items) <= limit:
            return items
        step = max(1, (len(items) + limit // 4 - 1) // (limit // 4))
        selected = []
        for start in range(0, len(items), step):
            bucket = items[start:start+step]
            indexes = {0, len(bucket)-1}
            for field in ('buy_variational_sell_lighter_bps', 'buy_lighter_sell_variational_bps',
                          'reference_buy_variational_sell_lighter_bps', 'reference_buy_lighter_sell_variational_bps',
                          'mark_spread_bps'):
                valid = [(i, row.get(field)) for i,row in enumerate(bucket) if row.get(field) is not None]
                if valid:
                    indexes.add(min(valid, key=lambda x:x[1])[0]); indexes.add(max(valid,key=lambda x:x[1])[0])
                missing = next((i for i,row in enumerate(bucket) if row.get(field) is None), None)
                if missing is not None: indexes.add(missing)
            selected.extend(bucket[i] for i in sorted(indexes))
        return selected
