import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
import requests
from src.common.market_data import MarketDataError, number, age_seconds
from src.variational.public_client import VariationalPublicClient
from src.lighter.api_trader import LighterApiTrader


def session_for(data):
    session = Mock()
    session.headers = {}
    session.get.return_value.json.return_value = data
    return session


def stats():
    return {'listings': [
        {'ticker': 'BTC', 'name': 'Bitcoin', 'mark_price': '83000',
         'quotes': {'updated_at': (datetime.now(timezone.utc)-timedelta(seconds=610)).isoformat(),
                    'size_1k': {'bid': '82999', 'ask': '83001'}}},
        {'ticker': 'US100S', 'name': 'Swap on US Non-Financial 100', 'mark_price': '30633.55'}]}


def lighter_stats():
    return {'code': 200, 'order_book_details': [
        {'symbol': 'BTC', 'market_type': 'perp', 'market_id': 1, 'mark_price': '83020',
         'last_trade_price': '83021', 'supported_size_decimals': 5, 'status': 'active'},
        {'symbol': 'US100', 'market_type': 'perp', 'market_id': 181, 'mark_price': '30571',
         'last_trade_price': '30571', 'status': 'active'}]}


class PublicDataTests(unittest.TestCase):
    def test_variational_no_wallet_and_cache(self):
        session = session_for(stats())
        trader = VariationalPublicClient(session=session)
        btc = trader.get_snapshot('BTC')
        swap = trader.get_snapshot('US100')
        self.assertEqual(btc['mark_price'], 83000)
        self.assertEqual(swap['exchange_symbol'], 'US100S')
        self.assertEqual(swap['instrument_type'], 'swap')
        self.assertIsNone(swap['bid'])
        self.assertTrue(btc['quote_stale'])
        self.assertFalse(btc['executable'])
        self.assertEqual(session.get.call_count, 1)
        self.assertIsNone(btc['mark_updated_at'])

    def test_unknown_symbol_never_uses_another_market(self):
        trader = VariationalPublicClient(session=session_for(stats()))
        with self.assertRaises(MarketDataError):
            trader.get_market_price('NOT_LISTED')

    def test_expired_cache_and_network_error_does_not_reuse_price(self):
        session = session_for(stats())
        trader = VariationalPublicClient(session=session)
        trader.get_market_price('BTC')
        trader.client._cache.clear()
        session.get.side_effect = requests.Timeout('secret must not appear')
        with self.assertRaisesRegex(MarketDataError, 'Timeout') as err:
            trader.get_market_price('BTC')
        self.assertNotIn('secret', str(err.exception))
        with self.assertRaises(MarketDataError):
            trader.get_market_price('US100')
        self.assertEqual(session.get.call_count, 2)

    def test_malformed_numbers(self):
        for value in ('NaN', 'Infinity', '-1', 0, None):
            with self.subTest(value=value), self.assertRaises(MarketDataError):
                number(value, True)
        self.assertIsNone(age_seconds('2026-01-01'))
        self.assertIsNone(age_seconds(None))

    def test_lighter_book_uses_market_id_and_remaining_size(self):
        session = session_for(lighter_stats())
        book = {'code': 200, 'bids': [{'price': '100', 'remaining_base_amount': '2'},
                                     {'price': '100', 'remaining_base_amount': '3'}],
                'asks': [{'price': '101', 'remaining_base_amount': '1'}]}
        session.get.return_value.json.side_effect = [lighter_stats(), book]
        trader = LighterApiTrader(session=session)
        result = trader.get_order_book('US100')
        self.assertEqual(result['market_id'], 181)
        self.assertEqual(result['bids'], [{'price': 100, 'size': 5}])
        self.assertEqual(session.get.call_args.kwargs['params']['market_id'], 181)
        self.assertNotIn('headers', session.get.call_args.kwargs)

    def test_lighter_market_price_falls_back_to_book_midpoint(self):
        markets = lighter_stats()
        for row in markets['order_book_details']:
            row['mark_price'] = None
            row['last_trade_price'] = None
        book = {'code': 200,
                'bids': [{'price': '100', 'remaining_base_amount': '2'}],
                'asks': [{'price': '101', 'remaining_base_amount': '1'}]}
        session = session_for(markets)
        session.get.return_value.json.side_effect = [markets, book]
        trader = LighterApiTrader(session=session)
        self.assertEqual(trader.get_market_price('BTC'), 100.5)

    def test_lighter_position_funding_reads_complete_paginated_settlement(self):
        session = session_for(lighter_stats())
        funding = {'code': 200, 'position_fundings': [
            {'timestamp': 1000, 'market_id': 1, 'funding_id': 7,
             'change': '-0.0012', 'rate': '0.0001', 'position_size': '1',
             'position_side': 'long'}], 'next_cursor': None}
        session.get.return_value.json.side_effect = [lighter_stats(), funding]
        trader = LighterApiTrader(session=session, account_index=7)
        result = trader.get_position_funding('BTC', 1, 2)
        self.assertTrue(result['complete'])
        self.assertEqual(result['rows'][0]['change'], '-0.0012')
        self.assertEqual(session.get.call_args.kwargs['params']['market_ids'], '1')
        self.assertEqual(session.get.call_args.kwargs['params']['account_index'], 7)

    def test_error_response_not_success_or_fake_order(self):
        trader = LighterApiTrader(session=session_for({'code': 400, 'message': 'bad'}))
        with self.assertRaises(MarketDataError):
            trader.get_market_price('BTC')
        self.assertFalse(trader.place_order('BTC', 'buy', 1)['success'])
        self.assertFalse(trader.live_ready)
