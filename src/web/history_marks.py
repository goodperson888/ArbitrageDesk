"""Read-only history marks from the dashboard's shared public quote feed."""
import json
import math
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener


class HistoryMarkUnavailable(ValueError):
    def __init__(self, reason):
        self.display_reason = reason
        super().__init__(reason)


class HistoryMarks:
    def __init__(self, fetch=None, now=time.time, cache_seconds=2, max_age=10):
        self.fetch = fetch or self._fetch
        self.now = now
        self.cache_seconds = cache_seconds
        self.max_age = max_age
        self._guard = threading.Lock()
        self._locks = {}
        self._cache = {}
        self._last_good = {}

    def last_snapshot(self, venue, symbol):
        with self._guard:
            value = self._last_good.get((venue, symbol))
            return dict(value) if value else None

    @staticmethod
    def _fetch(venue, symbol):
        # A fixed local public GET endpoint; no account credentials or orders.
        url = 'http://127.0.0.1:5002/api/v2/quotes?' + urlencode({'venue': venue, 'symbol': symbol})
        with build_opener(ProxyHandler({})).open(url, timeout=4) as response:
            return json.load(response)

    def snapshot(self, venue, symbol):
        key = (venue, symbol)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        # Concurrent history polls share one request per venue and symbol.
        with lock:
            cached = self._cache.get(key)
            if cached and self.now() - cached[0] < self.cache_seconds:
                value = cached[1]
                if isinstance(value, HistoryMarkUnavailable):
                    raise HistoryMarkUnavailable(value.display_reason)
                if self.now() - value['observed_at'] <= self.max_age:
                    return dict(value)
            try:
                rows = self.fetch(venue, symbol)
                row = next((r for r in rows if r.get('venue') == venue and
                            str(r.get('symbol', '')).removesuffix('-PERP') == symbol.removesuffix('-PERP')), None)
                if row is None:
                    raise HistoryMarkUnavailable('行情暂缺')
                stamp = datetime.fromisoformat(row['fetched_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('missing timezone')
                observed = stamp.timestamp()
                age = max(self.now() - observed, float(row.get('age_seconds') or 0))
                if row.get('stale') is not False or age > self.max_age or observed > self.now() + 2:
                    reason = str(row.get('reason') or '').lower()
                    raise HistoryMarkUnavailable('行情限流，等待新报价' if '429' in reason or '限流' in reason else '行情过期，等待新报价')
                bid, ask = row.get('bid'), row.get('ask')
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and
                           math.isfinite(v) and v > 0 for v in (bid, ask)) or bid > ask:
                    raise HistoryMarkUnavailable('盘口暂缺或无效')
                value = {'price': (bid + ask) / 2, 'observed_at': self.now() - age,
                         'bid': bid, 'ask': ask, 'fee_bps': (row.get('metadata') or {}).get('fee_bps'),
                         'updated_at': datetime.fromtimestamp(self.now() - age, timezone.utc).isoformat(),
                         'source': row.get('source') or 'public_quote'}
            except HistoryMarkUnavailable as exc:
                self._cache[key] = (self.now(), exc)
                raise
            except HTTPError as exc:
                error = HistoryMarkUnavailable('行情限流，等待新报价' if exc.code == 429 else '公共行情服务暂不可用')
                self._cache[key] = (self.now(), error)
                raise error from None
            except (URLError, TimeoutError, OSError, ValueError, TypeError, KeyError, AttributeError):
                error = HistoryMarkUnavailable('公共行情服务暂不可用')
                self._cache[key] = (self.now(), error)
                raise error from None
            self._cache[key] = (self.now(), value)
            with self._guard:
                self._last_good[key] = dict(value)
            return dict(value)


class HistoryMarkAdapter:
    def __init__(self, venue, marks, position_quotes=None):
        self.venue, self.marks = venue, marks
        self.position_quotes = position_quotes or {}

    def get_mark_snapshot(self, symbol):
        value = self.position_quotes.get((self.venue, symbol))
        if value:
            age = self.marks.now() - value['observed_at']
            if 0 <= age <= 2:
                with self.marks._guard:
                    self.marks._last_good[(self.venue, symbol)] = dict(value)
                return dict(value)
        return self.marks.snapshot(self.venue, symbol)

    def get_last_mark_snapshot(self, symbol):
        return self.marks.last_snapshot(self.venue, symbol)

    def get_market_price(self, symbol):
        return self.get_mark_snapshot(symbol)['price']
