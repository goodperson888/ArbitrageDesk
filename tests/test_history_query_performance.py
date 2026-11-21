import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api_v2.history import MultiVenueHistory, HISTORY_RETENTION_SECONDS, ROLLUP_5M_SECONDS
from src.common.history import HistoryStore
from src.execution.history import TradeHistory
from src.web.rollback_explanation import attach_rollback_explanations


class HistoryQueryPerformanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_legacy_event_migration_indexes_selective_reads_and_preserves_bad_json(self):
        path = self.root/'hedge_events.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,timestamp TEXT,event TEXT,detail TEXT)')
            db.executemany('INSERT INTO events(timestamp,event,detail) VALUES(?,?,?)', [
                ('now','stage_completed','{}'), ('now','blocked','not-json'),
                ('now','entry_receipt',json.dumps(dict(position_id='p',venue='lighter',order_id='order',filled_size=2))),
                ('now','planned',json.dumps(dict(execution_id='e',quantity=2,net_edge_bps=10))),
                ('now','hedge_decision',json.dumps(dict(position_id='p',execution_id='e',decision='unwind'))),
            ])
        history = TradeHistory(path)
        history.prepare_indexes()
        history.prepare_indexes()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],5)
            for key in ('position_id','execution_id'):
                expression = "json_extract(detail,'$."+key+"')"
                plan = db.execute('EXPLAIN QUERY PLAN SELECT id FROM events WHERE json_valid(detail) AND '
                    +expression+' IS NOT NULL AND '+expression+' IN (?)',('p',)).fetchall()
                self.assertIn('idx_events_'+key,str(plan))
                self.assertNotIn('SCAN events',str(plan))
            plan=db.execute("EXPLAIN QUERY PLAN SELECT detail FROM events WHERE event='close_receipt' ORDER BY id").fetchall()
            self.assertIn('idx_events_event_id',str(plan))
        self.assertEqual(history.receipt_orders(['p'])['p']['entry'],['order'])
        book=dict(closed_positions=[dict(id='p',execution_id='e',symbol='UNI',hedge_status='single_leg_recovered')])
        attach_rollback_explanations(book,self.root)
        self.assertIn('计划净价差：10 bps',book['closed_positions'][0]['rollback_explanation']['text'])

    def test_retention_is_indexed_periodic_and_does_not_delay_every_quote_insert(self):
        store=MultiVenueHistory(self.root/'multi.sqlite')
        now=1_000_000
        def row(symbol,stamp):
            return (symbol,stamp,'lighter->arcus','lighter','arcus',10,0,0,0,None)
        with patch('src.api_v2.history.time.monotonic',return_value=100):
            store._insert([row('old',now-HISTORY_RETENTION_SECONDS-301),row('current',now)],now)
        with store._connect() as db:
            self.assertEqual(db.execute('SELECT symbol FROM observations').fetchall(),[('current',)])
            db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)',row('expired',now-HISTORY_RETENTION_SECONDS-301))
        with patch('src.api_v2.history.time.monotonic',return_value=101):
            store._insert([row('new',now)],now)
        with store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations WHERE symbol='expired'").fetchone()[0],1)
            plan=db.execute('EXPLAIN QUERY PLAN DELETE FROM observations WHERE ts < ?',(now,)).fetchall()
            self.assertIn('idx_observations_ts',str(plan))
        with patch('src.api_v2.history.time.monotonic',return_value=400):
            store._insert([row('new',now+1)],now+1)
        with store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations WHERE symbol='expired'").fetchone()[0],0)
        charts=HistoryStore(self.root/'chart.sqlite')
        with charts._connect() as db:
            self.assertIn('idx_observations_ts',str(db.execute(
                'EXPLAIN QUERY PLAN DELETE FROM observations WHERE ts < ?',(now,)).fetchall()))

    def test_long_window_rollups_are_idempotent_and_survive_raw_retention(self):
        store = MultiVenueHistory(self.root/'long.sqlite')
        now = 10_000_000
        def row(stamp, value):
            return ('SUI-PERP', stamp, 'lighter->arcus', 'lighter', 'arcus', value, 0, 0, 1, None)
        old = now - 10 * 86400
        with patch('src.api_v2.history.time.monotonic', return_value=100):
            store._insert([row(old, 10), row(old + 30, 20)], now)
        # Replacing an observation with the same primary key must rebuild the
        # bucket rather than incrementing it a second time.
        with patch('src.api_v2.history.time.monotonic', return_value=101):
            store._insert([row(old, 10)], now)
        with store._connect() as db:
            count, total = db.execute(
                'SELECT sample_count, sum_bps FROM observation_rollups '
                'WHERE symbol=? AND bucket_seconds=?', ('SUI-PERP', ROLLUP_5M_SECONDS)).fetchone()
            self.assertEqual((count, total), (2, 30))
        with patch('src.api_v2.history.time.time', return_value=now):
            rows = store.read('SUI-PERP', 30 * 86400, 'lighter->arcus')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['aggregate'])
        self.assertEqual(rows[0]['sample_count'], 2)
