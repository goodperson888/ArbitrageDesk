import unittest
from unittest.mock import Mock

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter, normalize_order_update
from src.arcus.pricing import PriceGridError
from src.arcus.trading_bounds import validate_trading_quote
from src.common.market_data import utc_now
from src.execution.telemetry import safe_reason


class TradingBoundsTests(unittest.TestCase):
    def setUp(self):
        self.market = dict(tick_size='.01', trading_bounds={'lower':'169.61', 'upper':'183.74'})
        self.quote = dict(symbol='NVDA', size=.0652, bid=183.74, ask=183.75,
                          updated_at=utc_now(), executable=True, instrument_type='perpetual')

    def test_exact_bound_and_outside_prices_are_not_executable_on_either_side(self):
        for side, price in (('sell',183.74), ('sell',184), ('buy',169.61), ('buy',169)):
            with self.subTest(side=side, price=price), self.assertRaises(PriceGridError):
                validate_trading_quote(self.market, side, dict(bid=price,ask=price))

    def test_best_fill_is_checked_even_when_depth_worst_price_is_inside_band(self):
        for side, key, best in (('sell','bids',183.74), ('buy','asks',169.61)):
            quote = dict(bid=180,ask=180,sizing_book={key:[{'price':best,'size':.01}]})
            with self.subTest(side=side), self.assertRaises(PriceGridError):
                validate_trading_quote(self.market, side, quote)

    def test_in_band_and_regular_hours_quotes_remain_eligible(self):
        for side in ('buy','sell'):
            validate_trading_quote(self.market, side, dict(bid=180,ask=180.01))
            validate_trading_quote(dict(trading_bounds={'lower':None,'upper':None}),side,self.quote)
            validate_trading_quote({},side,self.quote)

    def test_partial_invalid_or_nonfinite_bounds_fail_closed(self):
        for bounds in ({'lower':None,'upper':'184'}, {'lower':'184','upper':'170'},
                       {'lower':'NaN','upper':'184'}, {'lower':True,'upper':'184'}, {}):
            with self.subTest(bounds=bounds), self.assertRaisesRegex(PriceGridError,'边界无效'):
                validate_trading_quote(dict(trading_bounds=bounds),'sell',self.quote)

    def test_catalog_retains_band_without_additional_requests(self):
        client = ArcusClient.__new__(ArcusClient)
        client._markets, client._markets_at = {},0
        client._get = Mock(return_value={'markets':[dict(marketDisplayName='NVDA-USD',marketId=28,
            lowerTradingBound='169.61',upperTradingBound='183.74')]})
        for _ in range(3):
            self.assertEqual(client.market('NVDA')['trading_bounds'],self.market['trading_bounds'])
        client._get.assert_called_once_with('/v1/markets')

    def test_guard_rejects_before_account_read_journal_reserve_or_submission(self):
        client,journal = Mock(api_key='verified'),Mock()
        client.market.return_value=self.market
        adapter=ArcusExecutionAdapter(client,journal,'test',enabled=True,fee_bps=4.5)
        with self.assertRaises(PriceGridError) as caught:
            adapter.validate_quote_order('NVDA',.0652,'sell',self.quote,2)
        self.assertEqual(safe_reason(caught.exception),str(caught.exception))
        result=adapter.place_order('NVDA','sell',.0652,183.74,2,'bound',self.quote)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['submitted'])
        client.account.assert_not_called()
        client.submit.assert_not_called()
        journal.reserve.assert_not_called()

    def test_venue_band_rejection_has_specific_reason_and_invalidates_catalog_without_read(self):
        client=Mock(api_key='verified')
        adapter=ArcusExecutionAdapter(client,None,'test',enabled=True,fee_bps=4.5)
        order=normalize_order_update(dict(orderId='rejected',status='REJECTED',
            rejectionReason='FILL_WILL_EXCEED_TRADING_BOUND',filledSize='0'))
        receipt=adapter._receipt(order,None,.0652)
        self.assertEqual(receipt['platform_reason_code'],'FILL_WILL_EXCEED_TRADING_BOUND')
        self.assertIn('交易价格边界',receipt['platform_reason'])
        client.invalidate_market_rules.assert_called_once_with()
        client.orders.assert_not_called()

