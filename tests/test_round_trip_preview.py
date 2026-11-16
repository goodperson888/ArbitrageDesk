import copy
import unittest
from unittest.mock import Mock, patch

from src.execution.return_preview import round_trip_preview
from src.execution.target_return import exit_projection, projected_return
from src.api_v2.app import attach_history_stats, build_opportunities, opportunity_sort_key
from src.api_v2.return_estimates import attach_return_previews
from src.api_v2.models import QuoteSnapshot


POLICY = dict(movement_bps=2,slippage_tolerance=10,funding_reserve_bps=5,
              base_min_bps=5,entry_reserve_bps=3,max_bps=500)


def quote(bid,ask,fee):
    return dict(bid=bid,ask=ask,fee_bps=fee,buy_impact_bps=1,sell_impact_bps=2)


class SharedPreviewTests(unittest.TestCase):
    def setUp(self):
        self.buy,self.sell = quote(99.98,100.02,4.5),quote(100.18,100.22,0)

    def test_matches_execution_math_and_cost_breakdown_without_double_charging(self):
        for target in (-40,0,12):
            mode = 'zero' if target == 0 else 'center'
            p = round_trip_preview(self.buy,self.sell,.1,mode,target,100,POLICY)
            projection = exit_projection(self.buy,self.sell,target,4,3,5,100.22*1.0002)
            actual = projected_return(100.02,100.18,.1,4.5,0,1,2,projection)
            self.assertAlmostEqual(p['net_bps'],actual['net_edge_bps'])
            self.assertAlmostEqual(p['profit_usdc'],actual['estimated_entry_edge_usdc'])
            self.assertAlmostEqual(p['gross_bps']-sum(p['costs'].values()),p['net_bps'])
            self.assertEqual(p['minimum_bps'],8)

    def test_opening_uncertainty_uses_reserve_while_close_budget_and_guards_remain(self):
        policy = dict(POLICY, funding_reserve_bps=0)
        zero = round_trip_preview(self.buy,self.sell,.1,'center',-40,100,dict(policy,movement_bps=0))
        two = round_trip_preview(self.buy,self.sell,.1,'center',-40,100,policy)
        self.assertAlmostEqual(zero['opening_net_bps'],two['opening_net_bps'])
        self.assertAlmostEqual(zero['costs']['open_fees_bps'],two['costs']['open_fees_bps'])
        self.assertAlmostEqual(zero['costs']['open_impact_and_movement_bps'],two['costs']['open_impact_and_movement_bps'])
        self.assertEqual(two['opening_movement_cost_bps'],0)
        self.assertEqual(two['entry_reserve_bps'],3)
        self.assertEqual(two['minimum_bps'],8)
        self.assertGreater(two['components']['book_impact_bps'],0)
        self.assertAlmostEqual(two['components']['movement_budget_bps'],4,delta=.03)
        self.assertLess(two['net_bps'],zero['net_bps'],'future closing uncertainty is still charged')
        guarded = round_trip_preview(self.buy,self.sell,.1,'center',-40,100,dict(policy,movement_bps=9))
        self.assertGreater(guarded['net_bps'],guarded['minimum_bps'])
        self.assertFalse(guarded['meets_floor'],'opening impact plus movement still enforces the order guard')
        self.assertIn('每腿上限',guarded['reason'])

    def test_selected_center_zero_and_either_change_target_and_profit(self):
        center=round_trip_preview(self.buy,self.sell,.1,'center',10,100,POLICY)
        zero=round_trip_preview(self.buy,self.sell,.1,'zero',10,100,POLICY)
        either=round_trip_preview(self.buy,self.sell,.1,'either',10,100,POLICY)
        self.assertEqual(center['target_bps'],10)
        self.assertEqual(zero['target_bps'],0)
        self.assertLess(center['net_bps'],zero['net_bps'])
        self.assertEqual(either['target_bps'],10,'either freezes the nearest favorable target')

    def test_missing_costs_are_unknown_and_zero_target_does_not_require_center_history(self):
        for side,key in ((0,'fee_bps'),(0,'buy_impact_bps'),(0,'sell_impact_bps'),(1,'buy_impact_bps')):
            qs=copy.deepcopy([self.buy,self.sell]); qs[side][key]=None
            p=round_trip_preview(*qs,.1,'zero',None,0,POLICY)
            self.assertIsNone(p['net_bps']);self.assertFalse(p['meets_floor'])
            self.assertTrue(p['reason'])
        self.assertIsNotNone(round_trip_preview(self.buy,self.sell,.1,'zero',None,0,POLICY)['net_bps'])
        self.assertIsNone(round_trip_preview(self.buy,self.sell,.1,'either',None,0,POLICY)['net_bps'])

    def test_positive_opening_net_can_fail_full_cycle_and_show_exact_gap(self):
        p=round_trip_preview(quote(99.97,100,4.5),quote(100.25,100.3,0),.1,'center',8,100,POLICY)
        self.assertGreater(p['opening_net_bps'],0)
        self.assertLess(p['net_bps'],p['minimum_bps'])
        self.assertFalse(p['meets_floor'])
        self.assertAlmostEqual(p['gap_bps'],8-p['net_bps'])


class ApiPreviewTests(unittest.TestCase):
    def setUp(self):
        def snapshot(venue,bid,ask,fee):
            return QuoteSnapshot(venue=venue,symbol='BTC',venue_symbol='BTC',instrument_type='perpetual',
                source='test',bid=bid,ask=ask,stale=False,age_seconds=0,
                metadata=dict(taker_fee_bps=fee,bid_size_base=100,ask_size_base=100))
        self.rows=build_opportunities('BTC',[('arcus','lighter')],dict(
            arcus=snapshot('arcus',99.98,100,4.5),lighter=snapshot('lighter',100.3,100.32,0)),record=False,notional=12)
        self.history=Mock()
        self.history.pair_key.side_effect=lambda a,b:a+'->'+b
        self.history.reversion_stats.side_effect=lambda *args,**kwargs:dict(center_bps=6 if args[2]==86400 else 12,samples=100)
        self.history.stats_for.return_value={}

    def test_rest_and_stream_use_same_selected_target_and_window(self):
        stream=attach_return_previews(self.rows,self.history,POLICY,'zero',86400)
        with patch('src.api_v2.app.state.history',self.history),patch('src.api_v2.app.display_policy',return_value=POLICY):
            rest=attach_history_stats(self.rows,12,'zero',86400)
        self.assertEqual([r.return_preview for r in rest],[r.return_preview for r in stream])
        one=attach_return_previews(self.rows,self.history,POLICY,'center',86400)
        seven=attach_return_previews(self.rows,self.history,POLICY,'center',604800)
        self.assertEqual(one[0].return_preview['target_bps'],6)
        self.assertEqual(seven[0].return_preview['target_bps'],12)
        self.assertGreater(one[0].return_preview['net_bps'],seven[0].return_preview['net_bps'])

    def test_missing_close_depth_or_expired_quote_cannot_look_profitable(self):
        row=self.rows[0].model_copy(deep=True)
        row.return_inputs['buy']['sell_impact_bps']=None
        self.assertIsNone(attach_return_previews([row],self.history,POLICY)[0].estimated_reversion_profit_usdc)
        row=self.rows[0].model_copy(update={'sell_age_seconds':5.1})
        p=attach_return_previews([row],self.history,POLICY)[0].return_preview
        self.assertIsNone(p['net_bps']);self.assertFalse(p['meets_floor'])

    def test_return_ranking_beats_larger_opening_spread(self):
        a,b=[r.model_copy(deep=True) for r in self.rows]
        a.net_bps,a.estimated_reversion_profit_usdc=100,-.1
        a.return_preview=dict(meets_floor=False,net_bps=-10)
        b.net_bps,b.estimated_reversion_profit_usdc=-10,.01
        b.return_preview=dict(meets_floor=True,net_bps=10)
        self.assertGreater(opportunity_sort_key(b),opportunity_sort_key(a))
