"""Long retention must preserve evidence without multiplying hot-path work."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.api_v2.history import MultiVenueHistory
from src.api_v2.center_statistics import CenterStatistics


class HistoryRollupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MultiVenueHistory(Path(folder.name) / 'history.sqlite')
        self.now = 20_000_000 // 3600 * 3600
        self.route = 'arcus->lighter'

    def row(self, stamp, value, age=0):
        return ('SUI-PERP', stamp, self.route, 'arcus', 'lighter', value, age, 0, 1, None)

    def insert_raw(self, rows):
        with self.store._connect() as db:
            db.executemany('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)', rows)

    def age(self, now):
        with self.store._connect() as db:
            self.store._archive_history(db, now)

    def test_raw_to_5m_to_hour_preserves_count_sum_extrema_and_no_overlap(self):
        stamp = self.now - 8 * 86400
        self.insert_raw([self.row(stamp + i * 30, i) for i in range(10)])
        self.age(self.now)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT bucket_seconds,sample_count,sum_bps,min_bps,max_bps '
                                       'FROM observation_rollups').fetchone(), (300, 10, 45, 0, 9))
        later = self.now + 24 * 86400
        self.age(later)
        self.age(later)  # A second maintenance pass cannot count samples twice.
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT bucket_seconds,sample_count,sum_bps '
                                       'FROM observation_rollups').fetchall(), [(3600, 10, 45)])
        with patch('src.api_v2.history.time.time', return_value=later):
            chart = self.store.read('SUI-PERP', 90 * 86400, self.route)
        self.assertEqual(len(chart), 1)
        self.assertFalse(chart[0]['executable'], 'Historical averages never prove an executable quote')
        self.assertEqual(chart[0]['gross_bps'], 4.5)
        self.age(stamp + 92 * 86400)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observation_rollups').fetchone()[0], 0)

    def test_invalid_quotes_excluded_and_ordinary_insert_never_aggregates(self):
        old = self.now - 8 * 86400
        self.insert_raw([self.row(old, 10), self.row(old + 30, 999, age=6), self.row(old + 60, None)])
        self.age(self.now)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT sample_count,sum_bps FROM observation_rollups').fetchone(), (1, 10))
        self.store._last_cleanup = 100
        with patch('src.api_v2.history.time.monotonic', return_value=101), \
                patch.object(self.store, '_archive_history', side_effect=AssertionError('quote hot path')):
            self.store._insert([self.row(self.now, 2)], self.now)

    def test_mixed_resolutions_have_equal_time_weight_and_validity_counts(self):
        old = self.now - 8 * 86400
        recent = self.now - 300
        self.insert_raw([self.row(old, 20)] + [self.row(recent + i * 30, 0) for i in range(10)]
                        + [self.row(self.now, 999, age=20)])
        self.age(self.now)
        with patch('src.api_v2.history.time.time', return_value=self.now):
            samples = self.store.prepared_samples('SUI-PERP', self.route, 30 * 86400)
        self.assertEqual(samples['ordered'], [0, 20])
        self.assertEqual(samples['invalid_quote_samples'], 1)
        self.assertEqual(samples['raw_samples'], 12)
        self.assertEqual(samples['sample_interval_seconds'], 300)
        self.assertTrue(samples['approximate'])

    def test_long_touch_window_uses_raw_turns_instead_of_aggregate_turns(self):
        values = [0, 10, 0, -10] * 10
        self.insert_raw([self.row(self.now - (len(values) - 1 - i) * 30, v)
                         for i, v in enumerate(values)])
        with patch('src.api_v2.center_statistics.threading.Thread.start'):
            service = CenterStatistics(self.store)
        with patch('src.api_v2.history.time.time', return_value=self.now):
            result = service.calculate(('SUI-PERP', self.route, 90 * 86400, 90., 'touches', 2., 3))
        channel = result['channel_statistics']
        self.assertEqual(channel['touch_statistics']['upper']['count'], 10)
        self.assertEqual(channel['touch_window_seconds'], 7 * 86400)

    def test_incomplete_boundary_buckets_do_not_duplicate_and_output_retains_latest(self):
        now = self.now + 100
        cutoff = self.now - 7 * 86400
        self.insert_raw([self.row(cutoff - 30, 3), self.row(cutoff + 60, 4), self.row(now - 10, 5)])
        self.age(now)
        with patch('src.api_v2.history.time.time', return_value=now):
            rows = self.store.read('SUI-PERP', 30 * 86400, self.route)
            bounded = self.store.read('SUI-PERP', 30 * 86400, self.route, limit=2)
        self.assertEqual([r['gross_bps'] for r in rows], [3, 4, 5])
        self.assertEqual([r['gross_bps'] for r in bounded], [3, 5])

    def test_display_window_does_not_change_execution_target_window(self):
        from src.api_v2.app import attach_history_stats, state
        from src.api_v2.models import Opportunity

        row = Opportunity(canonical_symbol='SUI-PERP', buy_venue='arcus',
                          sell_venue='lighter', buy_price=1, sell_price=1.01, gross_bps=100)

        def cached_stats(history, symbol, route, seconds):
            return {'center_bps': seconds / 86400, 'samples': 100,
                    'history_samples': 100, 'history_avg_bps': seconds / 86400}

        preview = Mock(side_effect=lambda rows, *args: rows)
        with patch.object(state, 'history', self.store), \
                patch('src.api_v2.center_statistics.center_stats_for', side_effect=cached_stats) as stats, \
                patch('src.api_v2.app.attach_return_previews', preview):
            result = attach_history_stats([row], target_window_seconds=7 * 86400,
                                          history_window_seconds=30 * 86400)[0]
        self.assertEqual(result.history_target_window_seconds, 30 * 86400)
        self.assertEqual(result.history_target_avg_bps, 30)
        self.assertEqual(result.reversion_target_bps, 7)
        self.assertEqual(preview.call_args.args[-1], 7 * 86400)
        self.assertEqual(stats.call_count, 3, 'Shared windows should reuse cached statistics')
