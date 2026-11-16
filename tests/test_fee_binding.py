import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.web.local_credentials import bind_fee_verification, NAMES
from src.execution.runtime import runtime_blockers
from src.common.market_data import utc_now


class FeeBindingTests(unittest.TestCase):
    def test_readonly_binding_never_enables_execution_and_expires(self):
        values = dict(zip(NAMES, ['123', '4', 'ab'*40]))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'profile.json'
            path.write_text('{"live_enabled":false,"account_verified":false}')
            report = dict(authenticated=True, orders_submitted=False,
                          taker_fee_bps=0, checked_at=utc_now())
            bind_fee_verification(path, values, report)
            profile = json.loads(path.read_text())
            self.assertFalse(profile['live_enabled'])
            self.assertFalse(profile['account_verified'])
            self.assertNotIn(values[NAMES[2]], path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.dict('os.environ', values):
                self.assertFalse(any('Lighter 费率证据' in x for x in runtime_blockers(profile)))
                profile['lighter_fee_checked_at'] = '2000-01-01T00:00:00Z'
                self.assertTrue(any('Lighter 费率证据' in x for x in runtime_blockers(profile)))
                profile['lighter_fee_checked_at'] = utc_now()
                with patch.dict('os.environ', {'LIGHTER_API_PRIVATE_KEY':'cd'*40}):
                    self.assertTrue(any('Lighter 费率证据' in x for x in runtime_blockers(profile)))
