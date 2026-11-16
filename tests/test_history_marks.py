import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from src.web.history_marks import HistoryMarks, HistoryMarkAdapter, HistoryMarkUnavailable
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position


class HistoryMarksTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000.0
        self.calls = 0
        self.rows = [dict(venue='arcus', symbol='PUMP', bid=99, ask=101,
                          stale=False, age_seconds=0, source='arcus_websocket',
                          fetched_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat())]
        self.marks = HistoryMarks(fetch=self.fetch, now=lambda: self.now)

    def fetch(self, venue, symbol):
        self.calls += 1
        return self.rows

    def test_concurrent_polls_share_one_fetch_and_preserve_observation_time(self):
        barrier = threading.Barrier(8)
        def poll(_):
            barrier.wait()
            return self.marks.snapshot('arcus', 'PUMP')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(poll, range(8)))
        self.assertEqual(self.calls, 1)
        self.assertEqual([r['price'] for r in results], [100] * 8)
        self.now += 1
        self.assertEqual(self.marks.snapshot('arcus', 'PUMP')['observed_at'], self.now - 1)

    def test_stale_quote_never_survives_as_live_cache(self):
        self.rows[0]['age_seconds'] = 9.5
        self.marks.snapshot('arcus', 'PUMP')
        self.now += 1
        self.rows[0]['stale'] = True
        with self.assertRaisesRegex(HistoryMarkUnavailable, '过期'):
            self.marks.snapshot('arcus', 'PUMP')
        self.assertEqual(self.calls, 2)
        self.assertIsNotNone(self.marks.last_snapshot('arcus', 'PUMP'))

    def test_fresh_worker_quote_avoids_scanner_and_preserves_fee_and_age(self):
        value = {'price': 100, 'bid': 99, 'ask': 101, 'fee_bps': 2,
                 'source': 'arcus_websocket', 'observed_at': self.now, 'updated_at': self.rows[0]['fetched_at']}
        adapter = HistoryMarkAdapter('arcus', self.marks, {('arcus', 'PUMP'): value})
        self.assertEqual(adapter.get_mark_snapshot('PUMP')['fee_bps'], 2)
        self.assertEqual(self.calls, 0)
        self.now += 3
        adapter.get_mark_snapshot('PUMP')
        self.assertEqual(self.calls, 1)

    def test_stale_history_keeps_last_estimate_separate_from_current_pnl(self):
        with tempfile.TemporaryDirectory() as directory:
            def fetch(venue, symbol):
                return [{**self.rows[0], 'venue': venue}]
            marks = HistoryMarks(fetch=fetch, now=lambda: self.now)
            view = ArbitrageStrategy({'state_path': str(Path(directory)/'state.json')},
                HistoryMarkAdapter('lighter', marks), HistoryMarkAdapter('arcus', marks))
            p = Position('PUMP', 'long', 'sell', 1, 1, 98, 102, datetime.now(),
                         entry_size_variational=1, entry_size_lighter=1)
            self.assertEqual(view._position_mark(p)['unrealized_pnl_usdc'], 4)
            self.now += 12
            stale = view._position_mark(p)
            self.assertIsNone(stale['unrealized_pnl_usdc'])
            self.assertIsNone(stale['current_spread_bps'])
            self.assertEqual(stale['last_estimate']['unrealized_pnl_usdc'], 4)
            self.assertEqual(stale['last_estimate']['updated_at'], self.rows[0]['fetched_at'])

    def test_exit_estimate_uses_bid_for_long_ask_for_short_and_verified_fees(self):
        with tempfile.TemporaryDirectory() as directory:
            def fetch(venue, symbol):
                return [{**self.rows[0], 'venue': venue, 'metadata': {'fee_bps': 10}}]
            marks = HistoryMarks(fetch=fetch, now=lambda: self.now)
            view = ArbitrageStrategy({'state_path': str(Path(directory)/'state.json')},
                HistoryMarkAdapter('lighter', marks), HistoryMarkAdapter('arcus', marks))
            p = Position('PUMP', 'long', 'sell', 1, 1, 98, 102, datetime.now(),
                         entry_size_variational=1, entry_size_lighter=1, entry_fees_usdc=.1)
            row = view._position_mark(p)
            self.assertAlmostEqual(row['unrealized_pnl_usdc'], 3.9)
            self.assertAlmostEqual(row['estimated_exit_fee_usdc'], .2)
            self.assertAlmostEqual(row['estimated_exit_pnl_usdc'], 1.7)
            p.pending_fee_venues = ['lighter']
            self.assertNotIn('estimated_exit_pnl_usdc', view._position_mark(p))

    def test_invalid_or_wrong_market_is_unknown(self):
        for change in [dict(venue='lighter'), dict(symbol='BTC'), dict(bid=102),
                       dict(ask=None), dict(bid=float('nan')), dict(stale=True)]:
            with self.subTest(change=change):
                marks = HistoryMarks(fetch=lambda *_: [{**self.rows[0], **change}], now=lambda: self.now)
                with self.assertRaises(HistoryMarkUnavailable):
                    marks.snapshot('arcus', 'PUMP')

    def test_rate_limit_backoff_and_recovery(self):
        def limited(*_):
            self.calls += 1
            raise HTTPError('local', 429, 'limited', {}, None)
        self.marks.fetch = limited
        for _ in range(2):
            with self.assertRaisesRegex(HistoryMarkUnavailable, '限流'):
                self.marks.snapshot('arcus', 'PUMP')
        self.assertEqual(self.calls, 1)
        self.now += 3
        self.marks.fetch = self.fetch
        self.assertEqual(self.marks.snapshot('arcus', 'PUMP')['price'], 100)

    def test_position_marks_preserve_valid_leg_and_recover_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            def fetch(venue, symbol):
                return [{**self.rows[0], 'venue': venue,
                         'bid': 102 if venue == 'lighter' else 99,
                         'ask': 104 if venue == 'lighter' else 101,
                         'stale': venue == 'arcus' and self.calls == 0}]
            marks = HistoryMarks(fetch=fetch, now=lambda: self.now)
            view = ArbitrageStrategy({'state_path': str(Path(directory) / 'state.json')},
                                     HistoryMarkAdapter('lighter', marks), HistoryMarkAdapter('arcus', marks))
            position = Position('PUMP', 'short', 'buy', 2, 2, 104, 99,
                                datetime.now(), variational_venue='lighter', lighter_venue='arcus',
                                entry_fees_usdc=.1, entry_size_variational=2, entry_size_lighter=2)
            missing = view._position_mark(position)
            self.assertEqual(missing['mark_price_variational'], 103)
            self.assertIsNone(missing['mark_price_lighter'])
            self.assertIsNone(missing['unrealized_pnl_usdc'])
            self.assertIsNone(missing['current_spread_bps'])
            self.assertIn('arcus', missing['mark_errors'])
            self.calls = 1
            self.now += 3
            marked = view._position_mark(position)
            self.assertEqual(marked['mark_status'], 'live')
            self.assertAlmostEqual(marked['unrealized_pnl_usdc'], 3.9)
            self.assertAlmostEqual(marked['current_spread_bps'], (103-100)/103*10000)
            self.assertIn('未扣未来平仓', marked['mark_note'])
            position.variational_size = 0
            position.close_price_variational = 103
            position.realized_pnl_usdc = 1.9  # +2 closed PnL, less .1 entry fee
            partial = view._position_mark(position)
            self.assertTrue(partial['partially_closed'])
            self.assertAlmostEqual(partial['unrealized_pnl_usdc'], 3.9,
                                   msg='include closed leg result without charging entry fee twice')
            self.now += 3
            def remaining_only(venue, symbol):
                return [{**self.rows[0], 'venue': venue, 'stale': venue == 'lighter'}]
            marks.fetch = remaining_only
            remaining = view._position_mark(position)
            self.assertIsNone(remaining['current_spread_bps'])
            self.assertIsNone(remaining['mark_price_variational'])
            self.assertAlmostEqual(remaining['unrealized_pnl_usdc'], 3.9,
                                   msg='a flat leg missing a quote must not hide remaining-leg PnL')
            position.pending_fee_venues = ['lighter']
            pending = view._position_mark(position)
            self.assertIsNone(pending['unrealized_pnl_usdc'])
            self.assertIn('待核对', pending['pnl_error'])
