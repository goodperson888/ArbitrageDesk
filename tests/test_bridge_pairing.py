import os
import tempfile
import unittest
from pathlib import Path
from src.variational.quote_bridge import QuoteBridge


class PairingTests(unittest.TestCase):
    def test_restart_preserves_only_token_not_quotes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'token'
            first = QuoteBridge(path)
            second = QuoteBridge(path)
            self.assertEqual(first.token, second.token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(second.last_received_at)
            self.assertEqual(second.status()['symbols'], [])

    def test_invalid_or_symlink_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'token'
            path.write_text('invalid')
            with self.assertRaises(ValueError):
                QuoteBridge(path)
            alias = Path(root) / 'alias'
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                QuoteBridge(alias)

    def test_existing_permissions_restricted(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'token'
            original = QuoteBridge(path).token
            os.chmod(path, 0o644)
            self.assertEqual(QuoteBridge(path).token, original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
