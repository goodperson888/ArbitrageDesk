"""Sample existing quote checks only. Never an admission filter or a timer."""
import math
import time
from collections import OrderedDict, deque
from pathlib import Path

from src.common.market_data import utc_now


class OpportunityObserver:
    WINDOWS_MS = (100, 200, 300, 500, 1000)

    def __init__(self, path, writer):
        self.path, self.writer = Path(path), writer
        self.routes, self.attempts = OrderedDict(), OrderedDict()

    @staticmethod
    def key(route, quantity):
        return route, str(quantity)

    def observe(self, route, quantity, first, second, edge, minimum, gross):
        try:
            if not all(math.isfinite(float(v)) for v in (edge, minimum, gross)):
                return
            # A repeated cached snapshot is not another independent sample.
            version = tuple(tuple(str(q.get(k, '')) for k in
                ('updated_at', 'nonce', 'offset', 'bid', 'ask', 'size')) for q in (first, second))
            key, now = self.key(route, quantity), time.monotonic()
            rows = self.routes.pop(key, deque(maxlen=128))
            if rows and now-rows[-1]['at'] > 3:
                rows.clear()
            if not rows or rows[-1]['version'] != version:
                rows.append(dict(at=now, version=version, edge_bps=float(edge),
                                 minimum_bps=float(minimum), gross_bps=float(gross)))
            self.routes[key] = rows
            while len(self.routes) > 256:
                self.routes.popitem(last=False)
        except Exception:
            pass  # Optional evidence cannot change the trading decision.

    def begin(self, position_id, route, quantity):
        try:
            now = time.monotonic()
            rows = [dict(row) for row in self.routes.get(self.key(route, quantity), ())
                    if now-row['at'] <= 3]
            windows = {}
            for ms in self.WINDOWS_MS:
                cutoff = now-ms/1000
                prior = [i for i,row in enumerate(rows) if row['at'] <= cutoff]
                sample = rows[prior[-1]:] if prior else []
                gaps = ([b['at']-a['at'] for a,b in zip(sample,sample[1:])] +
                        [now-sample[-1]['at']] if sample else [])
                covered = bool(sample and len(sample) >= 3 and
                    max(gaps) <= min(.1, ms/2000))
                windows[str(ms)] = ('sampled_valid' if all(
                    r['edge_bps'] > 0 and r['edge_bps'] >= r['minimum_bps'] for r in sample)
                    else 'sampled_invalid') if covered else 'insufficient_samples'
            trend = 'insufficient_samples'
            if len(rows) >= 2:
                change = rows[-1]['gross_bps']-rows[-2]['gross_bps']
                trend = 'widening' if change > 0 else 'contracting' if change < 0 else 'unchanged'
            snapshot = dict(position_id=position_id, observed_at=utc_now(), route=route,
                quantity=quantity, observation_only=True, windows_ms=windows, trend=trend,
                samples=[dict(age_ms=round((now-r['at'])*1000,3),
                    edge_bps=r['edge_bps'], minimum_bps=r['minimum_bps'], gross_bps=r['gross_bps']) for r in rows],
                coverage_note='仅为已有报价检查的离散采样，不证明期间连续可成交')
            self.attempts[position_id] = snapshot
            while len(self.attempts) > 128:
                self.attempts.popitem(last=False)
        except Exception:
            pass

    def outcome(self, position_id, result, **details):
        try:
            snapshot = self.attempts.pop(position_id, None)
            if snapshot is not None and self.writer is not None:
                snapshot.update(outcome=result, completed_at=utc_now(), **details)
                self.writer.submit_entry(self.path, snapshot)
        except Exception:
            pass
