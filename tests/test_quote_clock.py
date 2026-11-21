"""Offline clock calibration and quote-age regression; no live orders or UDP."""
import struct
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, Mock, patch

from src.common.quote_clock import (QuoteClock, TimeSample, QuoteClockUnavailable,
                                    probe_time_server, NTP_EPOCH)
from src.common.market_data import MarketDataError, normalize_quote_time, age_seconds
from src.common.live_feed import ArcusBboFeed, LighterBookFeed
from tests.test_arcus_quote_clock import message_at
from tests import test_arcus_quote_clock as arcus_fixtures
from tests.test_live_feeds import book_message


class QuoteClockTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.mono = 1800000000., 100.
        self.clock = QuoteClock()
        self.patches = [
            patch('src.common.quote_clock.time.time', side_effect=lambda:self.wall),
            patch('src.common.quote_clock.time.monotonic', side_effect=lambda:self.mono),
            patch('src.common.market_data.quote_clock', self.clock),
            patch('src.common.market_data.datetime', wraps=datetime),
            patch('src.common.live_feed.datetime', wraps=datetime),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.update_datetimes()

    def update_datetimes(self):
        import src.common.market_data as market
        import src.common.live_feed as feeds
        market.datetime.now.return_value = self.received
        feeds.datetime.now.return_value = self.received

    @property
    def received(self):
        return datetime.fromtimestamp(self.wall, timezone.utc)

    def calibrate(self, offset=2.2):
        samples = [TimeSample(source, self.wall+offset, self.mono, .01)
                   for source in ('independent-a', 'independent-b')]
        self.assertTrue(self.clock.accept(samples))

    def test_requires_two_independent_agreeing_low_uncertainty_sources(self):
        sample = TimeSample('a', self.wall+2.2, self.mono, .01)
        self.assertFalse(self.clock.accept([sample]))
        self.assertFalse(self.clock.accept([sample, sample]))
        self.assertFalse(self.clock.accept([sample, TimeSample('b', self.wall+3, self.mono, .01)]))
        self.assertFalse(self.clock.accept([sample, TimeSample('b', self.wall+2.2, self.mono, .5)]))
        self.assertFalse(self.clock.accept([sample, TimeSample('b', float('nan'), self.mono, .01)]))
        self.assertTrue(self.clock.accept([TimeSample('outlier', self.wall+4, self.mono, .01),
                                          sample, TimeSample('b', self.wall+2.2, self.mono, .01)]))
        self.assertAlmostEqual(self.clock.status()['offset_seconds'], 2.2, places=5)

    def test_correction_preserves_real_age_in_both_clock_directions(self):
        for offset in (2.2, -2.2):
            with self.subTest(offset=offset):
                self.calibrate(offset)
                for true_age in (0, 1, 4):
                    observed = self.received+timedelta(seconds=offset-true_age)
                    normalized = normalize_quote_time(observed, self.received, received_monotonic=self.mono)
                    self.assertAlmostEqual((self.received-normalized).total_seconds(), true_age+.015, places=4)
                with self.assertRaises(MarketDataError):
                    normalize_quote_time(self.received+timedelta(seconds=offset+1), self.received,
                                         received_monotonic=self.mono)

    def test_expired_calibration_fails_closed_and_recovery_needs_no_restart(self):
        self.calibrate()
        self.mono += 301; self.wall += 301; self.update_datetimes()
        self.assertTrue(self.clock.status()['expired'])
        with self.assertRaisesRegex(MarketDataError, '校时已过期'):
            normalize_quote_time(self.received, self.received, received_monotonic=self.mono)
        self.calibrate()
        self.assertTrue(self.clock.status()['ready'])
        normalized = normalize_quote_time(self.received+timedelta(seconds=2.2), self.received,
                                         received_monotonic=self.mono)
        self.assertLess(age_seconds(normalized.isoformat()), .1)

    def test_cold_calibration_reports_warmup_without_accepting_future_quotes(self):
        self.clock._thread = Mock()
        self.clock._thread.is_alive.return_value = True
        self.assertTrue(self.clock.status()['calibrating'])
        with patch('src.common.quote_clock.probe_time_server') as probe:
            with self.assertRaisesRegex(MarketDataError, '后台校时中'):
                normalize_quote_time(self.received+timedelta(seconds=2.2), self.received,
                                     received_monotonic=self.mono)
            probe.assert_not_called()
        self.calibrate()
        self.assertFalse(self.clock.status()['calibrating'])
        self.assertLessEqual(normalize_quote_time(self.received+timedelta(seconds=2.2),
                             self.received, received_monotonic=self.mono), self.received)

    def test_without_calibration_worker_original_future_protection_is_unchanged(self):
        self.assertFalse(self.clock.status()['calibrating'])
        with self.assertRaisesRegex(MarketDataError, '行情时间超前'):
            normalize_quote_time(self.received+timedelta(seconds=2.2), self.received,
                                 received_monotonic=self.mono)

    def test_original_receipt_is_used_for_cached_rest_not_current_read_time(self):
        self.calibrate()
        receipt, mono = self.received, self.mono
        client = arcus_fixtures.ArcusQuoteClockTests().client(receipt, 2.2)
        payload = client._get.return_value
        payload['_local_fetched_monotonic'] = mono
        first = client.bbo('SUI')
        self.mono += .5; self.wall += .5; self.update_datetimes()
        second = client.bbo('SUI')
        self.assertLessEqual(second['updated_at'], first['updated_at'])
        self.assertGreaterEqual(age_seconds(second['updated_at']), .5)
        self.assertEqual(second['received_at'], first['received_at'])
        old_client = arcus_fixtures.ArcusQuoteClockTests().client(self.received, 2.2-600)
        self.assertGreaterEqual(age_seconds(old_client.bbo('SUI')['updated_at']), 600,
                                'a very old venue timestamp must not fall back to receipt time')

    def test_arcus_and_both_lighter_deployments_accept_fresh_reject_delayed_frames(self):
        self.calibrate()
        arcus = ArcusBboFeed({'SUI':'SUI-USD'}); arcus.connected = True
        arcus.on_message(message_at(self.received+timedelta(seconds=2.2)))
        self.assertIsNotNone(arcus.snapshot('SUI'))
        arcus.on_message(message_at(self.received+timedelta(seconds=2.2-4)))
        self.assertIsNone(arcus.snapshot('SUI'), 'a truly delayed quote must stay delayed after correction')
        for origin in ('wss://mainnet.zklighter.elliot.ai/stream', 'wss://api.rh.lighter.xyz/stream'):
            feed = LighterBookFeed({'BTC':1}, ws_url=origin); feed.connected = True
            stamp = (self.wall+2.2)*1000
            feed.on_message(dict(book_message(), timestamp=stamp))
            before = feed.snapshot('BTC'); self.assertIsNotNone(before)
            feed.on_message(book_message('update/order_book', nonce=20, begin=10, offset=105))
            self.assertEqual(feed.snapshot('BTC')['fetched_at'], before['fetched_at'])
            feed.on_message(dict(book_message('update/order_book', nonce=30, begin=20, offset=110), timestamp=stamp-20000))
            self.assertIsNone(feed.snapshot('BTC'))
            with self.assertRaisesRegex(ValueError, 'sequence gap'):
                feed.on_message(book_message('update/order_book', nonce=40, begin=0, offset=115))

    def test_monotonic_age_prevents_frozen_or_backward_wall_clock_renewal(self):
        self.calibrate()
        arcus = ArcusBboFeed({'SUI':'SUI-USD'}); arcus.connected = True
        arcus.on_message(message_at(self.received+timedelta(seconds=2.2)))
        feed = LighterBookFeed({'BTC':1}); feed.connected = True
        feed.on_message(dict(book_message(), timestamp=(self.wall+2.2)*1000))
        self.assertIsNotNone(arcus.snapshot('SUI'))
        self.assertIsNotNone(feed.snapshot('BTC'))
        self.mono += 1  # Wall clock deliberately does not advance.
        self.assertGreaterEqual(age_seconds(arcus.snapshot('SUI')['updated_at']), 1)
        self.assertGreaterEqual(age_seconds(feed.snapshot('BTC')['fetched_at']), 1)
        self.mono += 10
        self.assertIsNone(arcus.snapshot('SUI'))
        self.assertIsNone(feed.snapshot('BTC'))
        self.assertEqual(feed.quote_health('BTC')['quote_status'], 'stale')

    def test_quote_path_only_reads_memory_without_probes_or_waits(self):
        self.calibrate()
        with patch('src.common.quote_clock.probe_time_server') as probe, \
                patch('src.common.quote_clock.socket.socket') as socket, \
                patch.object(self.clock, 'start') as start:
            for _ in range(50):
                normalize_quote_time(self.received+timedelta(seconds=2.2), self.received,
                                     received_monotonic=self.mono)
            probe.assert_not_called(); socket.assert_not_called(); start.assert_not_called()

    def test_original_receipt_monotonic_survives_later_system_clock_step(self):
        self.calibrate()
        receipt, mono = self.received, self.mono
        observed = receipt+timedelta(seconds=2.2)
        original = normalize_quote_time(observed, receipt, received_monotonic=mono)
        self.mono += 1; self.wall += 21
        again = normalize_quote_time(observed, receipt, received_monotonic=mono)
        self.assertLessEqual(again, original)
        with self.assertRaises(QuoteClockUnavailable):
            self.clock.correction(receipt, self.mono+1)

    def test_failed_refresh_keeps_last_good_sample_without_extending_expiry(self):
        self.calibrate()
        original = self.clock.calibration
        stop = Mock(); stop.is_set.return_value = False
        stop.wait.side_effect = lambda seconds: stop.is_set.configure_mock(return_value=True)
        with patch('src.common.quote_clock.probe_time_server', side_effect=OSError):
            self.clock._run(stop)
        self.assertIs(self.clock.calibration, original)
        self.assertIn('后台重试', self.clock.status()['error'])
        stop.wait.assert_called_once_with(15)

    def test_start_is_single_daemon_and_stop_never_joins_or_sets_system_time(self):
        with patch('src.common.quote_clock.threading.Thread') as thread:
            self.clock.start(); self.clock.start(); self.clock.stop()
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs['daemon'])
        thread.return_value.join.assert_not_called()


class TimeProbeTests(unittest.TestCase):
    def probe(self, mutate=lambda packet:None, elapsed=.1, wall_elapsed=None):
        wall, mono, lead = 1800000000., 100., 2.2
        connection = MagicMock(); connection.__enter__.return_value = connection
        def response(size):
            packet = bytearray(48); packet[0] = 0x24; packet[1] = 2
            packet[24:32] = connection.send.call_args.args[0][40:48]
            for index, stamp in ((32,wall+lead+.04),(40,wall+lead+.05)):
                struct.pack_into('!II', packet, index, int(stamp)+NTP_EPOCH, int((stamp%1)*2**32))
            mutate(packet)
            return bytes(packet)
        connection.recv.side_effect = response
        with patch('src.common.quote_clock.socket.socket', return_value=connection), \
                patch('src.common.quote_clock.time.time', side_effect=[wall,wall+(elapsed if wall_elapsed is None else wall_elapsed)]), \
                patch('src.common.quote_clock.time.monotonic', side_effect=[mono,mono+elapsed]):
            return probe_time_server('time.example.test')

    def test_valid_packet_uses_network_round_trip_not_arrival_only(self):
        sample = self.probe()
        self.assertAlmostEqual(sample.utc, 1800000000.+2.295, places=5)
        self.assertAlmostEqual(sample.uncertainty, .045, places=5)

    def test_bad_origin_unsynchronized_mode_stratum_dispersion_and_short_packets_rejected(self):
        changes = [lambda p:p.__setitem__(0,0xe4), lambda p:p.__setitem__(0,0x23),
                   lambda p:p.__setitem__(1,0), lambda p:p.__setitem__(24,p[24]^1),
                   lambda p:struct.pack_into('!I',p,8,65536), lambda p:p.__delitem__(slice(30,None))]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.probe(change)

    def test_high_latency_and_system_clock_jump_during_probe_are_rejected(self):
        with self.assertRaises(ValueError):
            self.probe(elapsed=.8)
        with self.assertRaises(ValueError):
            self.probe(wall_elapsed=2)
