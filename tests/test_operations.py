import json
import tempfile
import unittest
from pathlib import Path
from src.common.market_data import utc_now
from src.variational.capture_report import report
from src.web.diagnostics import Diagnostics


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'events.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, events):
        self.path.write_text('\n'.join(json.dumps(e) for e in events))
        return report(self.path)

    def test_old_empty_positions_never_mean_flat(self):
        data = self.write([dict(kind='snapshot_response', endpoint='/api/positions', rows=[],
                                observed_at=utc_now(), http_status=200)])
        self.assertFalse(data['position']['known'])
        self.assertFalse(data['live_verified'])

    def test_only_fresh_complete_successful_positions_are_known(self):
        event = dict(kind='snapshot_response', endpoint='/api/positions', rows=[],
                     schema_version=2, complete=True, observed_at=utc_now(), http_status=200)
        self.assertTrue(self.write([event])['position']['known'])
        for change in ({'complete': False}, {'http_status': 500}, {'observed_at': '2001-01-01T00:00:00+00:00'}):
            self.assertFalse(self.write([{**event, **change}])['position']['known'])

    def test_chain_uses_rfq_and_verifies_account_quantity_price(self):
        common=dict(company_digest='company',pool_location_digest='pool',symbol='ETH',side='buy',qty=.00036,price=2706.96)
        rows = [dict(kind='quote_response',quote_id_digest='q',qty=.00036,symbol='ETH'),
                dict(kind='submit_request', capture_id='a', quote_id_digest='q',side='buy'),
                dict(kind='submit_response', capture_id='a', rfq_id_digest='r',http_status=200),
                dict(kind='snapshot_response', endpoint='/api/orders/v2', http_status=200,
                     rows=[dict(**common,order_id_digest='o', rfq_id_digest='r',status='cleared',clearing_status='success_trades_booked_into_pool')]),
                dict(kind='snapshot_response', endpoint='/api/trades', http_status=200,
                     rows=[dict(**common,id_digest='t', source_rfq_digest='r', source_quote_digest='different', status='confirmed')])]
        self.assertTrue(self.write(rows)['chains'][0]['linked'])
        rows[-1]['rows'][0]['source_rfq_digest'] = 'different'
        self.assertFalse(self.write(rows)['chains'][0]['linked'])
        rows[-1]['rows'][0]['source_rfq_digest'] = 'r'
        rows[-1]['rows'][0]['company_digest'] = 'other'
        self.assertFalse(self.write(rows)['chains'][0]['linked'])
        rows[-1]['rows'][0]['company_digest'] = 'company'
        rows[-1]['rows'][0]['qty'] = .00072
        self.assertFalse(self.write(rows)['chains'][0]['linked'])

    def test_diagnostics_never_store_exception_messages_and_deduplicate(self):
        store = Diagnostics(Path(self.tmp.name)/'diagnostics.sqlite')
        try:
            raise ValueError('secret-api-key-must-not-leak')
        except ValueError as exc:
            store.record('UNHANDLED_ERROR', '/api/operations', 500, exc=exc)
        store.record('UNHANDLED_ERROR', '/api/operations', 500)
        rows = store.recent()
        self.assertEqual(len(rows), 1)
        self.assertIn('test_operations.py', rows[0]['location'])
        self.assertNotIn('secret-api-key', json.dumps(rows))
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_real_redacted_round_trip_correlates_both_legs(self):
        fixture = Path(__file__).parent/'fixtures/variational_manual_round_trip.json'
        data = self.write(json.loads(fixture.read_text()))
        self.assertEqual(len(data['chains']), 2)
        self.assertTrue(all(chain['linked'] for chain in data['chains']))
        self.assertEqual([c['reduce_only'] for c in data['chains']], [False, True])
        self.assertFalse(data['live_verified'])
        # The archived flat response is evidence of that moment, not live state.
        self.assertFalse(data['position']['known'])

    def test_real_settlement_uses_company_and_closing_rfq(self):
        fixtures=Path(__file__).parent/'fixtures'
        events=json.loads((fixtures/'variational_manual_round_trip.json').read_text())
        settlement=json.loads((fixtures/'variational_manual_settlement.json').read_text())
        data=self.write(events+[settlement])
        rows=[r for r in data['settlements'] if r.get('realized_pnl_usdc') is not None]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['realized_pnl_usdc'],.000136)
        self.assertFalse(data['settlement_values_observed'])
        settlement['rows'][0]['company_digest']='wrong'
        self.assertFalse(any(r.get('realized_pnl_usdc') is not None for r in self.write(events+[settlement])['settlements']))

    def test_real_round_trip_missing_request_response_pair_cannot_link(self):
        fixture = Path(__file__).parent/'fixtures/variational_manual_round_trip.json'
        events = json.loads(fixture.read_text())
        events = [e for e in events if e.get('kind') != 'submit_response']
        self.assertFalse(any(c['linked'] for c in self.write(events)['chains']))

    def test_settlement_fields_are_reported_as_evidence_not_net_pnl(self):
        events = [
            dict(kind='snapshot_response', endpoint='/api/trades', http_status=200,
                 rows=[dict(id_digest='trade', company_digest='company', source_rfq_digest='rfq', status='confirmed')]),
            dict(kind='settlement_response', endpoint='/api/transfers', http_status=200,
                 complete=True, observed_at=utc_now(),
                 rows=[dict(id_digest='transfer', company_digest='company', source_rfq_digest='rfq', asset='USDC',
                            fee_amount=.01, status='confirmed')]),
        ]
        data = self.write(events)
        self.assertTrue(data['settlement_values_observed'])
        self.assertTrue(data['settlements'][0]['linked_to_confirmed_trade'])
        self.assertNotIn('net_realized_pnl_usdc', data)

    def test_unlinked_or_absent_settlement_values_do_not_claim_fee_verification(self):
        data = self.write([dict(kind='settlement_response', endpoint='/api/transfers', http_status=200,
                                complete=True, rows=[dict(id_digest='transfer', amount=10, status='settled')])])
        self.assertFalse(data['settlement_values_observed'])
        self.assertFalse(data['settlements'][0]['linked_to_confirmed_trade'])
