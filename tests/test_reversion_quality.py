import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from src.api_v2.center_statistics import CenterStatistics
from src.api_v2.history import MultiVenueHistory
from src.api_v2.reversion_quality import HORIZON_SECONDS, replay_quality
from src.execution.channel_policy import channel_policy


class ReversionQualityTests(unittest.TestCase):
    bounds = dict(lower_bps=10., center_bps=20., upper_bps=30.)

    def test_nonzero_center_can_return_both_sides_and_freezes_percentage_target(self):
        rows = [(0, 20), (30, 40), (60, 31), (90, 28), (120, 20), (150, 0), (180, 9), (210, 12)]
        result = replay_quality(rows, self.bounds, now=210, reversion_percent=60, band_bps=0)
        for side, target in [('upper', 28), ('lower', 12)]:
            self.assertEqual(result[side]['entered'], 1)
            self.assertEqual(result[side]['returned'], 1)
            self.assertEqual(result[side]['return_rate'], 1)
            self.assertEqual(result[side]['median_return_seconds'], 60)
            self.assertEqual(result[side]['recent_events'][0]['target_bps'], target)

    def test_timeout_pending_and_missing_quotes_have_distinct_denominators(self):
        # Continuous quotes prove 24 hours without reaching the target.
        rows = [(0, 20), (30, 31)] + [(t, 31) for t in range(60, HORIZON_SECONDS+31, 30)]
        result = replay_quality(rows, self.bounds, now=HORIZON_SECONDS+30)['upper']
        self.assertEqual((result['entered'], result['timed_out'], result['return_rate']), (1, 1, 0))
        pending = replay_quality([(0, 20), (30, 31), (60, 31)], self.bounds, now=70)['upper']
        self.assertEqual((pending['pending'], pending['resolved'], pending['return_rate']), (1, 0, None))
        stale = replay_quality([(0, 20), (30, 31), (60, 31)], self.bounds, now=100000)['upper']
        self.assertEqual((stale['interrupted'], stale['timed_out'], stale['resolved']), (1, 0, 0))
        gap = replay_quality([(0, 20), (30, 31), (600, 20)], self.bounds, now=600)['upper']
        self.assertEqual((gap['interrupted'], gap['returned']), (1, 0))

    def test_left_censoring_plateaus_future_rows_and_rearming(self):
        rows = [(0, 31), (30, 32), (60, 20), (90, 31), (120, 32), (150, 20), (180, 31), (999, 20)]
        result = replay_quality(rows, self.bounds, now=190)['upper']
        self.assertEqual((result['entered'], result['returned'], result['pending']), (2, 1, 1))
        self.assertEqual(result['recent_events'][0]['start_timestamp'], 180)

    def test_opposite_target_and_zero_target_do_not_substitute_center(self):
        rows = [(0, 20), (30, 35), (60, 20), (90, 10), (120, 0)]
        opposite = replay_quality(rows, self.bounds, now=120, opposite=True)['upper']
        zero = replay_quality(rows, self.bounds, now=120, target_mode='zero', band_bps=0)['upper']
        self.assertEqual(opposite['recent_events'][0]['duration_seconds'], 60)
        self.assertEqual(zero['recent_events'][0]['duration_seconds'], 90)

    def test_reader_only_queues_and_uses_shared_snapshot_without_history_read(self):
        with tempfile.TemporaryDirectory() as folder:
            history = MultiVenueHistory(Path(folder)/'history.sqlite')
            with patch('src.api_v2.center_statistics.threading.Thread.start'):
                service = CenterStatistics(history)
            policy = channel_policy({'channel_policy': dict(method='fixed', lower_bps=10, upper_bps=30)})
            with patch.object(history, '_connect', side_effect=AssertionError('reader must not query history')):
                for _ in range(3):
                    result = service.get_quality('SUI-PERP', 'a->b', policy=policy)
                    self.assertFalse(result['statistics_ready'])
            self.assertEqual(service.jobs.qsize(), 1)
            key = service.jobs.get_nowait()
            with patch.object(service, 'calculate', return_value=dict(center_bps=20, quality_ready=True,
                    last_sample_at=__import__('time').time(), samples=40, diagnostic_only=True)):
                service.refresh(key)
            with patch.object(history, '_connect', side_effect=AssertionError('snapshot reader')):
                self.assertTrue(service.get_quality('SUI-PERP', 'a->b', policy=policy)['quality_ready'])
                with patch('src.api_v2.center_statistics.threading.Thread.start'):
                    peer = CenterStatistics(history)
                self.assertTrue(peer.get_quality('SUI-PERP', 'a->b', policy=policy)['quality_ready'])
                self.assertEqual(peer.jobs.qsize(), 0, 'The other process must reuse the policy snapshot')
            # Policy changes cannot borrow the previous target's statistics.
            self.assertFalse(service.get_quality('SUI-PERP', 'a->b', policy={**policy,'reversion_percent':100})['statistics_ready'])

    def test_background_snapshot_caps_evidence_to_raw_week_and_keeps_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            history = MultiVenueHistory(Path(folder)/'history.sqlite')
            with patch('src.api_v2.center_statistics.threading.Thread.start'):
                service = CenterStatistics(history)
            service.get_quality('SUI-PERP', 'a->b', seconds=90*86400)
            result = service.calculate(service.jobs.get_nowait())
            self.assertEqual(result['seconds'], 7*86400)
            self.assertEqual(result['requested_seconds'], 90*86400)
            self.assertFalse(result['quality_ready'])
            self.assertIsNone(result['sides'])
            self.assertIsNone(result['center_bps'])
            self.assertTrue(result['diagnostic_only'])

    def test_api_only_reads_cached_diagnostics_and_rejects_invalid_policy(self):
        from src.api_v2.app import reversion_quality
        service = Mock()
        service.get_quality.return_value = {'diagnostic_only': True, 'statistics_ready': False}
        with patch('src.api_v2.center_statistics.service_for', return_value=service):
            result = asyncio.run(reversion_quality('SUI', 'a->b', 604800, 'center', 0., json.dumps({'method':'fixed'})))
            self.assertTrue(result['diagnostic_only'])
            self.assertEqual(service.get_quality.call_args.args[:2], ('SUI-PERP', 'a->b'))
        for pair, policy in [('a->a', None), ('a->b', '[]'), ('a->b', 'invalid')]:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(reversion_quality('SUI', pair, 604800, 'center', 0., policy))
            self.assertEqual(caught.exception.status_code, 400)
