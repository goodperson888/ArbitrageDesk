"""Local resizing uses two books, while final validation obtains new books."""
import copy
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.api_v2.center_statistics import close_service
from src.common.market_data import utc_now
from src.lighter.execution import LighterExecutionAdapter
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_execution_latency import NativeTrader


class BookTrader(NativeTrader):
    def __init__(self, price, venue):
        super().__init__(price, venue)
        self.verified_fee_bps = 0
        self.book_reads, self.local_quotes, self.book, self.minimum = 0, 0, None, 0
        self.constraints_reads = 0

    def get_sizing_book(self, symbol):
        self.book_reads += 1
        return copy.deepcopy(self.book) if self.book else super().get_sizing_book(symbol)

    def quote_from_sizing_book(self, symbol, quantity, book):
        self.local_quotes += 1
        return LighterExecutionAdapter.quote_from_sizing_book(self, symbol, quantity, book)

    def get_executable_quote(self, symbol, quantity):
        self.quote_reads += 1
        return self.quote_from_sizing_book(symbol, quantity, self.get_sizing_book(symbol))

    def order_constraints(self, symbol):
        self.constraints_reads += 1
        return dict(min_base_amount=self.minimum, min_quote_amount=0)


class NativeBookSizingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.a, self.b = BookTrader(102, 'lighter'), BookTrader(100, 'arcus')
        self.s = ArbitrageStrategy(dict(mode='live', state_path=str(Path(tmp.name)/'state.json'),
            trading=dict(position_size=102.02, max_positions=2),
            spread_threshold=dict(min=1, max=500), risk=dict(slippage_tolerance=1),
            wash_trading=dict(trade_interval_min=0, max_trades_per_day=10)), self.a, self.b)
        self.addCleanup(close_service, self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)
        self.op = ArbitrageOpportunity('BTC', 102, 100, 196, 'variational_higher', 0, datetime.now())

    @staticmethod
    def thin_book(bid, ask):
        return dict(fetched_at=utc_now(),
            bids=[dict(price=bid, size=.2), dict(price=bid-2, size=100)],
            asks=[dict(price=ask, size=.2), dict(price=ask+2, size=100)])

    def test_valid_size_unchanged_with_one_book_per_venue_and_no_accounts(self):
        plan, _, _, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['quantity'], 1)
        self.assertEqual((self.a.book_reads, self.b.book_reads), (1, 1))
        self.assertEqual((self.a.local_quotes, self.b.local_quotes), (1, 1))
        self.assertEqual((self.a.reads, self.b.reads), (0, 0))
        self.assertFalse(self.a.sends or self.b.sends)

    def test_two_native_books_are_read_in_parallel(self):
        barrier = threading.Barrier(2)
        for trader in (self.a, self.b):
            original = trader.get_sizing_book
            def read(symbol, original=original):
                barrier.wait(timeout=1)
                return original(symbol)
            trader.get_sizing_book = read
        self.assertEqual(self.s.plan_entry(self.op)[0]['quantity'], 1)

    def test_same_valid_plan_uses_two_books_instead_of_three(self):
        class LegacyBookTrader(BookTrader):
            quote_from_sizing_book = None

            def get_executable_quote(self, symbol, quantity):
                return LighterExecutionAdapter.quote_from_sizing_book(
                    self, symbol, quantity, self.get_sizing_book(symbol))
        native = self.s.plan_entry(self.op)[0]
        self.s.variational_trader = LegacyBookTrader(102, 'lighter')
        self.s.lighter_trader = LegacyBookTrader(100, 'arcus')
        legacy = self.s.plan_entry(self.op)[0]
        self.assertEqual({k: v for k, v in native.items() if k not in
            ('book_utilization_percent', 'paired_depth_quantity', 'depth_quantity_limit')}, legacy)
        self.assertEqual(self.a.book_reads+self.b.book_reads, 2)
        self.assertEqual(self.s.variational_trader.book_reads+self.s.lighter_trader.book_reads, 3)

    def test_first_book_worse_price_shrinks_locally_instead_of_losing_opportunity(self):
        self.a.book = self.thin_book(102, 102)
        plan, a, b, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['quantity'], .16)
        self.assertEqual((a['size'], b['size']), (.16, .16))
        self.assertGreater(plan['net_edge_bps'], self.s.execution_min_bps('BTC'))
        self.assertEqual((self.a.book_reads, self.b.book_reads), (1, 1))
        self.assertLessEqual(self.a.local_quotes, 6)
        self.assertEqual((self.a.constraints_reads, self.b.constraints_reads), (1, 1))

    def test_second_book_slippage_shrinks_without_additional_requests(self):
        self.b.book = self.thin_book(100, 100)
        plan, _, _, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['quantity'], .16)
        self.assertEqual((self.a.book_reads, self.b.book_reads), (1, 1))

    def test_first_venue_depth_also_limits_equal_size(self):
        self.a.book = dict(fetched_at=utc_now(),
            bids=[dict(price=102, size=.2)], asks=[dict(price=102, size=.3)])
        self.assertEqual(self.s.plan_entry(self.op)[0]['quantity'], .16)

    def test_final_fixed_size_does_not_shrink_or_reuse_earlier_books(self):
        plan = self.s.plan_entry(self.op)[0]
        self.b.book = self.thin_book(100, 100)
        with self.assertRaisesRegex(ValueError, '滑点'):
            self.s.plan_entry(self.op, fixed_quantity=plan['quantity'], fixed_direction=plan['direction'])
        self.assertEqual((self.a.book_reads, self.b.book_reads), (2, 2))
        self.assertFalse(self.a.sends or self.b.sends)

    def test_smaller_amount_still_must_meet_minimum_and_original_profit_floor(self):
        self.a.book, self.a.minimum = self.thin_book(102, 102), .3
        with self.assertRaisesRegex(ValueError, '最小下单'):
            self.s.plan_entry(self.op)
        self.a.minimum = 0
        self.s.spread_threshold_min = 250
        with self.assertRaises(ValueError):
            self.s.plan_entry(self.op)
        self.assertFalse(self.a.sends or self.b.sends)

    def test_stale_book_blocks_without_any_order_or_account_read(self):
        self.b.book = self.thin_book(100, 100)
        self.b.book['fetched_at'] = (datetime.now(timezone.utc)-timedelta(seconds=6)).isoformat()
        with self.assertRaisesRegex(ValueError, '过期'):
            self.s.plan_entry(self.op)
        self.assertEqual((self.a.reads, self.b.reads), (0, 0))

    def test_actual_entry_keeps_account_reads_and_paired_quantity(self):
        self.a.book = self.thin_book(102, 102)
        self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertEqual((self.a.reads, self.b.reads), (1, 1))
        self.assertEqual((self.a.sends[0][2], self.b.sends[0][2]), (.16, .16))

    def test_larger_order_uses_smaller_directional_depth_once(self):
        self.s.position_size = 61212
        self.a.book = dict(fetched_at=utc_now(),
            bids=[dict(price=102, size=1000)], asks=[dict(price=102, size=1000)])
        self.b.book = dict(fetched_at=utc_now(),
            bids=[dict(price=100, size=1000)], asks=[dict(price=100, size=600)])
        plan, a, b, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['quantity'], 480)
        self.assertEqual(plan['paired_depth_quantity'], 600)
        self.assertEqual((a['size'], b['size']), (480, 480))
        self.assertEqual((self.a.book_reads, self.b.book_reads), (1, 1))
        self.s.position_size = 20402.04
        self.assertEqual(self.s.plan_entry(self.op)[0]['quantity'], 200)

    def test_buy_first_direction_and_quantity_rounding(self):
        self.a.price, self.b.price = 100, 102
        self.a.book = dict(fetched_at=utc_now(),
            bids=[dict(price=100, size=100)], asks=[dict(price=100, size=.333)])
        self.b.book = dict(fetched_at=utc_now(),
            bids=[dict(price=102, size=.777)], asks=[dict(price=102, size=100)])
        plan, a, b, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['direction'], 'buy_variational')
        self.assertEqual(plan['quantity'], .266)
        self.assertEqual((a['size'], b['size']), (.266, .266))

    def test_hundred_percent_disables_reserve_and_minimums_still_apply(self):
        self.a.book = self.thin_book(102, 102)
        self.s.book_utilization_percent = 100
        self.assertEqual(self.s.plan_entry(self.op)[0]['quantity'], .2)
        self.s.book_utilization_percent, self.a.minimum = 80, .18
        with self.assertRaisesRegex(ValueError, '最小下单'):
            self.s.plan_entry(self.op)
        self.assertFalse(self.a.sends or self.b.sends)

    def test_target_return_depth_can_enter_with_negative_opening_spread(self):
        self.s.config['entry_policy'] = dict(calculation_version=2, funding_reserve_bps=0)
        self.s.close_target_mode = 'center'
        self.s._target_policy_stats = lambda symbol, direction: dict(
            center_bps=-30 if direction == 'buy_variational' else 30, samples=100)
        self.a.price = 100.1
        self.a.book = dict(fetched_at=utc_now(),
            bids=[dict(price=100.1, size=100)], asks=[dict(price=100.1, size=.5)])
        self.b.book = dict(fetched_at=utc_now(),
            bids=[dict(price=100, size=.4)], asks=[dict(price=100, size=100)])
        plan, _, _, _ = self.s.plan_entry(self.op)
        self.assertEqual(plan['direction'], 'buy_variational')
        self.assertEqual(plan['quantity'], .32)
        self.assertLess(plan['opening_edge_bps'], 0)
        self.assertGreater(plan['net_edge_bps'], self.s.execution_min_bps('BTC'))
        self.assertEqual(plan['target_context']['target_bps'], -30)

    def test_final_depth_drop_resizes_both_account_tickets_without_account_reread(self):
        original = self.a.get_sizing_book
        def changing_book(symbol):
            if self.a.book_reads:
                self.a.book = dict(fetched_at=utc_now(),
                    bids=[dict(price=102, size=.5)], asks=[dict(price=102, size=100)])
            return original(symbol)
        self.a.get_sizing_book = changing_book
        self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertEqual((self.a.sends[0][2], self.b.sends[0][2]), (.4, .4))
        self.assertEqual((self.a.reads, self.b.reads), (1, 1))
        events = self.s.history.recent()
        resized = next(e['detail'] for e in events if e['event'] == 'entry_requoted')
        self.assertEqual((resized['requested_quantity'], resized['quantity']), (1, .4))

    def test_first_partial_fill_hedges_actual_fill_without_second_utilization_discount(self):
        self.a.book = dict(fetched_at=utc_now(),
            bids=[dict(price=102, size=.5)], asks=[dict(price=102, size=100)])
        original = self.a.place_order
        def partial(*args, **kwargs):
            receipt = original(*args, **kwargs)
            receipt.update(status='partial', filled_size=.2)
            return receipt
        self.a.place_order = partial
        self.assertTrue(self.s.execute_arbitrage(self.op))
        self.assertEqual((self.a.sends[0][2], self.b.sends[0][2]), (.4, .2))
        self.assertEqual((self.s.positions[0].variational_size, self.s.positions[0].lighter_size), (.2, .2))

    def test_final_downsize_below_minimum_sends_neither_leg(self):
        self.a.minimum = .45
        original = self.a.get_sizing_book
        def changing_book(symbol):
            if self.a.book_reads:
                self.a.book = dict(fetched_at=utc_now(),
                    bids=[dict(price=102, size=.5)], asks=[dict(price=102, size=100)])
            return original(symbol)
        self.a.get_sizing_book = changing_book
        self.assertFalse(self.s.execute_arbitrage(self.op))
        self.assertFalse(self.a.sends or self.b.sends)


if __name__ == '__main__':
    unittest.main()
