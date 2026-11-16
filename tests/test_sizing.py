import tempfile
import unittest
from pathlib import Path
from src.common.sizing import allocate, paired_depth_capacity, book_utilization_percent
from src.execution.history import TradeHistory


class SizingTests(unittest.TestCase):
    def test_depth_capacity_excludes_unprotected_and_unprofitable_levels(self):
        first = dict(asks=[dict(price=100, size=1), dict(price=100.05, size=2),
                           dict(price=101, size=100)])
        second = dict(bids=[dict(price=101, size=2), dict(price=100.99, size=2)])
        self.assertEqual(paired_depth_capacity(first, second, 'buy_variational', 10,
            lambda buy, sell: sell-buy >= .95), 2)
        self.assertEqual(paired_depth_capacity(first, second, 'buy_variational', 10,
            lambda buy, sell: True), 3)
        self.assertEqual(paired_depth_capacity(first, second, 'buy_variational', -1,
            lambda buy, sell: True), 0)

    def test_utilization_rejects_unknown_or_invalid_values(self):
        self.assertEqual(book_utilization_percent(), 80)
        for value in (0, -1, 101, True, None, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                book_utilization_percent(value)

    def plans(self, bids, asks=None, cap=1000, **kwargs):
        return allocate({'bid':99, 'ask':100, 'fee_bps':0},
                        {'bids':bids,'asks':asks or [{'price':101,'size':10}]},
                        cap, 3, 5, 500, lighter_fee_bps=0, **kwargs)

    def test_stop_at_first_unprofitable_level(self):
        p=self.plans([{'price':101,'size':.4},{'price':100.01,'size':10}])[0]
        self.assertTrue(p['available']);self.assertEqual(p['quantity'], .4)
        self.assertEqual(p['lighter_notional'], 40.4)

    def test_notional_cap_includes_protective_price(self):
        p=self.plans([{'price':101,'size':100}],cap=1,slippage_bps=10)[0]
        self.assertTrue(p['available']);self.assertLessEqual(p['protected_notional'],1)
        self.assertEqual(p['quantity'],.009)

    def test_never_extrapolate_missing_depth(self):
        p=self.plans([{'price':101,'size':.013}])[0]
        self.assertEqual(p['quantity'],.013)

    def test_unknown_fee_rejected(self):
        with self.assertRaises(Exception):
            allocate({'bid':99,'ask':100,'fee_bps':0},{'bids':[]},1,3,0,500)

    def test_size_bound_quote_limits_plan(self):
        p=self.plans([{'price':101,'size':2}],max_quantity=.1)[0]
        self.assertEqual(p['quantity'],.1)

    def test_directional_slippage_uses_buy_and_sell_legs_separately(self):
        p=self.plans([{'price':101,'size':1}], slippage_bps={
            'buy_variational': {'buy': 2, 'sell': 4},
            'sell_variational': {'buy': 3, 'sell': 1},
        })[0]
        self.assertTrue(p['available'])
        self.assertEqual(p['buy_slippage_bps'], 2)
        self.assertEqual(p['sell_slippage_bps'], 4)
        self.assertEqual(p['slippage_budget_bps'], 6)
        self.assertAlmostEqual(p['net_edge_bps'], 93.0297029703, places=6)

    def test_sub_step_and_empty_book_cannot_allocate(self):
        self.assertFalse(self.plans([{'price':101,'size':.0001}])[0]['available'])
        self.assertFalse(self.plans([])[0]['available'])

    def test_preview_one_dollar_reports_platform_minimum(self):
        from src.common.allocation_preview import allocation_preview
        row={'details':{'lighter':{'min_quote_amount':'10.000000'}}}
        p=allocation_preview(row,{'trading':{'position_size':1}})
        self.assertFalse(p['executable'])
        self.assertIn('最小下单',p['status'])
        self.assertEqual(p['plans'],[])

    def test_preview_prefers_observed_slippage_and_falls_back_over_limit(self):
        from src.common.allocation_preview import allocation_preview
        row={'details':{
                'variational': {'bid':99, 'ask':100, 'quote_size':1},
                'lighter': {'size_decimals':3, 'min_quote_amount':0, 'min_base_amount':0},
            },
            'book': {'bids':[{'price':101, 'size':1}], 'asks':[{'price':98, 'size':1}]},
            'comparison': {'slippage': {'directions': {
                'a': {'variational_bps':2, 'lighter_bps':4, 'known_legs':2},
                'b': {'variational_bps':20, 'lighter_bps':3, 'known_legs':2},
            }}}}
        settings={'trading':{'position_size':100}, 'risk':{'slippage_tolerance':10},
                  'spread_threshold':{'min':5, 'max':500}}
        result=allocation_preview(row, settings)
        a=next(plan for plan in result['plans'] if plan['direction']=='buy_variational')
        b=next(plan for plan in result['plans'] if plan['direction']=='sell_variational')
        self.assertEqual((a['buy_slippage_bps'], a['sell_slippage_bps']), (2, 4))
        self.assertEqual((b['buy_slippage_bps'], b['sell_slippage_bps']), (3, 10))
        self.assertEqual(a['slippage_observed_legs'], 2)
        self.assertIn('实时观测优先', a['slippage_mode'])

    def test_execution_quote_reaches_last_depth_level(self):
        from unittest.mock import Mock
        from src.lighter.execution import LighterExecutionAdapter
        from src.common.market_data import utc_now
        public=Mock()
        public.get_order_book.return_value={'fetched_at':utc_now(),
            'bids':[{'price':99,'size':1},{'price':98,'size':1}],
            'asks':[{'price':100,'size':1},{'price':101,'size':1}]}
        adapter=LighterExecutionAdapter(public,Mock(),Mock(),123,enabled=True)
        adapter.verified_fee_bps=0
        q=adapter.get_executable_quote('BTC',2)
        self.assertEqual(q['ask'],101)
        self.assertEqual(q['ask_vwap'],100.5)
        self.assertEqual(q['bid'],98)
        self.assertEqual(q['bid_vwap'],98.5)

    def test_history_survives_restart_and_excludes_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.sqlite'
            TradeHistory(path).append('planned',symbol='BTC',quantity=.01,private_key='must-not-save')
            events=TradeHistory(path).recent()
            self.assertEqual(events[0]['detail'],{'symbol':'BTC','quantity':.01})
            self.assertTrue(events[0]['timestamp'])

if __name__=='__main__': unittest.main()
