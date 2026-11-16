import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone
from src.common.market_data import utc_now
from src.variational.omni_contract import OmniContract, identity, collection, QUOTE, ORDERS, TRADES, POSITIONS, BALANCE
from src.variational.receipt_reader import OmniResponseReader
from src.execution.preflight import budget_check

ACCOUNT = dict(company='fixture-company', pool_location='fixture-pool')
INSTRUMENT = dict(underlying='ETH', instrument_type='perpetual_future')


def page(rows):
    return dict(result=rows, pagination=dict(last_page=dict(offset=0, limit=100), next_page=None, object_count=len(rows)))


def order(**changes):
    return {**ACCOUNT, 'instrument': INSTRUMENT, 'order_id': 'o1', 'rfq_id': 'r1',
            'qty': '0.004', 'price': '2700', 'side': 'buy', 'is_reduce_only': False,
            'status': 'cleared', 'clearing_status': 'success_trades_booked_into_pool', 'tif': 'fill_or_kill', **changes}


def trade(**changes):
    return {**ACCOUNT, 'instrument': INSTRUMENT, 'id': 't1', 'source_rfq': 'r1', 'source_quote': 'q1',
            'qty': '0.004', 'price': '2700', 'side': 'buy', 'status': 'confirmed', 'role': 'taker', 'trade_type': 'trade', **changes}


class ContractTests(unittest.TestCase):
    def test_actual_api_string_first_offset_is_complete_but_not_later_pages(self):
        payload=page([order()])
        for offset,expected in [('0',True),(0,True),('1',False),(False,False),(None,False)]:
            payload['pagination']['last_page']['offset']=offset
            self.assertEqual(collection(payload)[1],expected)

    def test_missing_funding_does_not_discard_real_position(self):
        self.ingest(POSITIONS,[{'position_info':{**ACCOUNT,'instrument':INSTRUMENT,'qty':'.004'},'cum_funding':None}])
        snapshot=self.contract.snapshots[POSITIONS]
        self.assertEqual(snapshot['positions']['ETH'],'0.004')
        self.assertIsNone(snapshot['cumulative_funding']['ETH'])

    def test_readonly_refresh_observes_complete_account_and_invalidates_failed_refresh(self):
        driver=Mock();driver.current_url='https://omni.variational.io/perpetual/ETH'
        payloads={ORDERS:page([order()]),POSITIONS:[],BALANCE:{'max_withdrawable_amount':'12'}}
        payloads[ORDERS]['pagination']['last_page']['offset']='0'
        driver.execute_async_script.return_value=[dict(path=path,status=200,payload=value,observed_at=utc_now()) for path,value in payloads.items()]
        reader=OmniResponseReader(driver,{'adapter':'omni_v1'},identity(ACCOUNT))
        reader.refresh_account_snapshots()
        self.assertEqual(reader.contract.account()['pending_orders'],0)
        self.assertEqual(reader.contract.account()['positions'],{})
        driver.execute_async_script.return_value=[dict(path=BALANCE,status=401)]
        reader.refresh_account_snapshots()
        with self.assertRaises(ValueError):reader.contract.account()

    def test_readonly_refresh_does_not_retimestamp_old_responses(self):
        driver=Mock();driver.current_url='https://omni.variational.io/perpetual/ETH'
        driver.execute_async_script.return_value=[dict(path=BALANCE,status=200,
            payload={'max_withdrawable_amount':'12'},observed_at='2000-01-01T00:00:00Z')]
        reader=OmniResponseReader(driver,{'adapter':'omni_v1'},identity(ACCOUNT))
        reader.refresh_account_snapshots()
        self.assertNotIn(BALANCE,reader.contract.snapshots)

    def setUp(self):
        self.at = utc_now()
        self.contract = OmniContract(identity(ACCOUNT))
        self.quote = dict(account_id=identity(ACCOUNT), quote_id='q1', symbol='ETH', size='0.004', max_slippage_bps=10)
        self.contract.arm(self.quote, 'buy', '.004', False, self.at)
        self.submit = dict(quote_id='q1', side='buy', is_reduce_only=False, max_slippage=.001)

    def ingest(self, path, body, request=None):
        self.contract.ingest(path, request or {}, body, utc_now())

    def link(self):
        self.ingest('/api/orders/new/market', {'rfq_id': 'r1'}, self.submit)
        self.ingest(ORDERS, page([order()]))

    def test_submit_success_and_cleared_order_are_not_enough(self):
        self.link()
        self.assertIsNone(self.contract.receipt)
        self.ingest(TRADES, page([trade()]))
        receipt = self.contract.receipt
        self.assertEqual(receipt['filled_size'], '0.004')
        self.assertEqual(receipt['average_price'], '2700')
        self.assertIsNone(receipt['fees_usdc'])
        self.assertIsNone(receipt['funding_usdc'])

    def test_zero_fee_requires_current_official_policy_bound_quote_and_normal_fill(self):
        self.contract.fee_policy={'source_url':'https://docs.variational.io/omni/trading/fees',
                                 'scope':'confirmed_taker_trade','fee_bps':0,'verified_at':utc_now()}
        self.contract.expected=None
        self.contract.arm({**self.quote,'fee_bps':0},'buy','.004',False,utc_now())
        self.link();self.ingest(TRADES,page([trade()]))
        self.assertEqual(self.contract.receipt['fees_usdc'],'0')
        self.assertEqual(self.contract.receipt['fee_basis'],'official_zero_trade_fee_policy_and_bound_quote')
        self.assertIsNone(self.contract.receipt['funding_usdc'])
        self.contract.fee_policy['verified_at']='2000-01-01T00:00:00Z'
        self.ingest(TRADES,page([trade()]))
        self.assertIsNone(self.contract.receipt['fees_usdc'])
        self.contract.fee_policy['verified_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        self.ingest(TRADES,page([trade()]))
        self.assertEqual(self.contract.receipt['fees_usdc'],'0')
        self.contract.fee_policy['verified_at']=utc_now()
        self.contract.expected['quoted_zero_fee']=False
        self.ingest(TRADES,page([trade()]))
        self.assertIsNone(self.contract.receipt['fees_usdc'])

    def test_out_of_order_responses_link_without_guessing(self):
        self.ingest(TRADES, page([trade()]))
        self.ingest(ORDERS, page([order()]))
        self.assertIsNone(self.contract.receipt)
        self.ingest('/api/quotes/accept', {'rfq_id': 'r1'}, self.submit)
        self.assertTrue(self.contract.receipt['confirmed'])

    def test_partial_page_pending_and_duplicate_trades_are_not_fills(self):
        self.link()
        self.ingest(TRADES, page([trade(qty='.002')]))
        self.assertIsNone(self.contract.receipt)
        self.ingest(TRADES, page([trade(status='pending')]))
        self.assertIsNone(self.contract.receipt)
        self.ingest(TRADES, page([trade(qty='.002'), trade(qty='.002')]))
        self.assertIsNotNone(self.contract.error)

    def test_missing_source_quote_account_side_quantity_and_price_fail_closed(self):
        for change in ({'source_quote': None}, {'company': 'other'}, {'side': 'sell'}, {'qty': '.005'}, {'price': '2701'}):
            with self.subTest(change=change):
                self.setUp(); self.link(); self.ingest(TRADES, page([trade(**change)]))
                self.assertIsNone(self.contract.receipt)
                self.assertIsNotNone(self.contract.error)

    def test_observed_source_quote_is_distinct_and_rfq_is_authoritative(self):
        self.link()
        self.ingest(TRADES, page([trade(source_quote='venue-execution-quote', source_rfq='other')]))
        self.assertIsNone(self.contract.receipt)
        self.ingest(TRADES, page([trade(source_quote='venue-execution-quote')]))
        self.assertTrue(self.contract.receipt['confirmed'])
        self.assertEqual(self.contract.receipt['quote_id'],'q1')
        self.assertEqual(self.contract.receipt['venue_source_quotes'],['venue-execution-quote'])

    def test_multiple_submissions_and_plan_extras_are_unknown(self):
        for payload in ({'rfq_id': 'r2'}, {'rfq_id': 'r1', 'take_profit_rfq_id': 'tp'}):
            self.setUp(); self.link()
            self.ingest('/api/orders/new/market', payload, self.submit)
            self.assertIsNotNone(self.contract.error)

    def test_wider_site_slippage_is_not_accepted(self):
        self.ingest('/api/orders/new/market', {'rfq_id':'r1'}, {**self.submit, 'max_slippage':.005})
        self.assertIsNotNone(self.contract.error)

    def test_account_requires_complete_bound_orders_and_nested_positions(self):
        self.ingest(ORDERS, page([order()]))
        self.ingest(POSITIONS, [{'position_info': {**ACCOUNT, 'instrument': INSTRUMENT, 'qty': '.004'}, 'cum_funding': '0.0001'}])
        self.ingest(BALANCE, {'max_withdrawable_amount': '12'})
        self.assertEqual(self.contract.account()['positions'], {'ETH': '0.004'})
        incomplete = page([order()]); incomplete['pagination']['next_page'] = {'offset': 100}
        self.ingest(ORDERS, incomplete)
        with self.assertRaises(ValueError): self.contract.account()

    def test_invalid_latest_positions_invalidate_old_snapshot(self):
        self.ingest(ORDERS, page([order()]))
        self.ingest(POSITIONS, [])
        self.ingest(BALANCE, {'max_withdrawable_amount': '12'})
        self.assertEqual(self.contract.account()['positions'], {})
        self.ingest(POSITIONS, {'rows': []})
        with self.assertRaises(ValueError): self.contract.account()

    def test_quote_precision_server_timestamp_and_fee_units(self):
        request = dict(instrument=INSTRUMENT, qty='.004')
        body = dict(instrument=INSTRUMENT, qty='.004', bid='2699', ask='2700', mark_price='2700', quote_id='q1',
                    timestamp=utc_now(), margin_requirements={'estimated_fees_bid': '0', 'estimated_fees_ask': '0'},
                    qty_limits={side: dict(min_qty_tick='.0001', min_qty='.0001', max_qty='100') for side in ('bid','ask')})
        self.ingest(QUOTE, body, request)
        self.assertEqual(self.contract.quotes['ETH']['fee_bps'], 0)
        self.assertFalse(self.contract.quotes['ETH']['executable'])
        body['margin_requirements']['estimated_fees_ask'] = '1'
        self.ingest(QUOTE, body, request)
        self.assertIsNone(self.contract.quotes['ETH']['fee_bps'])
        body['timestamp'] = '2000-01-01T00:00:00+00:00'
        self.ingest(QUOTE, body, request)
        self.assertEqual(self.contract.quotes, {})

    def test_reader_uses_actual_chain(self):
        driver = Mock(); driver.get_log.return_value = []
        reader = OmniResponseReader(driver, {'adapter': 'omni_v1'}, identity(ACCOUNT))
        reader.arm(self.quote, 'buy', '.004', False)
        reader.ingest('/api/orders/new/market', self.submit, {'rfq_id':'r1'}, utc_now())
        reader.ingest(ORDERS, {}, page([order()]), utc_now())
        reader.ingest(TRADES, {}, page([trade()]), utc_now())
        self.assertEqual(reader.wait_terminal(.1)['filled_size'], '0.004')


class BudgetTests(unittest.TestCase):
    def test_one_dollar_never_bumped_to_ten(self):
        market = dict(symbol='ETH', market_type='perp', status='active', supported_size_decimals=4,
                      min_quote_amount='10', min_base_amount='.002')
        result = budget_check('ETH', 1, market, 2700)
        self.assertFalse(result['budget_allowed'])
        self.assertEqual(result['cap_usdc'], 1)
        self.assertEqual(result['minimum_quantity_at_reference'], '0.0038')
        self.assertTrue(budget_check('ETH', 12, market, 2700)['budget_allowed'])
        self.assertFalse(budget_check('ETH', 10, market, 2700)['budget_allowed'])
