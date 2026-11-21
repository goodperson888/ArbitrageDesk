"""Short-lived, account-wide evidence; background reads never authorize orders.

Use the beginning of a read as its age, discard reads racing a mutation or
account notification, and singleflight foreground misses with background reads.
No balances, credentials or raw account payloads are written to disk.
"""
import threading
import time
from contextlib import contextmanager


class AccountSnapshotCache:
    max_age = 2.0
    refresh_interval = 5.0

    def __init__(self, read, identity, revision=lambda: None):
        self.read, self.identity, self.revision = read, identity, revision
        self.lock = threading.RLock()
        self.read_lock = threading.Lock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.snapshot = None
        self.generation = 0
        self.mutations = 0
        self.uncertain = False
        self.owner = None
        self.retry_at = 0.0
        self.error = None
        self.background_ready = lambda: True

    def _token(self):
        return self.identity(), self.revision(), self.generation

    def get(self):
        with self.lock:
            item = self.snapshot
            if (item is None or self.stop_event.is_set() or self.mutations or self.uncertain or
                    item[2] != self._token() or
                    not 0 <= time.monotonic() - item[1] <= self.max_age):
                return None
            return item

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.snapshot = None
        self.wake.set()

    def reserve(self, execution_id, token):
        with self.lock:
            if not execution_id or self.owner not in (None, execution_id):
                raise ValueError('账户额度已由另一笔执行预占')
            item = self.get()
            if item is None or item[2] != token:
                raise ValueError('账户快照已变化，需重新核对')
            self.owner = execution_id

    def valid_proof(self, execution_id, token):
        with self.lock:
            item = self.get()
            return self.owner == execution_id and item is not None and item[2] == token

    def release(self, execution_id):
        with self.lock:
            if self.owner == execution_id:
                self.owner = None

    @contextmanager
    def mutation(self):
        with self.lock:
            self.mutations += 1
            self.invalidate()
        outcome = {'confirmed': False}
        try:
            yield outcome
        finally:
            with self.lock:
                self.uncertain = self.uncertain or not outcome['confirmed']
                self.mutations -= 1
                self.invalidate()

    def reconciled(self, confirmed):
        if confirmed:
            with self.lock:
                self.uncertain = False
                self.invalidate()

    def refresh(self, reuse=True):
        if reuse:
            item = self.get()
            if item is not None:
                return item
        with self._read_guard():
            if reuse:
                item = self.get()
                if item is not None:
                    return item
            with self.lock:
                if self.stop_event.is_set():
                    raise ValueError('账户快照维护已停止')
                if self.mutations or self.uncertain:
                    raise ValueError('账户有执行中或待核对订单，快照不可复用')
                if time.monotonic() < self.retry_at:
                    raise ValueError('账户快照读取失败，正在等待重查')
                token, started = self._token(), time.monotonic()
            try:
                state = self.read()
            except Exception as exc:
                with self.lock:
                    self.invalidate()
                    self.error = type(exc).__name__
                    self.retry_at = time.monotonic() + max(5, float(getattr(exc, 'retry_after_seconds', 0)))
                raise
            with self.lock:
                if token != self._token() or self.mutations or self.uncertain:
                    self.snapshot = None
                    raise ValueError('读取期间账户状态已变化，旧结果不可用于开仓')
                self.generation += 1
                self.snapshot = state, started, self._token()
                self.error = None
                self.retry_at = 0.0
                return self.snapshot

    @contextmanager
    def _read_guard(self):
        if not self.read_lock.acquire(timeout=2):
            raise ValueError('账户快照已有读取任务，等待后台重试')
        try:
            yield
        finally:
            self.read_lock.release()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='entry-account-snapshot', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            try:
                if self.background_ready():
                    self.refresh()
            except Exception:
                pass  # Never log an account response or retry a 429 inline.
            self.wake.wait(self.refresh_interval)
            # Invalidations may arrive in bursts. Failed reads retain retry_at
            # and cannot turn this wake-up into another network request.

    def stop(self):
        self.stop_event.set()
        self.invalidate()

    def status(self):
        with self.lock:
            item = self.get()
            return dict(ready=item is not None,
                        age_seconds=round(time.monotonic() - self.snapshot[1], 3) if self.snapshot else None,
                        max_age_seconds=self.max_age, refresh_interval_seconds=self.refresh_interval,
                        mutation_in_progress=bool(self.mutations), reconciliation_required=self.uncertain,
                        error=self.error)

