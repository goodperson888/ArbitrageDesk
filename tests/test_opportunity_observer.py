"""Offline observation evidence; no trading decisions or extra quote requests."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.execution.close_diagnostics import DiagnosticWriter
from src.execution.opportunity_observer import OpportunityObserver


class OpportunityObservationTests(unittest.TestCase):
    def setUp(self):
        self.writer=Mock()
        self.observer=OpportunityObserver(Path('/unused'),self.writer)
        self.clock=[10.]
        patcher=patch('src.execution.opportunity_observer.time.monotonic',side_effect=lambda:self.clock[0])
        patcher.start(); self.addCleanup(patcher.stop)

    def sample(self, version, edge=12, gross=20, quantity=1, route='UNI:a->b:buy'):
        quote=dict(updated_at=str(version),bid=100,ask=101,size=quantity)
        self.observer.observe(route,quantity,quote,quote,edge,8,gross)

    def test_repeated_cache_does_not_invent_stability_or_cross_quantity_routes(self):
        for at in (10,10.1,10.2,10.3):
            self.clock[0]=at; self.sample('same')
        self.observer.begin('one','UNI:a->b:buy',1)
        snapshot=self.observer.attempts['one']
        self.assertEqual(len(snapshot['samples']),1)
        self.assertEqual(set(snapshot['windows_ms'].values()),{'insufficient_samples'})
        self.observer.begin('two','UNI:a->b:buy',2)
        self.assertEqual(self.observer.attempts['two']['samples'],[])
        self.observer.begin('three','UNI:b->a:sell',1)
        self.assertEqual(self.observer.attempts['three']['samples'],[])
        self.writer.submit_entry.assert_not_called()

    def test_sampled_validity_is_frozen_before_fill_and_linked_to_outcome(self):
        for i in range(8):
            self.clock[0]=10+i*.04; self.sample(i,gross=20+i)
        self.observer.begin('one','UNI:a->b:buy',1)
        self.clock[0]=10.35; self.sample(99,edge=-1)
        self.observer.outcome('one','paired',filled_quantity=1)
        snapshot=self.writer.submit_entry.call_args.args[1]
        self.assertEqual(snapshot['windows_ms']['200'],'sampled_valid')
        self.assertEqual(snapshot['windows_ms']['1000'],'insufficient_samples')
        self.assertEqual(snapshot['trend'],'widening')
        self.assertTrue(all(r['edge_bps']==12 for r in snapshot['samples']))
        self.assertEqual(snapshot['outcome'],'paired')
        self.observer.outcome('one','rollback')
        self.assertEqual(self.writer.submit_entry.call_count,1)

    def test_invalid_sample_and_long_gaps_cannot_be_reported_as_stable(self):
        for i in range(8):
            self.clock[0]=10+i*.04; self.sample(i,edge=7 if i==5 else 12)
        self.observer.begin('one','UNI:a->b:buy',1)
        self.assertEqual(self.observer.attempts['one']['windows_ms']['200'],'sampled_invalid')
        self.clock[0]=11; self.sample(100)
        self.observer.begin('two','UNI:a->b:buy',1)
        self.assertEqual(self.observer.attempts['two']['windows_ms']['1000'],'insufficient_samples')
        self.clock[0]=20; self.sample(101)
        self.assertEqual(len(self.observer.routes[self.observer.key('UNI:a->b:buy',1)]),1)

    def test_storage_failure_cannot_raise_or_change_a_trading_decision(self):
        self.sample(1); self.observer.begin('one','UNI:a->b:buy',1)
        self.writer.submit_entry.side_effect=RuntimeError('unavailable')
        self.observer.outcome('one','rollback')
        self.assertEqual(self.observer.attempts,{})
        for i in range(300): self.sample(i,route=str(i))
        self.assertEqual(len(self.observer.routes),256)

    def test_shared_background_writer_persists_separate_evidence_database(self):
        with tempfile.TemporaryDirectory() as directory:
            writer=DiagnosticWriter()
            try:
                path=Path(directory)/'entry_observations.sqlite'
                observer=OpportunityObserver(path,writer)
                observer.begin('attempt','UNI:a->b:buy',1)
                observer.outcome('attempt','rollback',reason_code='price')
                self.assertTrue(writer.flush(1))
                with sqlite3.connect(path) as db:
                    payload=json.loads(db.execute('SELECT detail FROM entry_observations').fetchone()[0])
                self.assertEqual(payload['position_id'],'attempt')
                self.assertEqual(payload['reason_code'],'price')
                self.assertTrue(payload['observation_only'])
            finally:
                writer.close()
