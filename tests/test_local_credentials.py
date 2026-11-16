import unittest
from pathlib import Path
from unittest.mock import patch
from src.web import app as web
from src.web import local_credentials as credentials
from src.execution.browser_session import select_market_tab, valid_debugger_address
import test_web


class CredentialTests(unittest.TestCase):
    def setUp(self):
        test_web.WebTests.setUp(self)
        # Credential tests must use their own runtime lock even when the
        # user's real execution worker is running outside this test suite.
        p = patch.object(web, 'ROOT', Path(self.temp.name))
        p.start(); self.patches.append(p)
        profile = Path(self.temp.name)/'execution.json'
        profile.write_text('{"live_enabled": false, "account_verified": false}')
        p = patch.object(web, 'execution_profile_path', profile)
        p.start(); self.patches.append(p)
        self.secret_path = Path(self.temp.name)/'.env'
        self.payload = {'account_index': 123, 'key_index': 4, 'private_key': 'ab'*40}
        for name, value in {'credentials_path': self.secret_path, 'credentials_verified': False, 'lighter_verification': {}}.items():
            p = patch.object(web, name, value); p.start(); self.patches.append(p)
        self.headers = {'X-Local-Token': web.credentials_token}

    tearDown = test_web.WebTests.tearDown

    def test_secret_security_and_durable_save(self):
        self.secret_path.write_text("OTHER='keep'\nLIGHTER_API_PRIVATE_KEY='old'\n")
        with patch.object(credentials, 'verify', return_value={'authenticated':True,'orders_submitted':False}) as verify:
            response = self.client.post('/api/local-credentials', json=self.payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        self.assertEqual(self.secret_path.stat().st_mode & 0o777, 0o600)
        self.assertIn("OTHER='keep'", self.secret_path.read_text())
        self.assertEqual(self.secret_path.read_text().count('LIGHTER_API_PRIVATE_KEY='), 1)
        self.assertFalse(web.trading_status['running'])
        for endpoint in ('/api/local-credentials', '/api/config', '/api/status'):
            result = self.client.get(endpoint)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(self.payload['private_key'], result.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/local-credentials').headers['Cache-Control'], 'no-store')

    def test_untrusted_requests_never_verify_or_save(self):
        with patch.object(credentials, 'verify') as verify:
            cases = [{}, {'headers': {'X-Local-Token': 'wrong'}},
                     {'headers': {**self.headers, 'Origin': 'https://evil.example'}},
                     {'headers': self.headers, 'base_url': 'http://evil.example'},
                     {'headers': self.headers, 'environ_overrides': {'REMOTE_ADDR': '10.0.0.1'}}]
            for extra in cases:
                self.assertEqual(self.client.post('/api/local-credentials', json=self.payload, **extra).status_code, 403)
            verify.assert_not_called()
        self.assertFalse(self.secret_path.exists())
        self.assertEqual(self.client.get('/', base_url='http://evil.example').status_code, 403)

    def test_failure_preserves_existing_secret_and_hides_exception(self):
        self.secret_path.write_text("OTHER='keep'\n")
        with patch.object(credentials, 'verify', side_effect=RuntimeError(self.payload['private_key'])):
            response = self.client.post('/api/local-credentials', json=self.payload, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(self.payload['private_key'], response.get_data(as_text=True))
        self.assertEqual(self.secret_path.read_text(), "OTHER='keep'\n")

    def test_invalid_credentials_do_not_verify(self):
        with patch.object(credentials, 'verify') as verify:
            for change in ({'key_index': 3}, {'account_index': True}, {'private_key': 'ab'*32}, {'private_key': 'x'*80}):
                response = self.client.post('/api/local-credentials', json={**self.payload, **change}, headers=self.headers)
                self.assertEqual(response.status_code, 400)
            verify.assert_not_called()

    def test_recheck_uses_saved_key_without_returning_or_rewriting_it(self):
        credentials.save(self.secret_path,credentials.validate(self.payload))
        before=self.secret_path.read_text()
        with patch.object(credentials,'verify',return_value={'authenticated':True,'orders_submitted':False}) as verify:
            result=self.client.post('/api/local-credentials/check',json={},headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(verify.call_args.args[1]['LIGHTER_API_PRIVATE_KEY'],self.payload['private_key'])
        self.assertEqual(before,self.secret_path.read_text())
        self.assertNotIn(self.payload['private_key'],self.client.get('/api/local-credentials').get_data(as_text=True))


class FakeBrowser:
    def __init__(self, urls):
        self.urls = urls
        self.current_window_handle = 'start'
        self.window_handles = list(urls)
        self.switch_to = self

    def window(self, handle):
        self.current_window_handle = handle

    @property
    def current_url(self):
        return self.urls[self.current_window_handle]


class BrowserSelectionTests(unittest.TestCase):
    def test_unique_exact_market_found_without_navigation(self):
        driver = FakeBrowser({'start': 'https://example.com', 'eth': 'https://omni.variational.io/perpetual/ETH'})
        self.assertEqual(select_market_tab(driver, 'ETH'), 'eth')

    def test_duplicates_and_spoofs_fail_and_restore(self):
        for urls in ({'start': 'https://omni.variational.io.evil.com/perpetual/ETH'},
                     {'start': 'https://omni.variational.io/perpetual/ETH', 'dup': 'https://omni.variational.io/perpetual/ETH'}):
            driver = FakeBrowser(urls)
            with self.assertRaises(ValueError): select_market_tab(driver, 'ETH')
            self.assertEqual(driver.current_window_handle, 'start')

    def test_debugger_address_only_loopback_and_valid_port(self):
        self.assertTrue(valid_debugger_address('127.0.0.1:9222'))
        for address in (None, '', '127.0.0.1:0', '127.0.0.1:99999', '127.0.0.1:9222@evil.com', '0.0.0.0:9222'):
            self.assertFalse(valid_debugger_address(address))
