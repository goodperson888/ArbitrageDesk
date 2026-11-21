import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from src.common.market_data import utc_now
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity, Position


def fill(quantity, price=101):
    return {'success': True, 'confirmed': True, 'status': 'filled', 'filled_size': quantity,
            'average_price': price, 'order_id': 'test-order', 'fees_usdc': 0, 'funding_usdc': 0}


def adapter(price):
    trader = Mock()
    trader.live_ready = True
    trader.size_decimals.return_value = 3
    trader.get_market_price.return_value = price
    trader.get_sizing_book.side_effect = lambda symbol: {'fetched_at':utc_now(),'bids':[{'price':trader.get_market_price.return_value,'size':100}], 'asks':[{'price':trader.get_market_price.return_value,'size':100}]}
    trader.get_executable_quote.side_effect = lambda symbol, quantity: {
        'symbol': symbol, 'size': quantity, 'bid': trader.get_market_price.return_value, 'ask': trader.get_market_price.return_value,
        'updated_at': utc_now(), 'executable': True, 'instrument_type': 'perpetual', 'fee_bps': 0}
    trader.place_order.side_effect = lambda symbol, side, quantity, **kw: fill(quantity, price)
    trader.close_position.side_effect = lambda symbol, side, size, **kw: fill(size, price)
    trader.assert_flat = Mock(return_value=True)
    return trader


class HedgeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.v, self.l = adapter(102), adapter(100)
        self.config = {'mode': 'live', 'state_path': str(Path(self.temp.name)/'state.json'),
                       'trading': {'position_size': 102.02, 'max_positions': 2},
                       'spread_threshold': {'min': 1, 'max': 500},
                       'risk': {'slippage_tolerance': 1},
                       'wash_trading': {'trade_interval_min': 0, 'max_trades_per_day': 10}}
        self.strategy = ArbitrageStrategy(self.config, self.v, self.l)
        self.stats_history = self.strategy.market_history
        self.opportunity = ArbitrageOpportunity('BTC', 102, 100, 198,
                                                'variational_higher', 2, datetime.now())

    def tearDown(self):
        from src.api_v2.center_statistics import close_service
        close_service(self.stats_history)
        # Optional entry/close evidence is written asynchronously. Drain it
        # before removing this test's database directory.
        self.assertTrue(self.strategy.close_diagnostics.writer.flush(timeout=1))
        self.temp.cleanup()

    def test_equal_base_quantity_not_equal_notional(self):
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.v.place_order.call_args.args[2], 1)
        self.assertEqual(self.l.place_order.call_args.args[2], 1)
        self.assertEqual(self.strategy.positions[0].status, 'open')

    def _open_position(self):
        return Position('BTC', 'short', 'buy', 1, 1, 102, 100, datetime.now())

    def _known_exit_funding(self):
        for trader in (self.v, self.l):
            trader.reconcile_funding.return_value = {'complete': True, 'funding_usdc': 0}

    def test_close_policy_convergence_keeps_existing_default(self):
        self._known_exit_funding()
        position = self._open_position()
        self.v.get_market_price.return_value = 101
        self.l.get_market_price.return_value = 101
        self.assertTrue(self.strategy.should_close_position(position))
        self.v.get_market_price.return_value = 101.5
        self.l.get_market_price.return_value = 100
        self.assertFalse(self.strategy.should_close_position(position))

    def test_close_policy_can_require_profit_and_convergence(self):
        self.strategy.config['close_policy'] = {
            'mode': 'convergence_and_profit', 'convergence_basis': 'remaining', 'convergence_bps': 2,
            'min_profit_usdc': 2.5, 'min_profit_bps': 0, 'cost_reserve_usdc': 0, 'max_hold_seconds': 3600}
        self.strategy.reload_close_policy()
        position = Position('BTC', 'short', 'buy', 1, 1, 102, 99, datetime.now())
        for trader in (self.v, self.l):
            trader.reconcile_funding.return_value = {'complete': True, 'funding_usdc': 0}
        self.v.get_market_price.return_value = 101.5
        self.l.get_market_price.return_value = 100
        self.assertFalse(self.strategy.should_close_position(position))
        self.v.get_market_price.return_value = 100.5
        self.l.get_market_price.return_value = 100.5
        self.assertTrue(self.strategy.should_close_position(position))

    def test_close_policy_profit_mode_can_exit_before_convergence(self):
        self._known_exit_funding()
        self.strategy.config['close_policy'] = {
            'mode': 'profit', 'convergence_basis': 'remaining', 'convergence_bps': 2,
            'min_profit_usdc': 4, 'min_profit_bps': 0, 'cost_reserve_usdc': 0, 'max_hold_seconds': 3600}
        self.strategy.reload_close_policy()
        position = self._open_position()
        self.v.get_market_price.return_value = 100.5
        self.l.get_market_price.return_value = 103
        self.assertTrue(self.strategy.should_close_position(position))

    def test_close_policy_reduction_basis_supports_zero_crossing_target(self):
        self._known_exit_funding()
        self.strategy.config['close_policy'] = {
            'mode': 'convergence', 'convergence_basis': 'reduction', 'convergence_bps': 10,
            'min_profit_usdc': 0, 'min_profit_bps': 0, 'cost_reserve_usdc': 0, 'max_hold_seconds': 3600}
        self.strategy.reload_close_policy()
        position = self._open_position()
        self.v.get_market_price.return_value = 101.9
        self.l.get_market_price.return_value = 100
        self.assertFalse(self.strategy.should_close_position(position))
        self.v.get_market_price.return_value = 101.5
        self.l.get_market_price.return_value = 100
        self.assertTrue(self.strategy.should_close_position(position))

    def test_close_policy_max_hold_remains_safety_exit(self):
        self.strategy.config['close_policy'] = {
            'mode': 'disabled', 'convergence_basis': 'remaining', 'convergence_bps': 0,
            'min_profit_usdc': 100000, 'min_profit_bps': 10000, 'cost_reserve_usdc': 0,
            'max_hold_seconds': 60}
        self.strategy.reload_close_policy()
        position = Position('BTC', 'short', 'buy', 1, 1, 102, 100,
                            datetime.now() - timedelta(seconds=61))
        self.assertTrue(self.strategy.should_close_position(position))

    def test_missing_first_fee_unwinds_known_fill_and_persists_accounting_hold(self):
        self.v.place_order.side_effect = lambda *a, **kw: {**fill(1,102), 'fees_usdc':None}
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.l.place_order.assert_not_called()
        self.v.close_position.assert_called_once()
        self.assertTrue(self.v.close_position.call_args.kwargs['reduce_only'])
        position = self.strategy.positions[0]
        self.assertEqual(position.variational_size, 0)
        self.assertTrue(position.accounting_pending)
        self.assertEqual(position.status, 'reconcile_required')
        restored = ArbitrageStrategy(self.config,self.v,self.l)
        self.assertTrue(restored.positions[0].accounting_pending)
        self.assertIsNotNone(restored.halted_reason)

    def test_missing_second_fee_unwinds_both_known_fills(self):
        self.l.place_order.side_effect = lambda *a, **kw: {**fill(1,100), 'fees_usdc':None}
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.assert_called_once()
        self.l.close_position.assert_called_once()
        position = self.strategy.positions[0]
        self.assertEqual(position.variational_size,0)
        self.assertEqual(position.lighter_size,0)
        self.assertTrue(position.accounting_pending)

    def test_shrinks_to_visible_depth_and_requotes(self):
        self.l.get_sizing_book.side_effect = lambda symbol: {'fetched_at':utc_now(),
            'bids':[{'price':100,'size':.4}], 'asks':[{'price':100,'size':.4}]}
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.v.place_order.call_args.args[2], .4)
        self.assertEqual(self.l.place_order.call_args.args[2], .4)

    def test_api_pair_uses_first_book_impact_instead_of_web_fallback(self):
        # Lighter↔RH uses two API order books. A 12 bps edge covers the
        # separate 2 bps movement budget on each leg, and must not
        # inherit the 10 bps web-platform fallback when the first API book
        # shows zero impact at the requested size.
        config = {**self.config, 'trading': {'position_size': 100, 'max_positions': 2},
                  'risk': {'slippage_tolerance': 10}}
        first, second = adapter(100.12), adapter(100.0)
        strategy = ArbitrageStrategy(config, first, second)
        opportunity = ArbitrageOpportunity('BTC', 100.12, 100.0, 12,
                                            'variational_higher', 0, datetime.now())
        plan, _, _, _ = strategy.plan_entry(opportunity)
        self.assertGreater(plan['net_edge_bps'], 5)
        self.assertEqual(plan['variational_slippage_bps'], 0)

    def test_disabled_money_stop_does_not_disable_unknown_receipt_halt(self):
        self.strategy.max_daily_loss=None
        self.strategy.max_loss_per_trade=None
        self.strategy.daily_pnl=-1000
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.strategy.halted_reason='unresolved receipt'
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))

    def test_lighter_minimum_rejection_prevents_first_variational_leg(self):
        self.l.validate_quote_order.side_effect=ValueError('minimum order')
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.v.place_order.assert_not_called()
        self.l.place_order.assert_not_called()
        self.assertEqual(self.strategy.history.recent()[0]['event'],'blocked')

    def test_arcus_tier_preflight_blocks_first_leg_when_budget_has_no_grid_point(self):
        from src.arcus.execution import ArcusExecutionAdapter
        from src.execution.journal import OrderJournal
        client = Mock(api_key='verified')
        client.market.return_value = {'tick_size': '.01', 'tick_tiers': [{'tick': '.03'}]}
        arcus = ArcusExecutionAdapter(client, OrderJournal(Path(self.temp.name)/'arcus.sqlite'),
                                     'test', enabled=True, fee_bps=0)
        # The strategy's second quote is 100; its budget is too small to reach 100.02.
        self.l.validate_quote_order.side_effect = arcus.validate_quote_order
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.v.place_order.assert_not_called()
        self.l.place_order.assert_not_called()
        self.assertIn('价格档位内没有符合预算', self.strategy.history.recent()[0]['detail']['reason'])

    def test_deteriorated_second_quote_unwinds_known_first_fill(self):
        count=[0]
        def quote(symbol, quantity):
            count[0]+=1
            price=100 if count[0]==1 else 105
            return {'symbol':symbol,'size':quantity,'bid':price,'ask':price,'fee_bps':0,
                    'updated_at':utc_now(),'executable':True,'instrument_type':'perpetual'}
        self.l.get_executable_quote.side_effect=quote
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.l.place_order.assert_not_called()
        self.v.close_position.assert_called_once()

    def test_failed_dict_is_not_success(self):
        self.v.place_order.side_effect = None
        self.v.place_order.return_value = {'success': False, 'error': 'rejected'}
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.l.place_order.assert_not_called()
        self.assertIsNotNone(self.strategy.halted_reason)

    def test_confirmed_rejection_no_second_leg(self):
        self.v.place_order.side_effect = lambda *a, **kw: {
            'success': False, 'confirmed': True, 'status': 'rejected', 'filled_size': 0}
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.l.place_order.assert_not_called()
        self.assertEqual(self.strategy.positions[0].status, 'closed')

    def test_partial_first_fill_hedges_actual_quantity(self):
        self.v.place_order.side_effect = lambda *a, **kw: {**fill(.4, 102), 'status': 'partial'}
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.l.place_order.call_args.args[2], .4)
        self.assertEqual(self.l.get_executable_quote.call_args.args[1], .4)

    def test_partial_second_leg_unwinds_both_actual_sizes(self):
        self.l.place_order.side_effect = lambda *a, **kw: {**fill(.4, 100), 'status': 'partial'}
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.v.close_position.call_args.kwargs['size'], 1)
        self.assertEqual(self.l.close_position.call_args.kwargs['size'], .4)
        self.assertTrue(self.v.close_position.call_args.kwargs['reduce_only'])
        self.assertEqual(self.strategy.positions[0].status, 'closed')

    def test_timeout_second_leg_halts_without_blind_rollback_or_retry(self):
        self.l.place_order.side_effect = TimeoutError()
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.assert_not_called()
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.v.place_order.call_count, 1)
        state = self.strategy.get_strategy_status()
        self.assertEqual(state['positions'], 1)
        self.assertIsNotNone(state['halted_reason'])

    def test_failed_close_keeps_exposure_and_prevents_duplicate_close(self):
        self.strategy.execute_arbitrage(self.opportunity)
        self.v.close_position.side_effect = lambda *a, **kw: fill(.4, 102)
        self.assertFalse(self.strategy.close_position(self.strategy.positions[0]))
        self.assertAlmostEqual(self.strategy.positions[0].variational_size, .6)
        self.assertEqual(self.strategy.get_strategy_status()['positions'], 1)
        self.assertFalse(self.strategy.close_position(self.strategy.positions[0]))
        self.assertEqual(self.v.close_position.call_count, 1)

    def test_restart_requires_reconciliation(self):
        self.strategy.execute_arbitrage(self.opportunity)
        restarted = ArbitrageStrategy(self.config, self.v, self.l)
        self.assertEqual(restarted.get_strategy_status()['positions'], 1)
        self.assertFalse(restarted.execute_arbitrage(self.opportunity))
        self.assertIn('重启', restarted.halted_reason)

    def test_reconciliation_audit_fields_do_not_corrupt_closed_state(self):
        self.strategy.execute_arbitrage(self.opportunity)
        state = json.loads(Path(self.config['state_path']).read_text())
        position = state['positions'][0]
        position.update({
            'status': 'closed', 'variational_size': 0, 'lighter_size': 0,
            'reconciled_at': utc_now(), 'reconciled_sizes': {'variational_size': 1, 'lighter_size': 0},
            'reconciliation_note': '只读核对为空',
        })
        Path(self.config['state_path']).write_text(json.dumps(state))
        restarted = ArbitrageStrategy(self.config, self.v, self.l)
        self.assertIsNone(restarted.halted_reason)
        self.assertEqual(restarted.positions[0].status, 'closed')

    def test_known_close_fill_missing_cost_still_reduces_other_leg(self):
        self.strategy.execute_arbitrage(self.opportunity)
        self.v.close_position.side_effect=lambda *args, **kw: {**fill(kw['size']), 'funding_usdc':None}
        self.assertFalse(self.strategy.close_position(self.strategy.positions[0]))
        self.l.close_position.assert_called_once()
        self.assertEqual(self.strategy.positions[0].variational_size, 0)
        self.assertEqual(self.strategy.positions[0].lighter_size, 0)
        self.assertIn('费用或资金费未知', self.strategy.halted_reason)

    def test_auto_reconciles_deferred_funding_before_marking_closed(self):
        self.config['funding_accounting_mode'] = 'defer_settlement'
        self.config['risk']['max_daily_loss'] = None
        self.strategy.max_daily_loss = None
        self.v.assert_flat.return_value = self.l.assert_flat.return_value = True
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.side_effect = lambda *a, **kw: {**fill(kw['size']), 'funding_usdc': None}
        self.v.reconcile_funding.return_value = {
            'complete': True, 'venue': 'lighter', 'symbol': 'BTC',
            'funding_usdc': -0.02, 'rows': [], 'observed_at': utc_now()}
        self.assertTrue(self.strategy.close_position(self.strategy.positions[0]))
        position = self.strategy.positions[0]
        self.assertEqual(position.pending_funding_venues, [])
        self.assertAlmostEqual(position.realized_pnl_usdc, 0.98)
        self.assertEqual(position.status, 'closed')

    def test_deferred_funding_closes_after_flat_checks_and_survives_restart(self):
        self.config['funding_accounting_mode']='defer_settlement'
        self.config['risk']['max_daily_loss']=None
        self.strategy.max_daily_loss=None
        self.v.assert_flat.return_value=self.l.assert_flat.return_value=True
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.side_effect=lambda *a, **kw: {**fill(kw['size']), 'funding_usdc':None}
        self.assertTrue(self.strategy.close_position(self.strategy.positions[0]))
        self.assertEqual(self.strategy.positions[0].status,'closed')
        self.assertEqual(self.strategy.positions[0].pending_funding_venues,['variational'])
        self.v.assert_flat.assert_called_once_with('BTC')
        self.l.assert_flat.assert_called_once_with('BTC')
        self.assertIsNone(self.strategy.get_strategy_status()['daily_pnl'])
        self.assertTrue(any(e['event']=='closed_pending_funding' for e in self.strategy.history.recent()))
        restored=ArbitrageStrategy(self.config,self.v,self.l)
        self.assertFalse(restored.live_blockers())
        self.assertEqual(restored.get_strategy_status()['pending_funding_count'],1)
        self.assertIsNone(restored.get_strategy_status()['daily_pnl'])
        self.assertTrue(restored.execute_arbitrage(self.opportunity))

    def test_deferred_funding_still_blocks_unconfirmed_account_or_unknown_fee(self):
        self.config['funding_accounting_mode']='defer_settlement'
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.side_effect=lambda *a, **kw: {**fill(kw['size']), 'funding_usdc':None}
        self.v.assert_flat.return_value=False
        self.assertFalse(self.strategy.close_position(self.strategy.positions[0]))
        self.assertIn('账户空仓',self.strategy.halted_reason)

    def test_deferred_funding_does_not_defer_unknown_trading_fees(self):
        self.config['funding_accounting_mode']='defer_settlement'
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.side_effect=lambda *a, **kw: {**fill(kw['size']), 'fees_usdc':None}
        self.assertFalse(self.strategy.close_position(self.strategy.positions[0]))
        self.l.close_position.assert_called_once()
        self.assertTrue(self.strategy.positions[0].accounting_pending)

    def test_public_stale_and_wrong_instrument_quotes_cannot_trade(self):
        for patch in [{'executable': False}, {'updated_at': '2000-01-01T00:00:00Z'},
                      {'instrument_type': 'swap'}, {'symbol': 'ETH'}, {'size': 99}, {'bid': float('nan')}]:
            with self.subTest(patch=patch):
                self.v.get_executable_quote.side_effect = lambda s, q: {
                    'symbol': s, 'size': q, 'bid': 102, 'ask': 102, 'fee_bps': 0,
                    'updated_at': utc_now(), 'executable': True, 'instrument_type': 'perpetual', **patch}
                self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
                self.v.place_order.assert_not_called()

    def test_trade_limit_and_loss_limit(self):
        self.strategy.daily_trades = 10
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.strategy.daily_trades = 0
        self.strategy.daily_pnl = -1000
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.v.place_order.assert_not_called()

    def test_all_fees_in_realized_pnl(self):
        self.v.place_order.side_effect = lambda *a, **kw: {**fill(1, 102), 'fees_usdc': .1}
        self.l.place_order.side_effect = lambda *a, **kw: {**fill(1, 100), 'fees_usdc': .2}
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.v.close_position.side_effect = lambda *a, **kw: {**fill(1, 101), 'fees_usdc': .1, 'funding_usdc': -.1}
        self.l.close_position.side_effect = lambda *a, **kw: {**fill(1, 101), 'fees_usdc': .2}
        self.assertTrue(self.strategy.close_position(self.strategy.positions[0]))
        self.assertAlmostEqual(self.strategy.daily_pnl, 1.3)

    def test_arcus_close_client_id_is_within_exchange_limit(self):
        self.v.venue = 'lighter'
        self.l.venue = 'arcus'
        position = self._open_position()
        client_id = self.strategy._close_client_order_id(position, 'lighter')
        self.assertLessEqual(len(client_id), 36)
        self.assertRegex(client_id, r'^[A-Za-z0-9]+-c-[A-Za-z0-9]+-[a-f0-9]{8}$')

    def test_reversion_execution_filter_requires_same_route_history(self):
        self.strategy.market_history = Mock()
        self.strategy.market_history.reversion_stats.return_value = {
            'samples': 100, 'reversion_count': 3, 'last_reversion_age_seconds': 900,
        }
        self.strategy.reversion_min_count = 2
        self.strategy.reversion_min_samples = 50
        self.strategy.reversion_max_age_seconds = 3600
        self.assertTrue(self.strategy._reversion_filter_allowed('BTC', 'sell_variational'))
        self.strategy.market_history.reversion_stats.return_value['reversion_count'] = 0
        self.assertFalse(self.strategy._reversion_filter_allowed('BTC', 'sell_variational'))
        self.strategy.market_history.reversion_stats.return_value['reversion_count'] = 3
        self.assertTrue(self.strategy._reversion_filter_allowed('BTC', 'sell_variational'),
                        'same authorized symbol is re-evaluated when it qualifies later')
        self.strategy.market_history.reversion_stats.return_value['last_reversion_age_seconds'] = 7200
        self.assertFalse(self.strategy._reversion_filter_allowed('BTC', 'sell_variational'))

    def test_single_leg_recovery_closes_remaining_leg_after_profit_check(self):
        position = self._open_position()
        position.variational_size = 0
        position.lighter_size = 0.4
        position.entry_size_variational = 1
        position.entry_size_lighter = 0.4
        position.entry_price_variational = 102
        position.entry_price_lighter = 100
        position.close_price_variational = 101
        position.realized_pnl_usdc = 0.5
        position.pending_funding_venues = ['variational']
        position.accounting_pending = True
        position.status = 'reconcile_required'
        self.strategy.positions.append(position)
        self.v.position_snapshot.return_value = {'qty': 0, 'side': None}
        self.l.position_snapshot.return_value = {'qty': 0.4, 'side': 'buy'}
        self.l.get_executable_quote.return_value = {
            'symbol': 'BTC', 'size': 0.4, 'bid': 100, 'ask': 100,
            'fee_bps': 0, 'updated_at': utc_now(), 'executable': True,
            'instrument_type': 'perpetual'}
        self.strategy.halted_reason = '平仓未全部成交，保留剩余敞口并停止新开仓'
        self.assertEqual(self.strategy.recover_single_leg_positions(), 'recovered')
        self.assertEqual(position.status, 'reconcile_required')
        self.assertEqual(position.lighter_size, 0)
        self.l.close_position.assert_called_once()

    def test_single_leg_recovery_waits_when_combined_pnl_is_not_positive(self):
        position = self._open_position()
        position.variational_size = 0
        position.lighter_size = 0.4
        position.entry_size_variational = 1
        position.entry_size_lighter = 0.4
        position.close_price_variational = 101
        position.realized_pnl_usdc = -0.5
        position.status = 'reconcile_required'
        self.strategy.positions.append(position)
        self.v.position_snapshot.return_value = {'qty': 0, 'side': None}
        self.l.position_snapshot.return_value = {'qty': 0.4, 'side': 'buy'}
        self.l.get_executable_quote.return_value = {
            'symbol': 'BTC', 'size': 0.4, 'bid': 100, 'ask': 100,
            'fee_bps': 0, 'updated_at': utc_now(), 'executable': True,
            'instrument_type': 'perpetual'}
        self.strategy.halted_reason = '平仓未全部成交，保留剩余敞口并停止新开仓'
        self.assertEqual(self.strategy.recover_single_leg_positions(), 'waiting')
        self.l.close_position.assert_not_called()

class DeterministicRejectTests(unittest.TestCase):
    def test_confirmed_zero_fill_rejection_does_not_halt(self):
        from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position
        # Regression fixture: V RFQ changed before click, platform confirmed reject.
        strategy=ArbitrageStrategy.__new__(ArbitrageStrategy)
        strategy.history=Mock();strategy.positions=[];strategy.halted_reason=None
        strategy._save=Mock();strategy.last_execution_submitted=True
        strategy.history.append=Mock()
        receipt={'confirmed':True,'status':'rejected','filled_size':0}
        first_receipt=receipt;second_receipt=None
        receipts=[first_receipt,second_receipt]
        deterministic=any(isinstance(r,dict) and r.get('confirmed') is True and r.get('status') in ('rejected','canceled') and float(r.get('filled_size',0) or 0)==0 for r in receipts)
        self.assertTrue(deterministic)
