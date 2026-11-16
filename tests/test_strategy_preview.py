import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.api_v2.history import MultiVenueHistory
from src.api_v2.return_estimates import attach_return_previews
from src.web.strategy_preview import strategy_preview
import test_round_trip_preview as fixtures


class StrategyPreviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.history=MultiVenueHistory(self.root/'quotes.sqlite')
        self.payload={'symbol':'sui','venues':['lighter','arcus'],'channel_policy':{'method':'fixed'}}

    def test_channel_form_defaults_pass_native_number_step_constraints(self):
        from html.parser import HTMLParser
        from decimal import Decimal
        fields=[]
        class Parser(HTMLParser):
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='input' and a.get('id','').startswith('channel-'):fields.append(a)
        Parser().feed((Path(__file__).resolve().parents[1]/'src/web/templates/index.html').read_text())
        self.assertGreater(len(fields),5)
        for a in fields:
            with self.subTest(field=a['id']):
                value=Decimal(a['value']);low=Decimal(a.get('min','0'));step=Decimal(a.get('step','1'))
                self.assertGreaterEqual(value,low)
                self.assertLessEqual(value,Decimal(a['max']))
                self.assertEqual((value-low)%step,0,'default must pass browser reportValidity')

    def test_preview_is_read_only_filters_stale_history_and_preserves_frozen_target(self):
        now=int(time.time());route='arcus->lighter'
        with closing(self.history._connect()) as db,db:
            db.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)',[
                ('SUI-PERP',now-i,route,'arcus','lighter',float(i),age,age,0,None)
                for i,age in [(1,0),(2,6),(3,0)]])
        saved={'positions':[dict(id='open',symbol='SUI',status='open',variational_venue='lighter',
            lighter_venue='arcus',entry_target={'target_bps':-12,'channel':{'exit_axis_bps':10}}),
            dict(id='closed',symbol='SUI',status='closed')]}
        path=self.root/'hedge_state_test.json';path.write_text(json.dumps(saved));before=path.read_bytes()
        with patch('src.web.strategy_preview.center_stats_for',return_value={'center_ready':False}):
            result=strategy_preview(self.history,self.payload,self.root)
        self.assertFalse(result['orders_submitted']);self.assertIsNone(result['error'])
        self.assertEqual(result['axis_venues'],['arcus','lighter'])
        self.assertEqual([p['value_bps'] for p in result['points']],[3,1])
        self.assertEqual(result['positions'][0]['entry_target'],saved['positions'][0]['entry_target'])
        self.assertEqual(path.read_bytes(),before)

    def test_history_warmup_is_explicit_and_invalid_selection_is_rejected(self):
        with patch('src.web.strategy_preview.center_stats_for',return_value={'center_ready':False}):
            result=strategy_preview(self.history,dict(self.payload,channel_policy={}),self.root)
        self.assertIsNone(result['bounds']);self.assertIn('积累',result['error'])
        for change in ({'symbol':'../config'},{'venues':['arcus','arcus']},{'venues':['unknown','arcus']},
                {'channel_policy':{'method':'invalid'}}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                strategy_preview(self.history,dict(self.payload,**change),self.root)

    def test_fixed_channel_preview_ignores_legacy_center_readiness_and_filter_window(self):
        f=fixtures.ApiPreviewTests();f.setUp()
        policy=dict(fixtures.POLICY,_entry_mode='channel',_channel_policy={'method':'fixed','window_seconds':900})
        with patch('src.api_v2.center_statistics.center_stats_for',return_value={'center_ready':False}):
            rows=attach_return_previews(f.rows,f.history,policy,'center',604800)
        upper=next(r for r in rows if r.buy_venue=='arcus')
        self.assertTrue(upper.reversion_ready)
        self.assertEqual(upper.return_preview['window_seconds'],900)
        self.assertIsNotNone(upper.return_preview['net_bps'])

    def test_invalid_channel_cannot_leak_legacy_target_or_profit(self):
        f=fixtures.ApiPreviewTests();f.setUp()
        policy=dict(fixtures.POLICY,_entry_mode='channel',_channel_policy={})
        with patch('src.api_v2.center_statistics.center_stats_for',return_value={'center_ready':False}):
            rows=attach_return_previews(f.rows,f.history,policy,'zero')
        for row in rows:
            self.assertFalse(row.reversion_ready);self.assertIsNone(row.reversion_target_bps)
            self.assertIsNone(row.estimated_reversion_profit_usdc)
            self.assertEqual(row.return_preview['target_mode'],'channel')
