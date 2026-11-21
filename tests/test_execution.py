import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from src.common.market_data import utc_now
from src.execution.journal import OrderJournal, execution_lease
from src.execution.orders import lighter_plan
from src.execution.worker import ExecutionWorker
from src.lighter.execution import LighterExecutionAdapter, OfficialSDKTransport, terminal_receipt
from src.variational.execution import VariationalExecutionAdapter, OmniForm, verify_receipt
from src.variational.receipt_reader import OmniResponseReader


def market():
    return dict(symbol='BTC', market_id=1, market_type='perp', status='active',
                supported_size_decimals=3, supported_price_decimals=2,
                min_base_amount='0.001', min_quote_amount='1')


def quote():
    return dict(symbol='BTC', size=1, bid=99, ask=100, updated_at=utc_now(),
                executable=True, instrument_type='perpetual', fee_bps=0, quote_id='rfq-1')


def order(index=123, status='filled', size='1'):
    return dict(owner_account_index=7, market_index=1, client_order_index=index,
                is_ask=False, reduce_only=False, initial_base_amount='1', status=status,
                filled_base_amount=size, filled_quote_amount=str(float(size)*100),
                order_id='order-9', order_index=9)


def trades(size='1', fee=0):
    return [dict(trade_id=1, market_id=1, bid_id=9, bid_account_id=7,
                 is_maker_ask=True, taker_fee=fee, integrator_taker_fee=0, size=size)]


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(Path(self.tmp.name)/'orders.sqlite')
        self.plan = lighter_plan(market(), 'BTC', 'buy', 1, 100, 10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_precision_and_directional_protection(self):
        self.assertEqual(self.plan['base_amount'], 1000)
        self.assertEqual(self.plan['price'], 10010)
        buy = lighter_plan(market(), 'BTC', 'buy', 1, '100.009', 0)
        sell = lighter_plan(market(), 'BTC', 'sell', 1, '100.009', 0)
        self.assertEqual(buy['price'], 10000)
        self.assertEqual(sell['price'], 10001)
        for size in ('0.0001', 'NaN', '-1'):
            with self.assertRaises(ValueError):
                lighter_plan(market(), 'BTC', 'buy', size, 100, 10)

    def test_journal_restart_duplicate_and_unknown_halt(self):
        row, new = self.journal.reserve('lighter', 7, 'client-1', self.plan)
        self.assertTrue(new)
        restarted = OrderJournal(self.journal.path)
        self.assertFalse(restarted.reserve('lighter', 7, 'client-1', self.plan)[1])
        with self.assertRaises(ValueError):
            restarted.reserve('lighter', 7, 'client-1', {**self.plan, 'size': '2'})
        with self.assertRaises(RuntimeError):
            restarted.reserve('lighter', 7, 'client-2', self.plan)
        restarted.finish(row['seq'], {'confirmed': True, 'status': 'canceled', 'filled_size': 0})
        self.assertTrue(restarted.reserve('lighter', 7, 'client-2', self.plan)[1])

    def test_process_lease(self):
        path = Path(self.tmp.name)/'execution.lock'
        with execution_lease(path):
            with self.assertRaises(RuntimeError):
                with execution_lease(path):
                    pass
        with execution_lease(path):
            pass

    def test_terminal_partial_and_nonzero_fee_unknown(self):
        result = terminal_receipt(order(status='canceled-not-enough-liquidity', size='.4'),
                                  self.plan, 7, 123, trades('.4'))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['platform_status'], 'canceled-not-enough-liquidity')
        self.assertEqual(result['filled_size'], .4)
        self.assertEqual(result['fees_usdc'], 0)
        self.assertIsNone(result['funding_usdc'])
        result = terminal_receipt(order(), self.plan, 7, 123, trades(fee=100))
        self.assertIsNone(result['fees_usdc'])
        self.assertIsNone(terminal_receipt(order(status='open'), self.plan, 7, 123, []))

    def test_leverage_info_uses_account_settings_when_market_imf_is_missing(self):
        public = Mock()
        public.get_market.return_value = ({**market(),
                                           'min_initial_margin_fraction': None,
                                           'default_initial_margin_fraction': None}, utc_now())
        public.client.get.return_value = ({'accounts': [{
            'account_index': 7,
            'positions': [{'market_id': 1, 'initial_margin_fraction': '10.00', 'margin_mode': 0}],
        }]}, utc_now())
        adapter = LighterExecutionAdapter(public, Mock(), self.journal, 7, enabled=True)
        info = adapter.leverage_info('BTC')
        self.assertAlmostEqual(info['current_leverage'], 10.0)
        self.assertAlmostEqual(info['max_leverage'], 10.0)
        params = public.client.get.call_args.args[1]
        self.assertIs(params['active_only'], False)

    def test_wrong_order_and_protective_price_rejected(self):
        for changed in ({'owner_account_index': 8}, {'is_ask': True}, {'initial_base_amount': '2'},
                        {'filled_quote_amount': '101'}, {'filled_base_amount': '.5'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                terminal_receipt({**order(), **changed}, self.plan, 7, 123, trades())

    def adapter(self, timeout=False, found=True):
        public = Mock()
        public.get_market.return_value = (market(), utc_now())
        public.get_market_price.return_value = 100
        public.client.get.return_value = ({'accounts': [{'account_index': 7,
            'available_balance': '10000', 'positions': []}]}, utc_now())
        bridge = Mock()
        self.sent = []
        def call(name, *args):
            if name == 'active_orders': return []
            if name == 'submit':
                self.sent.append(args)
                if timeout: raise TimeoutError()
                return {'accepted': True}
            if name == 'find_order': return order(index=args[1]) if found else None
            if name == 'trades': return trades()
            raise AssertionError(name)
        bridge.call.side_effect = call
        result = LighterExecutionAdapter(public, bridge, self.journal, 7, enabled=True, poll_seconds=0)
        result.verified_fee_bps = 0
        return result

    def test_sdk_submission_reconciles_timeout_without_retry(self):
        adapter = self.adapter(timeout=True)
        args = ('BTC', 'buy', 1, 100, 10, 'id-1', quote())
        result = adapter.place_order(*args)
        self.assertTrue(result['confirmed'])
        self.assertEqual(adapter.place_order(*args), result)
        self.assertEqual(len(self.sent), 1)

    def test_size_bound_quote_preserves_stream_age_and_source_without_network(self):
        adapter = self.adapter()
        book = dict(bids=[dict(price=99, size=2)], asks=[dict(price=100, size=2)],
                    fetched_at=utc_now(), source='lighter_websocket', market_id=1,
                    offset=105, nonce=20, received_at=utc_now(), exchange_at=utc_now(), time_basis='exchange')
        adapter.public.reset_mock()
        q = adapter.quote_from_sizing_book('BTC', 1, book)
        self.assertEqual(q['updated_at'], book['fetched_at'])
        self.assertEqual(q['sizing_book'], book)
        for key in ('source', 'market_id', 'offset', 'nonce', 'received_at', 'exchange_at', 'time_basis'):
            self.assertEqual(q[key], book[key])
        self.assertEqual(adapter.public.mock_calls, [], 'metadata propagation adds no platform reads')
        with self.assertRaisesRegex(ValueError, '盘口过期'):
            adapter.quote_from_sizing_book('BTC', 1, dict(book, fetched_at='2000-01-01T00:00:00Z'))

    def test_verified_zero_fee_proof_accounts_missing_trade_fee_fields(self):
        adapter = self.adapter()
        adapter.verified_fee_bps = 0
        original = adapter.bridge.call.side_effect
        def missing_fee_fields(name, *args):
            if name == 'trades':
                return [dict(trade_id=1, market_id=1, bid_id=9, bid_account_id=7,
                             is_maker_ask=True, size='1')]
            return original(name, *args)
        adapter.bridge.call.side_effect = missing_fee_fields
        result = adapter._reconcile(self.plan, 123)
        self.assertEqual(result['fees_usdc'], 0.0)
        self.assertTrue(result['fees_verified'])

    def test_sdk_unknown_persists_and_blocks_new_client_id(self):
        adapter = self.adapter(timeout=True, found=False)
        result = adapter.place_order('BTC', 'buy', 1, 100, 10, 'id-1', quote())
        self.assertFalse(result['confirmed'])
        with self.assertRaises(RuntimeError):
            adapter.place_order('BTC', 'buy', 1, 100, 10, 'id-2', quote())
        self.assertEqual(len(self.sent), 1)

    def test_reduce_only_recovery_can_reserve_after_unknown_entry(self):
        row, _ = self.journal.reserve('lighter', 7, 'entry-unknown', self.plan)
        self.journal.finish(row['seq'], {'confirmed': True, 'status': 'filled',
                                         'filled_size': 1, 'fees_usdc': None})
        close_plan = {**self.plan, 'side': 'sell', 'is_ask': True, 'reduce_only': True}
        close_row, new = self.journal.reserve('lighter', 7, 'close-recovery', close_plan)
        self.assertTrue(new)
        self.assertEqual(close_row['state'], 'submitting')

    def test_reduce_only_reverses_side_and_checks_real_position(self):
        adapter = self.adapter()
        adapter.public.client.get.return_value[0]['accounts'][0]['positions'] = [
            {'market_id': 1, 'position': '1', 'sign': 1}]
        adapter.validate_order('BTC', .5, reduce_only=True, side='sell')
        with self.assertRaises(ValueError): adapter.validate_order('BTC', 2, True, 'sell')
        with self.assertRaises(ValueError): adapter.validate_order('BTC', .5, True, 'buy')

    def test_restart_reconciliation_only_queries_and_never_submits(self):
        adapter = self.adapter(found=True)
        row, _ = self.journal.reserve('lighter', 7, 'existing', self.plan)
        self.assertTrue(adapter.reconcile_order(row['seq'])['confirmed'])
        self.assertEqual(self.sent, [])

    def test_official_sdk_uses_ioc_and_sdk_managed_nonce(self):
        class SDK:
            ORDER_TYPE_LIMIT=0
            ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0
            DEFAULT_IOC_EXPIRY=0
            async def create_order(self, **kwargs):
                self.args=kwargs
                return None, {'code':200}, None
        sdk=SDK()
        result=asyncio.run(OfficialSDKTransport(sdk,7,5).submit(self.plan,123))
        self.assertTrue(result['accepted'])
        self.assertEqual(sdk.args['client_order_index'],123)
        self.assertEqual(sdk.args['time_in_force'],0)
        # Explicit api_key_index with nonce=-1 bypasses SDK nonce management.
        self.assertNotIn('api_key_index',sdk.args)
        self.assertNotIn('nonce',sdk.args)

    def test_worker_dry_run_and_kill_switch_never_submit(self):
        strategy=Mock();strategy.live_blockers.return_value=[];strategy.halted_reason=None
        strategy.get_strategy_status.return_value={'positions':0}
        kill=Path(self.tmp.name)/'STOP';kill.touch()
        worker=ExecutionWorker(strategy,['BTC'],str(Path(self.tmp.name)/'lock'),kill_path=kill)
        self.assertFalse(worker.run()['submitted'])
        worker.live=True;self.assertTrue(worker.run()['stopped'])
        strategy.execute_arbitrage.assert_not_called()


class BrowserExecutionTests(unittest.TestCase):
    def test_dom_rejects_wrong_unit_and_nonofficial_page(self):
        driver=Mock();driver.current_url='https://omni.variational.io/perpetual/BTC'
        element=Mock();element.is_displayed.return_value=True;element.text='USDC'
        driver.find_elements.return_value=[element]
        form=OmniForm(driver)
        with self.assertRaisesRegex(ValueError,'基础币或美元'):form.assert_market('BTC')
        element.text='$';self.assertEqual(form.assert_market('BTC'),'$')
        element.text='BTC';form.assert_market('BTC')
        driver.current_url='https://example.org/perpetual/BTC'
        with self.assertRaises(ValueError):form.assert_market('BTC')

    def test_dollar_mode_fills_notional_without_touching_leverage(self):
        driver=Mock();driver.current_url='https://omni.variational.io/perpetual/ETH'
        toggle=Mock();toggle.is_displayed.return_value=True;toggle.text='$'
        quantity=Mock();quantity.is_displayed.return_value=True
        quantity.get_attribute.return_value='0'
        def elements(_, selector):
            if selector == '[data-testid="input-mode-toggle"]': return [toggle]
            if selector == '[data-testid="quantity-input"]': return [quantity]
            raise AssertionError(selector)
        driver.find_elements.side_effect=elements
        form=OmniForm(driver)
        form.prepare_quote('ETH', '0.004', price='2700')
        quantity.clear.assert_called_once_with()
        quantity.send_keys.assert_called_once_with('10.800')
        self.assertNotIn('leverage', [call.args[0] for call in driver.find_elements.call_args_list])

    def test_dom_submit_requires_exact_verified_label_and_unique_control(self):
        driver=Mock();button=Mock();button.text='Connect Wallet'
        button.is_displayed.return_value=True;button.is_enabled.return_value=True
        driver.find_elements.return_value=[button]
        form=OmniForm(driver)
        with self.assertRaises(ValueError):form.click_submit('Buy')
        button.click.assert_not_called()
        button.text='Buy';driver.find_elements.return_value=[button,button]
        with self.assertRaises(ValueError):form.click_submit('Buy')
        button.click.assert_not_called()

    def test_dom_reads_signed_chinese_page_slippage(self):
        driver=Mock();driver.current_url='https://omni.variational.io/perpetual/ETH'
        button=Mock();button.is_displayed.return_value=True
        button.text='估: -0.03%\n/\n最大: 0.1%'
        driver.find_elements.return_value=[button]
        form=OmniForm(driver)
        self.assertAlmostEqual(float(form.estimated_slippage_bps()), -3.0)
        self.assertAlmostEqual(float(form.max_slippage_bps()), 10.0)

    def test_runtime_example_cannot_open_browser_or_sdk(self):
        from src.execution.runtime import runtime_blockers
        path=Path(__file__).resolve().parents[1]/'config/variational-execution.example.json'
        self.assertIn('执行配置默认关闭',runtime_blockers(json.loads(path.read_text())))

    def test_receipt_binding(self):
        receipt=dict(symbol='BTC',side='buy',quote_id='rfq-1',reduce_only=False,
                     requested_size=1,order_id='v1',confirmed=True,status='filled',
                     filled_size=1,average_price=100,fees_usdc=0,funding_usdc=None)
        self.assertTrue(verify_receipt(receipt,quote(),'buy',1,False)['confirmed'])
        for changed in ({'quote_id':'old'}, {'side':'sell'}, {'filled_size':2}, {'confirmed':False}):
            with self.assertRaises(ValueError):
                verify_receipt({**receipt,**changed},quote(),'buy',1,False)

    def test_click_timeout_never_double_click(self):
        with tempfile.TemporaryDirectory() as t:
            form,reader=Mock(),Mock()
            form.max_slippage_bps.return_value=5
            q=quote();reader.executable_quote.return_value=q
            reader.wait_terminal.side_effect=TimeoutError()
            adapter=VariationalExecutionAdapter(form,reader,OrderJournal(Path(t)/'j.sqlite'),'account1',
                {'account_verified':True,'receipt_schema_verified':True,'submit_labels':{'buy':'Buy'}},True)
            args=('BTC','buy',1,100,10,'v1',q)
            self.assertFalse(adapter.place_order(*args)['confirmed'])
            adapter.place_order(*args)
            form.click_submit.assert_called_once_with('Buy')

    def test_unverified_profile_never_clicks(self):
        with tempfile.TemporaryDirectory() as t:
            form=Mock()
            adapter=VariationalExecutionAdapter(form,Mock(),OrderJournal(Path(t)/'j.sqlite'),'a',{},True)
            self.assertEqual(adapter.place_order('BTC','buy',1,100,10,'v1',quote())['status'],'rejected')
            form.click_submit.assert_not_called()

    def test_reader_unknown_schema_cannot_upgrade_public_quote(self):
        reader=OmniResponseReader(Mock(),{},'a')
        reader.ingest('/api/quotes/indicative',{}, {'bid':'99','ask':'100'},utc_now())
        self.assertEqual(reader.quotes,{})


if __name__ == '__main__':
    unittest.main()
