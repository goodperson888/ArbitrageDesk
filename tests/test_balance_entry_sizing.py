"""Paired affordable sizing reuses one account read before either leg."""
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.api_v2.center_statistics import close_service
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_execution_latency import NativeTrader


class SizingTrader(NativeTrader):
    def __init__(self, price, venue, balance):
        super().__init__(price, venue)
        self.balance, self.minimum, self.account_reads = balance, 0, 0

    @staticmethod
    def entry_available_balance(state):
        return state['balance']

    def read_entry_account_state(self):
        self.account_reads += 1
        return dict(balance=self.balance)

    def validate_order(self, symbol, quantity, reduce_only=False, side=None, account_state=None):
        self.reads += 1
        state = account_state or self.read_entry_account_state()
        if quantity*self.price < self.minimum:
            raise ValueError('缩量后低于最低金额')
        if Decimal(str(quantity))*Decimal(str(self.price)) > Decimal(str(state['balance'])):
            raise ValueError('余额不足')
        return dict(available_balance=state['balance'], min_quote_amount=self.minimum)


class BalanceSizingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.a = SizingTrader(102, 'lighter', '10000')
        self.b = SizingTrader(100, 'arcus', '10000')
        self.s = ArbitrageStrategy(dict(mode='live', state_path=str(Path(tmp.name)/'state.json'),
            trading=dict(position_size=102.02,max_positions=2),
            spread_threshold=dict(min=1,max=500), risk=dict(slippage_tolerance=1),
            wash_trading=dict(trade_interval_min=0,max_trades_per_day=10)), self.a, self.b)
        self.addCleanup(close_service,self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush,1)

    def enter(self):
        return self.s.execute_arbitrage(ArbitrageOpportunity('BTC',102,100,196,
            'variational_higher',0,datetime.now()))

    def test_smaller_balance_quantizes_both_legs_equally_and_reads_once(self):
        self.a.balance = '51.1'
        self.assertTrue(self.enter())
        self.assertEqual(self.a.sends[0][2], .5)
        self.assertEqual(self.b.sends[0][2], .5)
        self.assertEqual((self.a.account_reads,self.b.account_reads), (1,1))
        self.assertEqual((self.a.reads,self.b.reads), (1,1))

    def test_normal_funded_entry_has_no_additional_account_reads(self):
        self.assertTrue(self.enter())
        self.assertEqual((self.a.account_reads,self.b.account_reads), (1,1))
        self.assertEqual((self.a.reads,self.b.reads), (1,1))
        self.assertEqual(self.a.sends[0][2],self.b.sends[0][2])

    def test_minimum_and_zero_or_unknown_balance_block_before_first_order(self):
        for balance in ('0', 'NaN', None, '0.0001', '20'):
            with self.subTest(balance=balance):
                self.a.balance, self.a.minimum = balance, 30
                self.s.entry_scan_backoff_until = None
                self.assertFalse(self.enter())
                self.assertEqual(self.a.sends + self.b.sends, [])

    def test_fee_and_movement_buffer_is_reserved_before_rounding(self):
        with self.s.history.execution_scope('sizing'):
            self.a.price = 100
            self.a.balance = '100'
            qty = self.a.precheck_order('BTC',1,'buy',allow_resize=True,
                reference_price=100,quantity_step='.001',cost_bps=100)
            self.assertEqual(qty,Decimal('.990'))
            self.a.resize_entry_precheck('BTC',Decimal('.500'),'buy')
            with self.assertRaises(ValueError):
                self.a.resize_entry_precheck('BTC',Decimal('.501'),'buy')
        self.assertEqual(self.a.account_reads,1)

    def test_final_quote_rechecks_profit_after_resize_before_submit(self):
        self.a.balance = '51.1'
        original = self.a.read_entry_account_state
        def read():
            state = original()
            self.b.price = 102
            return state
        self.a.read_entry_account_state = read
        self.assertFalse(self.enter())
        self.assertEqual(self.a.sends+self.b.sends,[])

    def test_poor_route_does_not_pause_other_funded_pair(self):
        c = SizingTrader(99,'lighter_rh','10000')
        self.s.configure_routes([(self.a,self.b),(self.a,c),(self.b,c)])
        self.a.balance = '0'
        self.assertFalse(self.enter())
        self.assertFalse(self.s.entry_route_allowed(0))
        self.assertTrue(self.s.entry_route_allowed(2))
        self.assertEqual(self.s.entry_scan_backoff_seconds(),0)
