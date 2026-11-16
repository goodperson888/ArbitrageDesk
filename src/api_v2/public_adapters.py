from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from .adapters import VenueAdapter, canonical_perpetual
from .models import CatalogMode, Instrument, QuoteSnapshot, VenueCapability


def _number(value: Any) -> Optional[float]:
    try:
        number = None if value is None or value == "" or isinstance(value, bool) else float(value)
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime:
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


class PublicRestAdapter(VenueAdapter):
    """Shared read-only REST adapter behavior for public exchange feeds."""

    def __init__(self, venue: str, display_name: str, base_url: str,
                 proxy_url: Optional[str] = None):
        self.venue = venue
        self.display_name = display_name
        self.base_url = base_url.rstrip("/")
        self.configured_symbols: List[str] = []
        self.capability = VenueCapability(
            market_data=["rest"], catalog_mode=CatalogMode.full,
            executable_quotes=False, order_submission=False,
            order_status_stream=False, fee_schedule=False, funding=True,
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0), proxy=proxy_url
        )
        self.proxy_url = proxy_url
        self._last_error: Optional[str] = None
        self._direct_client: Optional[httpx.AsyncClient] = None
        self._proxy_failed_until = 0.0
        self._instruments_cache: Optional[tuple[datetime, List[Instrument]]] = None
        self._quote_cache: Dict[str, tuple[datetime, QuoteSnapshot]] = {}

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        use_proxy = bool(self.proxy_url) and time.monotonic() >= self._proxy_failed_until
        try:
            if not self.proxy_url or use_proxy:
                response = await self._client.get(self.base_url + path, params=params)
            else:
                raise httpx.ProxyError("proxy temporarily unavailable")
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout,
                httpx.ReadTimeout, httpx.PoolTimeout):
            # Prefer the configured local proxy, but do not turn a stopped
            # proxy process into an empty dashboard. Public exchange feeds can
            # fall back to direct HTTPS; authenticated/order traffic does not.
            if not self.proxy_url:
                raise
            self._proxy_failed_until = time.monotonic() + 60.0
            if self._direct_client is None:
                self._direct_client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))
            response = await self._direct_client.get(self.base_url + path, params=params)
        response.raise_for_status()
        return response.json()

    async def list_instruments(self) -> List[Instrument]:
        now = datetime.now(timezone.utc)
        if self._instruments_cache and (now - self._instruments_cache[0]).total_seconds() < 60:
            return list(self._instruments_cache[1])
        rows = await self._list_instruments()
        self._instruments_cache = (now, rows)
        return list(rows)

    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        key = symbol.upper().replace("-PERP", "")
        cached = self._quote_cache.get(key)
        if cached and (datetime.now(timezone.utc) - cached[0]).total_seconds() < 1.0:
            return cached[1].model_copy(deep=True)
        try:
            quote = await self._get_quote(key)
        except Exception as exc:
            self._last_error = f"行情请求失败：{type(exc).__name__}"
            quote = QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=key,
                instrument_type="perpetual", source=self.venue,
                stale=True, reason=f"只读行情请求失败：{type(exc).__name__}",
            )
        else:
            self._last_error = quote.reason if quote.stale and quote.reason else None
        self._quote_cache[key] = (datetime.now(timezone.utc), quote)
        return quote.model_copy(deep=True)

    async def get_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        """Return a set of quotes, using a venue bulk endpoint when available.

        The fallback keeps the common adapter useful for a future public feed;
        concrete exchanges override ``_get_bulk_quotes`` to avoid one HTTP
        request per symbol during an all-market scan.
        """
        keys = sorted({str(symbol).upper().replace("-PERP", "") for symbol in symbols if symbol})
        try:
            rows = await self._get_bulk_quotes(keys)
        except (AttributeError, NotImplementedError, httpx.HTTPError):
            rows = {}
        if rows:
            for key, quote in rows.items():
                self._quote_cache[key] = (datetime.now(timezone.utc), quote)
            return {key: quote.model_copy(deep=True) for key, quote in rows.items()}
        # A bounded concurrency fallback prevents a malformed bulk response
        # from turning into an unbounded proxy burst.
        semaphore = asyncio.Semaphore(16)
        async def one(key: str):
            async with semaphore:
                return key, await self.get_quote(key)
        pairs = await asyncio.gather(*(one(key) for key in keys), return_exceptions=True)
        return {key: value for item in pairs if isinstance(item, tuple) for key, value in [item]}

    async def close(self) -> None:
        clients = [self._client]
        if self._direct_client is not None:
            clients.append(self._direct_client)
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)

    def status(self, error: Optional[str] = None) -> VenueStatus:
        return super().status(error or self._last_error)

    async def _list_instruments(self) -> List[Instrument]:
        raise NotImplementedError

    async def _get_quote(self, symbol: str) -> QuoteSnapshot:
        raise NotImplementedError

    async def _get_bulk_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        raise NotImplementedError


class BinanceFuturesAdapter(PublicRestAdapter):
    """Binance USDⓈ-M perpetual public market data."""

    def __init__(self, base_url: str = "https://fapi.binance.com",
                 proxy_url: Optional[str] = None):
        super().__init__("binance", "Binance Futures", base_url.rstrip("/"), proxy_url)

    async def _list_instruments(self) -> List[Instrument]:
        payload = await self._get("/fapi/v1/exchangeInfo")
        result = []
        for row in payload.get("symbols", []):
            if row.get("status") != "TRADING" or row.get("contractType") != "PERPETUAL":
                continue
            if row.get("quoteAsset") != "USDT":
                continue
            base = str(row.get("baseAsset", "")).upper()
            if not base:
                continue
            result.append(Instrument(
                venue=self.venue, venue_symbol=row["symbol"],
                canonical_symbol=canonical_perpetual(base),
                instrument_type="perpetual", base_asset=base,
                quote_asset=row.get("quoteAsset"),
                size_decimals=row.get("quantityPrecision"),
                price_decimals=row.get("pricePrecision"),
                metadata={"contract_type": row.get("contractType")},
            ))
        return result

    async def _get_quote(self, symbol: str) -> QuoteSnapshot:
        venue_symbol = f"{symbol}USDT"
        ticker = await self._get("/fapi/v1/ticker/bookTicker", {"symbol": venue_symbol})
        try:
            premium = await self._get("/fapi/v1/premiumIndex", {"symbol": venue_symbol})
        except Exception:
            premium = {}
        fetched = _time(ticker.get("time")) if ticker.get("time") else datetime.now(timezone.utc)
        return QuoteSnapshot(
            venue=self.venue, symbol=symbol, venue_symbol=venue_symbol,
            instrument_type="perpetual", bid=_number(ticker.get("bidPrice")),
            ask=_number(ticker.get("askPrice")),
            mark_price=_number(premium.get("markPrice")),
            quote_asset="USDT", source="binance_futures_rest",
            executable=False, stale=(datetime.now(timezone.utc) - fetched).total_seconds() > 10,
            age_seconds=max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()),
            fetched_at=fetched,
            reason="只读行情，未接入下单",
            metadata={"funding_rate": _number(premium.get("lastFundingRate")),
                      "bid_size_base": _number(ticker.get("bidQty")),
                      "ask_size_base": _number(ticker.get("askQty"))},
        )

    async def _get_bulk_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        tickers, premiums = await asyncio.gather(
            self._get("/fapi/v1/ticker/bookTicker"),
            self._get("/fapi/v1/premiumIndex"),
        )
        wanted = {f"{symbol}USDT": symbol for symbol in symbols}
        premium_by_symbol = {str(row.get("symbol")): row for row in premiums if isinstance(row, dict)}
        result = {}
        for row in tickers if isinstance(tickers, list) else []:
            venue_symbol = str(row.get("symbol", ""))
            symbol = wanted.get(venue_symbol)
            if not symbol:
                continue
            premium = premium_by_symbol.get(venue_symbol, {})
            fetched = _time(row.get("time")) if row.get("time") else datetime.now(timezone.utc)
            result[symbol] = QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=venue_symbol,
                instrument_type="perpetual", bid=_number(row.get("bidPrice")),
                ask=_number(row.get("askPrice")), mark_price=_number(premium.get("markPrice")),
                quote_asset="USDT", source="binance_futures_rest", executable=False,
                stale=(datetime.now(timezone.utc) - fetched).total_seconds() > 10,
                age_seconds=max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()),
                fetched_at=fetched, reason="只读行情，未接入下单",
                metadata={"funding_rate": _number(premium.get("lastFundingRate")),
                          "bid_size_base": _number(row.get("bidQty")),
                          "ask_size_base": _number(row.get("askQty"))},
            )
        return result


class GateFuturesAdapter(PublicRestAdapter):
    """Gate USDT-settled perpetual public market data."""

    def __init__(self, base_url: str = "https://api.gateio.ws/api/v4",
                 proxy_url: Optional[str] = None):
        super().__init__("gate", "Gate Futures", base_url.rstrip("/"), proxy_url)

    @staticmethod
    def _contract(symbol: str) -> str:
        return f"{symbol}_USDT"

    async def _list_instruments(self) -> List[Instrument]:
        rows = await self._get("/futures/usdt/contracts")
        result = []
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("name", ""))
            if not name.endswith("_USDT") or row.get("in_delisting"):
                continue
            base = name[:-5].upper()
            result.append(Instrument(
                venue=self.venue, venue_symbol=name,
                canonical_symbol=canonical_perpetual(base),
                instrument_type="perpetual", base_asset=base,
                quote_asset="USDT",
                min_base_amount=_number(row.get("order_size_min")),
                metadata={"quanto_multiplier": row.get("quanto_multiplier"),
                          "funding_rate": row.get("funding_rate_indicative"),
                          "contract_type": row.get("contract_type"),
                          "is_pre_market": bool(row.get("is_pre_market"))},
            ))
        return result

    async def _get_quote(self, symbol: str) -> QuoteSnapshot:
        contract = self._contract(symbol)
        rows = await self._get("/futures/usdt/tickers", {"contract": contract})
        row = rows[0] if isinstance(rows, list) and rows else {}
        fetched = _time(row.get("time")) if row.get("time") else datetime.now(timezone.utc)
        return QuoteSnapshot(
            venue=self.venue, symbol=symbol, venue_symbol=contract,
            instrument_type="perpetual", bid=_number(row.get("highest_bid") or row.get("bid")),
            ask=_number(row.get("lowest_ask") or row.get("ask")),
            mark_price=_number(row.get("mark_price")), quote_asset="USDT",
            source="gate_futures_rest", executable=False,
            stale=(datetime.now(timezone.utc) - fetched).total_seconds() > 10,
            age_seconds=max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()),
            fetched_at=fetched, reason="只读行情，未接入下单",
            metadata={"funding_rate": _number(row.get("funding_rate")),
                      "funding_rate_indicative": _number(row.get("funding_rate_indicative")),
                      "bid_size_base": _number(row.get("highest_size")) * _number(row.get("quanto_multiplier")) if _number(row.get("highest_size")) is not None and _number(row.get("quanto_multiplier")) is not None else None,
                      "ask_size_base": _number(row.get("lowest_size")) * _number(row.get("quanto_multiplier")) if _number(row.get("lowest_size")) is not None and _number(row.get("quanto_multiplier")) is not None else None},
        )

    async def _get_bulk_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        rows = await self._get("/futures/usdt/tickers")
        wanted = {self._contract(symbol): symbol for symbol in symbols}
        result = {}
        for row in rows if isinstance(rows, list) else []:
            contract = str(row.get("contract", ""))
            symbol = wanted.get(contract)
            if not symbol:
                continue
            fetched = _time(row.get("time")) if row.get("time") else datetime.now(timezone.utc)
            result[symbol] = QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=contract,
                instrument_type="perpetual", bid=_number(row.get("highest_bid") or row.get("bid")),
                ask=_number(row.get("lowest_ask") or row.get("ask")),
                mark_price=_number(row.get("mark_price")), quote_asset="USDT",
                source="gate_futures_rest", executable=False,
                stale=(datetime.now(timezone.utc) - fetched).total_seconds() > 10,
                age_seconds=max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()),
                fetched_at=fetched, reason="只读行情，未接入下单",
                metadata={"funding_rate": _number(row.get("funding_rate")),
                          "funding_rate_indicative": _number(row.get("funding_rate_indicative")),
                          "bid_size_base": _number(row.get("highest_size")) * _number(row.get("quanto_multiplier")) if _number(row.get("highest_size")) is not None and _number(row.get("quanto_multiplier")) is not None else None,
                          "ask_size_base": _number(row.get("lowest_size")) * _number(row.get("quanto_multiplier")) if _number(row.get("lowest_size")) is not None and _number(row.get("quanto_multiplier")) is not None else None},
            )
        return result


class OKXSwapAdapter(PublicRestAdapter):
    """OKX USDT-margined swap public market data."""

    def __init__(self, base_url: str = "https://www.okx.com",
                 proxy_url: Optional[str] = None):
        super().__init__("okx", "OKX Swap", base_url.rstrip("/"), proxy_url)

    async def _size_factors(self) -> Dict[str, float]:
        try:
            instruments = await self.list_instruments()
        except Exception:
            return {}
        result = {}
        for instrument in instruments:
            value = _number(instrument.metadata.get("ct_val"))
            # USDT linear contracts express ctVal in the underlying asset.
            # Missing or different units must not be guessed as one coin.
            if (value is not None and value > 0 and
                    instrument.metadata.get("ct_val_ccy") == instrument.base_asset):
                result[instrument.venue_symbol] = value
        return result

    @staticmethod
    def _base_size(raw_size: Any, factor: Optional[float]) -> Optional[float]:
        size = _number(raw_size)
        return size * factor if size is not None and size >= 0 and factor is not None else None

    async def _list_instruments(self) -> List[Instrument]:
        payload = await self._get("/api/v5/public/instruments", {"instType": "SWAP"})
        result = []
        for row in payload.get("data", []) if isinstance(payload, dict) else []:
            inst_id = str(row.get("instId", ""))
            if row.get("state") != "live" or row.get("settleCcy") != "USDT" or not inst_id.endswith("-USDT-SWAP"):
                continue
            base = str(row.get("baseCcy") or inst_id.split("-", 1)[0]).upper()
            if not base:
                continue
            result.append(Instrument(
                venue=self.venue, venue_symbol=inst_id,
                canonical_symbol=canonical_perpetual(base),
                instrument_type="perpetual", base_asset=base,
                quote_asset="USDT", size_decimals=None,
                price_decimals=None,
                metadata={"ct_val": row.get("ctVal"), "ct_val_ccy": row.get("ctValCcy"),
                          "tick_size": row.get("tickSz"), "rule_type": row.get("ruleType"),
                          "is_pre_market": row.get("ruleType") == "pre_market"},
            ))
        return result

    async def _get_quote(self, symbol: str) -> QuoteSnapshot:
        inst_id = f"{symbol}-USDT-SWAP"
        factor = (await self._size_factors()).get(inst_id)
        payload = await self._get("/api/v5/market/ticker", {"instId": inst_id})
        row = payload.get("data", [{}])[0] if isinstance(payload, dict) else {}
        fetched = _time(row.get("ts")) if row.get("ts") else datetime.now(timezone.utc)
        return QuoteSnapshot(
            venue=self.venue, symbol=symbol, venue_symbol=inst_id,
            instrument_type="perpetual", bid=_number(row.get("bidPx")),
            ask=_number(row.get("askPx")), mark_price=_number(row.get("last")),
            quote_asset="USDT", source="okx_swap_rest", executable=False,
            stale=(datetime.now(timezone.utc) - fetched).total_seconds() > 10,
            age_seconds=max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()),
            fetched_at=fetched, reason="只读行情，未接入下单",
            metadata={"last": _number(row.get("last")), "bid_size": _number(row.get("bidSz")),
                      "ask_size": _number(row.get("askSz")),
                      "bid_size_base": self._base_size(row.get("bidSz"), factor),
                      "ask_size_base": self._base_size(row.get("askSz"), factor)},
        )

    async def _get_bulk_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        payload = await self._get("/api/v5/market/tickers", {"instType": "SWAP"})
        factors = await self._size_factors()
        wanted = {f"{symbol}-USDT-SWAP": symbol for symbol in symbols}
        result = {}
        for row in payload.get("data", []) if isinstance(payload, dict) else []:
            inst_id = str(row.get("instId", ""))
            symbol = wanted.get(inst_id)
            if not symbol:
                continue
            fetched = _time(row.get("ts")) if row.get("ts") else datetime.now(timezone.utc)
            result[symbol] = QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=inst_id,
                instrument_type="perpetual", bid=_number(row.get("bidPx")),
                ask=_number(row.get("askPx")), mark_price=_number(row.get("last")),
                quote_asset="USDT", source="okx_swap_rest", executable=False,
                stale=(datetime.now(timezone.utc) - fetched).total_seconds() > 10,
                age_seconds=max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds()),
                fetched_at=fetched, reason="只读行情，未接入下单",
                metadata={"last": _number(row.get("last")), "bid_size": _number(row.get("bidSz")),
                          "ask_size": _number(row.get("askSz")),
                          "bid_size_base": self._base_size(row.get("bidSz"), factors.get(inst_id)),
                          "ask_size_base": self._base_size(row.get("askSz"), factors.get(inst_id))},
            )
        return result
