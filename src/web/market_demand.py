"""Track consumers of the optional, legacy two-venue dashboard."""
import threading
import time


class MarketDemand:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.leases = {}
        self.deadlines = {}

    def acquire(self, symbol):
        token = object()
        with self.lock:
            self.leases[token] = symbol
        return token

    def release(self, token):
        with self.lock:
            self.leases.pop(token, None)

    def touch(self, symbols, seconds=15):
        with self.lock:
            for symbol in symbols:
                self.deadlines[symbol] = self.clock() + seconds

    def active(self):
        with self.lock:
            now = self.clock()
            self.deadlines = {s: end for s, end in self.deadlines.items() if end > now}
            return tuple(sorted(set(self.leases.values()) | set(self.deadlines)))
