import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from src.common.live_feed import ArcusBboFeed, LighterBookFeed
from src.common.market_data import utc_now
from src.common.market_data import PublicClient
from src.execution.funding_forecast import forecast, funding_scenario
from src.execution.return_preview import round_trip_preview
from src.api_v2.return_estimates import attach_return_previews
from src.arcus.execution import ArcusExecutionAdapter
from src.arcus.execution import ArcusClient
from src.execution.worker import ExecutionWorker
from src.lighter.execution import LighterExecutionAdapter
from src.strategy.profit_exit import ProfitExitMixin
import test_execution as execution_fixtures
import test_round_trip_preview as preview_tests

POLICY, quote = preview_tests.POLICY, preview_tests.quote


class FundingScenarioTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000
        self.buy = dict(funding=forecast(.0001,10800,self.now,'a'))
        self.sell = dict(funding=forecast(.00004,10800,self.now,'b'))
        self.policy = dict(funding_mode='dynamic',funding_reserve_bps=5,
                           funding_hold_seconds=3600,funding_extra_bps=1)

    def test_net_direction_settlement_count_and_buffer_are_counted_once(self):
        p = funding_scenario(self.buy,self.sell,self.policy,self.now)
        self.assertEqual(p['mode'],'dynamic')
        self.assertAlmostEqual(p['expected_cost_bps'],.6)
        self.assertAlmostEqual(p['budget_bps'],1.6)
        self.assertEqual([x['settlements'] for x in p['legs']],[1,1])
        self.policy['funding_hold_seconds'] = 799
        self.assertEqual(funding_scenario(self.buy,self.sell,self.policy,self.now)['budget_bps'],1)
        self.policy['funding_hold_seconds'] = 4400
        self.assertEqual(funding_scenario(self.buy,self.sell,self.policy,self.now)['legs'][0]['settlements'],2)

    def test_receipts_are_visible_but_cannot_subsidize_admission(self):
        p = funding_scenario(self.sell,self.buy,self.policy,self.now)
        self.assertLess(p['signed_cost_bps'],0)
        self.assertEqual(p['budget_bps'],1)

    def test_equal_base_size_does_not_cancel_different_notional_funding_costs(self):
        buy=dict(self.buy,bid=200,ask=200)
        sell=dict(self.buy,bid=100,ask=100)
        p=funding_scenario(buy,sell,self.policy,self.now)
        self.assertAlmostEqual(p['expected_cost_bps'],.5)
        self.assertAlmostEqual(p['budget_bps'],1.5)

    def test_unknown_stale_future_and_settlement_boundary_use_full_fixed_fallback(self):
        for value in (None,forecast(.0001,10800,self.now-121),
                      forecast(.0001,10800,self.now+1),forecast(.0001,self.now,self.now)):
            p = funding_scenario(dict(funding=value),self.sell,self.policy,self.now)
            self.assertEqual(p['mode'],'fallback')
            self.assertEqual(p['budget_bps'],5)
        self.policy['funding_mode']='fixed'
        self.assertEqual(funding_scenario(self.buy,self.sell,self.policy,self.now)['budget_bps'],5)

    def test_cost_groups_sum_to_conservative_net_and_do_not_treat_target_as_fee(self):
        now=time.time();buy,sell=quote(99.98,100.02,4.5),quote(100.3,100.34,0)
        buy['funding'],sell['funding']=forecast(.0001,now+800,now),forecast(.00004,now+800,now)
        policy=dict(POLICY,**self.policy)
        p=round_trip_preview(buy,sell,.1,'center',8,100,policy)
        self.assertEqual(p['funding']['mode'],'dynamic')
        self.assertAlmostEqual(p['gross_bps']-sum(p['components'].values()),p['net_bps'])
        self.assertGreater(p['expected_net_bps'],p['net_bps'])
        self.assertGreater(p['components']['target_remaining_bps'],7.9)
        self.assertGreater(p['components']['close_quote_width_bps'],0)
        self.assertAlmostEqual(p['gross_bps']-sum(p['costs'].values()),p['net_bps'])

    def test_native_funding_units_timestamps_and_reset_never_change_book_freshness(self):
        now=time.time();last=int(now//3600)*3600
        feed=LighterBookFeed({'SOL':2})
        feed.on_message(dict(type='update/market_stats',timestamp=int(now*1000),market_stats={
            '2':dict(market_id=2,current_funding_rate='0.0012',funding_timestamp=last*1000)}))
        self.assertAlmostEqual(feed.funding['SOL']['rate'],.000012)
        self.assertEqual(feed.funding['SOL']['next_at'],last+3600)
        self.assertEqual(feed.books,{})
        feed.reset();self.assertEqual(feed.funding,{})
        arcus=ArcusBboFeed({'SOL':'SOL-USD'})
        arcus.on_message(dict(channel='predictedFunding',id='SOL-USD',contents={'market':'SOL-USD','rate1h':'.0000125'}))
        self.assertEqual(arcus.funding['SOL']['rate'],.0000125)
        self.assertEqual(arcus.quotes,{})
        arcus.reset();self.assertEqual(arcus.funding,{})

    def test_arcus_realized_funding_uses_microseconds_signed_amount_and_rejects_incomplete_pages(self):
        client=Mock(wallet_address='fixture',account_index=7)
        client.market.return_value=dict(symbol='SOL-USD',market_id=2)
        adapter=ArcusExecutionAdapter(client,None,'fixture',enabled=True,fee_bps=4.5)
        start,end=datetime.fromtimestamp(10000),datetime.fromtimestamp(12000)
        row=dict(marketId=2,time=10800_000_000,payment='-.02')
        client._get.return_value=dict(fundingPayments=[row])
        result=adapter.reconcile_funding('SOL',start,end)
        self.assertEqual(result['funding_usdc'],-.02)
        params=client._get.call_args.args[1]
        self.assertEqual(params['from'],10000_000_000)
        self.assertEqual(params['accountIndex'],7)
        for rows in ([dict(row,marketId=3)],[dict(row,time=1)],[row]*1000):
            client._get.return_value=dict(fundingPayments=rows)
            self.assertIsNone(adapter.reconcile_funding('SOL',start,end))
        client._get.return_value={};self.assertIsNone(adapter.reconcile_funding('SOL',start,end))

    def test_holding_poll_is_short_and_completed_scan_does_not_add_another_second(self):
        strategy=SimpleNamespace(positions=[SimpleNamespace(status='open')])
        worker=ExecutionWorker(strategy,[])
        worker.stop_event.wait=Mock()
        worker._cycle_started=time.monotonic()-.1
        worker._wait_cycle();self.assertLessEqual(worker.stop_event.wait.call_args.args[0],.15)
        strategy.positions=[];worker._cycle_started=time.monotonic()-1.2
        worker._wait_cycle();worker.stop_event.wait.assert_called_with(.05)


class LearnedDisplayTests(unittest.TestCase):
    def test_historical_movement_is_observational_and_does_not_raise_budget(self):
        fixture=preview_tests.ApiPreviewTests();fixture.setUp()
        now=time.time();key='BTC|arcus->lighter'
        samples=[dict(at=now,elapsed_ms=300,first_bps=4,second_bps=3) for _ in range(5)]
        policy=dict(POLICY,_route_risk={key:dict(samples=samples)})
        rows=attach_return_previews(fixture.rows,fixture.history,policy,'zero')
        target=next(r for r in rows if r.buy_venue=='arcus')
        reverse=next(r for r in rows if r.buy_venue=='lighter')
        self.assertEqual(target.return_preview['movement_bps'],2)
        self.assertEqual(target.return_preview['movement_evidence']['movement_bps'],4)
        self.assertEqual(reverse.return_preview['movement_bps'],2)
        self.assertNotIn('_route_risk',target.return_preview['policy'])


class FastPathTests(unittest.TestCase):
    def test_unrelated_public_reads_run_in_parallel_and_same_key_remains_singleflight(self):
        import httpx
        for venue in ('lighter','arcus'):
            session=Mock()
            if venue=='lighter':
                client=PublicClient('https://fixture.invalid',session=session)
                locks=client._key_locks
                read=lambda path:client.get(path,ttl=2)
            else:
                client=ArcusClient.__new__(ArcusClient)
                client._public_cache={};client._public_cache_lock=threading.RLock()
                client._public_key_locks=tuple(threading.RLock() for _ in range(32))
                client.http=session;client.base_url='https://fixture.invalid';client.api_key='fixture';client.rate_limit=Mock()
                locks=client._public_key_locks
                read=lambda path:client._get(path,ttl=2)
            first='/market-a'
            second=next('/market-'+str(i) for i in range(100) if hash(('/market-'+str(i),()))%len(locks)!=hash((first,()))%len(locks))
            barrier=threading.Barrier(2)
            def request(url,**kwargs):
                barrier.wait(timeout=1)
                return httpx.Response(200,json={'value':1},request=httpx.Request('GET',url))
            session.get.side_effect=request
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(read,path) for path in (first,second)]
                self.assertTrue(all(f.result() for f in futures))
            # A fresh shared-key request is deduplicated under contention.
            started=threading.Event();release=threading.Event();calls=[]
            def same(url,**kwargs):
                calls.append(url);started.set();release.wait(1)
                return httpx.Response(200,json={'value':2},request=httpx.Request('GET',url))
            session.get.side_effect=same
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(read,'/same');self.assertTrue(started.wait(1))
                b=pool.submit(read,'/same');release.set()
                self.assertEqual(a.result(),b.result())
            self.assertEqual(len(calls),1)

    def test_lighter_zero_fee_terminal_receipt_skips_extra_request_but_checks_identity(self):
        adapter=LighterExecutionAdapter.__new__(LighterExecutionAdapter)
        adapter.account_index=7;adapter.poll_seconds=0;adapter.verified_fee_bps=0
        adapter.bridge=Mock()
        adapter.bridge.call.return_value=execution_fixtures.order()
        plan=execution_fixtures.lighter_plan(execution_fixtures.market(),'BTC','buy',1,100,10)
        result=adapter._reconcile(plan,123)
        self.assertTrue(result['confirmed']);self.assertEqual(result['fees_usdc'],0)
        adapter.bridge.call.assert_called_once_with('find_order',1,123)
        adapter.bridge.call.reset_mock()
        adapter.bridge.call.return_value=dict(execution_fixtures.order(),owner_account_index=8)
        self.assertFalse(adapter._reconcile(plan,123)['confirmed'])
        adapter.verified_fee_bps=1
        adapter.bridge.call.side_effect=lambda name,*args:execution_fixtures.order() if name=='find_order' else execution_fixtures.trades()
        self.assertTrue(adapter._reconcile(plan,123)['confirmed'])
        self.assertIn('trades',[c.args[0] for c in adapter.bridge.call.call_args_list])

    def test_arcus_sized_ws_fast_path_requires_both_sizes_and_current_snapshot(self):
        client=Mock(api_key='fixture');adapter=ArcusExecutionAdapter(client,None,'fixture',enabled=True,fee_bps=4.5)
        adapter._reference_feed=Mock()
        q=dict(bid=100,ask=100.01,bid_size=1,ask_size=1,updated_at=utc_now())
        adapter._reference_feed.snapshot.return_value=q
        result=adapter.get_executable_quote('SOL',.5)
        self.assertEqual(result['bid'],100);self.assertEqual(result['size'],.5)
        client.orderbook.assert_not_called()
        adapter._reference_feed.snapshot.return_value=dict(q,bid_size=.1)
        client.orderbook.return_value=dict(fetched_at=utc_now(),bids=[dict(price=100,size=1)],asks=[dict(price=100.01,size=1)])
        adapter.get_executable_quote('SOL',.5);client.orderbook.assert_called_once()

    def test_funding_platform_reads_are_parallel_but_incomplete_accounting_still_blocks(self):
        s=ProfitExitMixin();s._exit_funding_lock=threading.Lock();s._exit_funding_cache={}
        barrier=threading.Barrier(2)
        def read(*args):
            barrier.wait(timeout=1)
            return dict(complete=True,funding_usdc=-.01)
        s.variational_trader=SimpleNamespace(reconcile_funding=read)
        s.lighter_trader=SimpleNamespace(reconcile_funding=read)
        p=SimpleNamespace(symbol='SOL',open_time=datetime.now())
        self.assertEqual(s._single_exit_funding(p),dict(variational=-.01,lighter=-.01))
        # Cache reuse cannot double-request a settlement during final checks.
        self.assertEqual(s._single_exit_funding(p),dict(variational=-.01,lighter=-.01))
