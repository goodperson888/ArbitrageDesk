import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api_v2.center_statistics import CenterStatistics
from src.api_v2.history import MultiVenueHistory


class SharedReversionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.history = MultiVenueHistory(Path(tmp.name)/'history.sqlite')
        self.now = int(time.time())
        self.symbol, self.route = 'SUI-PERP','lighter->arcus'
        with self.history._connect() as db:
            db.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)', [
                (self.symbol,self.now-1170+i*30,self.route,'lighter','arcus',v,0,0,0,None)
                for i,v in enumerate([0,10,0,0]*10)])
        with patch('src.api_v2.center_statistics.threading.Thread.start'):
            self.stats = CenterStatistics(self.history)
        self.key = (self.symbol,self.route,604800,90.,'reversion',2.,'center',5.)

    def test_summary_matches_full_events_and_shares_cleaned_samples(self):
        with patch.object(self.history,'_connect',wraps=self.history._connect) as connect:
            center = self.stats.calculate(self.key[:4])
            summary = self.stats.calculate(self.key)
        self.assertEqual(connect.call_count,1)
        full = self.history.reversion_stats(self.symbol,self.route,604800,2,'center',5)
        self.assertEqual(summary['reversion_count'],len(full['events']))
        for field in ('center_bps','samples','p10_bps','p90_bps','entry_upper_bps'):
            self.assertEqual(summary[field],center[field])
        self.assertNotIn('events',summary)

    def test_reader_is_nonblocking_and_peer_reuses_disk_snapshot(self):
        with patch.object(self.history,'_connect',side_effect=AssertionError('hot database read')):
            for _ in range(5):
                pending = self.stats.get_reversion(self.symbol,self.route,min_deviation_bps=5)
                self.assertFalse(pending['statistics_ready'])
                self.assertIsNone(pending['reversion_count'])
        self.assertEqual(self.stats.jobs.qsize(),1)
        self.stats.refresh(self.key)
        with patch('src.api_v2.center_statistics.threading.Thread.start'):
            peer = CenterStatistics(self.history)
        with patch.object(peer,'calculate',side_effect=AssertionError('duplicate calculation')):
            value = peer.get_reversion(self.symbol,self.route,min_deviation_bps=5)
        self.assertTrue(value['statistics_ready'])
        self.assertEqual(value['reversion_count'],10)

    def test_recency_advances_and_expired_snapshot_cannot_pass_filters(self):
        self.stats.refresh(self.key)
        first = self.stats.get_reversion(self.symbol,self.route,min_deviation_bps=5)
        with patch('src.api_v2.center_statistics.time.time',return_value=self.now+40):
            later = self.stats.get_reversion(self.symbol,self.route,min_deviation_bps=5)
        self.assertGreater(later['last_reversion_age_seconds'],first['last_reversion_age_seconds']+35)
        with patch('src.api_v2.center_statistics.time.time',return_value=self.now+180):
            stale = self.stats.get_reversion(self.symbol,self.route,min_deviation_bps=5)
        self.assertFalse(stale['statistics_ready'])

    def test_statistics_parameters_never_reuse_another_route_or_filter(self):
        self.stats.refresh(self.key)
        for kwargs in (dict(target_mode='zero'),dict(band_bps=3),dict(min_deviation_bps=6),dict(seconds=3600)):
            params = dict(min_deviation_bps=5)
            params.update(kwargs)
            value = self.stats.get_reversion(self.symbol,self.route,**params)
            self.assertFalse(value['statistics_ready'])

    def test_prepared_samples_invalidate_on_new_evidence(self):
        self.stats.calculate(self.key[:4])
        with self.history._connect() as db:
            db.execute('DELETE FROM observations')
        value = self.stats.calculate(self.key[:4])
        self.assertEqual(value['samples'],0)

    def test_runtime_closes_existing_worker_without_creating_one(self):
        from types import SimpleNamespace
        from src.execution.runtime import _close_history_statistics
        from src.api_v2.center_statistics import service_for
        service=service_for(self.history)
        _close_history_statistics(SimpleNamespace(market_history=self.history))
        self.assertTrue(service.closed.is_set())
        self.assertFalse(service.thread.is_alive())
        with patch('src.api_v2.center_statistics.CenterStatistics') as factory:
            _close_history_statistics(SimpleNamespace(market_history=self.history))
            _close_history_statistics(None)
        factory.assert_not_called()

    def test_saturated_background_queue_never_blocks_or_fails_a_reader(self):
        import queue
        self.stats.jobs=queue.Queue(maxsize=1)
        self.stats.jobs.put_nowait(('obsolete',))
        value=self.stats.get_reversion(self.symbol,self.route,min_deviation_bps=5)
        self.assertFalse(value['statistics_ready'])
        self.assertFalse(self.stats.pending)
