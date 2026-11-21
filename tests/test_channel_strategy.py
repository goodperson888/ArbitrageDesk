import copy
import math
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock

from src.execution.channel_policy import channel_policy, channel_bounds, channel_context, channel_target_bps, sample_statistics, touch_statistics
from src.execution.target_return import directed_spread, exit_projection
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity, Position
from test_hedge_policy import Trader


class ChannelMathTests(unittest.TestCase):
    def test_reverse_projection_preserves_canonical_target_when_quote_width_changes(self):
        a = dict(bid=100, ask=100.02, fee_bps=0)
        b = dict(bid=99.70, ask=99.72, fee_bps=0)
        for target in (-12, 0, 15):
            with self.subTest(target=target):
                original = channel_target_bps('lower', target, a, b)
                wider = dict(b, ask=99.92)
                stale = exit_projection(wider, a, original, 0, 0, 0)
                self.assertGreater(abs(directed_spread(stale['close_buy_price'], stale['close_sell_price']) - target), 19)
                refreshed = channel_target_bps('lower', target, a, wider)
                projection = exit_projection(wider, a, refreshed, 0, 0, 0)
                self.assertAlmostEqual(directed_spread(projection['close_buy_price'], projection['close_sell_price']), target)
                self.assertEqual(channel_target_bps('upper', target, a, wider), target)
        with self.assertRaisesRegex(ValueError, '方向'):
            channel_target_bps('invalid', 0, a, b)
        with self.assertRaises(ValueError):
            channel_target_bps('lower', float('nan'), a, b)

    def test_touch_channel_uses_repeated_turns_and_ignores_single_far_spike(self):
        values=[0,10,10,10,0,-10,-10,-10,0]*4+[0,90,0]
        rows=[(1000+i*30,v) for i,v in enumerate(values)]
        touches=touch_statistics(rows,2,3)
        self.assertEqual(touches['upper']['value_bps'],10)
        self.assertEqual(touches['lower']['value_bps'],-10)
        self.assertEqual(touches['upper']['count'],4)
        self.assertEqual(touches['lower']['count'],4)
        policy=channel_policy({'channel_policy':{'method':'touches'}})
        stats=dict(sample_statistics(rows,90),touch_statistics=touches)
        bounds=channel_bounds(policy,{'channel_statistics':stats})
        self.assertEqual((bounds['lower_bps'],bounds['upper_bps']),(-10,10))
        frozen=channel_context(policy,bounds,{'ask':100,'bid':100},{'ask':101,'bid':101},
            upper_direction=True,symbol='UNI',route='a->b',axis_venues=['a','b'])
        self.assertEqual(frozen['channel']['touch_statistics'],touches)

    def test_touch_channel_does_not_count_plateau_or_gaps_as_repeated_returns(self):
        self.assertIsNone(touch_statistics([(i*30,10) for i in range(40)],2,3)['upper'])
        rows=[(i*300,10 if i%2 else -10) for i in range(40)]
        result=touch_statistics(rows,2,3)
        self.assertIsNone(result['upper']); self.assertIsNone(result['lower'])
        policy=channel_policy({'channel_policy':{'method':'touches'}})
        stats=dict(sample_statistics(rows,90),touch_statistics=result)
        with self.assertRaisesRegex(ValueError,'独立触碰'):
            channel_bounds(policy,{'channel_statistics':stats})

    def test_equally_frequent_touch_zones_prefer_recent_turns(self):
        values=[0,10,0,-10]*3+[0,20,0,-20]*3+[0]
        touches=touch_statistics([(i*30,v) for i,v in enumerate(values)],2,3)
        self.assertEqual(touches['upper']['value_bps'],20)
        self.assertEqual(touches['lower']['value_bps'],-20)

    def test_touch_profile_must_match_config_and_parameters_are_validated(self):
        policy=channel_policy({'channel_policy':{'method':'touches'}})
        rows=[(i*30,v) for i,v in enumerate([0,10,0,-10]*10)]
        stats=dict(sample_statistics(rows,90),touch_statistics=touch_statistics(rows,1,3))
        with self.assertRaisesRegex(ValueError,'独立触碰'):
            channel_bounds(policy,{'channel_statistics':stats})
        for values in ({'touch_min_count':2.5},{'touch_min_count':True},{'touch_tolerance_bps':0}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                channel_policy({'channel_policy':values})

    def test_channel_wait_reasons_are_numeric_allowlisted_without_raw_error_leaks(self):
        from src.execution.telemetry import safe_reason
        for message in ('未到通道上边界: 12.30 / 20.00 bps',
                '通道有效样本不足或覆盖不足14.5分钟: 12/30',
                '通道宽度 123.45 bps 超过允许上限，暂不入场',
                '通道历史统计正在积累或更新，暂不入场'):
            self.assertEqual(safe_reason(ValueError(message)),message)
            self.assertNotIn('private',safe_reason(ValueError(message+' private credential')))

    def test_all_methods_share_bounds_and_five_second_samples(self):
        rows=[(1000+i*30,float(i)) for i in range(40)]
        rows += [(1000,0.)]*1000
        stats=sample_statistics(rows,90)
        self.assertEqual(stats['samples'],40)
        self.assertEqual(stats['center_bps'],19.5)
        for method in ('quantile','bollinger','robust','fixed'):
            p=channel_policy({'channel_policy':{'method':method,'min_samples':30}})
            result=channel_bounds(p,{'channel_statistics':stats})
            self.assertLess(result['lower_bps'],result['center_bps'])
            self.assertLess(result['center_bps'],result['upper_bps'])

    def test_invalid_parameters_do_not_silently_enable_strategy(self):
        for values in ({'quantile':100},{'method':'wrong'},{'min_samples':True},
                {'lower_bps':20,'upper_bps':10},{'min_width_bps':20,'max_width_bps':10},
                {'window_seconds':900.5},{'reversion_percent':0},{'multiplier':float('nan')}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                channel_policy({'channel_policy':values})
        for version,mode in ((1,'single'),(2,'spread_grid')):
            with self.assertRaises(ValueError):
                channel_policy({'entry_policy':{'mode':'channel','calculation_version':version},
                    'position_policy':{'mode':mode}})

    def test_warmup_is_required_and_wide_channel_is_not_compressed(self):
        p=channel_policy({'channel_policy':{'min_samples':30,'max_width_bps':10}})
        with self.assertRaisesRegex(ValueError,'积累'):
            channel_bounds(p,{'center_ready':False})
        stats={'samples':30,'sample_span_seconds':900,'lower_bps':-20,'upper_bps':20,'center_bps':0}
        with self.assertRaisesRegex(ValueError,'超过'):
            channel_bounds(p,{'channel_statistics':stats})
        p.update(max_width_bps=0,min_width_bps=60)
        self.assertEqual(channel_bounds(p,{'channel_statistics':stats})['width_bps'],60)

    def test_partial_reversion_and_opposite_targets_keep_fixed_axis(self):
        p=channel_policy({'channel_policy':{'method':'fixed','reversion_percent':50}})
        bounds=channel_bounds(p,{})
        a={'bid':99.99,'ask':100.01}; b={'bid':100.5,'ask':100.52}
        context=channel_context(p,bounds,a,b,upper_direction=True,symbol='UNI',route='a->b',axis_venues=['a','b'])
        self.assertAlmostEqual(context['target_bps'],directed_spread(a['ask'],b['bid'])/2)
        p['exit_mode']='opposite'
        context=channel_context(p,bounds,a,b,upper_direction=True,symbol='UNI',route='a->b',axis_venues=['a','b'])
        self.assertEqual(context['target_bps'],-20)
        b={'bid':99.5,'ask':99.52}
        lower=channel_context(p,bounds,a,b,upper_direction=False,symbol='UNI',route='b->a',axis_venues=['a','b'])
        self.assertEqual(lower['channel']['exit_axis_bps'],20)
        self.assertNotAlmostEqual(lower['target_bps'],-20,places=2)
        self.assertLess(lower['target_bps'],-20,'reverse target preserves both bid/ask widths')


class ChannelStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.a,self.b=Trader(100.7,'lighter'),Trader(100,'arcus')
        self.config={'mode':'live','state_path':str(Path(self.tmp.name)/'state.json'),
            'trading':{'position_size':100.25,'max_positions':3},
            'entry_policy':{'mode':'channel','calculation_version':2,'funding_reserve_bps':0},
            'channel_policy':{'method':'fixed','lower_bps':-20,'upper_bps':20,'reversion_percent':60},
            'position_policy':{'mode':'single'},'spread_threshold':{'min':5,'max':500},
            'risk':{'execution_price_movement_bps':2,'slippage_tolerance':10,'entry_extra_profit_bps':3,'max_loss_per_trade':None},
            'close_policy':{'mode':'convergence','target_mode':'center','max_hold_seconds':0,'min_profit_usdc':0},
            'wash_trading':{'trade_interval_min':0,'max_trades_per_day':100}}
        self.s=ArbitrageStrategy(self.config,self.a,self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service,self.s.market_history)
        self.addCleanup(self.s.close_diagnostics.writer.flush,1)

    def opportunity(self):
        return ArbitrageOpportunity('UNI',self.a.price,self.b.price,60,'variational_higher',0,datetime.now())

    def test_upper_and_lower_choose_equal_quantity_opposite_hedges(self):
        for price,expected,side in ((100.7,'sell_variational','upper'),(99.3,'buy_variational','lower')):
            self.a.price=price
            selected,v,l,step=self.s.plan_entry(self.opportunity())
            self.assertEqual(selected['direction'],expected)
            self.assertEqual(selected['target_context']['channel']['side'],side)
            self.assertGreaterEqual(selected['net_edge_bps'],8)
            self.assertEqual(selected['target_context']['channel']['axis_venues'],['arcus','lighter'])
            self.assertAlmostEqual(selected['variational_notional']/self.a.price,
                selected['lighter_notional']/self.b.price)
        self.assertEqual(self.a.sends+self.b.sends,[])

    def test_inside_channel_does_not_reach_first_submit(self):
        self.a.price=100.05
        with self.assertRaisesRegex(ValueError,'边界'):
            self.s.plan_entry(self.opportunity())
        self.assertFalse(self.s.execute_arbitrage(self.opportunity()))
        self.assertEqual(self.a.sends+self.b.sends,[])

    def test_final_quote_rechecks_boundary_without_moving_frozen_target(self):
        selected,*_=self.s.plan_entry(self.opportunity())
        frozen=copy.deepcopy(selected['target_context'])
        self.a.price=100.8
        again,*_=self.s.plan_entry(self.opportunity(),fixed_direction=selected['direction'],fixed_target=frozen)
        self.assertEqual(again['target_context'],frozen)
        self.a.price=100.01
        with self.assertRaisesRegex(ValueError,'边界'):
            self.s.plan_entry(self.opportunity(),fixed_direction=selected['direction'],fixed_target=frozen)

    def test_frozen_reverse_target_refreshes_for_final_quote_and_postfill_without_history_reads(self):
        self.s.config['channel_policy']['reversion_percent'] = 100
        a = dict(bid=100, ask=100.02, fee_bps=0)
        b = dict(bid=99.70, ask=99.72, fee_bps=0)
        frozen = self.s._channel_entry_context('UNI', 'buy_variational', (b, a))
        original = copy.deepcopy(frozen)
        wider = dict(b, ask=99.92)
        impacts = {d: dict(buy=0, sell=0) for d in ('buy_variational', 'sell_variational')}
        closing = {d: dict(variational=0, lighter=0) for d in impacts}
        with patch.object(self.s, '_target_policy_stats', side_effect=AssertionError('unexpected history read')):
            models, _ = self.s._target_models('UNI', wider, a, impacts, closing, frozen)
            model = models['buy_variational']
            self.assertEqual(model['context']['channel'], original['channel'])
            self.assertEqual(model['context']['frozen_at'], original['frozen_at'])
            self.assertNotAlmostEqual(model['context']['target_bps'], original['target_bps'])
            selected = dict(direction='buy_variational', target_context=frozen,
                target_projection=model['projection'], close_slippage_bps=closing['buy_variational'],
                buy_slippage_bps=0, sell_slippage_bps=0)
            completion = self.s._completion_target_projection(selected, wider, a, 99.72)
        for projection in (model['projection'], completion):
            self.assertAlmostEqual(directed_spread(projection['close_buy_price'], projection['close_sell_price']), 0)
        self.assertEqual(frozen, original, 'frozen canonical target and its source record remain unchanged')
        self.assertEqual(self.a.sends + self.b.sends, [])

    def test_saved_position_keeps_channel_on_restart_and_needs_profit(self):
        selected,v,l,_=self.s.plan_entry(self.opportunity())
        context=selected['target_context']
        p=Position('UNI','short','buy',1,1,self.a.price,self.b.price,datetime.now(),status='open',
            entry_size_variational=1,entry_size_lighter=1,variational_venue='lighter',lighter_venue='arcus',
            entry_target=context)
        self.s.positions=[p];self.s._save()
        restored=ArbitrageStrategy(self.config,self.a,self.b)
        self.addCleanup(restored.close_diagnostics.writer.flush,1)
        self.assertEqual(restored.positions[0].entry_target,context)
        self.assertIsNotNone(restored.halted_reason)
        self.s.config['channel_policy']['upper_bps']=900
        self.s.close_mode='profit'
        with patch.object(self.s,'_single_profit_estimate',return_value={'net_pnl_usdc':10,'net_pnl_bps':10}):
            self.assertIsNone(self.s.close_reason(p),'channel must reach its frozen target even if global mode changes')
        self.a.price=100
        with patch.object(self.s,'_single_profit_estimate',return_value={'net_pnl_usdc':-1,'net_pnl_bps':-1}):
            self.assertIsNone(self.s.close_reason(p),'touching the target cannot bypass profitability')

    def test_reverse_cycle_does_not_reopen_same_direction(self):
        self.s.config['channel_policy']['exit_mode']='opposite'
        selected,*_=self.s.plan_entry(self.opportunity())
        p=Position('UNI','short','buy',0,0,100.7,100,datetime.now(),status='closed',
            entry_size_variational=1,entry_size_lighter=1,entry_target=selected['target_context'],close_reason='价差收敛')
        self.s.positions=[p]
        with self.assertRaisesRegex(ValueError,'反向'):
            self.s.plan_entry(self.opportunity())
        self.a.price=99.3
        selected,*_=self.s.plan_entry(self.opportunity())
        self.assertEqual(selected['direction'],'buy_variational')


if __name__=='__main__':unittest.main()
