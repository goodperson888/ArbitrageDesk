"""Bounded background center snapshots, shared by local API and executor.

Readers never query historical observations or wait for a calculation. Raw
observations and detailed reversion events remain in the original database.
"""
import copy
from src.common import file_lock as fcntl
import hashlib
import json
import math
import os
import queue
import tempfile
import threading
import time
from pathlib import Path


_services = {}
_services_lock = threading.Lock()
VERSION = 4  # Execution coordinates; old ratio-denominator snapshots expire.


def center_stats_for(history, symbol, route, seconds=604800, entry_quantile=90):
    if callable(getattr(type(history), 'center_stats', None)):
        return history.center_stats(symbol, route, seconds, entry_quantile)
    return history.reversion_stats(symbol, route, seconds, 2.0, 'center',
                                   entry_quantile=entry_quantile)


def reversion_stats_for(history, *args, **kwargs):
    if callable(getattr(type(history), 'shared_reversion_stats', None)):
        return history.shared_reversion_stats(*args, **kwargs)
    return history.reversion_stats(*args, **kwargs)


def service_for(history):
    key = str(Path(history.path).resolve())
    with _services_lock:
        if key not in _services or _services[key].closed.is_set():
            _services[key] = CenterStatistics(history)
        return _services[key]


def close_service(history):
    """Close an existing worker without creating one during shutdown."""
    key = str(Path(history.path).resolve())
    with _services_lock:
        service = _services.pop(key, None)
    if service:
        service.close()


class CenterStatistics:
    def __init__(self, history):
        self.history = history
        self.directory = Path(history.path).with_suffix('.statistics')
        self.cache, self.checked, self.pending = {}, {}, set()
        self.lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=2048)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name='history-statistics', daemon=True)
        self.thread.start()

    def _path(self, key):
        digest = hashlib.sha256(json.dumps((VERSION, key)).encode()).hexdigest()[:32]
        return self.directory / (digest + '.json')

    def get(self, symbol, route, seconds=604800, quantile=90):
        key = (symbol, route, int(seconds), float(quantile))
        if key[2] <= 0 or not math.isfinite(key[3]) or not 50 <= key[3] <= 99.9:
            raise ValueError('历史中心窗口或入场分位参数无效')
        return self._get(key)

    def get_reversion(self, symbol, route, seconds=604800, band_bps=2.0,
                      target_mode='center', min_deviation_bps=0.0, entry_quantile=90.0):
        key = (symbol, route, int(seconds), float(entry_quantile), 'reversion',
               float(band_bps), target_mode, float(min_deviation_bps))
        if (key[2] <= 0 or not all(math.isfinite(v) for v in (key[3],key[5],key[7]))
                or not 50 <= key[3] <= 99.9 or key[5] <= 0 or key[7] < 0
                or target_mode not in ('center','zero','either')):
            raise ValueError('历史回归统计参数无效')
        return self._get(key)

    def _get(self, key):
        now = time.time()
        with self.lock:
            payload = self.cache.get(key)
            # Coalesce disk checks across rapid quote refreshes. Snapshot
            # files are atomically replaced by any process doing the job.
            if now - self.checked.get(key, 0) >= 1:
                self.checked[key] = now
                try:
                    path = self._path(key)
                    if path.stat().st_size <= 30000:
                        candidate = json.loads(path.read_text())
                        if (isinstance(candidate, dict) and candidate.get('version') == VERSION
                                and candidate.get('key') == list(key)
                                and isinstance(candidate.get('stats'), dict)
                                and isinstance(candidate.get('calculated_at'), (int, float))
                                and math.isfinite(candidate['calculated_at'])):
                            payload = self.cache[key] = candidate
                except (OSError, ValueError, TypeError):
                    pass
            age = now - payload['calculated_at'] if payload else math.inf
            if not 0 <= age < 30 and key not in self.pending and len(self.pending) < 2048:
                try:
                    self.jobs.put_nowait(key)
                except queue.Full:
                    pass  # Grouped requests may still have obsolete queue entries.
                else:
                    self.pending.add(key)
            if len(self.cache) > 2048:
                oldest = min(self.cache, key=lambda k: self.cache[k]['calculated_at'])
                self.cache.pop(oldest, None)
                self.checked.pop(oldest, None)
        if payload is None:
            return dict(symbol=key[0], pair_key=key[1], center_bps=None, samples=0,
                        statistics_ready=False, statistics_status='loading', reversion_count=None,
                        center_ready=False, center_status='loading',
                        center_reason='历史统计正在后台计算，等待有效样本', seconds=key[2])
        result = copy.deepcopy(payload['stats'])
        result['statistics_age_seconds'] = max(0, age)
        result['statistics_ready'] = 0 <= age <= 120
        result['statistics_status'] = 'ready' if result['statistics_ready'] else 'stale'
        if len(key) > 4:
            stamp = result.get('last_reversion_at')
            result['last_reversion_age_seconds'] = max(0,now-stamp) if stamp is not None else None
        latest = result.get('last_sample_at')
        if (not 0 <= age <= 120 or not isinstance(latest, (int, float))
                or not math.isfinite(latest) or not 0 <= now - latest <= 120):
            result.update(center_ready=False, center_status='stale', center_reason='历史中心最近有效样本已过期，等待更新')
        return result

    def calculate(self, key):
        symbol, route, seconds, quantile = key[:4]
        if len(key) > 4:
            result = dict(self.history.reversion_stats(symbol, route, seconds, key[5], key[6], key[7], quantile,
                                                      execution_basis=True))
            # The drawer reads detailed events on demand; scanning only needs
            # the complete count/recency summary, shared with the executor.
            result.pop('events', None)
            return result
        now = int(time.time())
        prepared = self.history.prepared_samples(symbol,route,seconds)
        from src.execution.target_return import ratio_to_directed_bps
        clean = [(stamp, ratio_to_directed_bps(value)) for stamp, value in prepared['rows']]
        anomalies, threshold = prepared['anomaly_count'], prepared['anomaly_threshold_bps']
        # Sort once for all requested quantiles; do not build reversion events.
        ordered = [ratio_to_directed_bps(value) for value in prepared['ordered']]
        def percentile(values, fraction):
            if not values:
                return None
            at = (len(values) - 1) * fraction
            low = int(at); high = min(low + 1, len(values) - 1)
            return values[low] + (values[high] - values[low]) * (at - low)
        center = percentile(ordered, .5)
        from src.execution.channel_policy import sample_statistics
        channel = sample_statistics(clean, quantile)
        span = clean[-1][0] - clean[0][0] if clean else 0
        latest = clean[-1][0] if clean else None
        ready = len(clean) >= 30 and span >= 870 and latest is not None and 0 <= now - latest <= 120
        recent = {}
        for window in (3600, 21600, 86400):
            if window <= seconds:
                values = sorted(value for stamp, value in clean if stamp >= now - window)
                recent[str(window)] = dict(center_bps=percentile(values, .5), samples=len(values))
        short = recent.get('21600', recent.get('3600', {}))
        drift = (short['center_bps'] - center if short.get('samples', 0) >= 30 and center is not None else None)
        p10, p90 = percentile(ordered, .1), percentile(ordered, .9)
        drifting = ready and drift is not None and abs(drift) > max(2, (p90 - p10) / 2)
        status = 'drifting' if drifting else 'ready' if ready else 'warming' if clean else 'missing'
        reason = ('近期中心与统计窗口中心有偏移，供稳定性参考' if drifting else
                  '中心样本就绪' if ready else '历史中心有效样本不足30个或覆盖不足14.5分钟')
        return dict(symbol=symbol, pair_key=route, seconds=seconds, center_bps=center, spread_basis='max_price',
            samples=len(clean), raw_samples=prepared['raw_samples'], invalid_quote_samples=prepared['invalid_quote_samples'],
            anomaly_count=anomalies, anomaly_threshold_bps=threshold,
            first_sample_at=clean[0][0] if clean else None, last_sample_at=latest,
            sample_span_seconds=span, center_ready=ready, center_status=status, center_reason=reason,
            recent_centers=recent, center_drift_bps=drift, p10_bps=p10, p90_bps=p90,
            entry_upper_bps=percentile(ordered, quantile / 100),
            entry_lower_bps=percentile(ordered, 1 - quantile / 100),
            channel_statistics=channel,
            history_samples=len(clean), history_min_bps=ordered[0] if ordered else None,
            history_max_bps=ordered[-1] if ordered else None,
            history_avg_bps=sum(ordered)/len(ordered) if ordered else None,
            history_raw_samples=prepared['raw_samples'], history_raw_min_bps=prepared['raw_min_bps'],
            history_raw_max_bps=prepared['raw_max_bps'],
            history_anomaly_count=anomalies, history_anomaly_threshold_bps=threshold)

    def refresh(self, key):
        if self.closed.is_set() or not Path(self.history.path).is_file():
            return
        # The history owner manages the parent directory. A finished/stopped
        # owner must never be recreated by a late diagnostic job.
        self.directory.mkdir(exist_ok=True)
        path = self._path(key)
        with path.with_suffix('.lock').open('a') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                previous = json.loads(path.read_text())
                if previous.get('version') == VERSION and 0 <= time.time() - previous['calculated_at'] < 30:
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass
            payload = dict(version=VERSION, key=key, calculated_at=time.time(), stats=self.calculate(key))
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.statistics-')
            try:
                with os.fdopen(fd, 'w') as out:
                    json.dump(payload, out, ensure_ascii=False, allow_nan=False)
                os.replace(temporary, path)
                with self.lock:
                    self.cache[key] = payload
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _run(self):
        while not self.closed.is_set():
            try:
                key = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            start = time.monotonic()
            group = []
            try:
                with self.lock:
                    # Consume center and full-summary requests for the same
                    # samples together, before the bounded sample cache moves
                    # on to hundreds of other routes. Queued duplicates below
                    # become cheap no-ops after the group finishes.
                    if key in self.pending:
                        group = [key] + [k for k in self.pending if k != key and k[:3] == key[:3]]
                for item in group:
                    try:
                        self.refresh(item)
                    except Exception:
                        pass
            except Exception:
                pass  # Read failure retains the previous snapshot, never a fabricated center.
            finally:
                with self.lock:
                    self.pending.difference_update(group)
                self.jobs.task_done()
            if group:
                self.closed.wait(min(.25, max(.005, time.monotonic() - start)))

    def close(self):
        self.closed.set()
        self.thread.join(timeout=.3)
