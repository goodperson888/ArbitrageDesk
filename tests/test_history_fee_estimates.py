import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_history_accounting import evidence
from src.execution.history_recovery import apply_close_history
from src.web.history_fee_estimates import apply_fee_estimates, verified_lighter_rate


RATE = {'lighter': {'bps': 0, 'checked_at': '2026-11-17T09:00:00+00:00'}}


class HistoryFeeEstimateTests(unittest.TestCase):
    def project(self, position, cache, rates=RATE):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'platform-order-history.json'
            path.write_text(json.dumps(cache))
            before = path.read_bytes()
            book = {'closed_positions': [copy.deepcopy(position)],
                    'summary': {'realized_pnl_usdc': None}}
            apply_close_history(book, folder)
            facts = copy.deepcopy(book['closed_positions'][0])
            apply_fee_estimates(book, rates)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(book['summary'], {'realized_pnl_usdc': None})
            row = book['closed_positions'][0]
            for field in ('accounting_pending', 'realized_pnl_usdc', 'known_fees_usdc',
                          'accounting_missing', 'recovered_close_fills'):
                self.assertEqual(row[field], facts[field], field)
            return row

    def test_zero_rate_is_provisional_and_preserves_known_fee_on_same_leg(self):
        position, cache = evidence()
        cache['rows'][2]['fees_usdc'] = '.02'
        cache['rows'][3]['fees_usdc'] = None
        row = self.project(position, cache)
        self.assertAlmostEqual(row['provisional_pnl_usdc'], .79)
        self.assertAlmostEqual(row['provisional_fees_usdc'], .22)
        self.assertEqual(row['fee_estimates'][0]['fees_usdc'], 0)
        self.assertEqual(row['fee_estimates'][0]['order_id'], 'l-close')
        self.assertTrue(row['accounting_pending'])
        self.assertIsNone(row['realized_pnl_usdc'])
        self.assertIn('102.00000000 × 0 bps', row['provisional_accounting_note'])
        self.assertIn('不代表历史账单', row['provisional_accounting_note'])

    def test_nonzero_rate_only_applies_to_missing_fee(self):
        position, cache = evidence()
        cache['rows'][3]['fees_usdc'] = None
        row = self.project(position, cache, {'lighter': dict(RATE['lighter'], bps=2)})
        self.assertAlmostEqual(row['provisional_pnl_usdc'], .7896)
        self.assertAlmostEqual(row['provisional_fees_usdc'], .2204)
        self.assertEqual(len(row['fee_estimates']), 1)

    def test_actual_bill_replaces_provisional_and_clears_estimate_fields(self):
        position, cache = evidence()
        cache['rows'][3]['fees_usdc'] = None
        row = self.project(position, cache)
        cache['rows'][3]['fees_usdc'] = '.04'
        settled = self.project(row, cache)
        self.assertFalse(settled['accounting_pending'])
        self.assertAlmostEqual(settled['realized_pnl_usdc'], .77)
        self.assertNotIn('provisional_pnl_usdc', settled)
        self.assertNotIn('fee_estimates', settled)

    def test_unknown_rate_missing_funding_and_cost_basis_mismatch_stay_unknown(self):
        for case in ('rate', 'funding', 'cost_basis', 'fills'):
            with self.subTest(case=case):
                position, cache = evidence()
                cache['rows'][3]['fees_usdc'] = None
                if case == 'funding':
                    cache['funding_intervals'] = []
                if case == 'cost_basis':
                    cache['rows'][1]['fee_inclusive_pnl_usdc'] = '999'
                if case == 'fills':
                    cache['rows'].pop(1)
                row = self.project(position, cache, {} if case == 'rate' else RATE)
                self.assertNotIn('provisional_pnl_usdc', row)

    def test_account_estimated_row_remains_pending_until_actual_fee_arrives(self):
        position, cache = evidence()
        cache['rows'][3].update(fees_usdc='.5', fee_estimated=True,
                               fee_source='account_rate_estimate')
        row = self.project(position, cache)
        self.assertAlmostEqual(row['provisional_pnl_usdc'], .81)
        self.assertTrue(row['accounting_pending'])

    def test_local_rate_requires_matching_credentials_and_valid_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            profile_path, env_path = Path(folder)/'profile.json', Path(folder)/'.env'
            key = 'ab'*40
            env_path.write_text('LIGHTER_ACCOUNT_INDEX=123\nLIGHTER_API_KEY_INDEX=4\n'
                                'LIGHTER_API_PRIVATE_KEY=' + key + '\n')
            profile = dict(lighter_fee_verified=True, lighter_fee_bps=0,
                lighter_fee_checked_at='2026-11-01T00:00:00+00:00',
                lighter_fee_account_index='123', lighter_fee_key_index='4',
                lighter_fee_key_digest=hashlib.sha256(key.encode()).hexdigest())
            profile_path.write_text(json.dumps(profile))
            self.assertEqual(verified_lighter_rate(profile_path, env_path)['lighter']['bps'], 0)
            for field, value in (('lighter_fee_verified', False), ('lighter_fee_bps', None),
                    ('lighter_fee_bps', True), ('lighter_fee_bps', float('nan')),
                    ('lighter_fee_bps', -1), ('lighter_fee_account_index', '456'),
                    ('lighter_fee_key_index', '5'), ('lighter_fee_key_digest', 'different'),
                    ('lighter_fee_checked_at', None),
                    ('lighter_fee_checked_at', 'invalid'),
                    ('lighter_fee_checked_at', '2099-01-01T00:00:00+00:00')):
                with self.subTest(field=field, value=value):
                    profile_path.write_text(json.dumps(dict(profile, **{field: value})))
                    self.assertEqual(verified_lighter_rate(profile_path, env_path), {})
            profile_path.write_text('[]')
            self.assertEqual(verified_lighter_rate(profile_path, env_path), {})
