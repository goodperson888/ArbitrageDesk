from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

from fastapi.testclient import TestClient

from src.api_v2.app import app, state
from src.api_v2.models import Instrument
from src.common.market_data import MarketDataError


def adapter(venue, **kwargs):
    return SimpleNamespace(venue=venue, display_name=venue, list_instruments=AsyncMock(**kwargs))


class CatalogApiTests(unittest.TestCase):
    def setUp(self):
        previous = state.adapters
        self.addCleanup(setattr, state, 'adapters', previous)
        self.client = TestClient(app)


    def test_requested_catalog_reports_transport_failure(self):
        state.adapters = {'rh': adapter('rh', side_effect=MarketDataError('本地代理不可用或直连失败'))}
        response = self.client.get('/api/v2/instruments?venue=rh')
        self.assertEqual(response.status_code, 503)
        self.assertIn('本地代理不可用或直连失败', response.json()['detail'])


    def test_catalog_does_not_expose_arbitrary_exception_text(self):
        state.adapters = {'rh': adapter('rh', side_effect=ValueError('private-token'))}
        response = self.client.get('/api/v2/instruments?venue=rh')
        self.assertEqual(response.status_code, 503)
        self.assertIn('ValueError', response.json()['detail'])
        self.assertNotIn('private-token', response.text)


    def test_requested_catalog_timeout_is_not_an_empty_success(self):
        state.adapters = {'rh': adapter('rh', return_value=None)}
        response = self.client.get('/api/v2/instruments?venue=rh')
        self.assertEqual(response.status_code, 504)
        self.assertIn('请求超时', response.json()['detail'])

    def test_combined_catalog_retains_healthy_venues(self):
        state.adapters = {
            'rh': adapter('rh', side_effect=MarketDataError('连接失败')),
            'arcus': adapter('arcus', return_value=[Instrument(
                venue='arcus', venue_symbol='BTC-USD', canonical_symbol='BTC-PERP')]),
        }
        response = self.client.get('/api/v2/instruments')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['venue'] for row in response.json()], ['arcus'])
