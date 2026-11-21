"""Public-data validation. A fetched mark is not an executable quote."""
import math
import threading
import re
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import httpx
import requests
from urllib.parse import urlsplit
from src.common.rate_limit import RateLimitGate
from src.common.quote_clock import quote_clock, QuoteClockUnavailable


class MarketDataError(RuntimeError):
    pass


@contextmanager
def request_slot(lock, timeout=None):
    acquired = lock.acquire() if timeout is None else lock.acquire(timeout=timeout)
    if not acquired:
        raise MarketDataError('只读请求等待超时，稍后重试')
    try:
        yield
    finally:
        lock.release()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_quote_time(observed, received, max_future_seconds=0.5, *, received_monotonic=None):
    """Map exchange UTC into the local receipt clock, preserving old ages.

    Only call on a newly received quote (or its original cached receipt).
    Never use the current clock to renew an existing cached quote.
    """
    try:
        offset, uncertainty = quote_clock.correction(received, received_monotonic)
    except QuoteClockUnavailable as exc:
        raise MarketDataError(str(exc)) from None
    observed = observed-timedelta(seconds=offset)
    if (observed - received).total_seconds() > max_future_seconds:
        if quote_clock.status().get('calibrating'):
            raise MarketDataError('行情后台校时中，等待可靠时间源')
        raise MarketDataError('行情时间超前，等待同步报价')
    return min(observed-timedelta(seconds=uncertainty), received)


def cached_quote_age(age, observed_monotonic=None):
    """A wall-clock adjustment must never extend a received quote's life."""
    if observed_monotonic is None:
        return age
    elapsed = time.monotonic()-observed_monotonic
    if not math.isfinite(elapsed) or elapsed < 0:
        return None
    return max(age, elapsed) if age is not None else None


def cached_quote_time(timestamp, observed_monotonic=None):
    """Expose monotonic age to downstream consumers of ISO timestamps too."""
    if observed_monotonic is None:
        return timestamp
    age = age_seconds(timestamp)
    elapsed = time.monotonic()-observed_monotonic
    if age is not None and math.isfinite(elapsed) and elapsed >= 0 and elapsed-age > .01:
        return (datetime.now(timezone.utc)-timedelta(seconds=elapsed)).isoformat()
    return timestamp


def number(value, positive=False):
    if isinstance(value, bool):
        raise MarketDataError('行情数值不能为布尔值')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise MarketDataError('行情数值缺失或格式错误') from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise MarketDataError('行情数值无效')
    return result


def age_seconds(timestamp):
    try:
        # Python 3.9 rejects nanosecond ISO fractions used by Variational.
        normalized = re.sub(r'(\.\d{6})\d+', r'\1', timestamp)
        parsed = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        return age if age >= -5 else None
    except (ValueError, TypeError, AttributeError):
        return None


class PublicClient:
    def __init__(self, base_url, ttl=10, session=None, proxy_url=None, request_timeout=12):
        self.base_url = base_url.rstrip('/')
        self.ttl = max(float(ttl), 2)
        self.request_timeout = max(float(request_timeout), 2.0)
        self.proxy_url = proxy_url
        self._proxy_failed_until = 0.0
        self._direct_session = None
        # httpx handles the TLS/HTTP behavior of the Lighter gateways more
        # reliably than the platform requests build on macOS.
        self.session = session or httpx.Client(
            proxy=proxy_url,
            timeout=httpx.Timeout(self.request_timeout, connect=5.0),
            http2=False,
            # Only use the proxy explicitly supplied by the application.
            # In particular, the shell's HTTPS_PROXY can route the direct
            # fallback back through a WAF edge and turn a valid request into
            # an HTTP 405 challenge.
            trust_env=False,
            headers={'Accept': 'application/json'},
        )
        if session is not None:
            self.session.headers.update({'Accept': 'application/json'})
        self._cache = {}
        self._http_rejections = {}
        self._lock = threading.RLock()
        self._key_locks = tuple(threading.RLock() for _ in range(32))
        self.rate_limit = RateLimitGate(self.base_url)

    def get(self, path, params=None, ttl=None, discovery=False, *, request_timeout=None):
        key = (path, tuple(sorted((params or {}).items())))
        # Keep one in-flight read per key; a slow optional market request
        # must not hold a global lock over account/other-market requests.
        with request_slot(self._key_locks[hash(key) % len(self._key_locks)],request_timeout):
            now = time.monotonic()
            rejected = self._http_rejections.get(key)
            if rejected and now < rejected[0]:
                raise MarketDataError(rejected[1])
            cached = self._cache.get(key)
            if cached and now - cached[0] < (self.ttl if ttl is None else ttl):
                if cached[2]:
                    raise MarketDataError(cached[2])
                return cached[1], cached[3]
            proxy_attempt_failed = False
            self.rate_limit.check()
            if discovery:
                from src.execution.preparation import is_preparing
                self.rate_limit.reserve_discovery(priority=is_preparing())
            try:
                response = None
                use_proxy = bool(self.proxy_url) and time.monotonic() >= self._proxy_failed_until
                request_session = self.session
                if not use_proxy and isinstance(self.session, httpx.Client) and self.proxy_url:
                    if self._direct_session is None:
                        self._direct_session = httpx.Client(
                            timeout=httpx.Timeout(self.request_timeout, connect=3.0),
                            http2=False, trust_env=False,
                            headers={'Accept': 'application/json'},
                        )
                    request_session = self._direct_session
                for attempt in range(2):
                    try:
                        if isinstance(request_session, httpx.Client):
                            options = ({'timeout':httpx.Timeout(request_timeout,connect=min(2,request_timeout),pool=.5)}
                                       if request_timeout is not None else {})
                            response = request_session.get(self.base_url + path, params=params,**options)
                        else:
                            response = request_session.get(self.base_url + path, params=params,
                                                           timeout=(min(5,request_timeout or 5), request_timeout or self.request_timeout))
                        self.rate_limit.reject(response)
                        response.raise_for_status()
                        break
                    except (httpx.HTTPError, requests.RequestException) as request_error:
                        # HTTP rejections are exchange responses, not a broken
                        # proxy. Never retry them through a different route.
                        if getattr(request_error, 'response', None) is not None:
                            raise
                        if not isinstance(request_session, httpx.Client) or attempt == 1:
                            raise
                        time.sleep(0.2)
                        if self.proxy_url and request_session is self.session:
                            proxy_attempt_failed = True
                            self._proxy_failed_until = time.monotonic() + 60.0
                            if self._direct_session is None:
                                self._direct_session = httpx.Client(
                                    timeout=httpx.Timeout(self.request_timeout, connect=3.0),
                                    http2=False, trust_env=False,
                                    headers={'Accept': 'application/json'},
                                )
                            request_session = self._direct_session
                if not isinstance(response, httpx.Response):
                    # Keep injected requests-like sessions usable by the
                    # existing unit tests and local tooling.
                    data = response.json()
                else:
                    # Client.get has already buffered the entire response.
                    data = response.json()
                if data is None:
                    raise ValueError('响应不是完整 JSON')
                response.close()
                if not isinstance(data, dict) or data.get('code', 200) != 200:
                    raise MarketDataError('接口返回错误格式或状态')
            except (httpx.HTTPError, requests.RequestException, ValueError, MarketDataError) as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                if proxy_attempt_failed and self.proxy_url:
                    parsed = urlsplit(self.proxy_url)
                    proxy_host = parsed.hostname or '本地代理'
                    proxy_port = ':' + str(parsed.port) if parsed.port else ''
                    error = f'本地代理不可用或直连失败（{proxy_host}{proxy_port}）'
                else:
                    error = '行情请求失败: ' + (f'HTTP {status}' if status else type(exc).__name__)
                # A rejected endpoint must not be hammered by every scan.
                # Keep a short cooldown independent of the caller's quote TTL;
                # no old price is returned while the endpoint is unavailable.
                if status == 405:
                    self._cache.pop(key, None)
                    self._http_rejections[key] = (time.monotonic() + 5, error)
                else:
                    self._cache[key] = (time.monotonic(), None, error, utc_now())
                raise MarketDataError(error) from None
            fetched_at = utc_now()
            self._http_rejections.pop(key, None)
            self._cache[key] = (time.monotonic(), data, None, fetched_at)
            return data, fetched_at

    def close(self):
        self.session.close()
        if self._direct_session is not None:
            self._direct_session.close()
