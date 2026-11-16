"""Share exchange HTTP cooldowns across local workers; never retry a 429 inline."""
from src.common import file_lock as fcntl
from src.common.platform_support import user_scope
import hashlib
import json
import math
import os
import random
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit


class RateLimitError(RuntimeError):
    def __init__(self, seconds):
        self.retry_after_seconds = max(1, math.ceil(seconds))
        super().__init__(f'接口限流 HTTP 429；暂停 REST 请求 {self.retry_after_seconds} 秒')


class DiscoveryThrottleError(RuntimeError):
    """Local public-book pacing; never an exchange rejection or global stop."""
    def __init__(self, seconds):
        self.wait_seconds = max(1, math.ceil(seconds))
        super().__init__(f'本地只读盘口 REST 共享节流；等待 {self.wait_seconds} 秒（非平台限流）')


class RateLimitGate:
    def __init__(self, base_url, directory=None):
        origin = urlsplit(base_url).netloc.lower()
        directory = Path(directory or (Path(tempfile.gettempdir()) / f'airdrop-http-cooldown-{user_scope()}'))
        self.path = directory / (hashlib.sha256(origin.encode()).hexdigest()[:24] + '.json')
        self.lock = threading.RLock()

    def _read(self):
        try:
            if self.path.stat().st_size > 2048:
                return {}
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def remaining(self):
        return max(0.0, float(self._read().get('until', 0)) - time.time())

    def check(self):
        remaining = self.remaining()
        if remaining > 0:
            raise RateLimitError(remaining)

    def discovery_delay(self, interval=2, priority=False):
        """Queue readiness without spending a slot or raising local throttles.

        Candidate coordinators reserve the next slot using the same bounded
        priority ticket as reserve_discovery. UI/background readers continue
        to share its deadline. The actual request must still reserve its slot.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pacing_path = self.path.with_suffix('.discovery.json')
        with self.lock, self.path.with_suffix('.lock').open('a') as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            exchange_delay = self.remaining()
            if exchange_delay:
                return exchange_delay
            now = time.time()
            try:
                pacing = json.loads(pacing_path.read_text())
                until = float(pacing.get('until', 0))
                priority_until = float(pacing.get('priority_until', 0))
            except (OSError, ValueError, TypeError):
                until = priority_until = 0
            if priority and until > now and priority_until < until + interval:
                self._write_discovery(pacing_path, dict(until=until, priority_until=until+interval))
            return max(0.0, (until if priority else max(until, priority_until)) - now)

    def reserve_discovery(self, interval=2, priority=False):
        """Bound optional REST book discovery across local processes.

        Cache hits and required execution/account/close reads do not consume
        this allowance. The real exchange cooldown still applies to all REST.
        A waiting candidate preparation may reserve the next slot ahead of
        reference probes, but can never shorten the shared request interval.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pacing_path = self.path.with_suffix('.discovery.json')
        with self.lock, self.path.with_suffix('.lock').open('a') as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            self.check()
            now = time.time()
            try:
                pacing = json.loads(pacing_path.read_text())
                until = float(pacing.get('until', 0))
                priority_until = float(pacing.get('priority_until', 0))
            except (OSError, ValueError, TypeError):
                until = priority_until = 0
            blocked_until = until if priority else max(until, priority_until)
            if blocked_until > now:
                if priority and until > now and priority_until < until + interval:
                    # One bounded reservation tied to the LAST admitted read.
                    # Repeated failures cannot push an abandoned ticket out
                    # forever; another process can use the slot after expiry.
                    self._write_discovery(pacing_path, dict(until=until, priority_until=until+interval))
                raise DiscoveryThrottleError(blocked_until - now)
            # Consuming the slot also clears the priority reservation.
            self._write_discovery(pacing_path, {'until': now + interval})

    def _write_discovery(self, path, payload):
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.discovery-')
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(payload, out)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def reject(self, response):
        if response.status_code != 429:
            return
        now = time.time()
        retry = response.headers.get('Retry-After')
        try:
            delay = float(retry)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(retry).timestamp() - now
            except (TypeError, ValueError, OverflowError):
                delay = 0
        if not math.isfinite(delay):
            delay = 0
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock, self.path.with_suffix('.lock').open('a') as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            old = self._read()
            failures = min(6, int(old.get('failures', 0)) + 1) if now - old.get('last', 0) < 600 else 1
            delay = max(1, delay, min(120, 5 * 2 ** (failures - 1)) + random.uniform(0, 1))
            until = max(now + delay, float(old.get('until', 0)))
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.cooldown-')
            try:
                with os.fdopen(fd, 'w') as out:
                    json.dump({'until': until, 'last': now, 'failures': failures}, out)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        raise RateLimitError(until - now)
