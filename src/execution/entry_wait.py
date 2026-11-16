"""Keep existing holdings observable while one coordinated entry awaits I/O.

Only the coordinator calls the monitor. Transport threads cannot mutate strategy
state through this hook, and the in-flight hedge is completed before any exit
is submitted. Fast calls have no polling delay.
"""
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context


_pump = ContextVar('entry_wait_pump', default=None)


class EntryWaitPump:
    def __init__(self, callback, symbol):
        self.callback, self.symbol = callback, symbol
        self.owner = threading.get_ident()
        self.pool = None

    def result(self, future):
        while not future.done():
            wait((future,), timeout=.25)
            if future.done():
                break
            token = _pump.set(None)
            try:
                try:
                    self.callback(self.symbol)
                except Exception:
                    # An advisory read must never discard an in-flight
                    # order's eventual terminal receipt.
                    pass
            finally:
                _pump.reset(token)
        return future.result()

    def call(self, method, *args, **kwargs):
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='entry-transport')
        future = self.pool.submit(copy_context().run, method, *args, **kwargs)
        return self.result(future)


def _current():
    pump = _pump.get()
    return pump if pump is not None and pump.owner == threading.get_ident() else None


def entry_result(future):
    pump = _current()
    return pump.result(future) if pump else future.result()


def entry_call(method, *args, **kwargs):
    pump = _current()
    return pump.call(method, *args, **kwargs) if pump else method(*args, **kwargs)


@contextmanager
def entry_wait_scope(callback, symbol):
    pump = EntryWaitPump(callback, symbol) if callable(callback) else None
    token = _pump.set(pump)
    try:
        yield
    finally:
        _pump.reset(token)
        if pump is not None and pump.pool is not None:
            pump.pool.shutdown(wait=True)
