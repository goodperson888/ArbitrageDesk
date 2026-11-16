from __future__ import annotations

import asyncio
import copy
import math
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from src.common.config_loader import ConfigLoader
from src.common.discovery import DiscoveryCooldown, DiscoveryCooldownError
from src.lighter.api_trader import LighterApiTrader
from src.variational.public_client import VariationalPublicClient
from .models import CatalogMode, Instrument, QuoteSnapshot, VenueCapability, VenueStatus


def _consume_task_exception(task: asyncio.Task) -> None:
    """Retrieve a shielded worker exception so failed retries stay observable but quiet."""
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


def _age_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError):
        return None


def canonical_perpetual(symbol: str) -> str:
    """Map venue spellings (ETH, ETH-USD, ETH-PERP) to one key."""
    base = (symbol or "").split("-", 1)[0].upper()
    return f"{base}-PERP" if base else symbol


def base_symbol(symbol: str) -> str:
    """Return the base key expected by the legacy venue clients."""
    value = (symbol or "").split("-", 1)[0].upper()
    return value or symbol


class VenueAdapter(ABC):
    venue: str
    display_name: str
    capability: VenueCapability
    configured_symbols: List[str]

    @abstractmethod
    async def list_instruments(self) -> List[Instrument]:
        raise NotImplementedError

    @abstractmethod
    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        raise NotImplementedError

    async def get_reference_candles(self, symbol: str, seconds: int = 86400,
                                    interval_seconds: int = 300) -> List[dict]:
        """Return non-executable exchange candle closes when supported."""
        return []

    def status(self, error: Optional[str] = None) -> VenueStatus:
        return VenueStatus(
            venue=self.venue,
            display_name=self.display_name,
            status="degraded" if error else "configured",
            transport=list(self.capability.market_data),
            capabilities=self.capability,
            configured_symbols=self.configured_symbols,
            last_error=error,
        )

    async def close(self) -> None:
        return None


class SyncVenueAdapter(VenueAdapter):
    """Adapter for the current proven synchronous public clients.

    The blocking client is always called in a worker thread.  It must never
    run directly on the FastAPI event loop.
    """

    def __init__(self, client: Any, venue: str, display_name: str,
                 configured_symbols: List[str], capability: VenueCapability):
        self.client = client
        self.venue = venue
        self.display_name = display_name
        self.configured_symbols = configured_symbols
        self.capability = capability
        self._last_error: Optional[str] = None
        self._instrument_cache: Optional[tuple[datetime, List[Instrument]]] = None
        self._instrument_task: Optional[asyncio.Task] = None
        self._quote_cache: Dict[str, tuple[datetime, QuoteSnapshot]] = {}
        self._quote_tasks: Dict[str, asyncio.Task] = {}
        self._live_feed = None
        self._discovery_cooldown = DiscoveryCooldown('Lighter RH' if venue == 'lighter_rh' else 'Lighter')

    def _start_lighter_feed(self, markets: Dict[str, Dict[str, Any]]) -> None:
        """Start the deployment-specific native order-book stream once.

        REST remains the fallback for cold starts and stream outages.  The
        websocket client owns its reconnect thread, so it never blocks the
        FastAPI event loop.
        """
        if self.venue not in ("lighter", "lighter_rh") or self._live_feed is not None:
            return
        api = getattr(self.client, "config", {}).get("api", {})
        ws_url = api.get("ws_url")
        if not ws_url:
            return
        mapping = {symbol: row.get("market_id") for symbol, row in markets.items()
                   if row.get("market_id") is not None}
        if not mapping:
            return
        from src.common.live_feed import LighterBookFeed
        self._live_feed = LighterBookFeed(mapping, api.get("proxy_url"), ws_url=ws_url)
        self._live_feed.start()

    async def list_instruments(self) -> List[Instrument]:
        def read() -> List[Instrument]:
            if self.capability.catalog_mode == CatalogMode.configured_only:
                rows = []
                for symbol in self.configured_symbols:
                    rows.append(Instrument(
                        venue=self.venue, venue_symbol=symbol,
                        canonical_symbol=canonical_perpetual(symbol),
                        instrument_type="perpetual",
                    ))
                return rows
            markets, _ = self.client.get_markets()
            self._start_lighter_feed(markets)
            return [Instrument(
                venue=self.venue,
                venue_symbol=symbol,
                canonical_symbol=canonical_perpetual(symbol),
                instrument_type="perpetual",
                quote_asset=row.get("quote_asset"),
                enabled=row.get("status") in (None, "active", 1, True),
                size_decimals=row.get("supported_size_decimals"),
                price_decimals=row.get("supported_price_decimals"),
                min_base_amount=row.get("min_base_amount"),
                min_quote_amount=row.get("min_quote_amount"),
                metadata={"market_id": row.get("market_id")},
            ) for symbol, row in markets.items()]

        now = datetime.now(timezone.utc)
        if self._instrument_cache and (now - self._instrument_cache[0]).total_seconds() < 30:
            return list(self._instrument_cache[1])
        if self._instrument_task is None or self._instrument_task.done():
            async def load_instruments() -> List[Instrument]:
                # Cache inside the worker task.  The HTTP route is deliberately
                # bounded, so its waiter may be cancelled before a slow venue
                # response completes (Lighter can take 20-30s on cold start).
                result = await asyncio.to_thread(read)
                self._instrument_cache = (datetime.now(timezone.utc), result)
                return result
            self._instrument_task = asyncio.create_task(load_instruments())
            self._instrument_task.add_done_callback(_consume_task_exception)
        try:
            result = await asyncio.shield(self._instrument_task)
            return list(result)
        except Exception:
            if self._instrument_task and self._instrument_task.done():
                self._instrument_task = None
            if self._instrument_cache:
                return list(self._instrument_cache[1])
            raise

    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        # A warm native book is sufficient for the read-only BBO. Refreshing
        # the REST catalog first can otherwise hide a healthy websocket behind
        # a slow metadata request on every catalog cache expiry.
        if self.venue in ("lighter", "lighter_rh") and self._live_feed is not None:
            book = self._live_feed.snapshot(base_symbol(symbol))
            if book is not None:
                age = _age_seconds(book.get("fetched_at"))
                if age is not None and 0 <= age <= 10 and book.get('bids') and book.get('asks'):
                    bids, asks = book.get("bids") or [], book.get("asks") or []
                    self._discovery_cooldown.fresh_stream(base_symbol(symbol))
                    self._last_error = None
                    return QuoteSnapshot(
                        venue=self.venue, symbol=symbol, venue_symbol=base_symbol(symbol),
                        instrument_type="perpetual", source="lighter_websocket",
                        bid=bids[0].get("price") if bids else None,
                        ask=asks[0].get("price") if asks else None,
                        executable=False, stale=False, age_seconds=age,
                        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=age),
                        metadata={"bid_size_base": bids[0].get("size") if bids else None,
                                  'funding': book.get('funding'),
                                  "ask_size_base": asks[0].get("size") if asks else None,
                                  "book_bids": bids, "book_asks": asks,
                                  "book_levels": len(bids) + len(asks)},
                    )
            if self._live_feed.discovery_issue(base_symbol(symbol)) == 'empty':
                self._discovery_cooldown.observe(base_symbol(symbol), 'empty')
        if self.venue in ("lighter", "lighter_rh"):
            try:
                self._discovery_cooldown.check(base_symbol(symbol))
            except DiscoveryCooldownError as exc:
                # Known empty markets do not need a REST catalog refresh or
                # consume the shared book allowance while their probe waits.
                self._last_error = str(exc)
                return QuoteSnapshot(venue=self.venue, symbol=symbol, venue_symbol=base_symbol(symbol),
                    instrument_type='perpetual', source='lighter_orderbook', stale=True, reason=str(exc))
        if self.venue in ("lighter", "lighter_rh") and self._live_feed is None:
            try:
                await self.list_instruments()
            except Exception:
                # The REST quote path below remains the cold-start fallback.
                pass
        def read() -> QuoteSnapshot:
            request_symbol = base_symbol(symbol)
            snapshot = self.client.get_snapshot(request_symbol)
            age = snapshot.get("quote_age_seconds")
            if age is None:
                age = _age_seconds(snapshot.get("fetched_at"))
            bid = snapshot.get("bid")
            ask = snapshot.get("ask")
            book_note = None
            book = None
            bid_size_base = None
            ask_size_base = None
            if self.venue in ("lighter", "lighter_rh"):
                try:
                    if self._live_feed is not None:
                        book = self._live_feed.snapshot(request_symbol)
                        issue = self._live_feed.discovery_issue(request_symbol)
                        if issue == 'empty':
                            self._discovery_cooldown.observe(request_symbol, 'empty')
                    if book is None or not book.get('bids') or not book.get('asks'):
                        self._discovery_cooldown.check(request_symbol)
                        book = self.client.get_order_book(request_symbol, discovery=True)
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    if not bids or not asks:
                        self._discovery_cooldown.failed(request_symbol, 'empty')
                        self._discovery_cooldown.check(request_symbol)
                    if book.get('source') == 'lighter_websocket':
                        self._discovery_cooldown.fresh_stream(request_symbol)
                    else:
                        self._discovery_cooldown.recovered(request_symbol)
                    bid = bids[0].get("price") if bids else None
                    ask = asks[0].get("price") if asks else None
                    bid_size_base = bids[0].get("size") if bids else None
                    ask_size_base = asks[0].get("size") if asks else None
                    age = _age_seconds(book.get("fetched_at"))
                except Exception as exc:
                    from src.common.rate_limit import DiscoveryThrottleError, RateLimitError
                    book_note = (str(exc) if isinstance(exc, (DiscoveryThrottleError, RateLimitError, DiscoveryCooldownError))
                                 else "Lighter 盘口获取失败: " + type(exc).__name__)
            self._last_error = book_note
            return QuoteSnapshot(
                venue=self.venue,
                symbol=symbol,
                venue_symbol=snapshot.get("exchange_symbol", symbol),
                instrument_type=snapshot.get("instrument_type", "perpetual"),
                bid=bid,
                ask=ask,
                mark_price=snapshot.get("mark_price"),
                quote_asset=snapshot.get("quote_asset"),
                source=("lighter_websocket" if self._live_feed is not None and book and book.get("source") == "lighter_websocket"
                        else "lighter_orderbook") if self.venue in ("lighter", "lighter_rh") else snapshot.get("source", self.venue),
                executable=bool(snapshot.get("executable")),
                stale=(bool(snapshot.get("quote_stale", True)) if self.venue not in ("lighter", "lighter_rh") else bool(book_note)) or age is None or age > 10,
                age_seconds=age,
                fetched_at=datetime.now(timezone.utc) - timedelta(seconds=age or 0),
                reason=book_note or snapshot.get("note"),
                metadata={"quote_notional_usdc": snapshot.get("quote_notional_usdc"),
                          'funding': book.get('funding') if book else None,
                          "bid_size_base": bid_size_base,
                          "ask_size_base": ask_size_base,
                          "book_bids": book.get("bids") if book else None,
                          "book_asks": book.get("asks") if book else None,
                          "book_levels": len(book.get("bids") or []) + len(book.get("asks") or []) if book else None},
            )

        cache_key = base_symbol(symbol)
        cached = self._quote_cache.get(cache_key)
        if cached and (datetime.now(timezone.utc) - cached[0]).total_seconds() < 2:
            return cached[1].model_copy(deep=True)
        task = self._quote_tasks.get(cache_key)
        if task is None or task.done():
            async def load_quote() -> QuoteSnapshot:
                # See list_instruments: keep the result even when the short
                # request timeout cancels the caller waiting on this task.
                result = await asyncio.to_thread(read)
                self._quote_cache[cache_key] = (datetime.now(timezone.utc), result)
                return result
            task = asyncio.create_task(load_quote())
            task.add_done_callback(_consume_task_exception)
            self._quote_tasks[cache_key] = task
        try:
            result = await asyncio.shield(task)
            return result.model_copy(deep=True)
        except Exception as exc:
            self._quote_tasks.pop(cache_key, None)
            self._last_error = f"行情请求失败：{type(exc).__name__}"
            return QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=symbol,
                instrument_type="unknown", source=self.venue, stale=True,
                reason=str(exc),
            )

    async def get_reference_candles(self, symbol: str, seconds: int = 86400,
                                    interval_seconds: int = 300) -> List[dict]:
        if self.venue not in ('lighter', 'lighter_rh'):
            return []
        resolution = {60: '1m', 300: '5m', 900: '15m', 3600: '1h'}.get(int(interval_seconds))
        if resolution is None:
            return []
        count_back = max(1, min(500, int(seconds // int(interval_seconds)) + 1))

        def read():
            market, _ = self.client.get_market(base_symbol(symbol))
            now_ms = int(time.time() * 1000)
            start_ms = max(0, now_ms - int(seconds) * 1000)
            data, _ = self.client.client.get('/api/v1/candles', {
                'market_id': market['market_id'], 'resolution': resolution,
                'start_timestamp': start_ms, 'end_timestamp': now_ms,
                'count_back': count_back, 'set_timestamp_to_end': False,
            }, ttl=300)
            rows = data.get('c') or data.get('candles') or []
            points = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                raw_ts = row.get('t') or row.get('timestamp') or row.get('openTime')
                close = row.get('c', row.get('close'))
                try:
                    timestamp = float(raw_ts)
                    if timestamp > 10_000_000_000_000:
                        timestamp /= 1_000_000
                    elif timestamp > 10_000_000_000:
                        timestamp /= 1_000
                    value = float(close)
                    if not math.isfinite(timestamp) or not math.isfinite(value) or value <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                points.append({'timestamp': timestamp, 'close': value,
                               'source': self.venue + '_candles',
                               'reference_kind': 'candle_close'})
            return sorted(points, key=lambda item: item['timestamp'])[-500:]

        return await asyncio.to_thread(read)

    def status(self, error: Optional[str] = None) -> VenueStatus:
        return super().status(error or self._last_error)

    async def close(self) -> None:
        if self._live_feed is not None:
            self._live_feed.stop()
        close = getattr(self.client, "close", None)
        if close:
            await asyncio.to_thread(close)


class StandXAdapter(VenueAdapter):
    venue = "standx"
    display_name = "StandX"

    def __init__(self, base_url: str = "https://perps.standx.com",
                 configured_symbols: Optional[List[str]] = None,
                 proxy_url: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.configured_symbols = configured_symbols or []
        self.capability = VenueCapability(
            # Current implementation polls the official REST order-book
            # endpoints; the API websocket below is a local polling stream,
            # not a native exchange websocket subscription.
            market_data=["rest"], catalog_mode=CatalogMode.full,
            executable_quotes=False, order_submission=False,
            order_status_stream=True, fee_schedule=True, funding=True,
        )
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0), proxy=proxy_url)
        self._proxy_url = proxy_url
        self._proxy_failed_until = 0.0
        self._direct_client = None
        self._catalog_lock = asyncio.Lock()
        self._catalog_cache: Optional[tuple[datetime, List[Instrument]]] = None

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        use_proxy = bool(self._proxy_url) and time.monotonic() >= self._proxy_failed_until
        try:
            if not self._proxy_url or use_proxy:
                response = await self._client.get(self.base_url + path, params=params)
            else:
                raise httpx.ProxyError("proxy temporarily unavailable")
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout,
                httpx.ReadTimeout, httpx.PoolTimeout):
            if not self._proxy_url:
                raise
            self._proxy_failed_until = time.monotonic() + 60.0
            if self._direct_client is None:
                self._direct_client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))
            response = await self._direct_client.get(self.base_url + path, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise RuntimeError("StandX 返回格式无效")
        return data

    async def list_instruments(self) -> List[Instrument]:
        async with self._catalog_lock:
            now = datetime.now(timezone.utc)
            if self._catalog_cache and (now - self._catalog_cache[0]).total_seconds() < 30:
                return list(self._catalog_cache[1])
            try:
                data = await self._get("/api/query_market_overview")
                rows = data.get("symbols", []) if isinstance(data, dict) else []
                async def enrich(row):
                    symbol = row.get("symbol")
                    base = row.get("base")
                    if not symbol or not base:
                        return None
                    info = {}
                    try:
                        info_data = await self._get("/api/query_symbol_info", {"symbol": symbol})
                        if isinstance(info_data, list) and info_data:
                            info = info_data[0]
                    except Exception:
                        pass
                    return Instrument(
                        venue=self.venue, venue_symbol=symbol, canonical_symbol=f"{base}-PERP",
                        instrument_type="perpetual", base_asset=base, quote_asset=row.get("quote"),
                        enabled=bool(info.get("enabled", True)),
                        size_decimals=info.get("qty_tick_decimals"),
                        price_decimals=info.get("price_tick_decimals"),
                        min_base_amount=float(info["min_order_qty"]) if info.get("min_order_qty") is not None else None,
                        metadata={"funding_rate": row.get("funding_rate"), "volume_24h": row.get("volume_24h"),
                                  "maker_fee": info.get("maker_fee"), "taker_fee": info.get("taker_fee")},
                    )
                result = [item for item in await asyncio.gather(*(enrich(row) for row in rows)) if item is not None]
                self._catalog_cache = (now, result)
                return list(result)
            except Exception:
                if self._catalog_cache:
                    return list(self._catalog_cache[1])
                raise

    @staticmethod
    def _venue_symbol(symbol: str) -> str:
        # The dashboard uses canonical bases (ETH/BTC); StandX public APIs
        # expose perpetuals as ETH-USD/BTC-USD.
        return f"{base_symbol(symbol)}-USD"

    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        try:
            venue_symbol = self._venue_symbol(symbol)
            market = await self._get("/api/query_symbol_market", {"symbol": venue_symbol})
            server_time = market.get("time")
            age = _age_seconds(server_time)
            fetched = datetime.now(timezone.utc)
            if server_time:
                try:
                    parsed_time = datetime.fromisoformat(str(server_time).replace("Z", "+00:00"))
                    if parsed_time.tzinfo is not None:
                        fetched = parsed_time
                except (TypeError, ValueError):
                    pass
            spread = market.get("spread") or [None, None]
            bid = float(spread[0]) if spread[0] is not None else None
            ask = float(spread[1]) if spread[1] is not None else None
            return QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=market.get("symbol", venue_symbol),
                instrument_type="perpetual", bid=bid, ask=ask,
                mark_price=float(market["mark_price"]) if market.get("mark_price") is not None else None,
                quote_asset=market.get("quote"), source="standx_rest", executable=False,
                stale=age is None or age > 10.0, age_seconds=age, fetched_at=fetched,
                metadata={"funding_rate": market.get("funding_rate"),
                          "next_funding_time": market.get("next_funding_time")},
            )
        except Exception as exc:
            return QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=self._venue_symbol(symbol),
                instrument_type="unknown", source="standx_rest", stale=True,
                reason=str(exc),
            )

    def status(self, error: Optional[str] = None) -> VenueStatus:
        return super().status(error)

    async def close(self) -> None:
        clients = [self._client]
        if self._direct_client is not None:
            clients.append(self._direct_client)
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


def build_adapters(config_path: str = "config/config.yaml") -> Dict[str, VenueAdapter]:
    # Import optional public connectors lazily so the proven venue adapters can
    # still be imported in environments that only install the core clients.
    from .public_adapters import BinanceFuturesAdapter, GateFuturesAdapter, OKXSwapAdapter
    from .arcus_adapter import ArcusAdapter
    config = ConfigLoader(config_path)
    proxy = config.get("common.network.proxy_url")
    v_cfg = copy.deepcopy(config.get_variational_config())
    l_cfg = copy.deepcopy(config.get_lighter_config())
    rh_cfg = copy.deepcopy(config.get("lighter_rh", {}))
    v_cfg.setdefault("api", {})
    l_cfg.setdefault("api", {})
    rh_cfg.setdefault("api", {})
    # Lighter's REST origin is reachable directly on some networks while the
    # local proxy may terminate its chunked response. An explicit per-venue
    # setting wins over the common proxy; V keeps inheriting the common value.
    if "proxy_url" not in v_cfg["api"]:
        v_cfg["api"]["proxy_url"] = proxy
    if "proxy_url" not in l_cfg["api"]:
        l_cfg["api"]["proxy_url"] = proxy
    if "proxy_url" not in rh_cfg["api"]:
        rh_cfg["api"]["proxy_url"] = proxy
    l_cfg["api"].setdefault("ws_url", "wss://mainnet.zklighter.elliot.ai/stream")
    rh_cfg["api"].setdefault("ws_url", "wss://api.rh.lighter.xyz/stream")
    v_symbols = list(v_cfg.get("markets", []))
    l_symbols = list(l_cfg.get("markets", []))
    variational = SyncVenueAdapter(
        VariationalPublicClient(v_cfg), "variational", "Variational", v_symbols,
        VenueCapability(market_data=["rest", "browser"], catalog_mode=CatalogMode.configured_only,
                        executable_quotes=False, order_submission=False,
                        order_status_stream=False, fee_schedule=False, funding=True),
    )
    lighter = SyncVenueAdapter(
        LighterApiTrader(l_cfg), "lighter", "Lighter", l_symbols,
        VenueCapability(market_data=["rest", "websocket"], catalog_mode=CatalogMode.full,
                        executable_quotes=False, order_submission=False,
                        order_status_stream=True, fee_schedule=True, funding=True),
    )
    lighter_rh = SyncVenueAdapter(
        LighterApiTrader(rh_cfg), "lighter_rh", "Lighter RH · Robinhood Chain",
        list(rh_cfg.get("markets", [])),
        VenueCapability(market_data=["rest", "websocket"], catalog_mode=CatalogMode.full,
                        executable_quotes=False, order_submission=False,
                        order_status_stream=True, fee_schedule=True, funding=True),
    )
    standx = StandXAdapter(config.get("standx.api.base_url", "https://perps.standx.com"),
                           list(config.get("standx.markets", [])), proxy)
    binance = BinanceFuturesAdapter(config.get("binance.api.base_url", "https://fapi.binance.com"), proxy)
    gate = GateFuturesAdapter(config.get("gate.api.base_url", "https://api.gateio.ws/api/v4"), proxy)
    okx = OKXSwapAdapter(config.get("okx.api.base_url", "https://www.okx.com"), proxy)
    arcus = ArcusAdapter(
        config.get("arcus.api.base_url", "https://api.arcus.xyz"),
        config.get("arcus.api.ws_url", "wss://api.arcus.xyz/v1/ws"),
        list(config.get("arcus.markets", [])),
        config.get("arcus.api.proxy_url", proxy),
    )
    return {"variational": variational, "lighter": lighter, "lighter_rh": lighter_rh,
            "standx": standx, "binance": binance, "gate": gate, "okx": okx,
            "arcus": arcus}
