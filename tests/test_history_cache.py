import copy
import json
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import test_web
from src.execution.history import TradeHistory
from src.execution.journal import OrderJournal
from src.strategy.arbitrage_strategy import Position
from src.web import app as web
from src.web.history_cache import ProjectionCache, evidence_version, enrich_history
from src.web.history_marks import HistoryMarks, HistoryMarkUnavailable


class HistoryCacheTests(unittest.TestCase):
    def setUp(self):
        fixture = test_web.WebTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.root = Path(fixture.temp.name)/'data'
        self.root.mkdir(exist_ok=True)
        self.path = self.root/'hedge_state_lighter_arcus.json'
        TradeHistory(self.root/'hedge_events.sqlite').prepare_indexes()
        self.position = Position('SUI','short','buy',2,2,104,99,datetime.now(),
            id='cached',status='open',variational_venue='lighter',lighter_venue='arcus',
            entry_size_variational=2,entry_size_lighter=2,entry_fees_usdc=.1)
        self.save()
        for name,value in (('history_projection_cache',ProjectionCache()),
                           ('history_detail_cache',ProjectionCache(capacity=2048))):
            p=patch.object(web,name,value);p.start();self.addCleanup(p.stop)

    def save(self):
        self.path.write_text(json.dumps(dict(positions=[asdict(self.position)],daily_trades=1,
            daily_pnl=0,day=datetime.now().date().isoformat()),default=lambda v:v.isoformat()))

    def monitor(self, price):
        return dict(heartbeat_stale=False,positions=[dict(id='cached',symbol='SUI',legs={
            venue:dict(venue=venue,bid=price,ask=price,fee_bps=0,
                       updated_at=datetime.now(timezone.utc).isoformat())
            for venue in ('lighter','arcus')})])

    def test_projection_reused_but_marks_and_heartbeat_are_always_current(self):
        monitor = Mock(return_value=self.monitor(100))
        marks = HistoryMarks(fetch=Mock(side_effect=HistoryMarkUnavailable('expired')))
        with patch('src.execution.position_monitor.read_position_monitor',monitor), \
             patch.object(web,'history_marks',marks), \
             patch.object(web,'_build_history_pair',wraps=web._build_history_pair) as build:
            first = web._history_pair_overview(self.path,['lighter','arcus'])
            monitor.return_value=self.monitor(102)
            second=web._history_pair_overview(self.path,['lighter','arcus'])
            self.assertEqual(first['open_positions'][0]['mark_price_lighter'],100)
            self.assertEqual(second['open_positions'][0]['mark_price_lighter'],102)
            monitor.return_value=dict(heartbeat_stale=True,positions=[])
            stale=web._history_pair_overview(self.path,['lighter','arcus'])
        self.assertIsNone(stale['open_positions'][0]['unrealized_pnl_usdc'])
        self.assertEqual(build.call_count,1)
        self.assertEqual(monitor.call_count,3)

    def test_state_fees_receipts_orders_and_config_invalidate_cached_accounting(self):
        marks = HistoryMarks(fetch=Mock(side_effect=HistoryMarkUnavailable('expired')))
        def read():
            return web._history_pair_overview(self.path,['lighter','arcus'])
        with patch.object(web,'history_marks',marks), \
             patch.object(web,'_build_history_pair',wraps=web._build_history_pair) as build:
            read();read()
            self.assertEqual(build.call_count,1)
            self.position.entry_fees_usdc=.2;self.save();read()
            self.assertEqual(build.call_count,2)
            (self.root/'platform-order-history.json').write_text('{"rows": []}')
            read();self.assertEqual(build.call_count,3)
            history=TradeHistory(self.root/'hedge_events.sqlite')
            history.append('stage_started',stage='unrelated quote')
            read();self.assertEqual(build.call_count,3)
            history.append('close_receipt',position_id='cached',venue='arcus',order_id='new')
            read();self.assertEqual(build.call_count,4)
            journal=OrderJournal(self.root/'execution_orders.sqlite')
            reserved,_=journal.reserve('arcus','test','cache-test',{'symbol':'SUI'})
            read();self.assertEqual(build.call_count,5)
            journal.finish(reserved['seq'],dict(confirmed=True,filled_size=0,status='canceled'))
            read();self.assertEqual(build.call_count,6)
            settings=copy.deepcopy(web.config_loader.get_strategy_config())
            settings.setdefault('risk',{})['slippage_tolerance']=7
            with patch.object(web.config_loader,'get_strategy_config',return_value=settings):
                read();self.assertEqual(build.call_count,7)

    def test_closed_diagnostics_are_cached_and_new_evidence_refreshes(self):
        import sqlite3
        path=self.root/'close_diagnostics.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE close_decisions(id INTEGER PRIMARY KEY,position_id TEXT,observed_at TEXT,detail TEXT)')
            db.execute('CREATE INDEX close_position ON close_decisions(position_id,id)')
        source=dict(closed_positions=[dict(id='p',hedge_status='paired')])
        cache=ProjectionCache()
        with patch('src.web.close_diagnostics.attach_close_diagnostics') as decorate:
            enrich_history(copy.deepcopy(source),self.root,cache)
            enrich_history(copy.deepcopy(source),self.root,cache)
            self.assertEqual(decorate.call_count,1)
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO close_decisions VALUES(1,'p','now','{}')")
            enrich_history(copy.deepcopy(source),self.root,cache)
            self.assertEqual(decorate.call_count,2)

    def test_cache_is_bounded_and_failed_build_is_not_reused(self):
        cache=ProjectionCache(capacity=2)
        build=Mock(side_effect=[ValueError('incomplete'),1])
        with self.assertRaises(ValueError):
            cache.get('a',1,build)
        self.assertEqual(cache.get('a',1,build),1)
        cache.get('b',1,lambda:2)
        cache.get('c',1,lambda:3)
        self.assertEqual(len(cache.values),2)
        self.assertNotIn('a',cache.values)
