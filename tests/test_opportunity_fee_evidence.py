import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.api_v2.app import _fee_evidence, attach_history_stats, build_opportunities
from src.api_v2.models import Opportunity, QuoteSnapshot


class OpportunityFeeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'data').mkdir()
        (self.root / 'config').mkdir()
        self.patch = patch('src.api_v2.app.__file__', str(self.root / 'src/api_v2/app.py'))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def report(self, **overrides):
        report = dict(authenticated=True, fee_verified=True, taker_fee_bps=4.5,
                      checked_at=datetime.now(timezone.utc).isoformat())
        report.update(overrides)
        (self.root / 'data/platform-verification.json').write_text(json.dumps({'arcus': report}))

    def test_arcus_verified_rate_reaches_both_opportunity_directions(self):
        self.report()
        (self.root / 'config/variational-execution.local.json').write_text(json.dumps(dict(
            lighter_fee_verified=True, lighter_fee_bps=0,
            lighter_fee_checked_at=datetime.now(timezone.utc).isoformat())))
        def quote(venue, bid, ask):
            return QuoteSnapshot(venue=venue, symbol='SUI', venue_symbol='SUI',
                                 instrument_type='perpetual', source='test',
                                 bid=bid, ask=ask, stale=False, age_seconds=0)
        rows = build_opportunities('SUI', [('arcus', 'lighter')], {
            'arcus': quote('arcus', 99, 100),
            'lighter': quote('lighter', 101, 102)}, record=False)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIsNotNone(row.net_bps)
            self.assertIsNotNone(row.wear_breakdown['fees_bps'])
            self.assertIsNone(row.wear_bps)  # Missing impact is still unknown.
        self.assertAlmostEqual(rows[0].net_bps, 95.5)

    def test_invalid_unverified_or_expired_arcus_fees_stay_unknown(self):
        cases = [dict(authenticated=False), dict(fee_verified=False),
                 dict(taker_fee_bps=None), dict(taker_fee_bps=True),
                 dict(taker_fee_bps=-1), dict(taker_fee_bps=float('nan')),
                 dict(checked_at='2020-01-01T00:00:00+00:00'),
                 dict(checked_at=datetime.now().isoformat()),
                 dict(checked_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())]
        for case in cases:
            with self.subTest(case=case):
                self.report(**case)
                self.assertEqual(_fee_evidence('arcus'), (None, False))

    def test_recent_display_only_fee_is_marked_for_recheck(self):
        self.report(checked_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
        self.assertEqual(_fee_evidence('arcus'), (4.5, True))
        self.report(taker_fee_bps=0)
        self.assertEqual(_fee_evidence('arcus'), (0, False))


class OpportunityReversionEstimateTests(unittest.TestCase):
    def estimate(self, gross=20, net=15, center=0, wear=None, fees=5, samples=100):
        row = Opportunity(canonical_symbol='SUI-PERP', buy_venue='arcus',
                          sell_venue='lighter', buy_price=100, sell_price=100.2,
                          gross_bps=gross, net_bps=net, wear_bps=wear,
                          wear_breakdown={'fees_bps': fees})
        history = Mock()
        history.pair_key.return_value = 'arcus:lighter'
        history.stats_for.return_value = {}
        history.reversion_stats.return_value = dict(center_bps=center, samples=samples)
        with patch('src.api_v2.app.state.history', history):
            return attach_history_stats([row], notional=1000)[0]

    def test_entry_only_values_never_invent_a_round_trip_without_close_quotes(self):
        for kwargs in [dict(), dict(net=12,wear=8), dict(gross=-10,net=-15,center=-40), dict(center=20)]:
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.estimate(**kwargs).estimated_reversion_profit_usdc)

    def test_unknown_costs_and_short_history_remain_unknown(self):
        for kwargs in [dict(net=None),dict(fees=None),dict(samples=29)]:
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.estimate(**kwargs).estimated_reversion_profit_usdc)
