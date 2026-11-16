"""Per-market discovery backoff. Never used for execution or holding reads."""
import math
import re
import threading
import time

from src.common.market_data import MarketDataError


class EmptyBookError(MarketDataError):
    pass


ARCUS_SUBSCRIPTIONS_PER_CONNECTION = 100


class ArcusSubscriptionError(RuntimeError):
    pass


def arcus_subscription_batches(items, channels_per_market):
    """Budget channels, not markets, against Arcus' per-socket limit."""
    size = ARCUS_SUBSCRIPTIONS_PER_CONNECTION // channels_per_market
    if size < 1:
        raise ValueError('Arcus 每个市场的订阅数量超过单连接上限')
    items = list(items)
    return [items[i:i+size] for i in range(0, len(items), size)]


def check_arcus_subscription_error(message):
    if message.get('type') == 'error':
        text = str(message.get('message', '')).lower()
        reason = ('Arcus 单连接订阅超过平台上限（100），行情覆盖不完整'
                  if 'too many subscriptions' in text else 'Arcus 行情订阅被拒绝')
        raise ArcusSubscriptionError(reason)


class DiscoveryCooldownError(RuntimeError):
    def __init__(self, venue, kind, seconds):
        self.wait_seconds = max(1, math.ceil(seconds))
        reason = {'unavailable': '市场暂不可用', 'empty': '盘口为空或缺少一侧'}[kind]
        super().__init__(f'{venue} {reason}；参考价探测冷却 {self.wait_seconds} 秒')


class DiscoveryCooldown:
    """One probe after 30/60/120/240/300 seconds; fresh WS bypasses it.

    Keep expired entries until recovery so a persistent WS error/empty book
    cannot continually restart the initial delay and starve recovery probes.
    """
    def __init__(self, venue):
        self.venue = venue
        self._states = {}
        self._observed = set()
        self._lock = threading.RLock()

    def observe(self, symbol, kind):
        with self._lock:
            # A successful REST probe may precede the next WS update. Do not
            # let the same old WS error/empty snapshot restart the cooldown.
            if (symbol, kind) in self._observed:
                return
            self._observed.add((symbol, kind))
            self._states.setdefault(symbol, (time.monotonic() + 30, 0, kind))

    def failed(self, symbol, kind):
        with self._lock:
            previous = self._states.get(symbol)
            failures = min(5, (previous[1] if previous else 0) + 1)
            self._states[symbol] = (time.monotonic() + min(300, 30 * 2 ** (failures - 1)),
                                    failures, kind)

    def check(self, symbol):
        with self._lock:
            state = self._states.get(symbol)
            remaining = max(0.0, state[0] - time.monotonic()) if state else 0.0
            if remaining > 0:
                raise DiscoveryCooldownError(self.venue, state[2], remaining)

    def remaining(self, symbol):
        with self._lock:
            state = self._states.get(symbol)
            return max(0.0, state[0] - time.monotonic()) if state else 0.0

    def recovered(self, symbol):
        with self._lock:
            self._states.pop(symbol, None)

    def fresh_stream(self, symbol):
        with self._lock:
            self.recovered(symbol)
            self._observed.difference_update({(symbol, 'empty'), (symbol, 'unavailable')})


def arcus_unavailable_market(message):
    """Recognize only the public server's fixed market-unavailable envelope."""
    if not isinstance(message, dict) or message.get('type') != 'error':
        return None
    match = re.fullmatch(r"Market '([A-Za-z0-9._-]{1,80})' is not available", str(message.get('message', '')))
    return match[1].upper() if match else None
