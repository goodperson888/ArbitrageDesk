import unittest
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.api_v2.arcus_adapter import ArcusAdapter
from src.web import local_credentials


class ArcusAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_and_bbo_quote_uses_official_top_of_book(self):
        adapter = ArcusAdapter(proxy_url=None)

        async def fake_get(path):
            if path == '/v1/markets':
                return {'markets': [{
                    'marketId': 1, 'marketDisplayName': 'BTC-USD',
                    'tickSize': '0.1', 'stepSize': '0.0001',
                    'tickTiers': [{'upToPrice':'500000','tick':'0.1'}, {'tick':'0.2'}],
                    'minOrderSize': '0.001', 'minOrderNotional': '5',
                    'oraclePrice': '65000',
                }]}
            if path == '/v1/bbo/BTC-USD':
                return {'bestBid': {'price': '64999', 'size': '0.5'},
                        'bestAsk': {'price': '65001', 'size': '0.4'},
                        'timestamp': '1791306008993277'}
            self.fail('unexpected path: ' + path)

        adapter._get = fake_get
        instruments = await adapter.list_instruments()
        quote = await adapter.get_quote('BTC')
        self.assertEqual(instruments[0].canonical_symbol, 'BTC-PERP')
        self.assertEqual(instruments[0].metadata['tick_tiers'], [{'upToPrice':'500000','tick':'0.1'}, {'tick':'0.2'}])
        self.assertEqual(quote.mark_price, 65000.0)
        self.assertEqual(quote.bid, 64999.0)
        self.assertEqual(quote.ask, 65001.0)
        self.assertEqual(quote.source, 'arcus_rest')
        self.assertEqual(quote.metadata['bid_size_base'], 0.5)
        self.assertEqual(quote.metadata['ask_size_base'], 0.4)
        self.assertFalse(quote.executable)
        await adapter.close()

    async def test_bbo_websocket_message_is_normalized(self):
        adapter = ArcusAdapter(proxy_url=None)
        adapter._market_by_id = {
            '1': type('InstrumentStub', (), {
                'venue_symbol': 'BTC-USD', 'canonical_symbol': 'BTC-PERP'
            })()
        }
        adapter._handle_ws_message({
            'channel': 'bbo', 'id': 'BTC-USD',
            'contents': {
                'bestBid': {'price': '64998', 'size': '0.2'},
                'bestAsk': {'price': '65002', 'size': '0.3'},
                'timestamp': '1791306008993277',
            },
        })
        self.assertEqual(adapter._bbo['BTC-PERP']['bid'], 64998.0)
        self.assertEqual(adapter._bbo['BTC-PERP']['ask_size'], 0.3)
        await adapter.close()


class ArcusCredentialTest(unittest.TestCase):
    def test_arcus_key_shape_is_validated_without_echoing_secret(self):
        values = local_credentials.validate({
            'wallet_address': '0xwallet', 'account_index': 0,
            'api_signing_key': 'a' * 64,
        }, 'arcus')
        self.assertEqual(values['ARCUS_ACCOUNT_INDEX'], '0')
        self.assertNotIn('api_signing_key', values)

    def test_arcus_fee_schedule_ppm_defaults_to_base_taker_rate(self):
        self.assertEqual(local_credentials._arcus_taker_fee_bps(
            {}, {'tiers': [
                {'level': 0, 'name': 'Base', 'volume_threshold': 0, 'taker_fee_ppm': 450},
                {'level': 1, 'name': 'Bronze', 'volume_threshold': 10, 'taker_fee_ppm': 380},
            ]}), 4.5)

    def test_arcus_verification_matches_registered_key_and_subaccount(self):
        private = '01' * 32
        public = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(private)).public_key().public_bytes_raw().hex()

        class Response:
            def __init__(self, status, body):
                self.status_code, self.body = status, body
                self.content = b'{}'

            def json(self):
                return self.body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        'status', request=httpx.Request('GET', 'https://api.arcus.xyz'),
                        response=httpx.Response(self.status_code))

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, url, **kwargs):
                if url.endswith('/v1/apiKeys'):
                    return Response(200, {'apiKeys': [{
                        'apiKey': public, 'accountIndex': 1,
                        'status': 'ACTIVE', 'apiWalletName': '价差'}]})
                if url.endswith('/v1/account'):
                    return Response(200, {'equity': '12', 'freeCollateral': '10',
                                          'takerFeeBps': 5})
                raise AssertionError('unexpected URL: ' + url)

        values = {'ARCUS_WALLET_ADDRESS': '0x' + 'ab' * 20,
                  'ARCUS_ACCOUNT_INDEX': '1', 'ARCUS_API_SIGNING_KEY': private}
        with patch('httpx.Client', Client):
            report = local_credentials.verify_arcus(values)
        self.assertTrue(report['api_key_match'])
        self.assertEqual(report['registered_account_index'], 1)
        self.assertIn('Subaccount #1', report['message'])

    def test_arcus_verification_reports_subaccount_mismatch(self):
        private = '02' * 32
        public = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(private)).public_key().public_bytes_raw().hex()

        class Response:
            status_code = 200
            content = b'{}'

            def json(self):
                return {'apiKeys': [{'apiKey': public, 'accountIndex': 1, 'status': 'ACTIVE'}]}

            def raise_for_status(self):
                return None

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, url, **kwargs):
                return Response()

        values = {'ARCUS_WALLET_ADDRESS': '0x' + 'cd' * 20,
                  'ARCUS_ACCOUNT_INDEX': '0', 'ARCUS_API_SIGNING_KEY': private}
        with patch('httpx.Client', Client):
            with self.assertRaisesRegex(local_credentials.CredentialError, '当前账户索引填的是 #0'):
                local_credentials.verify_arcus(values)


if __name__ == '__main__':
    unittest.main()
