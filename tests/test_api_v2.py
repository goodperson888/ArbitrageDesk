from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api_v2.app import app, state
from src.api_v2.adapters import VenueAdapter
from src.api_v2.models import CatalogMode, Instrument, QuoteSnapshot, VenueCapability


class FakeAdapter(VenueAdapter):
    def __init__(self, venue, ask, bid):
        self.venue = venue
        self.display_name = venue.title()
        self.configured_symbols = ["ETH"]
        self.capability = VenueCapability(
            market_data=["test"], catalog_mode=CatalogMode.full,
            executable_quotes=True, order_submission=False,
        )
        self.ask = ask
        self.bid = bid

    async def list_instruments(self):
        return [Instrument(venue=self.venue, venue_symbol="ETH-USD",
                           canonical_symbol="ETH", base_asset="ETH")]

    async def get_quote(self, symbol):
        return QuoteSnapshot(
            venue=self.venue, symbol=symbol, venue_symbol="ETH-USD",
            instrument_type="perpetual", bid=self.bid, ask=self.ask,
            mark_price=(self.bid + self.ask) / 2, source="test",
            executable=True, stale=False, age_seconds=0,
            fetched_at=datetime.now(timezone.utc),
        )


def test_v2_health_and_venues():
    with TestClient(app) as client:
        response = client.get("/api/v2/health")
        assert response.status_code == 200
        assert response.json()["mode"] == "readonly"
        response = client.get("/api/v2/venues")
        assert response.status_code == 200
        assert {item["venue"] for item in response.json()} == {"variational", "lighter", "standx"}


def test_v2_opportunities_use_canonical_pair_and_never_submit():
    previous = state.adapters
    try:
        with TestClient(app) as client:
            state.adapters = {"alpha": FakeAdapter("alpha", ask=100, bid=99),
                              "beta": FakeAdapter("beta", ask=101, bid=100)}
            response = client.get("/api/v2/opportunities?symbol=ETH&pair=alpha,beta")
            assert response.status_code == 200
            rows = response.json()
            assert len(rows) == 2
            assert rows[0]["buy_venue"] == "alpha"
            assert rows[0]["sell_venue"] == "beta"
            assert rows[0]["executable"] is True
            assert all("order" not in row for row in rows)
    finally:
        state.adapters = previous


def test_v2_opportunities_without_pair_scan_all_directions():
    previous = state.adapters
    try:
        with TestClient(app) as client:
            state.adapters = {"alpha": FakeAdapter("alpha", ask=100, bid=99),
                              "beta": FakeAdapter("beta", ask=101, bid=100),
                              "gamma": FakeAdapter("gamma", ask=102, bid=101)}
            response = client.get("/api/v2/opportunities?symbol=ETH-PERP")
            assert response.status_code == 200
            rows = response.json()
            assert len(rows) == 6
            assert rows[0]["gross_bps"] >= rows[-1]["gross_bps"]
            assert 0 <= rows[0]["buy_age_seconds"] < 2
    finally:
        state.adapters = previous
