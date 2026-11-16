import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from src.execution.runtime import _run_pair_verified


class PairPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'data').mkdir()
        (self.root / '.env').write_text(
            'LIGHTER_ACCOUNT_INDEX=1\nLIGHTER_API_KEY_INDEX=4\n'
            'LIGHTER_API_PRIVATE_KEY=test-only\nARCUS_WALLET_ADDRESS=test-wallet\n'
            'ARCUS_ACCOUNT_INDEX=0\nARCUS_API_SIGNING_KEY=test-only\n')
        (self.root / 'data/platform-verification.json').write_text(
            json.dumps({'arcus': {'taker_fee_bps': 4.5}}))
        self.profile = {'venues': ['lighter', 'arcus'], 'symbol': 'SOL',
                        'symbols': ['SOL'], 'lighter_fee_bps': 0}
        self.config = Mock(config_path=self.root / 'config.yaml')
        self.config.get.return_value = None
        self.config.get_strategy_config.return_value = {}
        self.strategy = Mock()
        self.strategy.positions = []
        self.strategy.plan_entry.return_value = (
            {'direction': 'sell_variational', 'quantity': 0.1,
             'variational_price': 101, 'lighter_vwap': 100}, None, None, None)
        self.public, self.sdk, self.arcus = Mock(), Mock(), Mock()
        self.lighter_adapter = Mock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, value in (
            ('src.execution.runtime.OrderJournal', Mock()),
            ('src.execution.runtime.ArbitrageStrategy', Mock(return_value=self.strategy)),
            ('src.execution.platform_clients.build_lighter_session',
             Mock(return_value=(self.public, self.sdk))),
            ('src.execution.runtime.LighterExecutionAdapter', Mock(return_value=self.lighter_adapter)),
            ('src.arcus.execution.ArcusClient', Mock(return_value=self.arcus)),
            ('src.arcus.execution.ArcusExecutionAdapter', Mock()),
        ):
            stack.enter_context(patch(target, value))
        self.worker = stack.enter_context(patch('src.execution.runtime.ExecutionWorker'))

    def test_preview_uses_actual_sizing_and_maps_selected_venues_without_worker(self):
        result = _run_pair_verified(self.config, self.profile, self.root, plan_only=True)
        self.assertTrue(result['ready'])
        self.assertFalse(result['orders_submitted'])
        self.assertEqual(result['plan']['buy_venue'], 'arcus')
        self.assertEqual(result['plan']['sell_venue'], 'lighter')
        self.assertEqual(result['plan']['buy_price'], 100)
        self.assertEqual(result['plan']['sell_price'], 101)
        self.strategy.check_arbitrage_opportunity.assert_called_once_with('SOL')
        self.strategy.plan_entry.assert_called_once()
        self.strategy.resume_verified_position.assert_not_called()
        self.worker.assert_not_called()
        self.lighter_adapter.start_reference_feed.assert_not_called()
        self.public.close.assert_called_once()
        self.sdk.close.assert_called_once()
        self.arcus.close.assert_called_once()
        self.assertFalse((self.root / 'data/hedge_state_lighter_arcus.json').exists())

    def test_missing_quote_does_not_size_or_start_worker(self):
        self.strategy.check_arbitrage_opportunity.return_value = None
        result = _run_pair_verified(self.config, self.profile, self.root, plan_only=True)
        self.assertFalse(result['ready'])
        self.assertFalse(result['orders_submitted'])
        self.strategy.plan_entry.assert_not_called()
        self.worker.assert_not_called()

    def test_failed_sizing_closes_clients_without_worker_or_resume(self):
        self.strategy.plan_entry.side_effect = ValueError('insufficient depth')
        with self.assertRaisesRegex(ValueError, 'insufficient depth'):
            _run_pair_verified(self.config, self.profile, self.root, plan_only=True)
        self.worker.assert_not_called()
        self.strategy.resume_verified_position.assert_not_called()
        self.arcus.close.assert_called_once()

    def test_preview_rejects_multiple_symbols_without_starting_worker(self):
        self.profile['symbols'] = ['SOL', 'ETH']
        with self.assertRaises(ValueError):
            _run_pair_verified(self.config, self.profile, self.root, plan_only=True)
        self.strategy.plan_entry.assert_not_called()
        self.worker.assert_not_called()

    def test_worker_starts_public_feed_and_stops_it_on_exit(self):
        _run_pair_verified(self.config, self.profile, self.root, once=True)
        self.lighter_adapter.start_reference_feed.assert_called_once_with(['SOL'])
        self.lighter_adapter.close_reference_feed.assert_called_once()
        self.worker.return_value.run_once.assert_called_once()

    def test_worker_closes_public_feed_on_error_and_rest_fallback_on_setup_failure(self):
        self.lighter_adapter.start_reference_feed.side_effect = ValueError('stream unavailable')
        self.worker.return_value.run_once.side_effect = ValueError('worker failed')
        with self.assertRaisesRegex(ValueError, 'worker failed'):
            _run_pair_verified(self.config, self.profile, self.root, once=True)
        self.assertGreaterEqual(self.lighter_adapter.close_reference_feed.call_count, 1)
        self.public.close.assert_called_once()
        self.sdk.close.assert_called_once()

    def test_restart_refreshes_account_proof_before_starting_worker(self):
        path = self.root/'data/hedge_state_lighter_arcus.json'
        path.write_text(json.dumps({'positions': [{'status': 'reconcile_required'}]}))
        self.strategy.resume_verified_position.return_value = True
        report = {'success': True, 'orders_submitted': False, 'resume_ready': True}
        sequence = []
        self.strategy.resume_verified_position.side_effect = lambda: sequence.append('resume') or True
        self.worker.side_effect = lambda *a, **kw: sequence.append('worker') or Mock()
        with patch('src.execution.reconcile_pair.reconcile_pair',
                   side_effect=lambda *a: sequence.append('fresh-account-read') or report):
            _run_pair_verified(self.config, self.profile, self.root)
        self.assertEqual(sequence, ['fresh-account-read', 'resume', 'worker'])

    def test_failed_startup_recheck_never_starts_worker(self):
        from src.common.rate_limit import RateLimitError
        path = self.root/'data/hedge_state_lighter_arcus.json'
        path.write_text(json.dumps({'positions': [{'status': 'reconcile_required'}]}))
        with patch('src.execution.reconcile_pair.reconcile_pair', side_effect=RateLimitError(5)):
            with self.assertRaises(RateLimitError):
                _run_pair_verified(self.config, self.profile, self.root)
        self.worker.assert_not_called()
        self.strategy.resume_verified_position.assert_not_called()
        self.public.close.assert_called_once()
        self.sdk.close.assert_called_once()
        self.arcus.close.assert_called_once()

    def test_failed_proof_consumption_never_starts_worker(self):
        path = self.root/'data/hedge_state_lighter_arcus.json'
        path.write_text(json.dumps({'positions': [{'status': 'reconcile_required'}]}))
        self.strategy.resume_verified_position.return_value = False
        with patch('src.execution.reconcile_pair.reconcile_pair', return_value={
                'success': True, 'orders_submitted': False, 'resume_ready': True}):
            with self.assertRaisesRegex(ValueError, '恢复凭据'):
                _run_pair_verified(self.config, self.profile, self.root)
        self.worker.assert_not_called()

    def test_preview_does_not_refresh_or_consume_recovery_proof(self):
        path = self.root/'data/hedge_state_lighter_arcus.json'
        path.write_text(json.dumps({'positions': [{'status': 'reconcile_required'}]}))
        before = path.read_bytes()
        with patch('src.execution.reconcile_pair.reconcile_pair') as reconcile:
            _run_pair_verified(self.config, self.profile, self.root, plan_only=True)
        reconcile.assert_not_called()
        self.strategy.resume_verified_position.assert_not_called()
        self.worker.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
