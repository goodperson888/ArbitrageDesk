import unittest
from unittest.mock import AsyncMock

from src.api_v2.app import _book_impact_bps, build_opportunities
from src.api_v2.models import Instrument, QuoteSnapshot
from src.api_v2.public_adapters import OKXSwapAdapter


class OpportunityCostTests(unittest.TestCase):
    def quotes(self):
        def quote(venue, bid, ask, **metadata):
            return QuoteSnapshot(venue=venue, symbol='BTC', venue_symbol='BTC',
                                 instrument_type='perpetual', source='test',
                                 bid=bid, ask=ask, stale=False, age_seconds=0,
                                 metadata=metadata)
        return {'buy': quote('buy', 99, 100, ask_size_base=3),
                'sell': quote('sell', 101, 102, bid_size_base=2)}

    def row(self, quotes, notional=1000):
        return next(row for row in build_opportunities('BTC', [('buy', 'sell')], quotes,
                                                       record=False, notional=notional) if row.buy_venue == 'buy')

    def test_fresh_top_of_book_covers_both_equal_quantity_legs(self):
        quotes = self.quotes()
        quotes['buy'].metadata['taker_fee_bps'] = 5
        quotes['sell'].metadata['taker_fee_bps'] = 6
        for notional in (150, 200):  # quantity 1.5, then exactly 2 base
            with self.subTest(notional=notional):
                row = self.row(quotes, notional)
                self.assertEqual(row.wear_breakdown['slippage_bps'], 0)
                self.assertAlmostEqual(row.wear_bps, 11.06)
                self.assertAlmostEqual(row.net_bps, 88.94)

    def test_top_size_cannot_cover_a_smaller_sell_notional_quantity(self):
        quotes = self.quotes()
        quotes['buy'].metadata['taker_fee_bps'] = 0
        quotes['sell'].metadata.update(taker_fee_bps=0, bid_size_base=1.99)
        # The sell leg also needs 2 base, not 200 / 101 base.
        row = self.row(quotes, 200)
        self.assertIsNone(row.wear_breakdown['slippage_bps'])
        self.assertIsNone(row.wear_bps)
        self.assertAlmostEqual(row.net_bps, 100)

    def test_top_fallback_needs_fresh_valid_price_and_correct_side_size(self):
        base = self.quotes()['buy']
        variants = [base.model_copy(update={'stale': True}),
                    base.model_copy(update={'age_seconds': 10.01}),
                    base.model_copy(update={'age_seconds': None}),
                    base.model_copy(update={'age_seconds': -1}),
                    base.model_copy(update={'ask': None}),
                    base.model_copy(update={'ask': float('nan')})]
        for size in (None, True, -1, 0, float('nan'), float('inf')):
            variants.append(base.model_copy(update={'metadata': {'ask_size_base': size, 'bid_size_base': 100}}))
        for quote in variants:
            with self.subTest(quote=quote):
                self.assertIsNone(_book_impact_bps(quote, 'buy', 1))

    def test_full_depth_takes_precedence_over_bbo_fallback(self):
        quote = self.quotes()['buy']
        quote.metadata['book_asks'] = [{'price': 100, 'size': 1}, {'price': 101, 'size': 2}]
        self.assertAlmostEqual(_book_impact_bps(quote, 'buy', 2), 50)
        self.assertIsNone(_book_impact_bps(quote, 'buy', 4))
        quote.stale = True
        self.assertIsNone(_book_impact_bps(quote, 'buy', 2))

    def test_sell_top_uses_bid_quantity_and_empty_depth_can_use_fresh_bbo(self):
        quote = self.quotes()['sell']
        quote.metadata['book_bids'] = []
        self.assertEqual(_book_impact_bps(quote, 'sell', 2), 0)
        self.assertIsNone(_book_impact_bps(quote, 'sell', 2.01))
        quote.metadata = {'ask_size_base': 100}
        self.assertIsNone(_book_impact_bps(quote, 'sell', 1))

    def test_capacity_limited_by_sell_leg_and_unknown_costs_not_zero(self):
        row = self.row(self.quotes())
        self.assertEqual(row.max_base_amount, 2)
        self.assertEqual(row.max_notional_usdc, 200)
        self.assertIsNone(row.net_bps)
        self.assertIsNone(row.wear_bps)

    def test_net_spread_is_available_with_verified_fees_before_slippage(self):
        quotes = self.quotes()
        quotes['buy'].metadata['taker_fee_bps'] = 0
        quotes['sell'].metadata['taker_fee_bps'] = 0
        row = self.row(quotes)
        self.assertAlmostEqual(row.net_bps, 100.0)
        self.assertEqual(row.wear_breakdown['fees_bps'], 0.0)
        self.assertIsNone(row.wear_breakdown['slippage_bps'])
        self.assertIsNone(row.wear_bps)

    def test_open_costs_normalized_to_buy_notional(self):
        quotes = self.quotes()
        quotes['buy'].metadata.update(taker_fee_bps=5, slippage_bps=1)
        quotes['sell'].metadata.update(taker_fee_bps=6, slippage_bps=2)
        row = self.row(quotes)
        self.assertAlmostEqual(row.wear_breakdown['fees_bps'], 11.06)
        self.assertAlmostEqual(row.wear_bps, 14.08)
        # When both fees and same-size order-book impact are known, net spread
        # subtracts the complete verified entry wear.
        self.assertAlmostEqual(row.net_bps, 85.92)
        self.assertIsNone(row.wear_breakdown['funding_bps'])

    def test_missing_invalid_or_stale_capacity_is_unknown(self):
        for size in (None, float('nan'), float('inf'), -1, True):
            quotes = self.quotes()
            quotes['sell'].metadata['bid_size_base'] = size
            self.assertIsNone(self.row(quotes).max_notional_usdc)
        quotes = self.quotes()
        quotes['sell'].stale = True
        self.assertIsNone(self.row(quotes).max_notional_usdc)
        self.assertIn('过期', self.row(quotes).depth_reason)

    def test_empty_level_means_zero_capacity(self):
        quotes = self.quotes()
        quotes['sell'].metadata['bid_size_base'] = 0
        self.assertEqual(self.row(quotes).max_notional_usdc, 0)


class OKXDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_and_bulk_convert_contracts_using_catalog(self):
        adapter = OKXSwapAdapter()
        adapter.list_instruments = AsyncMock(return_value=[Instrument(
            venue='okx', venue_symbol='BTC-USDT-SWAP', canonical_symbol='BTC-PERP',
            base_asset='BTC', metadata={'ct_val': '0.01', 'ct_val_ccy': 'BTC'})])
        adapter._get = AsyncMock(return_value={'data': [{
            'instId': 'BTC-USDT-SWAP', 'bidPx': '100', 'askPx': '101',
            'bidSz': '25', 'askSz': '10'}]})
        try:
            single = await adapter._get_quote('BTC')
            bulk = (await adapter._get_bulk_quotes(['BTC']))['BTC']
            for row in (single, bulk):
                self.assertEqual(row.metadata['bid_size_base'], 0.25)
                self.assertEqual(row.metadata['ask_size_base'], 0.1)
            adapter.list_instruments.return_value = []
            row = await adapter._get_quote('BTC')
            self.assertIsNone(row.metadata['ask_size_base'])
        finally:
            await adapter.close()
