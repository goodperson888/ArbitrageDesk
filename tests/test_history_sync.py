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

    def test_live_closed_accounting_is_small_nonblocking_and_does_not_query_open_groups(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(row):
            self.assertEqual(row['id'], 'closed')
            entered.set()
            self.assertTrue(release.wait(2))
            return dict(success=True, orders_submitted=False, complete=False)
        self.active.return_value = True
        self.sync = HistorySync(self.fetch, self.active, now=lambda: self.time,
                                pending_fetch=fetch, read_ready=lambda: True)
        row = dict(pending(), id='closed', status='closed', recovered_close_fills={'lighter': {}})
        self.book['closed_positions'] = [row]
        self.sync.poll(self.book)
        self.assertTrue(entered.wait(1))
        for _ in range(20):
            self.assertEqual(self.sync.poll(self.book)['state'], 'refreshing')
        release.set()
        self.finish()
        self.fetch.assert_not_called()
        self.assertEqual(self.sync._state['state'], 'partial', 'successful HTTP without missing fees is not settled')
        self.time += 599
        self.sync.poll(self.book)
        self.assertEqual(self.sync._pending_failures['closed'], 1)

    def test_live_busy_and_settled_rows_do_not_start_background_reads(self):
        self.active.return_value = True
        read = Mock()
        ready = Mock(return_value=False)
        self.sync = HistorySync(self.fetch, self.active, now=lambda: self.time, pending_fetch=read, read_ready=ready)
        self.book['closed_positions'] = [dict(pending(), id='closed', status='closed', recovered_close_fills={'lighter': {}})]
        self.sync.poll(self.book)
        read.assert_not_called()
        ready.return_value = True
        self.book['closed_positions'][0]['accounting_pending'] = False
        self.sync.poll(self.book)
        read.assert_not_called()

    def test_live_trade_starting_after_scheduling_yields_without_request(self):
        self.active.return_value = True
        read = Mock()
        ready = Mock(side_effect=[True, False])
        self.sync = HistorySync(self.fetch, self.active, now=lambda: self.time, pending_fetch=read, read_ready=ready)
        self.sync.poll({'closed_positions':[dict(pending(), id='closed', status='closed', recovered_close_fills={'lighter': {}})]})
        self.finish()
        read.assert_not_called()

    def test_recent_persisted_probe_survives_scheduler_restart(self):
        from src.common.market_data import utc_now
        self.active.return_value = True
        read = Mock()
        self.sync = HistorySync(self.fetch, self.active, now=lambda: self.time,
                                pending_fetch=read, read_ready=lambda: True)
        row = dict(pending(), id='closed', status='closed', recovered_close_fills={'lighter': {}},
                   accounting_check=dict(checked_at=utc_now(), retry_after_seconds=1200))
        self.sync.poll({'closed_positions':[row]})
        read.assert_not_called()
