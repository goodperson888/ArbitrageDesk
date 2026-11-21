import fcntl
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.api_v2.history import MultiVenueHistory
from src.api_v2.center_statistics import CenterStatistics
from src.strategy.arbitrage_strategy import ArbitrageStrategy


class CenterStatisticsTests(unittest.TestCase):
    def test_touch_profiles_are_cached_separately_without_synchronous_history_read(self):
        self.insert([0,10,0,-10]*10)
        key=self.key+('touches',2.,3)
        value=self.stats.calculate(key)
        self.assertEqual(value['channel_statistics']['touch_statistics']['upper']['count'],10)
        with patch.object(self.stats,'calculate',side_effect=AssertionError('hot path')), \
                patch.object(self.history,'_connect',side_effect=AssertionError('hot path')):
            self.stats.get(*self.key,touch_tolerance_bps=2,touch_min_count=3)
            self.stats.get(*self.key,touch_tolerance_bps=3,touch_min_count=3)
            self.stats.get(*self.key,touch_tolerance_bps=2,touch_min_count=3)
        self.assertEqual(self.stats.jobs.qsize(),2)
        self.assertNotEqual(self.stats._path(key),self.stats._path(self.key))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.history = MultiVenueHistory(Path(tmp.name)/'history.sqlite')
        with patch('src.api_v2.center_statistics.threading.Thread.start'):
            self.stats = CenterStatistics(self.history)
        self.key = ('SUI-PERP','lighter->arcus',604800,90.)
        self.now = int(time.time())

    def insert(self, values, buy_age=0, sell_age=0, symbol='SUI-PERP', route='lighter->arcus', start=None, step=30):
        start = self.now - (len(values)-1)*step if start is None else start
        with self.history._connect() as db:
            db.executemany('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)',
                [(symbol,start+i*step,route,*route.split('->'),v,buy_age,sell_age,0,None)
                 for i,v in enumerate(values)])

    def test_fresh_reference_quotes_can_form_center_without_execution_permission(self):
        self.insert([2,4]*20)
        value = self.stats.calculate(self.key)
        self.assertAlmostEqual(value['center_bps'], (2/1.0002 + 4/1.0004)/2)
        self.assertTrue(value['center_ready'])
        self.assertEqual(value['samples'], 40)
        self.assertNotIn('events', value)
        self.assertAlmostEqual(value['recent_centers']['3600']['center_bps'], (2/1.0002 + 4/1.0004)/2)

    def test_channel_and_center_use_execution_coordinates_without_rewriting_history(self):
        self.insert([100]*40)
        value = self.stats.calculate(self.key)
        self.assertEqual(value['spread_basis'], 'max_price')
        self.assertAlmostEqual(value['channel_statistics']['center_bps'], 100/1.01)
        self.assertAlmostEqual(value['center_bps'], 100/1.01)
        with self.history._connect() as db:
            self.assertEqual(db.execute('SELECT DISTINCT gross_bps FROM observations').fetchall(), [(100.,)])

    def test_stale_missing_age_and_spikes_do_not_shift_center(self):
        self.insert([3]*40)
        self.insert([90]*50,buy_age=6,start=self.now-6000)
        self.insert([80]*30,sell_age=None,start=self.now-10000)
        self.insert([1000],start=self.now-12000)
        value = self.stats.calculate(self.key)
        self.assertAlmostEqual(value['center_bps'], 3/1.0003)
        self.assertEqual(value['samples'],40)
        self.assertEqual(value['raw_samples'],121)
        self.assertEqual(value['invalid_quote_samples'],80)
        self.assertEqual(value['anomaly_count'],1)
        full = self.history.reversion_stats(*self.key[:3])
        self.assertAlmostEqual(full['center_bps']/(1+full['center_bps']/10000),value['center_bps'])
        self.assertEqual(full['samples'],value['samples'])

    def test_sparse_or_stale_samples_are_visible_but_cannot_approve_entry(self):
        self.insert([2]*40,step=1)
        value = self.stats.calculate(self.key)
        self.assertAlmostEqual(value['center_bps'],2/1.0002)
        self.assertFalse(value['center_ready'])
        self.assertEqual(value['sample_span_seconds'],39)
        with self.history._connect() as db:
            db.execute('DELETE FROM observations')
        self.insert([2]*40,start=self.now-10000)
        self.stats.refresh(self.key)
        value = self.stats.get(*self.key)
        self.assertFalse(value['center_ready'])
        self.assertEqual(value['center_status'],'stale')

    def test_empty_is_unknown_and_opposite_route_never_supplies_center(self):
        self.insert([20]*40,route='arcus->lighter')
        value = self.stats.calculate(self.key)
        self.assertIsNone(value['center_bps'])
        self.assertFalse(value['center_ready'])

    def test_reader_never_queries_observations_and_coalesces_background_requests(self):
        with patch.object(self.stats,'calculate',side_effect=AssertionError('synchronous calculation')), \
             patch.object(self.history,'_connect',side_effect=AssertionError('hot-path database read')):
            for _ in range(10):
                value = self.stats.get(*self.key)
                self.assertIsNone(value['center_bps'])
        self.assertEqual(self.stats.jobs.qsize(),1)

    def test_atomic_snapshot_is_shared_with_other_process_reader(self):
        self.insert([3]*40)
        self.stats.refresh(self.key)
        with patch('src.api_v2.center_statistics.threading.Thread.start'):
            peer = CenterStatistics(self.history)
        with patch.object(peer,'calculate',side_effect=AssertionError('duplicate calculation')):
            value = peer.get(*self.key)
        self.assertAlmostEqual(value['center_bps'],3/1.0003)
        self.assertTrue(value['center_ready'])
        self.assertEqual(peer.jobs.qsize(),0)

    def test_cross_process_lock_prevents_duplicate_calculation(self):
        self.stats.directory.mkdir()
        with self.stats._path(self.key).with_suffix('.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(self.stats,'calculate',side_effect=AssertionError('duplicate job')):
                self.stats.refresh(self.key)
        self.assertFalse(self.stats._path(self.key).exists())

    def test_source_expiration_and_corrupt_snapshot_never_invent_zero(self):
        self.stats.directory.mkdir()
        self.stats._path(self.key).write_text('{bad json')
        self.assertIsNone(self.stats.get(*self.key)['center_bps'])
        self.insert([3]*40)
        self.stats.refresh(self.key)
        with patch('src.api_v2.center_statistics.time.time',return_value=self.now+180):
            value = self.stats.get(*self.key)
        self.assertAlmostEqual(value['center_bps'],3/1.0003)
        self.assertFalse(value['center_ready'])

    def test_center_drift_is_diagnostic_and_preserves_configured_window(self):
        self.insert([0]*80,start=self.now-90000)
        self.insert([10]*40)
        value = self.stats.calculate(self.key)
        self.assertEqual(value['center_bps'],0)
        self.assertAlmostEqual(value['recent_centers']['21600']['center_bps'],10/1.001)
        self.assertEqual(value['center_status'],'drifting')
        self.assertTrue(value['center_ready'])

    def test_invalid_parameters_and_snapshot_shape_are_not_accepted(self):
        for window, quantile in ((0,90),(604800,100),(604800,float('nan'))):
            with self.assertRaises(ValueError):
                self.stats.get(*self.key[:2],window,quantile)
        self.stats.directory.mkdir()
        for malformed in ([],dict(version=1,key=list(self.key),calculated_at='bad',stats={})):
            self.stats._path(self.key).write_text(json.dumps(malformed))
            self.stats.checked.clear()
            self.assertIsNone(self.stats.get(*self.key)['center_bps'])

    def test_shutdown_does_not_create_a_worker(self):
        from src.api_v2.center_statistics import close_service
        with patch('src.api_v2.center_statistics.CenterStatistics') as worker:
            close_service(self.history)
            worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
