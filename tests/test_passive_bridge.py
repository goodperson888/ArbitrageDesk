import json
import unittest
from unittest.mock import Mock,patch
from src.variational.passive_bridge import PassiveQuoteBridge,safe_quote,market_target,QUOTE_URL,is_quote_url
from src.common.market_data import utc_now
from src.variational.quote_bridge import QuoteBridge

class PassiveTests(unittest.TestCase):
    def request(self):return {'instrument':{'underlying':'ETH','instrument_type':'perpetual_future'},'qty':'.0045','secret':'never-forward'}
    def body(self):return {'bid':'2650','ask':'2651','timestamp':utc_now(),'quote_id':'secret-id','account_id':'secret-account','margin_requirements':{'estimated_fees_bid':0,'estimated_fees_ask':0}}
    def test_payload_is_allowlisted_and_accepted(self):
        payload=safe_quote(self.request(),self.body())
        self.assertNotIn('secret',json.dumps(payload))
        bridge=QuoteBridge();bridge.ingest(payload)
        self.assertEqual(bridge.snapshot('ETH')['quote_size'],.0045)
        self.assertFalse(bridge.snapshot('ETH')['executable'])
    def test_reject_foreign_pages_and_symbols(self):
        for url in ('http://omni.variational.io/perpetual/ETH','https://evil.test/perpetual/ETH','https://omni.variational.io.evil.test/perpetual/ETH'):
            self.assertFalse(market_target({'type':'page','url':url}))
        self.assertTrue(market_target({'type':'page','url':'https://omni.variational.io/perpetual/ETH'}))
        with self.assertRaises(ValueError):safe_quote({'instrument':{'underlying':'OTHER'}},self.body())

    def test_quote_endpoint_allows_query_parameters_and_envelopes(self):
        self.assertTrue(is_quote_url(QUOTE_URL+'?cache_bust=1'))
        self.assertFalse(is_quote_url('https://omni.variational.io.evil.test/api/quotes/indicative'))
        payload=safe_quote(self.request(),{'quote':self.body()})
        self.assertEqual(payload['bid'],'2650')

    def test_missing_quote_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'quote_fields_missing'):
            safe_quote(self.request(), {'instrument': self.request()['instrument']})
    def test_listener_only_enables_network_and_reads_one_quote_body(self):
        observer=PassiveQuoteBridge(QuoteBridge());sock=Mock()
        events=[{'method':'Network.requestWillBeSent','params':{'requestId':'q','request':{'url':QUOTE_URL,'method':'POST','postData':json.dumps(self.request())}}},
            {'method':'Network.responseReceived','params':{'requestId':'q','response':{'url':QUOTE_URL,'status':200}}},
            {'method':'Network.loadingFinished','params':{'requestId':'q'}},
            {'id':2,'result':{'body':json.dumps(self.body())}},None]
        sock.recv.side_effect=[json.dumps(x) if x else '' for x in events]
        with patch('src.variational.passive_bridge.websocket.create_connection',return_value=sock):
            observer._listen('page',{'webSocketDebuggerUrl':'ws://127.0.0.1:9222/devtools/page/page','url':'https://omni.variational.io/perpetual/ETH'})
        commands=[json.loads(call.args[0])['method'] for call in sock.send.call_args_list]
        self.assertEqual(commands,['Network.enable','Network.getResponseBody'])
        self.assertEqual(observer.received,1)
        self.assertIsNotNone(observer.bridge.snapshot('ETH'))
