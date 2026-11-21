"""Background SNTP calibration for market data; never sets the system clock.

Only independently agreeing time servers can supply a correction. Readers
use an immutable in-memory sample; no DNS, sockets or waiting on quote paths.
"""
import math
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from statistics import median


NTP_EPOCH = 2208988800
SERVERS = ('time.cloudflare.com', 'time.apple.com', 'time.windows.com')
MAX_DELAY = .5
MAX_UNCERTAINTY = .3
MAX_OFFSET = 300
MAX_AGE = 300


class QuoteClockUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class TimeSample:
    source: str
    utc: float
    monotonic: float
    uncertainty: float


@dataclass(frozen=True)
class Calibration:
    utc: float
    monotonic: float
    uncertainty: float
    sources: tuple


def _ntp_time(packet, offset, reference):
    seconds, fraction = struct.unpack_from('!II', packet, offset)
    if not seconds and not fraction:
        raise ValueError('missing NTP timestamp')
    stamp = seconds - NTP_EPOCH + fraction / 2**32
    # Resolve the 2036 era rollover against the bounded local clock error.
    return stamp + round((reference - stamp) / 2**32) * 2**32


def probe_time_server(host):
    """One bounded UDP query, RFC 5905 timestamps and round-trip correction."""
    request = bytearray(48)
    request[0] = 0x23  # NTP v4 client.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(1)
        connection.connect((host, 123))
        sent_wall, sent_mono = time.time(), time.monotonic()
        # Keep the true transmit time and randomize its low fractional bits.
        fraction = (int((sent_wall % 1) * 2**32) & 0xffff0000) | secrets.randbits(16)
        struct.pack_into('!II', request, 40, (int(sent_wall) + NTP_EPOCH) & 0xffffffff, fraction)
        connection.send(request)
        packet = connection.recv(512)
        received_mono, received_wall = time.monotonic(), time.time()
    elapsed = received_mono - sent_mono
    if (len(packet) < 48 or packet[0] >> 6 == 3 or
            ((packet[0] >> 3) & 7) not in (3, 4) or packet[0] & 7 != 4 or
            not 1 <= packet[1] <= 15 or packet[24:32] != request[40:48]):
        raise ValueError('invalid NTP response')
    if not 0 <= elapsed <= 1 or abs((received_wall-sent_wall)-elapsed) > .05:
        raise ValueError('local clock changed during probe')
    received = _ntp_time(packet, 32, sent_wall)
    transmitted = _ntp_time(packet, 40, sent_wall)
    processing = transmitted - received
    delay = elapsed - processing
    root_delay = max(0, struct.unpack_from('!i', packet, 4)[0] / 65536)
    dispersion = struct.unpack_from('!I', packet, 8)[0] / 65536
    uncertainty = max(0, delay)/2 + root_delay/2 + dispersion
    if (processing < 0 or not -.001 <= delay <= MAX_DELAY or
            uncertainty > MAX_UNCERTAINTY):
        raise ValueError('uncertain NTP response')
    utc = transmitted + max(0, delay)/2
    if abs(utc-received_wall) > MAX_OFFSET:
        raise ValueError('clock offset exceeds bound')
    return TimeSample(host, utc, received_mono, uncertainty)


class QuoteClock:
    def __init__(self):
        self.calibration = None
        self.error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._stop.is_set():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name='quote-clock', daemon=True)
            self._thread.start()

    def stop(self):
        # No join: a slow DNS/UDP read must never hold up service shutdown.
        with self._lock:
            self._stop.set()
            self.calibration = None

    def accept(self, samples):
        now = time.monotonic()
        samples = [s for s in samples if all(math.isfinite(v) for v in
            (s.utc, s.monotonic, s.uncertainty)) and 0 <= now-s.monotonic <= 10
            and 0 <= s.uncertainty <= MAX_UNCERTAINTY]
        # At least two different server names must agree. A third source can
        # recover from one unreachable or inconsistent source.
        for i, first in enumerate(samples):
            for second in samples[i+1:]:
                if first.source == second.source:
                    continue
                times = [s.utc+now-s.monotonic for s in (first, second)]
                difference = abs(times[0]-times[1])
                uncertainty = max(first.uncertainty, second.uncertainty)+difference/2+.005
                utc = median(times)
                if difference > .25 or uncertainty > MAX_UNCERTAINTY or abs(utc-time.time()) > MAX_OFFSET:
                    continue
                self.calibration = Calibration(utc, now, uncertainty, (first.source, second.source))
                self.error = None
                return True
        return False

    def _run(self, stop):
        while not stop.is_set():
            samples, success = [], False
            for host in SERVERS:
                try:
                    samples.append(probe_time_server(host))
                except (OSError, ValueError, OverflowError):
                    pass
                with self._lock:
                    if stop.is_set():
                        return
                    if self.accept(samples):
                        success = True
                        break
            if not success:
                self.error = '时间源未达成可靠校准，后台重试中'
            stop.wait(60 if success else 15)

    def correction(self, received, received_monotonic=None):
        calibration = self.calibration
        if calibration is None:
            return 0., 0.  # Preserve the existing strict local-clock check.
        now = time.monotonic()
        age = now-calibration.monotonic
        if not 0 <= age <= MAX_AGE:
            raise QuoteClockUnavailable('行情校时已过期，等待后台校准')
        if received_monotonic is None:
            received_monotonic = now-(time.time()-received.timestamp())
        if not math.isfinite(received_monotonic) or not 0 <= now-received_monotonic <= 120:
            raise QuoteClockUnavailable('行情接收时间无效，等待新报价')
        trusted = calibration.utc+received_monotonic-calibration.monotonic
        # Drift allowance grows only with calibration age, never per read.
        return trusted-received.timestamp(), calibration.uncertainty+age*.00005

    def status(self):
        calibration = self.calibration
        age = time.monotonic()-calibration.monotonic if calibration else None
        valid = age is not None and 0 <= age <= MAX_AGE
        return {'ready': valid, 'age_seconds': age,
                'calibrating': calibration is None and self._thread is not None and
                               self._thread.is_alive() and not self._stop.is_set(),
                'offset_seconds': calibration.utc+age-time.time() if valid else None,
                'uncertainty_seconds': calibration.uncertainty if valid else None,
                'sources': list(calibration.sources) if calibration else [], 'error': self.error,
                'expired': calibration is not None and not valid}


quote_clock = QuoteClock()
