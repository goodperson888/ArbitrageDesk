import threading
import unittest
from unittest.mock import Mock

from src.web.history_sync import HistorySync


def pending(**values):
    return dict(status='reconcile_required', accounting_pending=True, symbol='PUMP',
                variational_venue='lighter', lighter_venue='arcus', **values)


class HistorySyncTests(unittest.TestCase):
    def setUp(self):
        self.time = 1000
        self.fetch = Mock(return_value={'success': True, 'orders_submitted': False})
        self.active = Mock(return_value=False)
        self.sync = HistorySync(self.fetch, self.active, now=lambda: self.time)
        self.book = {'open_positions': [pending()], 'closed_positions': []}

    def finish(self):
        self.sync._thread.join(timeout=2)
        self.assertFalse(self.sync._thread.is_alive())

    def test_polls_are_nonblocking_and_share_one_inflight_read(self):
        entered, release = threading.Event(), threading.Event()
        def read(*args):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError('page poll blocked on history read')
            return {'success': True, 'orders_submitted': False}
        self.fetch.side_effect = read
        first = self.sync.poll(self.book)
        self.assertEqual(first['state'], 'refreshing')
        self.assertTrue(entered.wait(timeout=1))
        for _ in range(20):
            self.assertEqual(self.sync.poll(self.book)['state'], 'refreshing')
        self.fetch.assert_called_once_with(['lighter', 'arcus'], ['PUMP'], 100)
        release.set()
        self.finish()

    def test_successful_read_observes_interval_and_live_execution_defers(self):
        self.sync.poll(self.book)
        self.finish()
        self.time += 179
        self.sync.poll(self.book)
        self.fetch.assert_called_once()
        self.active.return_value = True
        self.time += 2
        self.assertEqual(self.sync.poll(self.book)['state'], 'deferred')
        self.fetch.assert_called_once()
        self.active.return_value = False
        self.sync.poll(self.book)
        self.finish()
        self.assertEqual(self.fetch.call_count, 2)

    def test_partial_failure_preserves_evidence_and_backs_off(self):
        self.fetch.return_value = {'success': False, 'orders_submitted': False,
                                  'errors': [{'venue': 'lighter', 'error': 'RateLimitError'}]}
        self.sync.poll(self.book)
        self.finish()
        self.time += 180
        state = self.sync.poll(self.book)
        self.assertEqual(state['state'], 'partial')
        self.assertEqual(state['retry_after_seconds'], 180)
        self.fetch.assert_called_once()
        self.time += 180
        self.sync.poll(self.book)
        self.finish()
        self.assertEqual(self.fetch.call_count, 2)

    def test_settled_rows_never_trigger_account_reads(self):
        self.sync.poll({'closed_positions': [dict(pending(), status='closed', accounting_pending=False)]})
        self.fetch.assert_not_called()
        self.assertIsNone(self.sync._thread)

    def test_batches_have_at_most_five_symbols_and_pairs_do_not_mix(self):
        rows = [dict(pending(), symbol='TOKEN'+str(i)) for i in range(8)]
        rows.append(dict(pending(), lighter_venue='lighter_rh'))
        self.sync.poll({'open_positions': rows})
        self.finish()
        self.assertEqual(self.fetch.call_args.args[:2], (['lighter', 'arcus'], ['TOKEN'+str(i) for i in range(5)]))
        self.time += 30
        self.sync.poll({'open_positions': rows})
        self.finish()
        self.assertEqual(self.fetch.call_args.args[:2], (['lighter', 'lighter_rh'], ['PUMP']))
        self.time += 150
        self.sync.poll({'open_positions': rows})
        self.finish()
        self.assertEqual(self.fetch.call_args.args[1][:3], ['TOKEN5', 'TOKEN6', 'TOKEN7'])

    def test_execution_starting_between_poll_and_read_skips_account_fetch(self):
        self.active.side_effect = [False, True]
        self.sync.poll(self.book)
        self.finish()
        self.fetch.assert_not_called()
