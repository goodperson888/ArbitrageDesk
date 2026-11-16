"""Public Arcus market-data adapter.

Arcus exposes a full derivatives market catalog, a low-latency ``bbo`` /
``l2Orderbook`` websocket feed, and a REST BBO endpoint.  The adapter uses
the official BBO feed for real top-of-book quotes and keeps the oracle price
as a separate mark/reference value.  Arcus order submission is still kept
outside this read-only adapter until its signed order lifecycle is wired.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from src.common.rate_limit import RateLimitGate
from src.common.discovery import DiscoveryCooldown, DiscoveryCooldownError, arcus_unavailable_market
from src.common.rate_limit import RateLimitError, DiscoveryThrottleError
from src.common.discovery import (ArcusSubscriptionError, arcus_subscription_batches,
                                  check_arcus_subscription_error)
from src.execution.funding_forecast import forecast

from .adapters import VenueAdapter, canonical_perpetual
from .models import CatalogMode, Instrument, QuoteSnapshot, VenueCapability, VenueStatus


def _number(value: Any) -> Optional[float]:
    try:
        number = None if value is None or isinstance(value, bool) else float(value)
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class ArcusAdapter(VenueAdapter):
    """Arcus public catalog, BBO feed, and oracle mark/reference feed.

    ``bbo`` is the official best-bid/offer channel.  ``oraclePrices`` remains
    useful as the mark/reference price, while BBO supplies the real
    top-of-book used by the opportunity scanner.  ``executable`` stays false
    because this adapter intentionally has no signed order submission path.
    """

    venue = "arcus"
    display_name = "Arcus"

    def __init__(self, base_url: str = "https://api.arcus.xyz",
                 ws_url: str = "wss://api.arcus.xyz/v1/ws",
                 configured_symbols: Optional[List[str]] = None,
                 proxy_url: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        self.proxy_url = proxy_url
        self.configured_symbols = configured_symbols or []
        self.capability = VenueCapability(
            market_data=["rest", "websocket"], catalog_mode=CatalogMode.full,
            executable_quotes=False, order_submission=False,
            order_status_stream=True, fee_schedule=False, funding=True,
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0), proxy=proxy_url
        )
        self._direct_client: Optional[httpx.AsyncClient] = None
        self._proxy_failed_until = 0.0
        self._catalog_cache: Optional[tuple[datetime, List[Instrument]]] = None
        self._market_by_id: Dict[str, Instrument] = {}
        self._market_by_name: Dict[str, Instrument] = {}
        self._oracle: Dict[str, tuple[float, datetime]] = {}
        self._bbo: Dict[str, dict] = {}
        self._funding: Dict[str, dict] = {}
        self._bbo_rest_at: Dict[str, float] = {}
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_connected = False
        self._ws_connected_shards = set()
        self._ws_shard_errors = {}
        self._ws_shard_count = 0
        self._last_error: Optional[str] = None
        self.rate_limit = RateLimitGate(self.base_url)
        self._discovery_cooldown = DiscoveryCooldown('Arcus')

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.rate_limit.check()
        if path.startswith('/v1/bbo/'):
            self.rate_limit.reserve_discovery()
        use_proxy = bool(self.proxy_url) and time.monotonic() >= self._proxy_failed_until
        try:
            if not self.proxy_url or use_proxy:
                response = await self._client.get(self.base_url + path, params=params)
            else:
                raise httpx.ProxyError("proxy temporarily unavailable")
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout,
                httpx.ReadTimeout, httpx.PoolTimeout):
            if not self.proxy_url:
                raise
            self._proxy_failed_until = time.monotonic() + 60.0
            if self._direct_client is None:
                self._direct_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(8.0, connect=3.0)
                )
            response = await self._direct_client.get(self.base_url + path, params=params)
        self.rate_limit.reject(response)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _level(value: Any) -> tuple[Optional[float], Optional[float]]:
        if not isinstance(value, dict):
            return None, None
        return _number(value.get("price")), _number(value.get("size"))

    @staticmethod
    def _observed_timestamp(value: Any, received: datetime) -> datetime:
        """Convert Arcus' Unix-microsecond timestamp without trusting bad clocks."""
        stamp = _number(value)
        if not stamp or stamp <= 1_000_000_000_000:
            return received
        try:
            observed = datetime.fromtimestamp(stamp / 1_000_000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return received
        age = (received - observed).total_seconds()
        return observed if -2 <= age <= 120 else received

    def _handle_ws_message(self, message: Any) -> None:
        """Normalize Arcus channel envelopes without logging raw payloads."""
        if not isinstance(message, dict):
            return
        unavailable = arcus_unavailable_market(message)
        if unavailable:
            for instrument in self._market_by_id.values():
                if instrument.venue_symbol.upper() == unavailable:
                    self._bbo.pop(instrument.canonical_symbol, None)
                    self._discovery_cooldown.observe(instrument.canonical_symbol, 'unavailable')
            return
        check_arcus_subscription_error(message)
        channel = message.get("channel")
        contents = message.get("contents") or {}
        if not isinstance(contents, dict):
            return
        if channel == 'predictedFunding':
            market = contents.get('market') or message.get('id')
            instrument = self._market_by_name.get(str(market).upper()) or next((x for x in self._market_by_id.values()
                               if x.venue_symbol.upper() == str(market).upper()), None)
            now = time.time()
            value = forecast(contents.get('rate1h'), (math.floor(now/3600)+1)*3600,
                             now, 'arcus_predictedFunding')
            if instrument and value:
                self._funding[instrument.canonical_symbol] = value
            return
        if channel == "oraclePrices":
            prices = contents.get("prices", [])
            for row in prices if isinstance(prices, list) else []:
                if not isinstance(row, dict):
                    continue
                value = _number(row.get("price") or row.get("oraclePrice"))
                instrument = self._market_by_id.get(str(row.get("marketId")))
                if value is not None and value > 0 and instrument:
                    self._oracle[instrument.canonical_symbol] = (value, datetime.now(timezone.utc))
            return
        if channel != "bbo":
            return
        market = str(message.get("id") or "").upper()
        instrument = self._market_by_name.get(market) or next((row for row in self._market_by_id.values()
                           if row.venue_symbol.upper() == market), None)
        if instrument is None:
            return
        bid, bid_size = self._level(contents.get("bestBid"))
        ask, ask_size = self._level(contents.get("bestAsk"))
        if bid is None and ask is None:
            return
        # BBO timestamps are Unix microseconds.  Use receipt time when a
        # malformed/clock-skewed stamp is supplied.
        received = datetime.now(timezone.utc)
        observed = self._observed_timestamp(contents.get("timestamp"), received)
        self._bbo[instrument.canonical_symbol] = {
            "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
            "observed": observed, "received": received, "source": "arcus_websocket",
        }
        if bid is not None and ask is not None and 0 < bid <= ask and (received - observed).total_seconds() <= 10:
            self._discovery_cooldown.fresh_stream(instrument.canonical_symbol)

    @staticmethod
    def _market_name(row: dict) -> str:
        return str(row.get("marketDisplayName") or row.get("name") or "").strip()

    async def list_instruments(self) -> List[Instrument]:
        now = datetime.now(timezone.utc)
        if self._catalog_cache and (now - self._catalog_cache[0]).total_seconds() < 60:
            return list(self._catalog_cache[1])
        try:
            payload = await self._get("/v1/markets")
            rows = payload.get("markets", []) if isinstance(payload, dict) else []
            result: List[Instrument] = []
            self._market_by_id = {}
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                market = self._market_name(row)
                if not market:
                    continue
                base = market.split("-", 1)[0].upper()
                instrument = Instrument(
                    venue=self.venue, venue_symbol=market,
                    canonical_symbol=canonical_perpetual(base),
                    instrument_type="perpetual", base_asset=base,
                    quote_asset=(market.split("-", 1)[1].upper()
                                 if "-" in market else "USD"),
                    enabled=row.get("status") not in ("inactive", "disabled", False),
                    price_decimals=None, size_decimals=None,
                    min_base_amount=_number(row.get("minOrderSize")),
                    min_quote_amount=_number(row.get("minOrderNotional")),
                    metadata={
                        "market_id": row.get("marketId"),
                        "tick_size": row.get("tickSize"),
                        "tick_tiers": row.get("tickTiers"),
                        "step_size": row.get("stepSize"),
                        "oracle_price": _number(row.get("oraclePrice")),
                        "quote_kind": "bbo",
                        "funding": forecast(
                            row.get('nextFundingRate'), row.get('nextFundingAt'), now.timestamp(), 'arcus_markets'),
                    },
                )
                result.append(instrument)
                if row.get("marketId") is not None:
                    self._market_by_id[str(row["marketId"])] = instrument
            self._catalog_cache = (now, result)
            self._market_by_name = {row.venue_symbol.upper():row for row in result}
            self._ensure_ws()
            self._last_error = None
            return list(result)
        except Exception as exc:
            self._last_error = f"Arcus 市场目录请求失败：{type(exc).__name__}"
            if self._catalog_cache:
                return list(self._catalog_cache[1])
            raise

    def _ensure_ws(self) -> None:
        if self._ws_task is None or self._ws_task.done():
            try:
                self._ws_task = asyncio.create_task(self._run_ws())
            except RuntimeError:
                # Called outside a running loop during tests; REST still works.
                self._ws_task = None

    async def _run_ws(self) -> None:
        """Keep the public oracle and BBO streams warm with bounded reconnect backoff."""
        try:
            import websockets
        except ImportError:
            self._last_error = "Arcus WebSocket 依赖未安装，已使用 REST BBO"
            return
        batches = arcus_subscription_batches(self._market_by_id.values(), 3)
        self._ws_shard_count = len(batches)
        self._ws_connected_shards.clear()
        self._ws_shard_errors.clear()
        await asyncio.gather(*(self._run_ws_shard(i, batch, websockets.connect)
                               for i, batch in enumerate(batches)))

    async def _run_ws_shard(self, index, instruments, connect):
        backoff = 1.0
        while True:
            try:
                async with connect(self.ws_url, proxy=self.proxy_url or None, open_timeout=8,
                                   ping_interval=20, ping_timeout=20) as ws:
                    self._ws_connected_shards.add(index)
                    self._ws_shard_errors.pop(index, None)
                    self._ws_connected = len(self._ws_connected_shards) == self._ws_shard_count
                    for instrument in instruments:
                        market = instrument.venue_symbol
                        await ws.send(json.dumps({
                            "type": "subscribe", "channel": "oraclePrices", "id": market
                        }))
                        await ws.send(json.dumps({
                            "type": "subscribe", "channel": "bbo", "id": market
                        }))
                        await ws.send(json.dumps({
                            'type': 'subscribe', 'channel': 'predictedFunding', 'id': market
                        }))
                    async for raw in ws:
                        message = json.loads(raw) if isinstance(raw, str) else raw
                        self._handle_ws_message(message)
                        backoff = 1.0
                    raise ConnectionError('Arcus 行情连接已关闭')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                note = (str(exc) if isinstance(exc, ArcusSubscriptionError) else
                        f"Arcus 行情连接 {index+1} 暂不可用：{type(exc).__name__}；已回退 REST")
                self._ws_shard_errors[index] = note
                self._last_error = note
            finally:
                self._ws_connected_shards.discard(index)
                self._ws_connected = bool(self._ws_shard_count) and len(self._ws_connected_shards) == self._ws_shard_count
                for instrument in instruments:
                    symbol = instrument.canonical_symbol
                    if self._bbo.get(symbol, {}).get('source') == 'arcus_websocket':
                        self._bbo.pop(symbol, None)
                    self._funding.pop(symbol, None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        requested = str(symbol).upper().replace("-PERP", "")
        try:
            instruments = await self.list_instruments()
            instrument = next((row for row in instruments
                               if row.canonical_symbol == canonical_perpetual(requested)), None)
            if instrument is None:
                return QuoteSnapshot(
                    venue=self.venue, symbol=symbol, venue_symbol=symbol,
                    instrument_type="perpetual", source="arcus", stale=True,
                    reason="Arcus 未找到对应市场",
                )
            cached = self._oracle.get(instrument.canonical_symbol)
            value = cached[0] if cached else instrument.metadata.get("oracle_price")
            observed = cached[1] if cached else datetime.now(timezone.utc)
            bbo = self._bbo.get(instrument.canonical_symbol)
            now_mono = time.monotonic()
            cached_age = ((datetime.now(timezone.utc) - bbo['observed']).total_seconds()
                          if bbo else None)
            book_note = None
            # Low-volume markets may not emit another BBO event for a while.
            # Refresh stale/empty entries from the official REST BBO endpoint,
            # but throttle each market so a full catalog scan cannot hammer it.
            last_rest = self._bbo_rest_at.get(instrument.canonical_symbol)
            rest_due = (bbo is None or (cached_age is not None and cached_age > 10.0)) and (
                last_rest is None or now_mono - last_rest >= 15.0)
            if rest_due:
                try:
                    self._discovery_cooldown.check(instrument.canonical_symbol)
                    self._bbo_rest_at[instrument.canonical_symbol] = now_mono
                    payload = await self._get('/v1/bbo/' + instrument.venue_symbol)
                    if isinstance(payload, dict):
                        body = payload.get('data') if isinstance(payload.get('data'), dict) else payload
                        best_bid, bid_size = self._level(body.get('bestBid'))
                        best_ask, ask_size = self._level(body.get('bestAsk'))
                        if best_bid is not None or best_ask is not None:
                            received = datetime.now(timezone.utc)
                            observed = self._observed_timestamp(body.get('timestamp'), received)
                            bbo = {'bid': best_bid, 'ask': best_ask,
                                   'bid_size': bid_size, 'ask_size': ask_size,
                                   'observed': observed, 'received': received,
                                   'source': 'arcus_rest'}
                            self._bbo[instrument.canonical_symbol] = bbo
                            if best_bid is not None and best_ask is not None and 0 < best_bid <= best_ask:
                                self._discovery_cooldown.recovered(instrument.canonical_symbol)
                except Exception as exc:
                    if isinstance(exc, DiscoveryThrottleError):
                        # No request was sent. Do not make a local denial
                        # consume this market's 15-second probe interval.
                        if last_rest is None:
                            self._bbo_rest_at.pop(instrument.canonical_symbol, None)
                        else:
                            self._bbo_rest_at[instrument.canonical_symbol] = last_rest
                    # Keep a previously cached BBO for diagnostics; the age
                    # calculation below will mark it stale if necessary.
                    bbo = self._bbo.get(instrument.canonical_symbol)
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                        self._bbo.pop(instrument.canonical_symbol, None)
                        bbo = None
                        self._discovery_cooldown.failed(instrument.canonical_symbol, 'unavailable')
                    book_note = (str(exc) if isinstance(exc, (DiscoveryCooldownError, RateLimitError, DiscoveryThrottleError))
                                 else 'Arcus BBO 请求失败: ' + type(exc).__name__)
            if bbo is None:
                try:
                    self._discovery_cooldown.check(instrument.canonical_symbol)
                except DiscoveryCooldownError as exc:
                    book_note = str(exc)
            age = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
            book_age = (datetime.now(timezone.utc) - bbo['observed']).total_seconds() if bbo else None
            fresh_age = max(0.0, book_age) if book_age is not None else age
            return QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=instrument.venue_symbol,
                instrument_type="perpetual", bid=bbo.get('bid') if bbo else None,
                ask=bbo.get('ask') if bbo else None, mark_price=_number(value),
                quote_asset=instrument.quote_asset,
                source=(bbo.get('source') if bbo else 'arcus_rest'),
                # BBO is real market data, but Arcus order submission is not
                # wired into this read-only adapter yet.
                executable=False,
                stale=(bbo is None or fresh_age > 10.0), age_seconds=fresh_age,
                fetched_at=bbo['observed'] if bbo else observed,
                reason=book_note or ("Arcus BBO 公开行情；实盘下单需在连接与实盘检查中完成 API Signing Key 与费率核验"
                                     if bbo else "Arcus 暂未收到 BBO 盘口"),
                metadata={**instrument.metadata, "bid_size_base": bbo.get('bid_size') if bbo else None,
                          "ask_size_base": bbo.get('ask_size') if bbo else None,
                          'funding': self._funding.get(instrument.canonical_symbol) or instrument.metadata.get('funding')},
            )
        except Exception as exc:
            self._last_error = (str(exc) if isinstance(exc, (DiscoveryCooldownError, RateLimitError))
                                else f"Arcus 行情请求失败：{type(exc).__name__}")
            return QuoteSnapshot(
                venue=self.venue, symbol=symbol, venue_symbol=symbol,
                instrument_type="perpetual", source="arcus", stale=True,
                reason=self._last_error,
            )

    async def get_reference_candles(self, symbol: str, seconds: int = 86400,
                                    interval_seconds: int = 300) -> List[dict]:
        timeframe = {60: '1m', 300: '5m', 900: '15m', 3600: '1h'}.get(int(interval_seconds))
        if timeframe is None:
            return []
        instruments = await self.list_instruments()
        instrument = next((row for row in instruments
                           if row.canonical_symbol == canonical_perpetual(symbol)), None)
        if instrument is None:
            return []
        params = {'market': instrument.venue_symbol, 'timeframe': timeframe,
                  'to': int(time.time() * 1_000_000),
                  'countback': max(1, min(500, int(seconds // int(interval_seconds)) + 1))}
        payload = await self._get('/v1/candles', params)
        rows = payload.get('candles', payload.get('data', payload)) if isinstance(payload, dict) else payload
        points = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            raw_ts = row.get('openTime', row.get('open_time', row.get('timestamp', row.get('t'))))
            close = row.get('close', row.get('c'))
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
                           'source': 'arcus_candles', 'reference_kind': 'candle_close'})
        return sorted(points, key=lambda item: item['timestamp'])[-500:]

    def status(self, error: Optional[str] = None) -> VenueStatus:
        note = error or next(iter(self._ws_shard_errors.values()), None) or (None if self._ws_connected else self._last_error)
        return VenueStatus(
            venue=self.venue, display_name=self.display_name,
            status="degraded" if note else "configured",
            transport=list(self.capability.market_data), capabilities=self.capability,
            configured_symbols=self.configured_symbols, last_error=note,
        )

    async def close(self) -> None:
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
        clients = [self._client]
        if self._direct_client is not None:
            clients.append(self._direct_client)
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
