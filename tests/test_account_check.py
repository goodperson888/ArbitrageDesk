import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
from src.lighter.account_check import check, ReadOnlyCheckError
from src.web import local_credentials


class AccountCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_and_zero_fee_read_only(self):
        sdk=Mock();sdk.close=AsyncMock();sdk.check_client.return_value=None
        sdk.create_auth_token_with_expiry.return_value=('test-auth-token',None)
        sdk.order_api.account_active_orders=AsyncMock(return_value=SimpleNamespace(to_dict=lambda:{'code':200,'orders':[]}))
        account=Mock();account.account_limits=AsyncMock(return_value=SimpleNamespace(to_dict=lambda:{'code':200,'current_taker_fee_tick':0}))
        module=SimpleNamespace(SignerClient=Mock(return_value=sdk), AccountApi=Mock(return_value=account))
        public=Mock();public.base_url='https://mainnet.zklighter.elliot.ai'
        public.client.get.return_value=({'api_keys':[{'api_key_index':4}]},None)
        public.get_market.return_value=({'market_id':0,'min_quote_amount':'10','min_base_amount':'.002'},None)
        with patch.dict('sys.modules',{'lighter':module}),patch('src.lighter.account_check.LighterApiTrader',return_value=public):
            result=await check(1,4,'synthetic-test-key','http://127.0.0.1:7890')
            self.assertTrue(result['authenticated']);self.assertEqual(result['taker_fee_bps'],0)
            self.assertFalse(result['orders_submitted'])
            self.assertNotIn('test-auth-token',str(result))
            sdk.create_order.assert_not_called()
            sdk.check_client.assert_not_called()
            sdk.check_client.return_value='secret raw error'
            with self.assertRaises(ReadOnlyCheckError) as caught:
                await check(1,4,'synthetic-test-key',None)
            self.assertEqual(caught.exception.code,23)
            self.assertNotIn('secret raw error',str(caught.exception))
            sdk.check_client.return_value='private key does not match the one on Lighter. ownPubKey: secret'
            with self.assertRaises(ReadOnlyCheckError) as caught:
                await check(1,4,'synthetic-test-key',None)
            self.assertEqual(caught.exception.code,27)
            self.assertNotIn('secret',str(caught.exception))

    async def test_proxy_path_maps_invalid_signature_to_key_error(self):
        sdk=Mock();sdk.close=AsyncMock();sdk.create_auth_token_with_expiry.return_value=('test-auth-token',None)
        sdk.order_api.account_active_orders=AsyncMock(side_effect=Exception('HTTP response body: invalid signature'))
        public=Mock();public.base_url='https://api.rh.lighter.elliot.ai'
        public.client.get.return_value=({'api_keys':[{'api_key_index':4}]},None)
        public.get_market.return_value=({'market_id':0,'min_quote_amount':'10','min_base_amount':'.002'},None)
        module=SimpleNamespace(SignerClient=Mock(return_value=sdk), AccountApi=Mock())
        with patch.dict('sys.modules',{'lighter':module}),patch('src.lighter.account_check.LighterApiTrader',return_value=public):
            with self.assertRaises(ReadOnlyCheckError) as caught:
                await check(1,4,'synthetic-test-key','http://127.0.0.1:12000')
            self.assertEqual(caught.exception.code,27)

    async def test_bad_index_rejected_before_signer(self):
        module=SimpleNamespace(SignerClient=Mock())
        public=Mock();public.client.get.return_value=({'api_keys':[{'api_key_index':4}]},None)
        public.get_market.return_value=({},None)
        with patch.dict('sys.modules',{'lighter':module}),patch('src.lighter.account_check.LighterApiTrader',return_value=public):
            with self.assertRaises(ReadOnlyCheckError) as caught:
                await check(1,5,'synthetic-test-key',None)
            self.assertEqual(caught.exception.code,21)
            module.SignerClient.assert_not_called()

    async def test_public_key_is_not_accepted_as_private_key(self):
        module=SimpleNamespace(SignerClient=Mock())
        public=Mock();public.client.get.return_value=({'api_keys':[{'api_key_index':4,'public_key':'ab'*40}]},None)
        public.get_market.return_value=({},None)
        with patch.dict('sys.modules',{'lighter':module}),patch('src.lighter.account_check.LighterApiTrader',return_value=public):
            with self.assertRaises(ReadOnlyCheckError) as caught:await check(1,4,'ab'*40,None)
            self.assertEqual(caught.exception.code,28)
            module.SignerClient.assert_not_called()


class ChildDiagnosticTests(unittest.TestCase):
    def test_raw_process_output_never_becomes_public_error(self):
        from pathlib import Path
        with patch.object(Path,'exists',return_value=True),patch.object(local_credentials.subprocess,'run',return_value=SimpleNamespace(returncode=23,stdout='secret-test-output')):
            with self.assertRaises(local_credentials.CredentialError) as caught:
                local_credentials.verify(Path('/test'),{},None)
            self.assertNotIn('secret-test-output',str(caught.exception))
            self.assertIn('密钥匹配',str(caught.exception))
