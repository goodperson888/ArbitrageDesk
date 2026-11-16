import unittest

from src.variational.manual_events import normalize_manual_event, safe_path


class ManualEventsTests(unittest.TestCase):
    def test_only_variational_allowlisted_paths_are_accepted(self):
        self.assertEqual(safe_path('https://omni.variational.io/api/trades?x=1'), '/api/trades')
        self.assertIsNone(safe_path('https://omni.variational.io/api/auth'))
        self.assertIsNone(safe_path('https://evil.example/api/trades'))

    def test_submit_request_is_redacted(self):
        event = normalize_manual_event({
            'url': 'https://omni.variational.io/api/orders/new/market',
            'method': 'POST', 'status': 200, 'phase': 'request',
            'body': {'quote_id': 'secret', 'side': 'buy', 'max_slippage': 0.005,
                     'is_reduce_only': False, 'wallet': 'must-not-leak'},
        })
        self.assertEqual(event['side'], 'buy')
        self.assertTrue(event['quote_id_present'])
        self.assertNotIn('secret', json_text(event))
        self.assertNotIn('wallet', json_text(event))

    def test_order_and_trade_snapshot_keeps_status_and_numeric_fields(self):
        event = normalize_manual_event({
            'url': 'https://omni.variational.io/api/orders/v2',
            'method': 'GET', 'status': 200, 'phase': 'response',
            'body': {'result': [{'instrument': {'underlying': 'ETH'},
                                 'status': 'cleared', 'clearing_status': 'success_trades_booked_into_pool',
                                 'qty': '0.00036', 'price': '2717.7', 'order_id': 'private'}]},
        })
        self.assertEqual(event['rows'][0]['status'], 'cleared')
        self.assertEqual(event['rows'][0]['qty'], 0.00036)
        self.assertNotIn('private', json_text(event))

    def test_actual_nested_positions_and_empty_snapshot_are_distinct(self):
        base = dict(url='https://omni.variational.io/api/positions', method='GET', status=200, phase='response')
        event = normalize_manual_event({**base, 'body': [{'position_info': {
            'instrument': {'underlying': 'ETH'}, 'qty': '.00036', 'avg_entry_price': '2712.7'}, 'upnl': None}]})
        self.assertEqual(event['rows'][0]['qty'], .00036)
        self.assertTrue(event['complete'])
        self.assertNotIn('upnl', event['rows'][0])
        self.assertTrue(normalize_manual_event({**base, 'body': []})['complete'])
        self.assertFalse(normalize_manual_event({**base, 'body': {'rows': []}})['complete'])
        self.assertIsNone(normalize_manual_event({**base, 'phase': 'request'}))
        unsupported = normalize_manual_event({**base, 'body': [{'position_info': {
            'instrument': {'underlying': 'SOL'}, 'qty': '1'}}]})
        self.assertFalse(unsupported['complete'])

    def test_digest_survives_browser_and_server_redaction(self):
        import hashlib
        digest = hashlib.sha256(b'quote-1').hexdigest()[:16]
        base = dict(url='https://omni.variational.io/api/quotes/accept', method='POST', status=0, phase='request')
        direct = normalize_manual_event({**base, 'body': {'quote_id': 'quote-1'}})
        bridge = normalize_manual_event({**base, 'body': {'quote_id_digest': digest}})
        self.assertEqual(direct['quote_id_digest'], bridge['quote_id_digest'])
        self.assertNotIn('quote-1', json_text(direct))

    def test_pagination_and_truncation_cannot_prove_complete_snapshot(self):
        base = dict(url='https://omni.variational.io/api/orders/v2', method='GET', status=200, phase='response')
        for body in [{'rows': [], 'row_count': 30, 'complete': True},
                     {'result': [], 'pagination': {'next_page': {'offset': 20}}},
                     {'result': [], 'pagination': {'next_page': None, 'last_page': {'offset': 20}}}]:
            self.assertFalse(normalize_manual_event({**base, 'body': body})['complete'])

    def test_transfer_capture_is_strictly_allowlisted_and_redacted(self):
        event = normalize_manual_event({
            'url': 'https://omni.variational.io/api/transfers',
            'method': 'GET', 'status': 200, 'phase': 'response',
            'body': [{'id': 'private-transfer-id', 'rfq_id': 'private-rfq', 'asset': 'USDC', 'fee_asset': 'USDC',
                      'amount': '0.0001', 'fee_amount': '0.00001', 'funding_payment': '-0.00002',
                      'status': 'settled', 'wallet_address': 'private-wallet', 'api_key': 'private-key'}],
        })
        row = event['rows'][0]
        self.assertEqual(event['kind'], 'settlement_response')
        self.assertEqual(row['asset'], 'USDC')
        self.assertEqual(row['fee_asset'], 'USDC')
        self.assertEqual(row['fee_amount'], .00001)
        self.assertIn('id_digest', row)
        serialized = json_text(event)
        for secret in ('private-transfer-id', 'private-rfq', 'private-wallet', 'private-key'):
            self.assertNotIn(secret, serialized)
        self.assertIsNone(normalize_manual_event({
            'url': 'https://omni.variational.io/api/transfers', 'method': 'GET', 'status': 0,
            'phase': 'request', 'body': None,
        }))


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()
