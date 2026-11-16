"""Placement-grid regressions from the public Arcus USELESS market metadata."""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from src.arcus.execution import ArcusClient, ArcusExecutionAdapter, build_order_payload
from src.arcus.pricing import PriceGridError, placement_tick, protective_limit
from src.common.market_data import utc_now
from src.execution.journal import OrderJournal
from src.execution.telemetry import safe_reason


TIERS = [dict(upToPrice='0.05', tick='0.00001'), dict(upToPrice='0.1', tick='0.00002'),
         dict(upToPrice='0.2', tick='0.00005'), dict(upToPrice='0.5', tick='0.0001'),
         dict(upToPrice='1', tick='0.0002'), dict(tick='0.0005')]
MARKET = dict(market_id=66, symbol='USELESS-USD', tick_size='0.00001',
              step_size='0.0001', tick_tiers=TIERS)


class ArcusPriceTierTests(unittest.TestCase):
    def test_useless_limit_uses_current_tier_in_both_directions(self):
        self.assertEqual(protective_limit('.12345', 'buy', '2.17', '.00001', TIERS), '0.12345')
        self.assertEqual(protective_limit('.12345', 'sell', '2.17', '.00001', TIERS), '0.12345')
        self.assertEqual(protective_limit('.12307', 'buy', 0, '.00001', TIERS), '0.12305')
        self.assertEqual(protective_limit('.12307', 'sell', 0, '.00001', TIERS), '0.1231')

    def test_exclusive_upper_bound_and_rounding_across_uneven_bands(self):
        tiers = [dict(upToPrice='1', tick='.01'), dict(tick='.03')]
        self.assertEqual(placement_tick('1', '.01', tiers), Decimal('.03'))
        # Rounding on the upper-band grid crosses down into the lower band.
        self.assertEqual(protective_limit('1', 'buy', 0, '.01', tiers), '0.99')
        # Rounding on the lower-band grid crosses up into the upper band.
        self.assertEqual(protective_limit('.999', 'sell', 0, '.01', tiers), '1.02')

    def test_grid_alignment_never_widens_existing_budget_at_any_boundary(self):
        prices = ['.00001', '.04999', '.05', '.050001', '.099999', '.1', '.100001',
                  '.12345', '.199999', '.2', '.49999', '.5', '.99999', '1', '1.00001', '2']
        for price in prices:
            for slip in (0, 2, 10, 100):
                for side in ('buy', 'sell'):
                    with self.subTest(price=price, slip=slip, side=side):
                        limit = Decimal(protective_limit(price, side, slip, '.00001', TIERS))
                        bound = Decimal(price) * (1 + Decimal(slip if side == 'buy' else -slip) / 10000)
                        self.assertEqual(limit % placement_tick(limit, '.00001', TIERS), 0)
                        self.assertEqual(limit % Decimal('.00001'), 0)
                        self.assertLessEqual(limit, bound) if side == 'buy' else self.assertGreaterEqual(limit, bound)

    def test_malformed_tiers_fail_closed_without_falling_back_to_base_tick(self):
        cases = [[], {}, [dict(tick='0')], [dict(tick='NaN')], [dict(tick=True)],
                 [dict(upToPrice='1', tick='.00001')],
                 [dict(tick='.00001'), dict(tick='.00005')],
                 [dict(upToPrice='2', tick='.00001'), dict(upToPrice='1', tick='.00002'), dict(tick='.00005')],
                 [dict(tick='.000015')]]
        for tiers in cases:
            with self.subTest(tiers=tiers), self.assertRaisesRegex(PriceGridError, '规则无效'):
                protective_limit('.12345', 'buy', 2, '.00001', tiers)

    def test_signature_still_uses_base_tick_not_placement_tick(self):
        typed, body, _ = build_order_payload('0xTest', 0, 66, 'buy', '.12345', 97,
            '.00001', '.0001', 'example', tick_tiers=TIERS)
        self.assertEqual(json.loads(typed)['p'], 12345)
        self.assertEqual(body['price'], '0.12345')
        self.assertEqual(body['quantity'], '97')
        with self.assertRaisesRegex(PriceGridError, '不符合当前价格档位'):
            build_order_payload('0xTest', 0, 66, 'buy', '.12347', 97,
                '.00001', '.0001', 'example', tick_tiers=TIERS)

    def test_market_catalog_retains_tiers_and_reuses_cache(self):
        client = ArcusClient.__new__(ArcusClient)
        client._markets, client._markets_at = {}, 0
        client._get = Mock(return_value={'markets': [dict(marketDisplayName='USELESS-USD', marketId=66,
            tickSize='.00001', stepSize='.0001', tickTiers=TIERS)]})
        self.assertEqual(client.market('USELESS')['tick_tiers'], TIERS)
        self.assertEqual(client.market('USELESS')['tick_size'], '.00001')
        client._get.assert_called_once_with('/v1/markets')

    def test_client_checks_placement_grid_before_signing_or_sending(self):
        client = ArcusClient.__new__(ArcusClient)
        client.market = Mock(return_value=MARKET)
        client.wallet_address, client.account_index = '0xTest', 0
        client.ws, client.signing_key, client.api_key, client.order_timeout = Mock(), Mock(), 'test', 1
        with self.assertRaises(PriceGridError):
            client.submit('USELESS', 'buy', 97, '.12347', 'example')
        client.ws.rpc.assert_not_called()


class ArcusTierOrderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.client = Mock(api_key='verified', market=Mock(return_value=MARKET))
        self.client.submit.return_value = dict(accepted=False, error='fixture')
        self.journal = OrderJournal(Path(temp.name) / 'orders.sqlite')
        self.adapter = ArcusExecutionAdapter(self.client, self.journal, 'test', enabled=True, fee_bps=0)
        self.quote = dict(symbol='USELESS', size=97, bid=.12345, ask=.12345,
                          updated_at=utc_now(), executable=True, instrument_type='perpetual')

    def test_entry_and_reduce_only_close_share_protected_tier_rounding(self):
        for reduce_only in (False, True):
            for side in ('buy', 'sell'):
                with self.subTest(reduce_only=reduce_only, side=side), patch.object(self.adapter, 'validate_order'):
                    client_id = f'tier-{side}-{reduce_only}'
                    self.adapter.place_order('USELESS', side, 97, .12345, 2.17, client_id,
                                             self.quote, reduce_only=reduce_only)
                    self.client.submit.assert_called_with('USELESS', side, 97, '0.12345', client_id, reduce_only)
        with self.journal.connect() as db:
            intents = [json.loads(r['intent']) for r in db.execute('SELECT intent FROM orders')]
        self.assertEqual(len(intents), 4)
        for intent in intents:
            self.assertEqual(intent['price'], '0.12345')
            self.assertEqual(intent['size'], '97')
            self.assertEqual(intent['slippage_bps'], '2.17')

    def test_no_marketable_grid_point_blocks_preflight_and_proves_unsent(self):
        self.quote.update(bid=.12307, ask=.12307)
        for side in ('buy', 'sell'):
            with self.subTest(side=side):
                with self.assertRaisesRegex(PriceGridError, '没有符合预算'):
                    self.adapter.validate_quote_order('USELESS', 97, side, self.quote, 0)
                result = self.adapter.place_order('USELESS', side, 97, .12307, 0, 'no-grid', self.quote)
                self.assertFalse(result['submitted'])
                self.assertTrue(result['confirmed'])
                self.assertEqual(result['filled_size'], 0)
                self.assertEqual(safe_reason(ValueError(result['error'])), result['error'])
        self.client.submit.assert_not_called()
        self.assertEqual(self.journal.summary(), [])

    def test_metadata_change_before_signature_is_a_known_unsent_rejection(self):
        self.client.submit.side_effect = PriceGridError('Arcus 订单价格不符合当前价格档位步长')
        with patch.object(self.adapter, 'validate_order'):
            result = self.adapter.place_order('USELESS', 'buy', 97, .12345, 2, 'changed', self.quote)
        self.assertFalse(result['submitted'])
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.journal.summary()[0]['state'], 'terminal')


if __name__ == '__main__':
    unittest.main()
