import copy
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.execution.hedge_policy import HedgePolicy, completion_limit, completion_edge, unwind_estimate
from src.execution.orders import fresh_quote
from src.common.market_data import utc_now
from src.execution.journal import OrderJournal
from src.strategy.arbitrage_strategy import ArbitrageStrategy, ArbitrageOpportunity
from test_execution_latency import NativeTrader


class Trader(NativeTrader):
    fee_bps = 0

    def get_executable_quote(self, symbol, quantity):
        return dict(super().get_executable_quote(symbol, quantity), fee_bps=self.fee_bps)

    def close_position(self, symbol, side, size, **kwargs):
        return {'success': True, 'confirmed': True, 'status': 'filled', 'filled_size': size,
                'average_price': self.price, 'order_id': 'closed', 'fees_usdc': 0, 'funding_usdc': 0}


class BudgetMathTests(unittest.TestCase):
    def test_arcus_stream_connection_and_signing_delay_cannot_send_after_deadline(self):
        from src.arcus.execution import _ArcusWebSocket
        for delayed_at in ('connection', 'signing'):
            with self.subTest(delayed_at=delayed_at):
                ws = _ArcusWebSocket('wss://test', None, 'test', 1)
                ws._ws = Mock()
                clock = [100]
                def start():
                    if delayed_at == 'connection': clock[0] = 106
                def sign(*args):
                    if delayed_at == 'signing': clock[0] = 106
                    return b'test'
                key = Mock()
                key.sign.side_effect = sign
                with patch.object(ws, 'start', side_effect=start), \
                        patch('src.execution.orders.time.monotonic', side_effect=lambda: clock[0]), \
                        self.assertRaisesRegex(ValueError, '窗口超时'):
                    ws.rpc('placeOrder', {}, 'test', key, 'test', 1, submit_deadline_monotonic=105)
                ws._ws.send.assert_not_called()
                self.assertEqual(ws._pending, {})

    def test_lighter_sdk_does_not_submit_an_expired_queued_plan(self):
        import asyncio
        from unittest.mock import AsyncMock
        from src.lighter.execution import OfficialSDKTransport
        sdk = Mock(create_order=AsyncMock())
        transport = OfficialSDKTransport(sdk, 1, 1)
        with patch('src.execution.orders.time.monotonic', return_value=106), \
                self.assertRaisesRegex(ValueError, '窗口超时'):
            asyncio.run(transport.submit({'submit_deadline_monotonic': 105}, 1))
        sdk.create_order.assert_not_called()

    def test_buy_completion_limit_intersects_movement_profit_floor_and_amount(self):
        limit, budget = completion_limit(100.2, 0, 1, 100, 4.5, 2, 5, 101, True)
        self.assertAlmostEqual(budget, 100.02)
        self.assertLessEqual(limit, budget)
        self.assertGreaterEqual(completion_edge(100.2, 0, 1, limit, 4.5, True)[1], 5 - 1e-7)
        capped, _ = completion_limit(100, 0, 1, 100.1, 0, 2, 0, 100, True)
        self.assertLessEqual(capped, 100)

    def test_sell_completion_limit_covers_real_first_fee_and_second_fee(self):
        limit, budget = completion_limit(100, .01, .5, 100.2, 4.5, 2, 5, 101, False)
        self.assertGreaterEqual(limit, budget)
        self.assertGreaterEqual(completion_edge(100, .01, .5, limit, 4.5, False)[1], 5 - 1e-7)

    def test_invalid_fee_amount_and_policy_values_fail_closed(self):
        for risk in ({'execution_price_movement_bps': float('nan')},
                     {'hedge_max_delay_seconds': 0}, {'route_rollback_limit': 1.5},
                     {'route_rollback_limit': True}, {'route_rollback_cooldown_seconds': 0}):
            with self.subTest(risk=risk), self.assertRaises((ValueError, ArithmeticError)):
                HedgePolicy.from_config(risk)
        with self.assertRaises(ValueError):
            completion_limit(102, 0, 1, 100, 0, 2, 5, 101, True)

    def test_unwind_pnl_includes_spread_movement_and_both_known_fees(self):
        quote = {'bid': 99, 'ask': 101, 'fee_bps': 4.5}
        self.assertLess(unwind_estimate(100, .01, 1, quote, True, 2), -1.01)
        self.assertLess(unwind_estimate(100, .01, 1, quote, False, 2), -1.01)

    def test_fresh_quote_also_checks_the_submission_deadline(self):
        quote = Trader(100, 'lighter').get_executable_quote('UNI', 1)
        quote['submit_deadline_monotonic'] = time.monotonic() + 5
        fresh_quote(quote, 'UNI', 1)
        quote['submit_deadline_monotonic'] = .01
        with patch('src.execution.orders.time.monotonic', return_value=.02), \
                self.assertRaisesRegex(ValueError, '窗口超时'):
            fresh_quote(quote, 'UNI', 1)


class HedgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first, self.second = Trader(100.2, 'lighter'), Trader(100, 'arcus')
        self.config = {'mode': 'live', 'state_path': str(Path(self.tmp.name) / 'state.json'),
                       'trading': {'position_size': 100.25, 'max_positions': 1},
                       'spread_threshold': {'min': 5, 'max': 500},
                       'risk': {'slippage_tolerance': 10},
                       'wash_trading': {'trade_interval_min': 0, 'max_trades_per_day': 100}}
        self.strategy = ArbitrageStrategy(self.config, self.first, self.second)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.strategy.market_history)
        self.opportunity = ArbitrageOpportunity('UNI', 100.2, 100, 20, 'variational_higher', 0, datetime.now())
        self.send_first = self.first.place_order

    def move_after_first(self, price):
        def send(*args, **kwargs):
            result = self.send_first(*args, **kwargs)
            self.second.price = price
            return result
        return patch.object(self.first, 'place_order', side_effect=send)

    def fail_one(self):
        self.second.price = 100
        # Actual price movement must still respect the configured boundary.
        budget = self.strategy.execution_entry_movement_bps('UNI', 'sell_variational')
        with self.move_after_first(100 * (1 + (budget + 1) / 10000)):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))

    def test_zero_book_impact_still_reserves_price_movement_on_both_legs(self):
        plan, _, _, _ = self.strategy.plan_entry(self.opportunity)
        self.assertEqual(plan['variational_slippage_bps'], 0)
        self.assertEqual(plan['lighter_slippage_bps'], 0)
        self.assertEqual(plan['buy_slippage_bps'], 2)
        self.assertEqual(plan['sell_slippage_bps'], 2)
        self.assertLess(plan['net_edge_bps'], 16.1)

    def test_thin_edge_no_longer_opens_first_leg_before_spending_movement_budget(self):
        self.first.price = 100.08
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.first.sends + self.second.sends, [])

    def test_one_basis_point_move_completes_instead_of_zero_budget_rollback(self):
        with self.move_after_first(100.01):
            self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(len(self.second.sends), 1)
        event = next(e for e in self.strategy.history.recent() if e['event'] == 'hedge_decision')
        self.assertEqual(event['detail']['decision'], 'hedge')
        self.assertIn('unwind_pnl_estimate_usdc', event['detail'])
        self.assertGreater(event['detail']['protected_bound'], event['detail']['observed_price'])

    def test_both_trade_directions_use_a_finite_original_price_boundary(self):
        self.first.price, self.second.price = 100, 100.2
        self.opportunity.variational_price, self.opportunity.lighter_price = 100, 100.2
        with self.move_after_first(100.19):
            self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.second.sends[0][1], 'sell')

    def test_already_filled_leg_uses_base_floor_without_reapplying_entry_cushions(self):
        self.strategy.execution_safety_margin_bps = 3  # An explicit extra profit requirement.
        self.first.price = 100.14
        def send(*args, **kwargs):
            result = self.send_first(*args, **kwargs)
            result['average_price'] = 100.12  # Within the first leg's reserved 2 bps.
            self.second.price, self.second.fee_bps = 100.01, 3
            return result
        with patch.object(self.first, 'place_order', side_effect=send):
            self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        decision = next(e['detail'] for e in self.strategy.history.recent() if e['event'] == 'hedge_decision')
        self.assertGreaterEqual(decision['net_edge_bps'], 5 - 1e-7)
        self.assertLess(decision['net_edge_bps'], self.strategy.execution_min_bps('UNI'))

    def test_move_outside_reserved_budget_does_not_chase_price(self):
        self.fail_one()
        self.assertEqual(self.second.sends, [])
        self.assertEqual(self.strategy.positions[-1].status, 'closed')
        self.assertEqual(next(iter(self.strategy.route_rollback_guards.values()))['rollback_count'], 1)

    def test_no_extra_window_simulation_veto_after_configured_plan_passes(self):
        # The sized plan passes the configured floor. A hypothetical extra
        # two-times movement must not add another independent veto.
        self.first.price = 100.10
        before_reads = (self.first.quote_reads, self.second.quote_reads)
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual((len(self.first.sends), len(self.second.sends)), (1, 1))
        self.assertEqual((self.first.quote_reads-before_reads[0],
                          self.second.quote_reads-before_reads[1]), (3, 3))
        self.assertFalse(any(e['event']=='entry_hedge_precheck' for e in self.strategy.history.recent(200)))

    def test_adverse_second_quote_during_precheck_blocks_both_directions_before_first(self):
        for first, second, moved in ((100.2, 100, 100.03), (100, 100.2, 100.17)):
            with self.subTest(first=first):
                self.first.price, self.second.price = first, second
                check = self.first.precheck_order
                def changed(*args, **kwargs):
                    result = check(*args, **kwargs)
                    self.second.price = moved
                    return result
                with patch.object(self.first, 'precheck_order', side_effect=changed):
                    self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
                self.assertEqual(self.first.sends + self.second.sends, [])
                self.assertEqual(self.strategy.positions, [])
                rejection = next(e for e in self.strategy.history.recent() if e['event'] == 'blocked')
                self.assertIn('报价恶化超过价格变动预算', rejection['detail']['reason'])

    def test_favorable_second_quote_during_precheck_still_trades_without_extra_quote_reads(self):
        check = self.first.precheck_order
        def changed(*args, **kwargs):
            result = check(*args, **kwargs)
            self.second.price = 99.99
            return result
        with patch.object(self.first, 'precheck_order', side_effect=changed):
            self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual((self.first.quote_reads, self.second.quote_reads), (3, 3))

    def test_first_price_grid_rejection_is_checked_before_either_order(self):
        with patch.object(self.first, 'validate_quote_order',
                          side_effect=ValueError('Arcus 价格档位内没有符合预算的可成交限价')):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.first.sends + self.second.sends, [])
        self.assertEqual(self.strategy.positions, [])

    def test_one_rollback_policy_pauses_route_and_survives_reload_without_blocking_reverse(self):
        self.strategy.hedge_policy = HedgePolicy.from_config({'route_rollback_limit': 1})
        self.fail_one()
        before = len(self.first.sends)
        self.second.price = 100
        self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(len(self.first.sends), before)
        guard = next(iter(self.strategy.route_rollback_guards.values()))
        self.assertGreater(guard['paused_until'], time.time() + 890)
        self.assertIsNone(self.strategy._route_pause_reason('UNI', 'buy_variational'))
        config = copy.deepcopy(self.config)
        config['risk']['route_rollback_limit'] = 1
        restored = ArbitrageStrategy(config, self.first, self.second)
        self.assertIn('暂停', restored._route_pause_reason('UNI', 'sell_variational'))

    def test_hedge_diagnostic_separates_actual_quote_loss_from_positive_bound_edge(self):
        with self.move_after_first(100.3):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        decision = next(e['detail'] for e in self.strategy.history.recent() if e['event'] == 'hedge_decision')
        self.assertGreater(decision['net_edge_bps'], 0)
        self.assertLess(decision['observed_net_edge_bps'], 0)
        self.assertEqual(decision['decision'], 'unwind')

    def test_route_pause_does_not_prevent_closing_an_existing_position(self):
        from src.strategy.arbitrage_strategy import Position
        self.fail_one()
        self.fail_one()
        position = Position('UNI', 'short', 'buy', 1, 1, 100.2, 100, datetime.now())
        self.strategy.positions.append(position)
        self.assertTrue(self.strategy.close_position(position, reason='test close'))
        self.assertEqual(position.status, 'closed')
        self.assertIn('暂停', self.strategy._route_pause_reason('UNI', 'sell_variational'))

    def test_slow_first_receipt_exhausts_window_without_sending_second_order(self):
        clock = [100.0]
        def send(*args, **kwargs):
            result = self.send_first(*args, **kwargs)
            clock[0] += 6
            return result
        with patch('src.strategy.arbitrage_strategy.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.first, 'place_order', side_effect=send):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.second.sends, [])
        self.assertEqual(self.strategy.positions[-1].status, 'closed')

    def test_two_rollbacks_pause_only_that_route_and_survive_restart(self):
        self.fail_one()
        self.fail_one()
        reason = self.strategy._route_pause_reason('UNI', 'sell_variational')
        self.assertIn('暂停', reason)
        self.assertIsNone(self.strategy._route_pause_reason('UNI', 'buy_variational'))
        self.assertIsNone(self.strategy._route_pause_reason('ETH', 'sell_variational'))
        other = ArbitrageStrategy(dict(self.config, state_path=str(Path(self.tmp.name) / 'other.json')),
                                  Trader(100.2, 'lighter_rh'), self.second)
        other.route_rollback_guards = copy.deepcopy(self.strategy.route_rollback_guards)
        self.assertIsNone(other._route_pause_reason('UNI', 'sell_variational'))
        restored = ArbitrageStrategy(self.config, self.first, self.second)
        self.assertIsNone(restored.halted_reason)
        self.assertIn('暂停', restored._route_pause_reason('UNI', 'sell_variational'))
        self.second.price = 100
        before = len(self.first.sends)
        self.assertFalse(restored.execute_arbitrage(self.opportunity))
        self.assertEqual(len(self.first.sends), before)

    def test_expired_pause_allows_one_fresh_failure_and_starts_a_new_streak(self):
        self.fail_one()
        self.fail_one()
        until = next(iter(self.strategy.route_rollback_guards.values()))['paused_until']
        with patch('src.strategy.arbitrage_strategy.time.time', return_value=until + 1):
            self.assertIsNone(self.strategy._route_pause_reason('UNI', 'sell_variational'))
            self.fail_one()
        guard = next(iter(self.strategy.route_rollback_guards.values()))
        self.assertEqual(guard['rollback_count'], 1)
        self.assertEqual(guard['paused_until'], 0)

    def test_zero_fill_does_not_reset_streak_and_full_pair_does(self):
        self.fail_one()
        self.second.price = 100
        zero = {'confirmed': True, 'status': 'canceled', 'filled_size': 0}
        with patch.object(self.first, 'place_order', return_value=zero):
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(next(iter(self.strategy.route_rollback_guards.values()))['rollback_count'], 1)
        self.assertTrue(self.strategy.execute_arbitrage(self.opportunity))
        self.assertEqual(self.strategy.route_rollback_guards, {})

    def test_legacy_closed_positions_restore_streak_without_writing_file(self):
        self.fail_one()
        self.fail_one()
        path = Path(self.config['state_path'])
        payload = json.loads(path.read_text())
        payload.pop('route_rollback_guards')
        for row in payload['positions']:
            row.pop('entry_rollback_recorded')
        text = json.dumps(payload)
        path.write_text(text)
        restored = ArbitrageStrategy(self.config, self.first, self.second)
        self.assertIn('暂停', restored._route_pause_reason('UNI', 'sell_variational'))
        self.assertEqual(path.read_text(), text)
        restored._record_route_rollback(restored.positions[-1])
        self.assertEqual(next(iter(restored.route_rollback_guards.values()))['rollback_count'], 2)

    def test_unknown_second_receipt_halts_without_blind_rollback_or_retry(self):
        with patch.object(self.second, 'place_order', return_value={'confirmed': False, 'status': 'unknown'}), \
                patch.object(self.first, 'close_position') as close:
            self.assertFalse(self.strategy.execute_arbitrage(self.opportunity))
        close.assert_not_called()
        self.assertIsNotNone(self.strategy.halted_reason)
        self.assertEqual(self.strategy.route_rollback_guards, {})


class FinalAdapterDeadlineTests(unittest.TestCase):
    def test_slow_final_account_check_prevents_actual_submit_on_both_adapters(self):
        from src.lighter.execution import LighterExecutionAdapter
        from src.arcus.execution import ArcusExecutionAdapter
        for venue in ('lighter', 'arcus'):
            with self.subTest(venue=venue), tempfile.TemporaryDirectory() as tmp:
                client, bridge = Mock(api_key='test'), Mock()
                client.market.return_value = {'tick_size': '0.01'}
                client.get_market.return_value = (dict(symbol='UNI', market_type='perp', status='active',
                    market_id=1, supported_size_decimals=3, supported_price_decimals=2,
                    min_base_amount='.001', min_quote_amount='1'), utc_now())
                journal = OrderJournal(Path(tmp) / 'orders.sqlite')
                adapter = (LighterExecutionAdapter(client, bridge, journal, 1, enabled=True)
                           if venue == 'lighter' else ArcusExecutionAdapter(client, journal, 'test',
                                                                          enabled=True, fee_bps=0))
                adapter.verified_fee_bps = 0
                quote = Trader(100, venue).get_executable_quote('UNI', 1)
                quote['submit_deadline_monotonic'] = 105
                clock = [100]
                def validate(*args, **kwargs):
                    clock[0] = 106
                with patch('src.execution.orders.time.monotonic', side_effect=lambda: clock[0]), \
                        patch.object(adapter, 'validate_order', side_effect=validate) as check:
                    result = adapter.place_order('UNI', 'buy', 1, 100, 0, 'test', quote)
                self.assertEqual(result['status'], 'rejected')
                check.assert_called_once()
                bridge.call.assert_not_called()
                client.submit.assert_not_called()

