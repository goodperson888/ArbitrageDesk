import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from src.execution.entry_accounting import entry_evidence, apply_entry_evidence, read_entry_evidence
from src.strategy.arbitrage_strategy import Position


class EntryAccountingTests(unittest.TestCase):
    def setUp(self):
        self.p = Position('CASHCAT', 'short', 'buy', 139.3, 139.3, .08608, .085852,
            datetime.now(), id='position', status='open', entry_size_variational=139.3,
            entry_size_lighter=139.3, variational_venue='lighter', lighter_venue='arcus',
            entry_fees_usdc=.00510556, realized_pnl_usdc=-.00510556,
            entry_target={'target_bps': -8.36},
            entry_target_projection={'exit_credit_per_unit': -.00007})
        self.rows = [dict(venue='arcus', symbol='CASHCAT', order_id='arc',
            client_order_id='position-l', side='buy', reduce_only=False, status='filled',
            filled_size='139.3', average_price='.085835', fees_usdc='.00510556', fee_source='arcus_fills'),
            dict(venue='lighter', symbol='CASHCAT', order_id='light', client_order_id='numeric',
            side='sell', reduce_only=False, status='filled', filled_size='139.3',
            average_price='.08608', fees_usdc='0', fee_source='lighter_trade_zero_fees')]
        self.row = dict(asdict(self.p), entry_order_ids_by_venue={'lighter': ['light'], 'arcus': ['arc']})

    def test_actual_prices_correct_cost_and_preserve_exposure_and_frozen_target(self):
        before = asdict(self.p)
        evidence = entry_evidence(self.row, self.rows)
        self.assertTrue(apply_entry_evidence(self.p, evidence))
        self.assertEqual(self.p.entry_price_lighter, .085835)
        self.assertEqual(self.p.entry_fees_usdc, .00510556)
        for key in ('variational_size', 'lighter_size', 'entry_size_variational',
                    'entry_size_lighter', 'entry_target', 'open_time', 'status', 'id'):
            self.assertEqual(getattr(self.p, key), before[key])
        self.assertFalse(apply_entry_evidence(self.p, evidence))

    def test_identity_side_quantity_and_price_source_must_all_match(self):
        for changes in ({'venue':'lighter_rh'}, {'symbol':'SUI'}, {'side':'sell'},
                        {'filled_size':'139.2'}, {'reduce_only':True}, {'price_verified':False},
                        {'average_price':'NaN'}, {'average_price':'0'},
                        {'order_id':'other','client_order_id':'another-position-l'}):
            with self.subTest(changes=changes):
                rows = [dict(self.rows[0], **changes), self.rows[1]]
                self.assertNotIn('lighter', entry_evidence(self.row, rows))
        self.assertEqual(entry_evidence(self.row, self.rows + [self.rows[0]]), {})

    def test_partial_exit_is_never_rebased(self):
        self.p.lighter_size -= 1
        before = asdict(self.p)
        self.assertFalse(apply_entry_evidence(self.p, entry_evidence(self.row, self.rows)))
        self.assertEqual(before, asdict(self.p))

    def test_estimated_receipt_fee_is_not_relabelled_as_actual(self):
        rows = copy.deepcopy(self.rows)
        rows[0].update(fee_source='account_rate_estimate', fee_estimated=True, fees_usdc='.5')
        evidence = entry_evidence(self.row, rows)
        self.assertIsNone(evidence['lighter']['fees_usdc'])
        apply_entry_evidence(self.p, evidence)
        self.assertEqual(self.p.entry_fees_usdc, .00510556)

    def test_fee_correction_updates_subtotal_once(self):
        self.rows[0]['fees_usdc'] = '.004'
        evidence = entry_evidence(self.row, self.rows)
        apply_entry_evidence(self.p, evidence)
        self.assertAlmostEqual(self.p.realized_pnl_usdc, -.004)
        self.assertEqual(self.p.entry_fees_usdc, .004)
        self.assertFalse(apply_entry_evidence(self.p, evidence))

    def test_local_read_uses_recorded_order_ids_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'platform-order-history.json'
            path.write_text(json.dumps({'rows': self.rows}))
            before = path.read_bytes()
            history = Mock()
            history.receipt_orders.return_value = {'position': {'entry_by_venue': {'lighter':['light'], 'arcus':['arc']}}}
            result = read_entry_evidence(self.p, directory, history)
            self.assertEqual(set(result), {'variational', 'lighter'})
            self.assertEqual(path.read_bytes(), before)
            history.receipt_orders.assert_called_once_with(['position'])
