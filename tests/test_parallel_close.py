import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.common.market_data import utc_now
from src.strategy.arbitrage_strategy import ArbitrageStrategy, Position
from src.strategy.close_execution import CloseRetryReads


def fill(size, price=100, order_id='close'):
    return dict(confirmed=True, status='filled', filled_size=size, average_price=price,
                order_id=order_id, fees_usdc=0, funding_usdc=0)


class Trader:
    live_ready = True
    supports_close_quote_reuse = True

    def __init__(self, venue, side, size=2):
        self.venue, self.side, self.qty = venue, side, size
        self.calls, self.responses = [], []
        self.on_close = None
        self.snapshot_reads = 0

    def close_position(self, symbol, side, size, **kwargs):
        self.calls.append(dict(size=size, side=side, **kwargs))
        if self.on_close:
            self.on_close()
        result = self.responses.pop(0) if self.responses else fill(size, order_id=self.venue+str(len(self.calls)))
        if isinstance(result, Exception):
            raise result
        if result.get('confirmed'):
            self.qty -= result.get('filled_size', 0)
        return result

    def position_snapshot(self, symbol):
        self.snapshot_reads += 1
        return dict(qty=self.qty, side=self.side, fetched_at=utc_now())

    def get_executable_quote(self, symbol, quantity):
        return dict(symbol=symbol, size=quantity, bid=100, ask=100, bid_vwap=100, ask_vwap=100,
                    updated_at=utc_now(), executable=True, instrument_type='perpetual', fee_bps=0)

    def reconcile_funding(self, *args):
        return {'complete': True, 'funding_usdc': 0}

    def place_order(self, symbol, side, size, **kwargs):
        return self.close_position(symbol, 'buy' if side == 'sell' else 'short', size, **kwargs)


class ParallelCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = Trader('lighter', 'short'), Trader('arcus', 'long')
        self.config = {'mode': 'live', 'state_path': str(Path(self.tmp.name)/'state.json'),
                       'risk': {'slippage_tolerance': 7, 'single_leg_recovery_max_attempts': 2}}
        self.s = ArbitrageStrategy(self.config, self.a, self.b)
        from src.api_v2.center_statistics import close_service
        self.addCleanup(close_service, self.s.market_history)
        self.p = Position('PUMP', 'short', 'buy', 2, 2, 102, 100, datetime.now(), status='open',
                          entry_size_variational=2, entry_size_lighter=2,
                          variational_venue='lighter', lighter_venue='arcus')
        self.s.positions.append(self.p)
        self.addCleanup(self.s.close_diagnostics.writer.flush, 1)

    def test_both_intents_are_durable_and_transports_start_before_either_returns(self):
        barrier = threading.Barrier(2)
        def sent():
            saved = json.loads(Path(self.config['state_path']).read_text())['positions'][0]
            self.assertEqual(saved['status'], 'closing')
            self.assertEqual(set(saved['close_order_results']), {'variational', 'lighter'})
            self.assertTrue(all(not r['confirmed'] for r in saved['close_order_results'].values()))
            barrier.wait(timeout=1)
        self.a.on_close = self.b.on_close = sent
        self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        for trader in (self.a, self.b):
            self.assertEqual(len(trader.calls), 1)
            self.assertTrue(trader.calls[0]['reduce_only'])
            self.assertEqual(trader.calls[0]['slippage_bps'], 2)

    def test_exception_in_one_leg_does_not_lose_other_confirmed_close(self):
        self.a.responses = [TimeoutError('unknown submission')]
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual((self.p.variational_size, self.p.lighter_size), (2, 0))
        self.assertEqual(len(self.b.calls), 1)
        self.assertEqual(self.p.close_order_results['variational']['status'], 'unknown')
        self.assertEqual(self.p.close_order_results['lighter']['status'], 'filled')
        self.assertEqual(self.s.recover_single_leg_positions(), 'blocked')
        self.assertEqual(len(self.a.calls), 1, 'unknown submission is never automatically repeated')

    def test_canceled_zero_fill_retries_verified_remainder_without_profit_gate(self):
        self.b.responses = [dict(confirmed=True, status='canceled', filled_size=0, order_id='canceled'),
                            fill(2, 98, 'retry-filled')]
        self.p.realized_pnl_usdc = -20
        self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(len(self.a.calls), 1)
        self.assertEqual([c['size'] for c in self.b.calls], [2, 2])
        self.assertEqual(self.b.snapshot_reads, 1)
        self.assertLess(self.p.realized_pnl_usdc, 0, 'risk-reducing exit does not wait for a profit')
        self.assertNotEqual(self.b.calls[0]['client_order_id'], self.b.calls[1]['client_order_id'])

    def test_retry_account_read_overlaps_peer_receipt_but_does_not_send_early(self):
        account_read = threading.Event()
        release_peer = threading.Event()
        original = self.b.position_snapshot
        def snapshot(symbol):
            result = original(symbol)
            account_read.set()
            return result
        self.b.position_snapshot = snapshot
        self.a.on_close = lambda: release_peer.wait(timeout=2)
        self.b.responses = [dict(confirmed=True,status='canceled',filled_size=0,order_id='zero'),
                            fill(2,98,'retry')]
        result=[]
        worker=threading.Thread(target=lambda:result.append(self.s.close_position(self.p,'价差收敛')))
        worker.start()
        try:
            self.assertTrue(account_read.wait(timeout=1),'read must start while peer receipt is pending')
            self.assertEqual(len(self.b.calls),1,'account prefetch is not permission to resend')
        finally:
            release_peer.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result,[True])
        self.assertEqual(self.b.snapshot_reads,1,'fresh speculative account evidence is reused once')
        self.assertEqual([c['size'] for c in self.b.calls],[2,2])

    def test_two_remaining_account_checks_start_in_parallel(self):
        barrier=threading.Barrier(2)
        for name,trader in (('variational',self.a),('lighter',self.b)):
            self.p.close_attempts[name]=1
            self.p.close_order_results[name]=dict(confirmed=True,status='canceled',filled_size=0)
            original=trader.position_snapshot
            def snapshot(symbol,original=original):
                barrier.wait(timeout=1)
                return original(symbol)
            trader.position_snapshot=snapshot
        self.s._verify_close_retry(self.p,self.s._close_legs(self.p))
        self.assertEqual((self.a.snapshot_reads,self.b.snapshot_reads),(1,1))
        self.assertEqual(self.a.calls+self.b.calls,[])

    def test_residual_quote_and_account_read_overlap_and_quote_is_reused_once(self):
        barrier = threading.Barrier(2)
        original_snapshot = self.b.position_snapshot
        original_quote = self.b.get_executable_quote
        quote_reads = []
        def snapshot(symbol):
            barrier.wait(timeout=1)
            return original_snapshot(symbol)
        def quote(symbol, size):
            quote_reads.append(size)
            if self.b.calls:
                barrier.wait(timeout=1)
            return original_quote(symbol, size)
        self.b.position_snapshot = snapshot
        self.b.get_executable_quote = quote
        # End the peer first so the speculative account-only read cannot
        # satisfy this barrier before a residual has actually been selected.
        peer_finished = threading.Event()
        self.a.on_close = peer_finished.set
        self.b.on_close = lambda: peer_finished.wait(timeout=1)
        self.b.responses = [dict(confirmed=True, status='canceled', filled_size=0, order_id='zero'), fill(2, 98, 'retry')]
        self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(quote_reads, [2, 2], 'one initial estimate and one required retry quote')
        self.assertIn('quote', self.b.calls[1], 'reuse the completed size-bound quote in reduce-only submission')
        self.assertEqual(self.b.snapshot_reads, 1)
        self.assertTrue(self.b.calls[1]['reduce_only'])
        self.assertEqual(self.b.calls[1]['slippage_bps'], 7)

    def test_prefetched_residual_quote_is_size_and_attempt_scoped_and_stale_falls_back(self):
        leg = ('lighter', self.b, self.p.lighter_side, 2)
        self.p.close_attempts['lighter'] = 1
        self.p.close_order_results['lighter'] = dict(confirmed=True, status='canceled', client_order_id='old')
        with CloseRetryReads() as reads:
            reads.start(self.p, leg, quote=True)
            reads.quotes['lighter'][1].result(timeout=1)
            self.p.close_attempts['lighter'] = 2
            self.assertIsNone(reads.quote(self.p, leg), 'changed attempt cannot reuse a previous quote')
        self.b.get_executable_quote = lambda symbol, size: dict(symbol=symbol, size=size, bid=99, ask=100,
            updated_at='2000-01-01T00:00:00Z', executable=True, instrument_type='perpetual')
        with CloseRetryReads() as reads:
            reads.start(self.p, leg, quote=True)
            self.assertIsNone(reads.quote(self.p, leg))
        self.assertEqual(self.b.calls, [], 'a prefetched quote alone cannot send any order')

    def test_unknown_peer_does_not_request_or_submit_a_residual_quote(self):
        with patch.object(self.b, 'get_executable_quote', wraps=self.b.get_executable_quote) as quotes:
            self.a.responses = [TimeoutError('unknown')]
            self.b.responses = [dict(confirmed=True, status='canceled', filled_size=0, order_id='zero')]
            self.assertFalse(self.s.close_position(self.p, '价差收敛'))
            self.assertEqual(quotes.call_count, 1, 'only the initial profitable estimate may read a quote')
        self.assertEqual(len(self.b.calls), 1)

    def test_slow_residual_quote_does_not_hold_the_other_verified_transport(self):
        for name in ('variational', 'lighter'):
            self.p.close_attempts[name] = 1
            self.p.close_order_results[name] = dict(confirmed=True, status='canceled', client_order_id=name+'-old')
        self.p.close_started_time = datetime.now()
        sent = threading.Event()
        self.a.on_close = sent.set
        original_quote = self.b.get_executable_quote
        observed = []
        def slow_quote(symbol, size):
            observed.append(sent.wait(timeout=1))
            return original_quote(symbol, size)
        self.b.get_executable_quote = slow_quote
        self.assertTrue(self.s.close_position(self.p, '残余仓位处理'))
        self.assertEqual(observed, [True], 'peer sends while the slower quote is still being read')
        self.assertEqual((len(self.a.calls), len(self.b.calls)), (1, 1))

    def test_resumed_unfilled_profit_attempt_reuses_plan_without_extra_residual_quotes(self):
        for name in ('variational', 'lighter'):
            self.p.close_attempts[name] = 1
            self.p.close_order_results[name] = dict(confirmed=True, status='canceled', filled_size=0)
        self.p.close_started_time = datetime.now()
        with patch.object(self.a, 'get_executable_quote', wraps=self.a.get_executable_quote) as a_quotes, \
                patch.object(self.b, 'get_executable_quote', wraps=self.b.get_executable_quote) as b_quotes:
            self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual((a_quotes.call_count, b_quotes.call_count), (1, 1),
                         'a complete new profitable plan needs no extra residual quote request')

    def test_old_prefetched_account_is_refreshed_and_not_used_as_authorization(self):
        self.p.close_attempts['lighter']=1
        self.p.close_order_results['lighter']=dict(confirmed=True,status='canceled',client_order_id='old')
        leg=('lighter',self.b,self.p.lighter_side,2)
        with patch('src.strategy.close_execution.time.monotonic',return_value=100) as clock:
            with CloseRetryReads() as reads:
                reads.start(self.p,leg)
                reads.pending['lighter'][1].result(timeout=1)
                self.b.qty=1.5
                clock.return_value=103
                with self.assertRaisesRegex(ValueError,'需核对手动成交'):
                    self.s._verify_close_retry(self.p,[leg],reads)
        self.assertEqual(self.b.snapshot_reads,2)
        self.assertEqual(self.b.calls,[])

    def test_failed_speculative_account_read_cannot_allow_retry(self):
        self.p.close_attempts['lighter']=1
        self.p.close_order_results['lighter']=dict(confirmed=True,status='canceled',client_order_id='old')
        self.b.position_snapshot=lambda symbol: (_ for _ in ()).throw(TimeoutError('account unavailable'))
        with CloseRetryReads() as reads:
            leg=('lighter',self.b,self.p.lighter_side,2)
            reads.start(self.p,leg)
            with self.assertRaises(TimeoutError):
                self.s._verify_close_retry(self.p,[leg],reads)
        self.assertEqual(self.b.calls,[])

    def test_partial_fill_retries_only_residual_and_averages_all_closing_fills(self):
        self.b.responses = [fill(.5, 100, 'partial'), fill(1.5, 101, 'rest')]
        self.b.responses[0]['status'] = 'partial'
        self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual([c['size'] for c in self.b.calls], [2, 1.5])
        self.assertEqual(self.p.close_price_lighter, 100.75)
        self.assertEqual(self.p.lighter_size, 0)
        self.assertAlmostEqual(self.p.realized_pnl_usdc, 5.5)

    def test_retry_limit_is_durable_and_restart_cannot_reset_it(self):
        self.b.responses = [dict(confirmed=True, status='canceled', filled_size=0, order_id='c'+str(i))
                            for i in range(3)]
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(len(self.b.calls), 3)
        self.assertIn('次数已达上限', self.s.halted_reason)
        restarted = ArbitrageStrategy(self.config, self.a, self.b)
        self.assertIsInstance(restarted.positions[0].close_started_time, datetime)
        self.assertEqual(restarted.recover_single_leg_positions(), 'blocked')
        self.assertEqual(len(self.b.calls), 3)

    def test_changed_account_during_close_stops_retry(self):
        self.b.responses = [dict(confirmed=True, status='canceled', filled_size=0)]
        self.b.on_close = lambda: setattr(self.b, 'qty', 0)  # manual close after initial intent
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(len(self.b.calls), 1)
        self.assertIn('需核对手动成交', self.s.halted_reason)

    def test_duplicate_fill_receipt_is_not_applied_twice(self):
        self.b.responses = [dict(fill(.5, 100, 'same'), status='partial'), fill(.5, 100, 'same')]
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.p.lighter_size, 1.5)
        self.assertEqual(len(self.b.calls), 2)
        self.assertEqual(self.p.close_order_results['lighter']['status'], 'unknown')

    def test_restarted_grid_exit_residual_does_not_reenter_whole_trade_profit_gate(self):
        self.p.grid_cycle_id = 'cycle'
        self.p.close_started_time = datetime.now()
        self.p.close_reason = '网格逐层净收益退出'
        self.p.variational_size = self.a.qty = 0
        self.p.close_price_variational = 99
        self.p.realized_pnl_usdc = -20
        self.p.status = 'reconcile_required'
        self.p.close_attempts = {'variational': 1, 'lighter': 1}
        self.p.close_order_results = {'variational': fill(2, 99, 'other-closed'),
            'lighter': dict(confirmed=True, status='canceled', filled_size=0, order_id='canceled')}
        self.s._save()
        restarted = ArbitrageStrategy(self.config, self.a, self.b)
        with patch.object(restarted, '_grid_exit_estimate', side_effect=AssertionError('profit gate repeated')):
            self.assertEqual(restarted.recover_single_leg_positions(), 'recovered')
        self.assertEqual(self.a.calls, [])
        self.assertEqual([c['size'] for c in self.b.calls], [2])
        self.assertEqual(restarted.positions[0].status, 'closed')

    def test_retry_matches_aggregate_account_and_reduces_only_selected_layer(self):
        peer = Position('PUMP', 'short', 'buy', 3, 3, 102, 100, datetime.now(), status='open')
        self.s.positions.append(peer)
        self.a.qty = self.b.qty = 5
        self.b.responses = [dict(confirmed=True, status='canceled', filled_size=0), fill(2, 100, 'retry')]
        self.assertTrue(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual([c['size'] for c in self.b.calls], [2, 2])
        self.assertEqual((self.a.qty, self.b.qty), (3, 3))
        self.assertEqual((peer.variational_size, peer.lighter_size, peer.status), (3, 3, 'open'))

    def test_restart_does_not_silently_adopt_a_manual_partial_close(self):
        self.p.close_started_time = datetime.now()
        self.p.variational_size = self.a.qty = 0
        self.p.close_price_variational = 99
        self.p.status = 'reconcile_required'
        self.p.close_attempts = {'variational': 1, 'lighter': 1}
        self.p.close_order_results = {'variational': fill(2, 99, 'other-closed'),
            'lighter': dict(confirmed=True, status='canceled', filled_size=0)}
        self.s._save()
        self.b.qty = 1.5  # Account changed after the durable zero-fill receipt.
        restarted = ArbitrageStrategy(self.config, self.a, self.b)
        self.assertEqual(restarted.recover_single_leg_positions(), 'blocked')
        self.assertEqual(self.a.calls + self.b.calls, [])
        self.assertEqual(restarted.positions[0].lighter_size, 2)
        self.assertIn('需核对手动成交', restarted.halted_reason)

    def test_invalid_negative_local_size_cannot_be_finalized_as_closed(self):
        self.p.lighter_size = -1
        self.assertFalse(self.s.close_position(self.p, '价差收敛'))
        self.assertEqual(self.a.calls + self.b.calls, [])
        self.assertEqual(self.p.status, 'reconcile_required')
