"""Bounded observations from real equal-size entry attempts.

No requests or writes occur here. The coordinator persists this evidence with
its existing state saves, after the hedge decision, never between quote/send.
Price/exit-credit observations do not set execution budgets or profit floors.
Known zero/partial-fill outcomes retain the existing liquidity size recovery.
"""
import math
import time


REASON_LABELS = {
    'edge': '净价差不足', 'price': '价格保护越界', 'timeout': '补齐窗口超时',
    'amount': '金额上限', 'zero_fill': '第二腿零成交',
    'partial_fill': '第二腿部分成交', 'rejected': '第二腿拒单',
    'quantity': '数量步长不匹配', 'accounting': '费用待核对', 'unknown': '其他待核对',
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentile(values, fraction=.9):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else 0.0


class ExecutionWindowRisk:
    MAX_ROUTES = 256
    MAX_SAMPLES = 64
    MAX_AGE = 86400
    MIN_SAMPLES = 5
    FAILURE_MEMORY_SECONDS = 900

    def __init__(self, saved=None):
        self.routes = {}
        self._quality = {}
        if not isinstance(saved, dict):
            return
        for key, value in list(saved.items())[-self.MAX_ROUTES:]:
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            saved_samples = value.get('samples')
            saved_counts = value.get('counts')
            samples = [dict(s) for s in (saved_samples if isinstance(saved_samples, list) else [])[-self.MAX_SAMPLES:]
                       if isinstance(s, dict) and all(finite(s.get(k)) and s[k] >= 0
                           for k in ('at', 'elapsed_ms', 'first_bps', 'second_bps'))]
            for sample in samples:
                decay = sample.get('projection_decay_bps', 0.0)
                sample['projection_decay_bps'] = decay if finite(decay) and decay >= 0 else 0.0
                sample['failed'] = sample.get('failed') is True
                if not isinstance(sample.get('attempt_id'), str) or not 0 < len(sample['attempt_id']) <= 64:
                    sample.pop('attempt_id', None)
            counts = {k: int(n) for k, n in (saved_counts if isinstance(saved_counts, dict) else {}).items()
                      if k in REASON_LABELS and type(n) is int and n >= 0}
            profile = dict(samples=samples, counts=counts)
            if all(finite(value.get(k)) and value[k] > 0 for k in ('size_cap_usdc', 'size_cap_at')):
                profile.update(size_cap_usdc=value['size_cap_usdc'], size_cap_at=value['size_cap_at'])
            self.routes[key] = profile

    def _profile(self, key):
        if key not in self.routes:
            if len(self.routes) >= self.MAX_ROUTES:
                self.routes.pop(next(iter(self.routes)))
            self.routes[key] = dict(samples=[], counts={})
        return self.routes[key]

    def evidence(self, key, now=None):
        now = time.time() if now is None else now
        profile = self.routes.get(key, {})
        samples = [s for s in profile.get('samples', []) if 0 <= now - s['at'] <= self.MAX_AGE]
        ready = len(samples) >= self.MIN_SAMPLES
        # A realized rollback is useful evidence immediately. Keep its downside
        # for a short window even if a route has too few samples for a P90, or
        # one outlier would otherwise disappear in the percentile. Other routes
        # and the opposite direction retain their own evidence.
        failed = [s for s in samples if s.get('failed') and now - s['at'] <= self.FAILURE_MEMORY_SECONDS]
        movement = percentile([max(s['first_bps'], s['second_bps']) for s in samples]) if ready else 0.0
        decay = percentile([s.get('projection_decay_bps', 0.0) for s in samples]) if ready else 0.0
        return dict(samples=len(samples),
                    observation_only=True,
                    movement_bps=max([movement] + [max(s['first_bps'], s['second_bps']) for s in failed]),
                    projection_decay_bps=max([decay] + [s.get('projection_decay_bps', 0.0) for s in failed]),
                    recent_failures=len(failed),
                    window_ms=percentile([s['elapsed_ms'] for s in samples]) if ready else 0.0)

    def observe(self, key, first_reference, first_fill, second_reference, second_price,
                second_is_buy, elapsed_ms, now=None, projection_decay_bps=0.0, attempt_id=None):
        prices = (first_reference, first_fill, second_reference, second_price)
        if (not all(finite(p) and p > 0 for p in prices) or not finite(elapsed_ms) or elapsed_ms < 0 or
                not finite(projection_decay_bps) or projection_decay_bps < 0):
            return
        # A short first leg is harmed by a lower fill; its buy hedge by a
        # higher ask. The opposite route reverses both signs.
        first = (1 - first_fill / first_reference) if second_is_buy else (first_fill / first_reference - 1)
        second = (second_price / second_reference - 1) if second_is_buy else (1 - second_price / second_reference)
        if not finite(first * 10000) or not finite(second * 10000):
            return
        sample = dict(at=time.time() if now is None else now, elapsed_ms=elapsed_ms,
                      first_bps=max(0.0, first * 10000), second_bps=max(0.0, second * 10000),
                      projection_decay_bps=projection_decay_bps)
        profile = self._profile(key)
        if isinstance(attempt_id, str) and 0 < len(attempt_id) <= 64:
            sample['attempt_id'] = attempt_id
            # A local requote belongs to this same execution, not another
            # independent sample. Retain the worst observed downside so a
            # deterioration on the final retry is not lost.
            if profile['samples'] and profile['samples'][-1].get('attempt_id') == attempt_id:
                previous = profile['samples'][-1]
                for field in ('elapsed_ms', 'first_bps', 'second_bps', 'projection_decay_bps'):
                    previous[field] = max(previous.get(field, 0.0), sample[field])
                return
        profile['samples'] = (profile['samples'] + [sample])[-self.MAX_SAMPLES:]

    def outcome(self, key, reason, notional, now=None, ceiling=None, attempt_id=None):
        now = time.time() if now is None else now
        profile = self._profile(key)
        outcome_reason = reason if reason in REASON_LABELS or reason == 'paired' else 'unknown'
        if key not in self._quality and len(self._quality) >= self.MAX_ROUTES:
            self._quality.pop(next(iter(self._quality)))
        self._quality[key] = (self._quality.get(key, ()) + ((now, outcome_reason),))[-self.MAX_SAMPLES:]
        if reason == 'paired':
            # Restore size gradually; a single small successful hedge must
            # not immediately return a fragile route to the original size.
            if self.size_cap(key, now) is not None:
                enlarged = profile['size_cap_usdc'] * 1.25
                if finite(ceiling) and ceiling > 0 and enlarged >= ceiling:
                    profile.pop('size_cap_usdc', None)
                    profile.pop('size_cap_at', None)
                elif math.isfinite(enlarged):
                    profile['size_cap_usdc'] = enlarged
                    profile['size_cap_at'] = now
            return
        reason = reason if reason in REASON_LABELS else 'unknown'
        profile['counts'][reason] = profile['counts'].get(reason, 0) + 1
        if reason in ('edge', 'price') and profile['samples']:
            latest = profile['samples'][-1]
            if (0 <= now - latest['at'] <= self.FAILURE_MEMORY_SECONDS and
                    (attempt_id is None or latest.get('attempt_id') == attempt_id)):
                latest['failed'] = True
        if reason in ('zero_fill', 'partial_fill') and finite(notional) and notional > 0:
            profile['size_cap_usdc'] = min(notional, self.size_cap(key, now) or notional) / 2
            profile['size_cap_at'] = now

    def size_cap(self, key, now=None):
        now = time.time() if now is None else now
        profile = self.routes.get(key, {})
        if 0 <= now - profile.get('size_cap_at', 0) <= self.MAX_AGE:
            return profile.get('size_cap_usdc')
        return None

    def quality(self, key, now=None):
        """Only rank economically tied candidates; never alter entry guards."""
        now = time.time() if now is None else now
        rows = [reason for at, reason in self._quality.get(key, ())
                if 0 <= now - at <= self.FAILURE_MEMORY_SECONDS and
                reason in ('paired', 'edge', 'price', 'timeout', 'zero_fill', 'partial_fill')]
        return (1 + rows.count('paired')) / (2 + len(rows))


def hedge_failures(*, timely, within_cap, within_price, observed_net_bps, minimum_bps):
    failures = []
    if not timely:
        failures.append('timeout')
    if not within_cap:
        failures.append('amount')
    if observed_net_bps + 1e-7 < minimum_bps:
        failures.append('edge')
    if not within_price:
        failures.append('price')
    return failures
