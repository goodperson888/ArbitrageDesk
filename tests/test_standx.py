import base64
import unittest

from src.standx.client import StandXClient, StandXCredentials
from src.standx.crypto import public_key, sign


class StandXCryptoTests(unittest.TestCase):
    def test_rfc8032_ed25519_vector(self):
        seed = bytes.fromhex('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60')
        self.assertEqual(public_key(seed).hex(),
                         'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
        self.assertEqual(sign(b'', seed).hex(),
                         'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
                         '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b')

    def test_body_signature_headers_are_redacted_and_bound(self):
        client = StandXClient(StandXCredentials('0x' + '11' * 20, '22' * 32))
        client.token = 'token'
        client._request_seed = bytes.fromhex('33' * 32)
        headers = client._headers(b'{"qty":"1"}', signed=True)
        self.assertEqual(headers['Authorization'], 'Bearer token')
        self.assertEqual(headers['x-request-sign-version'], 'v1')
        self.assertRegex(headers['x-request-signature'], r'^[A-Za-z0-9+/]+={0,2}$')
        self.assertNotIn('22' * 32, headers['x-request-signature'])

