"""Optional manual-close accounting never participates in order submission."""
import copy
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_history_accounting import evidence
from scripts.query_pending_accounting import refresh
from src.common.market_data import utc_now
from src.common.rate_limit import RateLimitError, RateLimitGate
from src.execution.console import ExecutionConsole
from src.execution.history_accounting import store_evidence
from src.execution.history_recovery import apply_close_history
from src.execution.pending_accounting import pending_jobs, read_allowed, POSITION_FIELDS
from src.lighter.execution import OfficialSDKTransport


class PendingAccountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'data').mkdir()
        (self.root/'config').mkdir()
        (self.root/'config/config.yaml').write_text('{}')
        (self.root/'.env').write_text('LIGHTER_ACCOUNT_INDEX=7\nLIGHTER_API_KEY_INDEX=1\nLIGHTER_API_PRIVATE_KEY=test\n')
        position, self.cache = evidence()
        for row in self.cache['rows']:
            row['order_id'] = {'l-open':'100','l-close':'101'}.get(row['order_id'], row['order_id'])
        position['entry_order_ids'] = ['a-open', '100']
        position.update(status='closed')
        self.position = {key:value for key,value in position.items() if key in POSITION_FIELDS}
        self.path = self.root/'data/platform-order-history.json'
        self.public, self.sdk = Mock(), Mock()
        self.public.client.rate_limit.optional_read_ready.return_value = True
        self.trade = dict(trade_id='actual', bid_id='101', bid_account_id=7, market_id=9,
            size='1', price='102', timestamp=1793498400000, is_maker_ask=True,
            taker_fee=0, integrator_taker_fee=0)
        self.sdk.call.return_value = [self.trade]

    def save(self):
        self.path.write_text(json.dumps(self.cache))

    def run_refresh(self):
        with patch('scripts.query_pending_accounting.build_lighter_session', return_value=(self.public, self.sdk)), \
             patch('scripts.query_pending_accounting.historical_market_id', return_value=9):
            return refresh(self.root, self.position)

    def missing_fee(self):
        self.cache['rows'][3].pop('fees_usdc')
        self.save()

    def test_only_missing_manual_close_fee_is_read_and_final_ledger_settles(self):
        self.missing_fee()
        before = copy.deepcopy(self.position)
        report = self.run_refresh()
        self.assertTrue(report['complete'])
        self.assertFalse(report['orders_submitted'])
        self.assertEqual(report['reads'], 1)
        self.sdk.call.assert_called_once_with('trades', 9, 101, 1)
        self.assertEqual(self.position, before, 'accounting cannot mutate execution position state')
        book = dict(closed_positions=[copy.deepcopy(self.position)])
        apply_close_history(book, self.root/'data')
        self.assertAlmostEqual(book['closed_positions'][0]['realized_pnl_usdc'], .81)

    def test_omitted_platform_fee_stays_pending_and_cached_funding_is_not_read_again(self):
        self.missing_fee()
        self.sdk.call.return_value = [{key:value for key,value in self.trade.items() if 'fee' not in key}]
        report = self.run_refresh()
        self.assertFalse(report['complete'])
        self.assertEqual(report['reads'], 1)
        cached = json.loads(self.path.read_text())
        self.assertNotIn('fees_usdc', next(row for row in cached['rows'] if row['order_id']=='101'))
        self.assertEqual(cached['funding_intervals'], self.cache['funding_intervals'])

    def test_two_request_cap_preserves_unread_evidence_and_rotates_jobs(self):
        self.missing_fee()
        self.cache['rows'][2].pop('fees_usdc')
        self.cache['funding_intervals'] = [row for row in self.cache['funding_intervals'] if row['venue']!='lighter']
        self.save()
        self.sdk.call.return_value = []
        report = self.run_refresh()
        self.assertLessEqual(report['reads'], 2)
        self.assertEqual(self.sdk.call.call_count, 2)
        self.assertTrue(all(call.args[-1] == 1 for call in self.sdk.call.call_args_list))
        self.assertEqual(json.loads(self.path.read_text())['accounting_checks']['p']['cursor'], 2)

    def test_no_missing_evidence_reuses_cache_without_creating_a_session(self):
        self.save()
        report = self.run_refresh()
        self.assertTrue(report['complete'])
        self.sdk.call.assert_not_called()

    def test_only_missing_funding_window_is_read(self):
        self.cache['funding_intervals'] = [r for r in self.cache['funding_intervals'] if r['venue'] != 'lighter']
        self.save()
        self.sdk.call.return_value = []
        report = self.run_refresh()
        self.assertTrue(report['complete'])
        self.assertEqual(report['reads'], 1)
        self.assertEqual(self.sdk.call.call_args.args[0], 'position_funding')
        self.assertEqual(self.sdk.call.call_args.args[-1], 1)

    def test_optional_sdk_reads_never_follow_a_second_page(self):
        calls = []
        async def page(**kwargs):
            calls.append(kwargs)
            return dict(code=200, trades=[], position_fundings=[], next_cursor='second')
        sdk = Mock()
        sdk.create_auth_token_with_expiry.return_value = ('test-only', None)
        sdk.order_api.trades = page
        sdk.account_api.position_funding = page
        transport = OfficialSDKTransport(sdk, 1, 7)
        for read in (transport.trades(9, 101, 1), transport.position_funding(9, 1000, 2000, 1)):
            with self.assertRaisesRegex(RuntimeError, '分页未完整读取'):
                asyncio.run(read)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('cursor' not in args for args in calls))

    def test_no_complete_market_or_open_local_position_is_never_probed(self):
        self.missing_fee()
        self.assertEqual(pending_jobs(dict(self.position, status='open'), self.cache), [])
        self.cache['complete_markets'] = []
        self.assertEqual(pending_jobs(self.position, self.cache), [])

    def test_trade_starting_while_session_is_built_yields_before_http(self):
        self.missing_fee()
        with patch('scripts.query_pending_accounting.read_allowed', side_effect=[True, True, False]):
            report = self.run_refresh()
        self.assertTrue(report['deferred'])
        self.sdk.call.assert_not_called()

    def test_exchange_priority_slot_defers_optional_history(self):
        self.missing_fee()
        self.public.client.rate_limit.optional_read_ready.return_value = False
        report = self.run_refresh()
        self.assertTrue(report['deferred'])
        self.sdk.call.assert_not_called()

    def test_reference_scans_do_not_starve_accounting_but_candidate_reservations_do(self):
        gate = RateLimitGate('https://test.invalid', directory=self.root/'cooldown')
        gate.path.parent.mkdir()
        pacing = gate.path.with_suffix('.discovery.json')
        with patch('src.common.rate_limit.time.time', return_value=1000):
            pacing.write_text(json.dumps(dict(until=1002, background_until=1004)))
            self.assertTrue(gate.optional_read_ready())
            pacing.write_text(json.dumps(dict(until=1002, priority_until=1004)))
            self.assertFalse(gate.optional_read_ready())
            pacing.write_text('{}')
            gate.path.write_text(json.dumps(dict(until=995,failures=1)))
            self.assertFalse(gate.optional_read_ready(), 'recent exchange throttles get a recovery window')

    def test_429_records_shared_cooldown_and_preserves_verified_evidence(self):
        self.missing_fee()
        error = RuntimeError('response not exposed')
        error.status = 429
        error.headers = {'Retry-After':'60'}
        self.sdk.call.side_effect = error
        self.public.client.rate_limit.reject.side_effect = RateLimitError(60)
        report = self.run_refresh()
        self.assertFalse(report['success'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['reads'], 1)
        self.public.client.rate_limit.reject.assert_called_once()
        cached = json.loads(self.path.read_text())
        self.assertEqual(cached['rows'], self.cache['rows'])
        self.assertEqual(cached['funding_intervals'], self.cache['funding_intervals'])

    def test_partial_update_cannot_erase_another_history_refresh(self):
        self.save()
        extra = dict(self.cache['rows'][0], order_id='another-pair')
        store_evidence(self.root/'data', [extra], [['arcus','BABA']], [], [])
        store_evidence(self.root/'data', [self.cache['rows'][3]], [['lighter','BABA']], [], [],
                       {'p':dict(checked_at=utc_now(),cursor=1)})
        cached = json.loads(self.path.read_text())
        self.assertIn('another-pair', [row['order_id'] for row in cached['rows']])
        self.assertEqual(len(cached['funding_intervals']), 2)

    def live_monitor(self):
        self.console_path = self.root/'data/console_execution.json'
        self.console_path.write_text(json.dumps(dict(state='running',pid=123,venues=['lighter','arcus'])))
        self.monitor_path = self.root/'data/hedge_state_lighter_arcus.monitor.json'
        self.monitor = dict(worker_pid=123,updated_at=utc_now(),running=True,positions=[dict(position_status='open')])
        self.monitor_path.write_text(json.dumps(self.monitor))
        self.journal = self.root/'data/execution_orders.sqlite'
        with sqlite3.connect(self.journal) as db:
            db.execute('CREATE TABLE orders(state TEXT,result TEXT)')

    def test_stale_monitor_close_and_ambiguous_send_prevent_optional_reads(self):
        self.live_monitor()
        self.assertTrue(read_allowed(self.root))
        self.monitor['positions'][0]['phase']='closing'
        self.monitor_path.write_text(json.dumps(self.monitor))
        self.assertFalse(read_allowed(self.root))
        self.monitor['positions'][0].pop('phase')
        self.monitor['updated_at']='2020-01-01T00:00:00+00:00'
        self.monitor_path.write_text(json.dumps(self.monitor))
        self.assertFalse(read_allowed(self.root))
        self.monitor['updated_at']=utc_now()
        self.monitor_path.write_text(json.dumps(self.monitor))
        with sqlite3.connect(self.journal) as db:
            db.execute('INSERT INTO orders VALUES(?,?)', ('submitting',None))
        self.assertFalse(read_allowed(self.root))
        with sqlite3.connect(self.journal) as db:
            db.execute('UPDATE orders SET state=?, result=?', ('unknown',json.dumps(dict(confirmed=True))))
        self.assertTrue(read_allowed(self.root), 'confirmed fill with missing fees may still be audited')

    def test_console_uses_separate_audit_child_and_no_trading_lock(self):
        console = ExecutionConsole(self.root, Mock())
        with patch('src.execution.console.subprocess.run',return_value=Mock(returncode=0,
                stdout=json.dumps(dict(success=True,orders_submitted=False,complete=False)))) as run:
            report=console.pending_accounting(dict(self.position,private='never forward'))
        self.assertFalse(report['orders_submitted'])
        self.assertNotIn('private', json.loads(run.call_args.kwargs['input']))
        self.assertIn('query_pending_accounting.py',run.call_args.args[0][-1])
        self.assertFalse((self.root/'data/runtime.lock').exists())
