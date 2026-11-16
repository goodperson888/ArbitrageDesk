"""Read-only quote delivery must stay independent of slow market requests."""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import WebSocketDisconnect

from src.api_v2.adapters import SyncVenueAdapter
from src.api_v2.models import CatalogMode, Opportunity, VenueCapability


class QuoteStreamFreshnessTests(unittest.TestCase):
    def stream_module(self):
        async def load():
            return importlib.import_module('src.api_v2.app')
        return asyncio.run(load())

    def patch_attribute(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_warm_lighter_book_does_not_wait_for_rest_metadata(self):
        self.assert_warm_native_book('lighter')

    def test_warm_rh_book_does_not_wait_for_rest_metadata(self):
        self.assert_warm_native_book('lighter_rh')

    def assert_warm_native_book(self, venue):
        client = Mock()
        client.get_snapshot.side_effect = AssertionError('REST metadata must not block a warm book')
        adapter = SyncVenueAdapter(client, venue, venue, [], VenueCapability(
            market_data=['websocket'], catalog_mode=CatalogMode.full))
        adapter._live_feed = SimpleNamespace(discovery_issue=lambda symbol: None, snapshot=lambda symbol: {
            'source': 'lighter_websocket', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'bids': [{'price': 100, 'size': 2}], 'asks': [{'price': 101, 'size': 3}],
        })
        quote = asyncio.run(adapter.get_quote('ETH-PERP'))
        assert (quote.bid, quote.ask, quote.stale, quote.executable) == (100, 101, False, False)
        assert quote.metadata['book_bids'][0]['size'] == 2
        client.get_snapshot.assert_not_called()


    def test_expired_native_book_is_never_relabelled_fresh(self):
        client = Mock()
        client.get_snapshot.return_value = {'exchange_symbol': 'ETH', 'instrument_type': 'perpetual'}
        adapter = SyncVenueAdapter(client, 'lighter', 'Lighter', [], VenueCapability(
            market_data=['websocket'], catalog_mode=CatalogMode.full))
        adapter._live_feed = SimpleNamespace(discovery_issue=lambda symbol: None, snapshot=lambda symbol: {
            'source': 'lighter_websocket',
            'fetched_at': (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
            'bids': [{'price': 100, 'size': 2}], 'asks': [{'price': 101, 'size': 3}],
        })
        quote = asyncio.run(adapter.get_quote('ETH'))
        assert quote.stale and quote.age_seconds >= 30


    def test_fast_market_keeps_refreshing_while_another_market_is_pending(self):
        module = self.stream_module()
        capability = SimpleNamespace(market_data=['websocket'])
        self.patch_attribute(module.state, 'adapters', {
            name: SimpleNamespace(capability=capability) for name in ('alpha', 'beta')})
        calls, cancelled, frames = [], [], []

        async def calculate(symbol, *args, **kwargs):
            calls.append(symbol)
            if symbol == 'SLOW':
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(symbol)
            if symbol == 'BROKEN':
                raise RuntimeError('single market unavailable')
            return [Opportunity(canonical_symbol=symbol, buy_venue='alpha', sell_venue='beta',
                                buy_price=100, sell_price=101, gross_bps=100)]

        class Socket:
            async def accept(self):
                pass

            async def send_json(self, frame):
                if 'window_symbols' in frame:
                    frames.append(frame)
                    if len(frames) == 2:
                        raise WebSocketDisconnect()

        self.patch_attribute(module, 'calculate_opportunities', calculate)

        async def run():
            await asyncio.wait_for(module.opportunity_stream(
                Socket(), 'FAST,SLOW,BROKEN', 'alpha,beta', 12, 0, 0), timeout=3)

        asyncio.run(run())
        assert calls.count('FAST') == 2 and calls.count('SLOW') == 1
        assert cancelled == ['SLOW']
        assert all(set(frame['window_symbols']) == {'FAST', 'BROKEN'} for frame in frames)
        assert all(frame['rows'][0]['canonical_symbol'] == 'FAST' for frame in frames)


    def test_large_catalog_has_bounded_pending_work_and_cleans_up(self):
        module = self.stream_module()
        capability = SimpleNamespace(market_data=['websocket'])
        self.patch_attribute(module.state, 'adapters', {
            name: SimpleNamespace(capability=capability) for name in ('alpha', 'beta')})
        calls, cancelled = [], []

        async def calculate(symbol, *args, **kwargs):
            calls.append(symbol)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(symbol)

        class Socket:
            heartbeats = 0

            async def accept(self):
                pass

            async def send_json(self, frame):
                self.heartbeats += 1
                if self.heartbeats == 2:
                    raise WebSocketDisconnect()

        self.patch_attribute(module, 'calculate_opportunities', calculate)

        async def run():
            await asyncio.wait_for(module.opportunity_stream(
                Socket(), ','.join('M' + str(i) for i in range(240)), 'alpha,beta', 12, 0, 0), timeout=3)

        asyncio.run(run())
        assert len(calls) == len(set(calls)) == 100
        assert sorted(cancelled) == sorted(calls)
