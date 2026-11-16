import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock

from src.execution.grid_policy import GridPolicy, layer_funding
from src.execution.precheck import expected_entry_positions
from src.execution.worker import ExecutionWorker
from src.execution.orders import direction
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_execution_latency import NativeTrader
from src.common.market_data import utc_now


class GridTrader(NativeTrader):
    supports_grid_additions = True
    def __init__(self, price, venue):
        super().__init__(price, venue)
        self.exposure = 0.0
        self.closes = []
        self.funding_rows = []
        self.fee_bps = 0

    def get_executable_quote(self, symbol, quantity):
        return dict(super().get_executable_quote(symbol, quantity), fee_bps=self.fee_bps)

    def validate_order(self, symbol, quantity, reduce_only=False, side=None):
        self.validate_entry_position(symbol, self.exposure, direction(side))
        return super().validate_order(symbol, quantity, reduce_only, side)

    def place_order(self, *args, **kwargs):
        if kwargs.get('reduce_only'):
            return self.close_position(args[0], 'buy' if direction(args[1]) == 'sell' else 'sell', args[2])
        receipt = super().place_order(*args, **kwargs)
        self.exposure += receipt['filled_size'] * (1 if direction(args[1]) == 'buy' else -1)
        return receipt

    def position_snapshot(self, symbol):
        return {'qty': abs(self.exposure), 'side': 'long' if self.exposure > 0 else 'short'}

    def reconcile_funding(self, symbol, start, end):
        return {'complete': True, 'funding_usdc': sum(r['change'] for r in self.funding_rows),
                'rows': self.funding_rows, 'venue': self.venue}

    def close_position(self, symbol, side, size, **kwargs):
        if size > abs(self.exposure) + 1e-8:
            raise ValueError('cannot reduce more than actual account size')
        self.exposure -= size * (1 if direction(side) == 'buy' else -1)
        self.closes.append(size)
        return {'confirmed': True, 'status': 'filled', 'filled_size': size,
                'average_price': self.price, 'fees_usdc': 0, 'funding_usdc': None, 'order_id': 'close-' + str(len(self.closes))}

    def assert_flat(self, symbol):
        return abs(self.exposure) < 1e-8


class GridTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = GridTrader(100.2, 'lighter'), GridTrader(100, 'arcus')
        self.config = {'mode': 'live', 'state_path': str(Path(self.tmp.name) / 'state.json'),
                       'trading': {'position_size': 100.25, 'max_positions': 3},
                       'spread_threshold': {'min': 5, 'max': 500},
                       'risk': {'slippage_tolerance': 10, 'entry_extra_profit_bps': 0},
                       'wash_trading': {'trade_interval_min': 0, 'max_trades_per_day': 100},
                       'close_policy': {'max_hold_seconds': 3600, 'min_profit_usdc': 0},
                       'position_policy': {'mode': 'spread_grid', 'step_bps': 5, 'max_layers': 3,
                                           'layer_notional_usdc': 100.25, 'total_notional_usdc': 300}}
        self.s = ArbitrageStrategy(self.config, self.a, self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)

    def enter(self, price=None, symbol='UNI'):
        if price is not None:
            self.a.price = price
        return self.s.execute_arbitrage(ArbitrageOpportunity(symbol, self.a.price, self.b.price,
            abs(self.a.price-self.b.price)/max(self.a.price,self.b.price)*10000,
            'variational_higher', 0, datetime.now()))

    def test_grid_requires_better_net_spread_and_aggregate_positions_match(self):
        self.assertTrue(self.enter())
        self.assertFalse(self.enter(100.24))
        self.assertEqual(len(self.a.sends), 1)
        self.assertTrue(self.enter(100.35))
        self.assertEqual(self.s.reconcile_open_positions(0), 'ok')
        self.assertEqual([p.grid_layer for p in self.s.positions], [0, 1])
        self.assertEqual(len({p.grid_cycle_id for p in self.s.positions}), 1)

    def test_fee_increase_cannot_fake_a_better_grid_level(self):
        self.assertTrue(self.enter())
        self.a.fee_bps = 15
        self.assertFalse(self.enter(100.35))
        self.assertEqual(len(self.a.sends), 1)

    def test_grid_cannot_switch_symbol_or_reverse_route_mid_cycle(self):
        self.assertTrue(self.enter())
        self.assertFalse(self.enter(100.5, 'SOL'))
        self.assertFalse(self.enter(99.5))
        self.assertEqual(len(self.a.sends), 1)

    def test_closed_layers_not_reused_and_each_next_layer_beats_last_actual_fill(self):
        self.assertTrue(self.enter())
        self.assertTrue(self.enter(100.5))
        second = self.s.positions[-1]
        self.assertTrue(self.s.close_position(second, 'test'))
        self.assertEqual(self.s.reconcile_open_positions(0), 'ok')
        self.assertFalse(self.enter(100.45))
        self.assertTrue(self.enter(100.65))
        self.assertEqual(self.s.positions[-1].grid_layer, 2)
        self.assertFalse(self.enter(101))

    def test_single_mode_keeps_existing_no_addition_rule(self):
        self.s.grid_policy = GridPolicy(mode='single')
        self.assertTrue(self.enter())
        self.assertFalse(self.enter(100.5))
        self.assertIsNone(self.s.positions[0].grid_cycle_id)

    def test_continuous_grid_can_add_layers_even_with_single_position_cycle_default(self):
        worker = ExecutionWorker(self.s, ['UNI'], lock_path=Path(self.tmp.name)/'lease',
                                 kill_path=Path(self.tmp.name)/'STOP', live=True)
        prices = iter([100.2,100.35,100.5])
        check = self.s.check_arbitrage_opportunity
        def discover(symbol):
            self.a.price = next(prices)
            return check(symbol)
        with patch.object(worker,'_discover_parallel',side_effect=lambda **kwargs:[discover('UNI')]), \
                patch.object(self.s,'close_reason',return_value=None), \
                patch.object(worker.stop_event,'wait'):
            worker.run(max_cycles=3)
        self.assertEqual(len(self.a.sends),3)
        self.assertEqual(self.s.reconcile_open_positions(0),'ok')

    def test_unknown_hedge_receipt_stops_entire_grid_without_another_entry(self):
        self.assertTrue(self.enter())
        with patch.object(self.b,'place_order',return_value={'confirmed':False,'status':'unknown'}):
            self.assertFalse(self.enter(100.35))
        sends=len(self.a.sends)
        self.assertTrue(self.s.halted_reason)
        self.assertFalse(self.enter(100.7))
        self.assertEqual(len(self.a.sends),sends)

    def test_partial_layer_exit_preserves_other_layer_and_new_cycle_only_after_flat(self):
        self.assertTrue(self.enter())
        self.assertTrue(self.enter(100.35))
        original_cycle = self.s.positions[0].grid_cycle_id
        for p in list(self.s.positions):
            self.assertTrue(self.s.close_position(p, 'test'))
        self.assertAlmostEqual(self.a.exposure, 0)
        self.assertAlmostEqual(self.b.exposure, 0)
        self.assertTrue(self.enter(100.5))
        self.assertEqual(self.s.positions[-1].grid_layer, 0)
        self.assertNotEqual(self.s.positions[-1].grid_cycle_id, original_cycle)

    def test_manual_change_or_opposite_exposure_blocks_addition(self):
        self.assertTrue(self.enter())
        self.a.exposure -= .1
        self.assertFalse(self.enter(100.5))
        self.assertEqual(len(self.a.sends), 1)
        self.assertTrue(self.s.halted_reason)

    def test_restart_keeps_cycle_and_requires_account_recovery(self):
        self.assertTrue(self.enter())
        self.assertTrue(self.enter(100.35))
        restored = ArbitrageStrategy(self.config, self.a, self.b)
        self.assertTrue(restored.halted_reason)
        self.assertEqual(restored.positions[-1].grid_layer, 1)
        self.assertEqual(restored.positions[0].grid_cycle_id, self.s.positions[0].grid_cycle_id)
        restored.halted_reason = None  # Explicit simulated successful read-only resume.
        self.assertEqual(restored.reconcile_open_positions(0), 'ok')
        self.assertFalse(restored._grid_context('UNI','sell_variational')['minimum_bps'] < 30)

    def test_both_grid_and_global_amount_caps_and_global_layer_cap_apply(self):
        self.s.grid_policy = GridPolicy(mode='spread_grid', layer_notional_usdc=20, total_notional_usdc=30)
        self.s.max_total_position_notional = 25
        self.assertLessEqual(self.s.entry_notional_cap(), 20)
        self.assertTrue(self.enter())
        self.assertLessEqual(self.s.entry_notional_cap(), 5.1)
        self.s.max_positions = 1
        self.assertFalse(self.enter(100.5))

    def test_unsupported_execution_adapter_cannot_start_grid(self):
        self.b.supports_grid_additions = False
        self.assertFalse(self.enter())
        self.assertEqual(self.a.sends, [])

    def test_exit_uses_executable_prices_fees_budget_and_funding(self):
        self.assertTrue(self.enter())
        p = self.s.positions[0]
        self.a.price = 100
        self.assertEqual(self.s.close_reason(p), '网格逐层净收益退出')
        self.a.fee_bps = 30
        self.assertIsNone(self.s.close_reason(p))
        self.a.fee_bps = 0
        self.a.reconcile_funding = lambda *args: None
        self.assertIsNone(self.s.close_reason(p))
        p.open_time -= timedelta(hours=2)
        self.assertEqual(self.s.close_reason(p), '最长持仓保护')

    def test_normal_exit_rechecks_profit_and_pins_both_reduce_only_quote_bounds(self):
        self.assertTrue(self.enter())
        p=self.s.positions[0]
        self.a.price=100
        with patch.object(self.a,'place_order',wraps=self.a.place_order) as first, \
                patch.object(self.b,'place_order',wraps=self.b.place_order) as second:
            self.assertTrue(self.s.close_position(p,'网格逐层净收益退出'))
        self.assertTrue(first.call_args.kwargs['reduce_only'])
        self.assertTrue(second.call_args.kwargs['reduce_only'])
        self.assertEqual(first.call_args.kwargs['slippage_bps'],2)
        self.assertIn('submit_deadline_monotonic',first.call_args.kwargs['quote'])
        self.assertEqual(p.status,'closed')

    def test_first_close_zero_fill_after_parallel_peer_fill_reduces_verified_remainder(self):
        self.assertTrue(self.enter())
        p=self.s.positions[0]
        self.a.price=100
        with patch.object(self.a,'place_order',return_value={'confirmed':True,'status':'rejected','filled_size':0}):
            self.assertTrue(self.s.close_position(p,'网格逐层净收益退出'))
        self.assertEqual(p.status,'closed')
        self.assertEqual(len(self.a.closes),1)
        self.assertEqual(len(self.b.closes),1)
        self.assertIsNone(self.s.halted_reason)

    def test_unknown_parallel_close_retains_peer_fill_without_blind_retry(self):
        self.assertTrue(self.enter())
        p=self.s.positions[0];self.a.price=100
        with patch.object(self.a,'place_order',return_value={'confirmed':False,'status':'unknown'}) as first:
            self.assertFalse(self.s.close_position(p,'网格逐层净收益退出'))
        self.assertEqual(first.call_count,1)
        self.assertEqual(len(self.b.closes),1)
        self.assertEqual(p.lighter_size,0)
        self.assertEqual(p.variational_size,1)
        self.assertEqual(p.status,'reconcile_required')

    def test_both_confirmed_zero_fills_wait_for_another_profitable_grid_exit(self):
        self.assertTrue(self.enter())
        p = self.s.positions[0]
        self.a.price = 100
        zero = {'confirmed': True, 'status': 'canceled', 'filled_size': 0}
        with patch.object(self.a, 'place_order', return_value=zero) as first, \
                patch.object(self.b, 'place_order', return_value=zero) as second:
            self.assertFalse(self.s.close_position(p, '网格逐层净收益退出'))
        self.assertEqual((first.call_count, second.call_count), (1, 1))
        self.assertEqual(p.status, 'open')
        self.assertIsNone(p.close_started_time)
        self.assertEqual(p.close_attempts, {})
        self.assertIsNone(self.s.halted_reason)
        self.assertEqual(self.a.closes + self.b.closes, [])
        self.a.price = 103
        self.assertFalse(self.s.close_position(p, '网格逐层净收益退出'))
        self.assertEqual(self.a.closes + self.b.closes, [])

    def test_stale_peer_quote_prevents_both_grid_submissions(self):
        self.assertTrue(self.enter())
        p = self.s.positions[0]
        self.a.price = 100
        plan = self.s._grid_exit_estimate(p)
        plan['legs']['lighter']['quote']['updated_at'] = '2000-01-01T00:00:00+00:00'
        with patch.object(self.s, '_grid_exit_estimate', return_value=plan), \
                patch.object(self.a, 'place_order', wraps=self.a.place_order) as first, \
                patch.object(self.b, 'place_order', wraps=self.b.place_order) as second:
            self.assertFalse(self.s.close_position(p, '网格逐层净收益退出'))
        self.assertEqual((first.call_count, second.call_count), (0, 0))
        self.assertEqual(p.status, 'open')
        self.assertEqual(p.close_attempts, {})
        self.assertIsNone(self.s.halted_reason)

    def test_after_first_known_close_rejected_second_leg_uses_reduce_only_recovery(self):
        self.assertTrue(self.enter())
        p=self.s.positions[0];self.a.price=100
        with patch.object(self.b,'place_order',return_value={'confirmed':True,'status':'rejected','filled_size':0}):
            self.assertTrue(self.s.close_position(p,'网格逐层净收益退出'))
        self.assertEqual(len(self.b.closes),1)
        self.assertAlmostEqual(self.b.exposure,0)

    def test_two_profit_layers_do_not_duplicate_account_funding(self):
        self.assertTrue(self.enter())
        self.assertTrue(self.enter(100.35))
        now = datetime.now()
        positions = self.s.positions
        for p in positions: p.open_time = now - timedelta(seconds=10)
        row = {'timestamp': int((now-timedelta(seconds=1)).timestamp()*1000), 'funding_id': 'f1',
               'market_id': 1, 'position_size': abs(self.a.exposure), 'change': -.01}
        self.a.funding_rows = [row, row]  # Duplicate API rows cannot double-charge.
        for p in positions:
            self.assertTrue(self.s.close_position(p, 'test'))
        self.assertAlmostEqual(sum(p.funding_settlements['variational']['funding_usdc'] for p in positions), -.01)

    def test_historical_volatility_updates_same_budget_instead_of_profit_floor(self):
        with patch.object(self.s, '_route_reversion_stats', return_value={'samples': 100, 'p10_bps': 0, 'p90_bps': 40}):
            self.assertEqual(self.s.execution_min_bps('UNI'), 5)
            self.assertEqual(self.s.execution_movement_bps('UNI'), 4)
        self.s.config['risk']['execution_safety_margin_bps'] = 3
        self.s.execution_safety_margin_bps = 1
        self.assertEqual(self.s.execution_min_bps('UNI'), 6)

    def test_grid_releases_entry_reserve_but_keeps_actual_next_layer_floor(self):
        self.assertTrue(self.enter())
        self.s.execution_safety_margin_bps = 30
        context = self.s._grid_context('UNI', 'sell_variational')
        self.assertEqual(context['minimum_bps'], 35)
        self.assertLess(context['hedge_minimum_bps'], context['minimum_bps'])
        self.assertGreaterEqual(context['hedge_minimum_bps'], self.s.positions[0].grid_entry_net_bps + 5)

    def test_invalid_grid_configuration_rejected(self):
        for values in ({'step_bps': 0}, {'step_bps': float('nan')}, {'max_layers': 2.5},
                       {'max_layers': True}, {'total_notional_usdc': 1}, {'mode': 'random'}):
            config = copy.deepcopy(self.config)
            config['position_policy'].update(values)
            with self.subTest(values=values), self.assertRaises(ValueError): GridPolicy.from_config(config)
        config = copy.deepcopy(self.config)
        config['trading']['max_positions'] = 1
        self.assertEqual(GridPolicy.from_config(config).max_layers, 3)

    def test_saved_mode_switch_waits_until_entire_cycle_is_flat(self):
        import yaml
        path=Path(self.tmp.name)/'config.yaml'
        path.write_text(yaml.safe_dump({'strategy':self.config}))
        self.s.config_path=path
        self.s._config_mtime_ns=self.s._read_config_mtime()
        self.assertTrue(self.enter())
        changed=copy.deepcopy(self.config);changed['position_policy']['mode']='single'
        path.write_text(yaml.safe_dump({'strategy':changed}))
        self.assertFalse(self.s.reload_runtime_config())
        self.assertTrue(self.s.grid_enabled)
        self.assertTrue(self.s.close_position(self.s.positions[0],'test'))
        self.assertTrue(self.s.reload_runtime_config())
        self.assertFalse(self.s.grid_enabled)

    def test_actual_native_adapter_account_checks_allow_only_exact_authorized_exposure(self):
        from src.arcus.execution import ArcusExecutionAdapter
        from src.lighter.execution import LighterExecutionAdapter
        from test_arcus_execution import ArcusOrderPrecheckTest
        arcus=ArcusExecutionAdapter(ArcusOrderPrecheckTest.Client([]),None,'test',enabled=True,fee_bps=0)
        public,bridge=Mock(),Mock()
        public.get_market.return_value=({'market_id':1,'min_base_amount':'.001','min_quote_amount':'1'},utc_now())
        public.get_market_price.return_value=100
        public.client.get.return_value=({'accounts':[{'account_index':7,'available_balance':'1000',
            'positions':[{'market_id':1,'position':'1','sign':-1}]}]},utc_now())
        bridge.call.return_value=[]
        lighter=LighterExecutionAdapter(public,bridge,None,7,enabled=True)
        for adapter,symbol,qty,side,new_qty in [(arcus,'CRCL',.164,'buy',.01),(lighter,'UNI',-1,'sell',.01)]:
            with self.subTest(venue=type(adapter).__name__), patch.object(adapter,'get_market_price',return_value=100):
                with self.assertRaises(ValueError): adapter.validate_order(symbol,new_qty,side=side)
                with self.s.history.execution_scope('native-grid-check'):
                    with expected_entry_positions({(id(adapter),symbol):qty}):
                        adapter.validate_order(symbol,new_qty,side=side)
                    with expected_entry_positions({(id(adapter),symbol):qty*2}):
                        with self.assertRaises(ValueError): adapter.validate_order(symbol,new_qty,side=side)
                with self.assertRaises(ValueError): adapter.validate_order(symbol,new_qty,side=side)

    def test_funding_without_quantity_evidence_stays_unknown_instead_of_crediting_each_layer(self):
        self.assertTrue(self.enter())
        self.assertTrue(self.enter(100.35))
        snapshot={'complete':True,'rows':[{'funding_id':'f1','timestamp':datetime.now().isoformat(),'change':.1}]}
        self.assertIsNone(layer_funding(snapshot,self.s.positions[0],self.s.positions,'variational'))

    def test_addition_authorization_is_scoped_to_exact_symbol_side_and_execution(self):
        self.a.exposure = -1
        with self.assertRaises(ValueError): self.a.validate_entry_position('UNI', -1, 'sell')
        with self.s.history.execution_scope('grid-proof'):
            with expected_entry_positions({(id(self.a), 'UNI'): -1}):
                self.a.validate_entry_position('UNI', -1, 'sell')
                for symbol, actual, side in [('SOL',-1,'sell'), ('UNI',-2,'sell'), ('UNI',-1,'buy'),('UNI',0,'sell')]:
                    with self.assertRaises(ValueError): self.a.validate_entry_position(symbol, actual, side)
        with self.assertRaises(ValueError): self.a.validate_entry_position('UNI', -1, 'sell')


if __name__ == '__main__':
    unittest.main()
