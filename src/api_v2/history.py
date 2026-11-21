"""Small persistent store for multi-venue spread observations.

The dashboard used to persist two hard-coded V/L fields.  The v2 scanner is
the source of truth for the multi-venue page, so its history is keyed by the
actual buy/sell route instead of by a pair of venue-specific column names.
"""
from __future__ import annotations

import sqlite3
import math
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from .models import Opportunity


HISTORY_RETENTION_SECONDS = 7 * 86400
ROLLUP_5M_SECONDS = 5 * 60
ROLLUP_1H_SECONDS = 60 * 60
ROLLUP_5M_RETENTION_SECONDS = 30 * 86400
ROLLUP_1H_RETENTION_SECONDS = 90 * 86400
MAX_HISTORY_SECONDS = ROLLUP_1H_RETENTION_SECONDS
OBSERVATION_BUCKET_SECONDS = 30
# Keep raw observations for diagnosis, but keep single-feed spikes out of
# route statistics and reversion events.  The fixed floor catches isolated
# 0.5%+ dislocations while the MAD term adapts to quieter or noisier routes.
ANOMALY_MIN_BPS = 50.0
ANOMALY_HARD_LIMIT_BPS = 100.0
ANOMALY_MAD_MULTIPLIER = 6.0


class MultiVenueHistory:
    def center_stats(self, symbol, pair_key, seconds=604800, entry_quantile=90):
        from .center_statistics import service_for
        return service_for(self).get(symbol, pair_key, seconds, entry_quantile)

    def shared_reversion_stats(self, symbol, pair_key, seconds=604800, band_bps=2.0,
                               target_mode='center', min_deviation_bps=0.0, entry_quantile=90.0):
        from .center_statistics import service_for
        return service_for(self).get_reversion(symbol, pair_key, seconds, band_bps,
                                              target_mode, min_deviation_bps, entry_quantile)

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._sample_cache = {}
        self._last_cleanup = None
        self._stats_cache: dict[tuple[int, tuple[tuple[str, str], ...]], tuple[float, dict]] = {}
        self._reversion_cache: dict[tuple[str, str, int, float, str, float], tuple[float, dict]] = {}
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS observations (
                    symbol TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    pair_key TEXT NOT NULL,
                    buy_venue TEXT NOT NULL,
                    sell_venue TEXT NOT NULL,
                    gross_bps REAL,
                    buy_age_seconds REAL,
                    sell_age_seconds REAL,
                    executable INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    PRIMARY KEY(symbol, ts, pair_key)
                )"""
            )
            # Stats are filtered by the directed route and then by time. Keep
            # this local aggregate query from scanning the whole 24h table on
            # every opportunity refresh.
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_symbol_pair_ts "
                "ON observations(symbol, pair_key, ts)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(ts)")
            db.execute(
                """CREATE TABLE IF NOT EXISTS observation_rollups (
                    symbol TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    pair_key TEXT NOT NULL,
                    buy_venue TEXT NOT NULL,
                    sell_venue TEXT NOT NULL,
                    bucket_seconds INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    executable_count INTEGER NOT NULL DEFAULT 0,
                    sum_bps REAL NOT NULL,
                    sum_sq_bps REAL NOT NULL,
                    min_bps REAL NOT NULL,
                    max_bps REAL NOT NULL,
                    last_bps REAL NOT NULL,
                    last_ts INTEGER NOT NULL,
                    PRIMARY KEY(symbol, ts, pair_key, bucket_seconds)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rollups_symbol_pair_ts "
                "ON observation_rollups(symbol, pair_key, ts, bucket_seconds)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_rollups_bucket_ts "
                       "ON observation_rollups(bucket_seconds, ts)")
            # Retention deletes can leave free pages behind. Compact only on
            # startup when fragmentation is material, never during a quote
            # request, so exchange reads are not blocked by maintenance.
            page_count, freelist = db.execute(
                "PRAGMA page_count"
            ).fetchone()[0], db.execute("PRAGMA freelist_count").fetchone()[0]
            if page_count > 0 and freelist > 1000 and freelist / page_count > 0.20:
                db.execute("VACUUM")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def pair_key(buy_venue: str, sell_venue: str) -> str:
        return f"{buy_venue}->{sell_venue}"

    def append(self, symbol: str, rows: Iterable[Opportunity]) -> None:
        now = int(time.time())
        bucket = now - now % OBSERVATION_BUCKET_SECONDS
        values = [(
            symbol, bucket, self.pair_key(row.buy_venue, row.sell_venue),
            row.buy_venue, row.sell_venue, row.gross_bps,
            row.buy_age_seconds, row.sell_age_seconds, int(row.executable), row.reason,
        ) for row in rows]
        self._insert(values, now)

    def append_many(self, rows: Iterable[Opportunity]) -> None:
        now = int(time.time())
        bucket = now - now % OBSERVATION_BUCKET_SECONDS
        values = []
        for row in rows:
            values.append((
                row.canonical_symbol,
                bucket,
                self.pair_key(row.buy_venue, row.sell_venue),
                row.buy_venue,
                row.sell_venue,
                row.gross_bps,
                row.buy_age_seconds,
                row.sell_age_seconds,
                int(row.executable),
                row.reason,
            ))
        if not values:
            return
        self._insert(values, now)

    def _insert(self, values, now: int) -> None:
        if not values:
            return
        with self.lock:
            tick = time.monotonic()
            cleanup = self._last_cleanup is None or tick - self._last_cleanup >= 300
            with self._connect() as db:
                db.executemany(
                    """INSERT OR REPLACE INTO observations
                    (symbol, ts, pair_key, buy_venue, sell_venue, gross_bps,
                     buy_age_seconds, sell_age_seconds, executable, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                if cleanup:
                    self._archive_history(db, now)
            if cleanup:
                self._last_cleanup = tick

    @staticmethod
    def _archive_history(db, now: int) -> None:
        """Age complete buckets once per cleanup, never on every quote.

        Each pass transfers at most an hour per tier. INSERT OR IGNORE and
        source deletion share one transaction, including after a crash. Keep
        unprocessed source rows if a large backlog needs later passes.
        """
        raw_cutoff = (now - HISTORY_RETENTION_SECONDS) // ROLLUP_5M_SECONDS * ROLLUP_5M_SECONDS
        oldest = db.execute('SELECT MIN(ts) FROM observations WHERE ts<?', (raw_cutoff,)).fetchone()[0]
        if oldest is not None:
            end = min(raw_cutoff, (int(oldest) // ROLLUP_5M_SECONDS + 12) * ROLLUP_5M_SECONDS)
            db.execute("""INSERT OR IGNORE INTO observation_rollups
                (symbol,ts,pair_key,buy_venue,sell_venue,bucket_seconds,sample_count,
                 executable_count,sum_bps,sum_sq_bps,min_bps,max_bps,last_bps,last_ts)
                SELECT symbol, CAST(ts / 300 AS INTEGER)*300, pair_key, buy_venue,
                       sell_venue, 300, COUNT(*), SUM(executable), SUM(gross_bps),
                       SUM(gross_bps*gross_bps), MIN(gross_bps), MAX(gross_bps),
                       AVG(gross_bps), MAX(ts)
                  FROM observations WHERE ts<? AND gross_bps IS NOT NULL
                   AND ABS(gross_bps)<1e100
                   AND buy_age_seconds BETWEEN 0 AND 5 AND sell_age_seconds BETWEEN 0 AND 5
                 GROUP BY symbol,pair_key,CAST(ts / 300 AS INTEGER)""", (end,))
            db.execute('DELETE FROM observations WHERE ts<?', (end,))
        hour_cutoff = (now - ROLLUP_5M_RETENTION_SECONDS) // ROLLUP_1H_SECONDS * ROLLUP_1H_SECONDS
        oldest = db.execute('SELECT MIN(ts) FROM observation_rollups WHERE bucket_seconds=300 AND ts<?',
                            (hour_cutoff,)).fetchone()[0]
        if oldest is not None:
            end = min(hour_cutoff, (int(oldest) // ROLLUP_1H_SECONDS + 1) * ROLLUP_1H_SECONDS)
            db.execute("""INSERT OR IGNORE INTO observation_rollups
                (symbol,ts,pair_key,buy_venue,sell_venue,bucket_seconds,sample_count,
                 executable_count,sum_bps,sum_sq_bps,min_bps,max_bps,last_bps,last_ts)
                SELECT symbol, CAST(ts / 3600 AS INTEGER)*3600, pair_key, buy_venue,
                       sell_venue, 3600, SUM(sample_count), SUM(executable_count), SUM(sum_bps),
                       SUM(sum_sq_bps), MIN(min_bps), MAX(max_bps),
                       SUM(sum_bps)/SUM(sample_count), MAX(last_ts)
                  FROM observation_rollups WHERE bucket_seconds=300 AND ts<?
                 GROUP BY symbol,pair_key,CAST(ts / 3600 AS INTEGER)""", (end,))
            db.execute('DELETE FROM observation_rollups WHERE bucket_seconds=300 AND ts<?', (end,))
        db.execute('DELETE FROM observation_rollups WHERE bucket_seconds=3600 AND ts<?',
                   ((now - MAX_HISTORY_SECONDS) // ROLLUP_1H_SECONDS * ROLLUP_1H_SECONDS,))

    @staticmethod
    def _rollup_row_to_sample(row):
        ts, key, buy, sell, bucket_seconds, count, executable_count, total, total_sq, low, high, last, last_ts = row
        return {
            'timestamp': int(last_ts or ts), 'bucket_timestamp': int(ts),
            'pair_key': key, 'buy_venue': buy, 'sell_venue': sell,
            'gross_bps': float(total) / max(1, int(count)),
            'low_bps': float(low), 'high_bps': float(high),
            'spread_basis': 'buy_ask', 'buy_age_seconds': 0,
            'sell_age_seconds': 0, 'executable': False,
            'had_executable_samples': bool(executable_count),
            'aggregate': True, 'bucket_seconds': int(bucket_seconds),
            'sample_count': int(count),
        }

    def _read_rollup_rows(self, db, symbol, pair_key, start, now, bucket_seconds):
        clauses = ['symbol=?', 'ts>=?', 'ts<=?', 'bucket_seconds=?']
        params = [symbol, start, now, bucket_seconds]
        if pair_key:
            clauses.append('pair_key=?')
            params.append(pair_key)
        query = ('SELECT ts,pair_key,buy_venue,sell_venue,bucket_seconds,sample_count,'
                 'executable_count,sum_bps,sum_sq_bps,min_bps,max_bps,last_bps,last_ts '
                 'FROM observation_rollups WHERE ' + ' AND '.join(clauses) +
                 ' ORDER BY ts,pair_key,bucket_seconds')
        return [self._rollup_row_to_sample(row) for row in db.execute(query, params).fetchall()]

    def _read_long_rows(self, db, symbol, pair_key, seconds, now):
        start = now - int(seconds)
        raw_cutoff = (now - HISTORY_RETENTION_SECONDS) // ROLLUP_5M_SECONDS * ROLLUP_5M_SECONDS
        raw_start = max(start, raw_cutoff)
        clauses = ['symbol=?', 'ts>=?', 'ts<=?']
        params = [symbol, raw_start, now]
        if pair_key:
            clauses.append('pair_key=?')
            params.append(pair_key)
        raw = db.execute(
            'SELECT ts,pair_key,buy_venue,sell_venue,gross_bps,buy_age_seconds,'
            'sell_age_seconds,executable,reason FROM observations WHERE ' + ' AND '.join(clauses) +
            ' ORDER BY ts,pair_key', params).fetchall()
        rows = [dict(timestamp=ts,pair_key=key,buy_venue=buy,sell_venue=sell,gross_bps=gross,
                     spread_basis='buy_ask',buy_age_seconds=buy_age,sell_age_seconds=sell_age,
                     executable=bool(executable),reason=reason,aggregate=False)
                for ts,key,buy,sell,gross,buy_age,sell_age,executable,reason in raw]
        cutoff = raw_cutoff
        thirty_day_cutoff = (now - ROLLUP_5M_RETENTION_SECONDS) // ROLLUP_1H_SECONDS * ROLLUP_1H_SECONDS
        rows.extend(self._read_rollup_rows(
            db, symbol, pair_key, max(start // ROLLUP_5M_SECONDS * ROLLUP_5M_SECONDS, thirty_day_cutoff), cutoff - 1, ROLLUP_5M_SECONDS))
        rows.extend(self._read_rollup_rows(
            db, symbol, pair_key, start // ROLLUP_1H_SECONDS * ROLLUP_1H_SECONDS,
            min(cutoff - 1, thirty_day_cutoff - 1), ROLLUP_1H_SECONDS))
        # Raw rows cover the latest seven days. Rollup rows cover the older
        # portions at exactly one resolution, preventing duplicate points.
        return sorted((row for row in rows if start <= int(row['timestamp']) <= now),
                      key=lambda row: (int(row['timestamp']), row.get('pair_key') or ''))

    def read(self, symbol: str, seconds: int = 3600,
             pair_key: Optional[str] = None, limit: int = 1800) -> list[dict]:
        now = int(time.time())
        if int(seconds) > HISTORY_RETENTION_SECONDS:
            with self.lock, self._connect() as db:
                rows = self._read_long_rows(db, symbol, pair_key, int(seconds), now)
            numeric_values = [float(row['gross_bps']) for row in rows
                              if row.get('gross_bps') is not None]
            anomaly_mask, anomaly_threshold_bps, _ = self._anomaly_mask(numeric_values)
            result = []
            numeric_index = 0
            for row in rows:
                gross = row.get('gross_bps')
                anomalous = bool(anomaly_mask[numeric_index]) if gross is not None else False
                if gross is not None:
                    numeric_index += 1
                row.update(valid_for_center=not anomalous and gross is not None and all(
                               isinstance(age, (int, float)) and math.isfinite(age) and 0 <= age <= 5
                               for age in (row.get('buy_age_seconds'), row.get('sell_age_seconds'))),
                           anomalous=anomalous,
                           anomaly_reason='偏离历史中位数或超过绝对上限' if anomalous else None,
                           anomaly_threshold_bps=anomaly_threshold_bps)
                result.append(row)
            return self._limit_history_rows(result, limit)
        clauses = ["symbol = ?", "ts >= ?"]
        params: list[object] = [symbol, now - seconds]
        if pair_key:
            clauses.append("pair_key = ?")
            params.append(pair_key)
        query = (
            "SELECT ts, pair_key, buy_venue, sell_venue, gross_bps, "
            "buy_age_seconds, sell_age_seconds, executable, reason "
            "FROM observations WHERE " + " AND ".join(clauses) +
            " ORDER BY ts, pair_key"
        )
        with self.lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        numeric_values = [float(row[4]) for row in rows if row[4] is not None]
        anomaly_mask, anomaly_threshold_bps, _ = self._anomaly_mask(numeric_values)
        result = []
        numeric_index = 0
        for ts, key, buy, sell, gross, buy_age, sell_age, executable, reason in rows:
            anomalous = bool(anomaly_mask[numeric_index]) if gross is not None else False
            if gross is not None:
                numeric_index += 1
            result.append({
                "timestamp": ts,
                "pair_key": key,
                "buy_venue": buy,
                "sell_venue": sell,
                "gross_bps": gross,
                "spread_basis": "buy_ask",
                "buy_age_seconds": buy_age,
                "sell_age_seconds": sell_age,
                "valid_for_center": (not anomalous and all(isinstance(age, (int, float))
                    and math.isfinite(age) and 0 <= age <= 5 for age in (buy_age, sell_age))),
                "executable": bool(executable),
                "reason": reason,
                # Keep every raw point for diagnosis.  The chart can mark a
                # spike without letting it distort the robust reversion
                # statistics returned by ``reversion_stats``.
                "anomalous": anomalous,
                "anomaly_reason": "偏离历史中位数或超过绝对上限" if anomalous else None,
                "anomaly_threshold_bps": anomaly_threshold_bps,
            })
        return self._limit_history_rows(result, limit)

    @staticmethod
    def _limit_history_rows(result, limit):
        if len(result) <= limit:
            return result
        limit = max(1, int(limit))
        if limit == 1:
            return result[-1:]
        # Always retain both endpoints, especially the most recent sample.
        return [result[round(i * (len(result) - 1) / (limit - 1))] for i in range(limit)]

    def stats_for(self, rows: Iterable[Opportunity], seconds: int = 86400) -> dict[tuple[str, str], dict]:
        """Return robust route ranges and separately retain raw diagnostics."""
        keys = {(row.canonical_symbol, self.pair_key(row.buy_venue, row.sell_venue))
                for row in rows}
        if not keys:
            return {}
        cache_key = (seconds, tuple(sorted(keys)))
        cached = self._stats_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        now = int(time.time())
        clauses = ["(symbol = ? AND pair_key = ?)"] * len(keys)
        params: list[object] = [now - seconds]
        for symbol, pair_key in sorted(keys):
            params.extend((symbol, pair_key))
        query = (
            "SELECT symbol, pair_key, gross_bps FROM observations "
            "WHERE ts >= ? AND gross_bps IS NOT NULL AND (" + " OR ".join(clauses) + ")"
        )
        with self.lock, self._connect() as db:
            result = db.execute(query, params).fetchall()
        grouped: dict[tuple[str, str], list[float]] = {}
        for symbol, pair_key, gross in result:
            grouped.setdefault((symbol, pair_key), []).append(float(gross))
        result = {}
        for key, values in grouped.items():
            clean_rows, anomaly_count, threshold, _ = self._clean_rows(
                list(enumerate(values)))
            clean_values = [spread for _, spread in clean_rows]
            result[key] = {
                "history_samples": len(clean_values),
                "history_min_bps": min(clean_values) if clean_values else None,
                "history_max_bps": max(clean_values) if clean_values else None,
                "history_avg_bps": (sum(clean_values) / len(clean_values)
                                     if clean_values else None),
                "history_raw_samples": len(values),
                "history_raw_min_bps": min(values) if values else None,
                "history_raw_max_bps": max(values) if values else None,
                "history_anomaly_count": anomaly_count,
                "history_anomaly_threshold_bps": threshold,
            }
        self._stats_cache[cache_key] = (time.monotonic(), result)
        if len(self._stats_cache) > 256:
            oldest = min(self._stats_cache, key=lambda key: self._stats_cache[key][0])
            self._stats_cache.pop(oldest, None)
        return result

    @staticmethod
    def _quantile(values: list[float], fraction: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * min(1.0, max(0.0, fraction))
        lower = int(position)
        upper = min(len(ordered) - 1, lower + 1)
        weight = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * weight

    @classmethod
    def _anomaly_mask(cls, values: list[float]) -> tuple[list[bool], float, Optional[float]]:
        """Classify isolated route spikes without deleting raw observations."""
        if not values:
            return [], ANOMALY_MIN_BPS, None
        center = cls._quantile(values, 0.5)
        if center is None:
            return [False] * len(values), ANOMALY_MIN_BPS, None
        deviations = [abs(value - center) for value in values]
        mad = cls._quantile(deviations, 0.5) or 0.0
        adaptive = max(ANOMALY_MIN_BPS, ANOMALY_MAD_MULTIPLIER * 1.4826 * mad)
        threshold = min(ANOMALY_HARD_LIMIT_BPS, adaptive)
        mask = [abs(value) > ANOMALY_HARD_LIMIT_BPS or
                (abs(value) > ANOMALY_MIN_BPS and abs(value - center) > threshold)
                for value in values]
        return mask, threshold, center

    @classmethod
    def _clean_rows(cls, rows: list[tuple[int, float]]) -> tuple[list[tuple[int, float]], int, float, Optional[float]]:
        values = [float(spread) for _, spread in rows if spread is not None]
        mask, threshold, _ = cls._anomaly_mask(values)
        clean, index, anomalies = [], 0, 0
        for timestamp, spread in rows:
            if spread is None:
                continue
            if mask[index]:
                anomalies += 1
            else:
                clean.append((timestamp, float(spread)))
            index += 1
        return clean, anomalies, threshold, cls._quantile(values, 0.5)

    def prepared_samples(self, symbol, pair_key, seconds):
        """Reuse one bounded, cleaned sample set for centers and event statistics."""
        key = (symbol, pair_key, seconds)
        with self._sample_lock:
            tick = time.monotonic()
            source = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                           for p in (Path(self.path), Path(self.path + '-wal')))
            cached = self._sample_cache.get(key)
            if cached and tick - cached[0] < 30 and cached[2] == source:
                return cached[1]
            now = int(time.time())
            with self._connect() as db:
                if int(seconds) <= HISTORY_RETENTION_SECONDS:
                    raw = db.execute('SELECT ts,gross_bps,buy_age_seconds,sell_age_seconds FROM observations '
                        'WHERE symbol=? AND pair_key=? AND ts>=? AND gross_bps IS NOT NULL ORDER BY ts',
                        (symbol, pair_key, now-seconds)).fetchall()
                    fresh = [(stamp, value) for stamp, value, buy_age, sell_age in raw
                             if math.isfinite(value) and stamp <= now and all(isinstance(age,(int,float))
                                and math.isfinite(age) and 0 <= age <= 5 for age in (buy_age,sell_age))]
                    source_samples = len(raw)
                    aggregate_buckets = 0
                    sample_interval = OBSERVATION_BUCKET_SECONDS
                    invalid_samples = len(raw) - len(fresh)
                else:
                    long_rows = self._read_long_rows(db, symbol, pair_key, int(seconds), now)
                    sample_interval = (ROLLUP_1H_SECONDS if int(seconds) > ROLLUP_5M_RETENTION_SECONDS
                                       else ROLLUP_5M_SECONDS)
                    # Equal-duration representatives prevent the most recent
                    # raw seven days from dominating coarse older samples.
                    bins = {}
                    source_samples = invalid_samples = aggregate_buckets = 0
                    for row in long_rows:
                        count = int(row.get('sample_count') or 1)
                        source_samples += count
                        value = row.get('gross_bps')
                        if (not isinstance(value, (int, float)) or not math.isfinite(value)
                                or not all(isinstance(age, (int, float)) and math.isfinite(age)
                                           and 0 <= age <= 5 for age in
                                           (row.get('buy_age_seconds'), row.get('sell_age_seconds')))):
                            invalid_samples += count
                            continue
                        bucket = int(row['timestamp']) // sample_interval * sample_interval
                        total, samples, latest = bins.get(bucket, (0., 0, 0))
                        bins[bucket] = (total + float(value) * count, samples + count,
                                        max(latest, int(row['timestamp'])))
                        aggregate_buckets += int(bool(row.get('aggregate')))
                    fresh = [(latest, total / count) for _, (total, count, latest) in sorted(bins.items())]
            rows, anomalies, threshold, raw_center = self._clean_rows(fresh)
            finite = [r[1] for r in fresh if math.isfinite(r[1])]
            prepared = dict(rows=rows, ordered=sorted(value for _,value in rows),
                raw_samples=source_samples, invalid_quote_samples=invalid_samples,
                sample_interval_seconds=sample_interval, approximate=sample_interval > OBSERVATION_BUCKET_SECONDS,
                aggregate_buckets=aggregate_buckets,
                anomaly_count=anomalies, anomaly_threshold_bps=threshold,
                raw_center_bps=raw_center, raw_min_bps=min(finite,default=None),
                raw_max_bps=max(finite,default=None))
            self._sample_cache[key] = (tick,prepared,source)
            if len(self._sample_cache)>64:
                self._sample_cache.pop(min(self._sample_cache,key=lambda k:self._sample_cache[k][0]))
            return prepared

    @staticmethod
    def ordered_quantile(values, fraction):
        if not values:
            return None
        at=(len(values)-1)*fraction
        low=int(at); high=min(low+1,len(values)-1)
        return values[low]+(values[high]-values[low])*(at-low)

    def reversion_stats(self, symbol: str, pair_key: str, seconds: int = 7 * 86400,
                        band_bps: float = 2.0, target_mode: str = 'center',
                        min_deviation_bps: float = 0.0, entry_quantile: float = 90.0,
                        execution_basis: bool = False) -> dict:
        """Measure returns into a route-specific convergence band.

        ``center`` uses the observed median as the normal route level,
        ``zero`` keeps the historical near-zero definition, and ``either``
        counts a return to either band. ``min_deviation_bps`` filters out
        shallow excursions: an event only counts after its maximum distance
        from the selected target reaches that threshold. The same observations
        also provide robust P10/P90 entry reference values for the UI and
        execution layer.
        """
        if target_mode not in ('center', 'zero', 'either'):
            target_mode = 'center'
        min_deviation_bps = max(0.0, float(min_deviation_bps or 0.0))
        entry_quantile = float(entry_quantile)
        if not math.isfinite(entry_quantile) or not 50 <= entry_quantile <= 99.9:
            raise ValueError('入场分位无效')
        cache_key = (symbol, pair_key, seconds, float(band_bps), target_mode, min_deviation_bps, entry_quantile, execution_basis)
        cached = self._reversion_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        now = int(time.time())
        prepared = self.prepared_samples(symbol,pair_key,seconds)
        raw_samples = prepared['raw_samples']
        invalid_quote_samples = prepared['invalid_quote_samples']
        clean_rows = prepared['rows']
        anomaly_count = prepared['anomaly_count']
        anomaly_threshold_bps = prepared['anomaly_threshold_bps']
        # The raw point count remains visible in the response so the UI can
        # explain why a chart and the reversion sample count differ.
        rows = clean_rows
        values = prepared['ordered']
        if execution_basis:
            from src.execution.target_return import ratio_to_directed_bps
            rows = [(stamp, ratio_to_directed_bps(value)) for stamp, value in rows]
            values = [ratio_to_directed_bps(value) for value in values]
        center_bps = self.ordered_quantile(values, 0.5)
        p10_bps = self.ordered_quantile(values, 0.10)
        p90_bps = self.ordered_quantile(values, 0.90)
        center_target = float(center_bps or 0.0)
        zero_target = 0.0

        def inside_target(spread: float) -> bool:
            center_inside = abs(spread - center_target) <= abs(float(band_bps))
            zero_inside = abs(spread - zero_target) <= abs(float(band_bps))
            if target_mode == 'zero':
                return zero_inside
            if target_mode == 'either':
                return center_inside or zero_inside
            return center_inside

        def target_deviation(spread: float) -> float:
            distances = [abs(spread - center_target)]
            if target_mode in ('zero', 'either'):
                distances.append(abs(spread - zero_target))
            return min(distances)

        events = []
        outside_started_at = None
        outside_started_spread = None
        peak_deviation_bps = 0.0
        for timestamp, spread in rows:
            spread = float(spread)
            deviation = target_deviation(spread)
            if not inside_target(spread):
                if outside_started_at is None:
                    outside_started_at = int(timestamp)
                    outside_started_spread = spread
                    peak_deviation_bps = deviation
                else:
                    peak_deviation_bps = max(peak_deviation_bps, deviation)
            elif outside_started_at is not None:
                ended_at = int(timestamp)
                if peak_deviation_bps >= min_deviation_bps:
                    events.append({
                        # ``timestamp`` and ``spread_bps`` remain the end-point
                        # aliases used by the compact summary and older clients.
                        "timestamp": ended_at,
                        "spread_bps": spread,
                        "start_timestamp": outside_started_at,
                        "start_spread_bps": outside_started_spread,
                        "duration_seconds": max(0, ended_at - outside_started_at),
                        "target_mode": target_mode,
                        "target_bps": center_target if target_mode == 'center' else 0.0,
                        "peak_deviation_bps": peak_deviation_bps,
                    })
                outside_started_at = None
                outside_started_spread = None
                peak_deviation_bps = 0.0
        intervals = [later["timestamp"] - earlier["timestamp"]
                     for earlier, later in zip(events, events[1:])
                     if later["timestamp"] > earlier["timestamp"]]
        ordered = sorted(intervals)
        median_interval = None
        if ordered:
            middle = len(ordered) // 2
            median_interval = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        result = {
            'symbol': symbol,
            'pair_key': pair_key,
            'seconds': seconds,
            'sample_interval_seconds': prepared.get('sample_interval_seconds', 30),
            'approximate': prepared.get('approximate', False),
            'band_bps': band_bps,
            'min_deviation_bps': min_deviation_bps,
            'target_mode': target_mode,
            'spread_basis': 'max_price' if execution_basis else 'buy_ask',
            'center_bps': center_bps,
            'p10_bps': p10_bps,
            'p90_bps': p90_bps,
            'entry_upper_bps': self.ordered_quantile(values, entry_quantile / 100),
            'entry_lower_bps': self.ordered_quantile(values, 1 - entry_quantile / 100),
            'samples': len(rows),
            'raw_samples': raw_samples,
            'invalid_quote_samples': invalid_quote_samples,
            'sample_span_seconds': rows[-1][0] - rows[0][0] if rows else 0,
            'last_sample_at': rows[-1][0] if rows else None,
            'center_ready': (len(rows) >= 30 and rows[-1][0] - rows[0][0] >= 870 and
                             0 <= now - rows[-1][0] <= 120),
            'anomaly_count': anomaly_count,
            'anomaly_threshold_bps': anomaly_threshold_bps,
            'anomaly_policy': '异常点保留在原始图表，但不计入中心、分位数和回归次数',
            'reversion_count': len(events),
            'last_reversion_at': events[-1]['timestamp'] if events else None,
            'last_reversion_age_seconds': max(0, now - events[-1]["timestamp"]) if events else None,
            'median_interval_seconds': median_interval,
            # Keep the diagnostic payload small while retaining enough detail
            # for the drawer's expandable history table. The full event count
            # above remains available for filtering and summary statistics.
            'events': [
                {
                    **event,
                    'interval_seconds': (
                        event["timestamp"] - events[index - 1]["timestamp"]
                        if index > 0 else None
                    ),
                }
                for index, event in list(enumerate(events))[-50:][::-1]
            ],
        }
        self._reversion_cache[cache_key] = (time.monotonic(), result)
        if len(self._reversion_cache) > 2048:
            oldest = min(self._reversion_cache, key=lambda key: self._reversion_cache[key][0])
            self._reversion_cache.pop(oldest, None)
        return result
