import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock as Mock,patch
from src.execution.browser_launcher import open_browser

class BrowserLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.page={'id':'test','type':'page','url':'https://omni.variational.io/perpetual/ETH'}
    def opener(self):
        opener=Mock();response=Mock();response.read.return_value=b'{"id":"new"}'
        opener.open.return_value.__enter__.return_value=response
        return opener
    def test_reuse_does_not_launch_or_open_duplicate(self):
        opener=self.opener()
        with patch('src.execution.browser_launcher.targets',return_value=[self.page]),patch('src.execution.browser_launcher.build_opener',return_value=opener),patch('src.execution.browser_launcher.subprocess.Popen') as launch:
            result=open_browser(self.root,'127.0.0.1:9222','ETH')
            self.assertTrue(result['reused']);self.assertFalse(result['orders_submitted']);launch.assert_not_called()
            self.assertIn('/json/activate/test',opener.open.call_args.args[0])
    def test_missing_market_opens_only_official_page(self):
        opener=self.opener()
        with patch('src.execution.browser_launcher.targets',return_value=[]),patch('src.execution.browser_launcher.build_opener',return_value=opener):
            open_browser(self.root,'127.0.0.1:9222','BTC')
        request=opener.open.call_args_list[0].args[0]
        self.assertEqual(request.method,'PUT');self.assertIn('omni.variational.io',request.full_url)
    def test_new_browser_uses_isolated_profile_and_local_debugging(self):
        probe=Mock();probe.__enter__.return_value.connect_ex.return_value=1
        with patch('src.execution.browser_launcher.targets',side_effect=[OSError(),[self.page]]),patch('src.execution.browser_launcher.socket.socket',return_value=probe),patch('src.execution.browser_launcher.chrome_binary',return_value='/chrome'),patch('src.execution.browser_launcher.subprocess.Popen') as launch,patch('src.execution.browser_launcher.build_opener',return_value=self.opener()):
            result=open_browser(self.root,'127.0.0.1:9222','ETH')
        args=launch.call_args.args[0]
        self.assertIn('--remote-debugging-address=127.0.0.1',args)
        self.assertIn('--user-data-dir='+str((self.root/'data/variational-browser').resolve()),args)
        self.assertFalse(result['reused'])
    def test_duplicates_and_remote_address_rejected(self):
        with patch('src.execution.browser_launcher.targets',return_value=[self.page,self.page]),self.assertRaises(ValueError):
            open_browser(self.root,'127.0.0.1:9222','ETH')
        with self.assertRaises(ValueError):open_browser(self.root,'0.0.0.0:9222','ETH')
    def test_occupied_nonbrowser_port_is_never_replaced(self):
        probe=Mock();probe.__enter__.return_value.connect_ex.return_value=0
        with patch('src.execution.browser_launcher.targets',side_effect=ValueError()),patch('src.execution.browser_launcher.socket.socket',return_value=probe),patch('src.execution.browser_launcher.subprocess.Popen') as launch,self.assertRaises(ValueError):
            open_browser(self.root,'127.0.0.1:9222','ETH')
        launch.assert_not_called()
