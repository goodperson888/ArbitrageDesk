import base64
import json
import unittest
from scripts.inspect_variational_har import extract
from src.variational.schema_audit import safe_path


class HarAuditTests(unittest.TestCase):
    def test_identifier_path_segments_are_redacted(self):
        self.assertEqual(safe_path('https://omni.variational.io/api/orders/123456'),'/api/orders/:id')
        self.assertEqual(safe_path('https://omni.variational.io/api/orders/'+'ab'*20),'/api/orders/:id')

    def test_extract_never_copies_headers_secrets_or_identifiers(self):
        secret='sensitive-example'
        request={'url':'https://omni.variational.io/api/quotes/indicative?auth='+secret,'method':'POST',
                 'headers':[{'name':'authorization','value':secret}],
                 'postData':{'text':json.dumps({'qty':'0.004','signature':secret,'account_id':secret})}}
        response={'status':200,'content':{'text':json.dumps({'bid':'2700','ask':'2701','quote_id':secret,'token':secret})}}
        result=extract({'log':{'entries':[{'request':request,'response':response}]}})
        self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(result['samples'][0]['endpoint'],'/api/quotes/indicative')
        self.assertFalse(result['receipt_schema_verified'])
        self.assertFalse(result['orders_submitted_by_script'])

    def test_reject_other_origins_and_accept_base64(self):
        content={'encoding':'base64','text':base64.b64encode(b'{"orders":[]}').decode()}
        rows=[{'request':{'url':u,'method':'GET'},'response':{'status':200,'content':content}}
              for u in ('https://evil.example/api/orders','https://omni.variational.io/api/orders')]
        result=extract({'log':{'entries':rows}})
        self.assertEqual(len(result['samples']),1)
        self.assertEqual(result['samples'][0]['fields'][0]['count'],0)
