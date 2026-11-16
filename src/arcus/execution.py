"""Fail-closed Arcus signed execution.

The public market-data adapter deliberately stays read-only.  This module is
the separate private path used only after the local credential verifier has
bound the wallet, API key and account-specific fee evidence.  Arcus accepts a
signed WebSocket RPC request, but that acknowledgement is not a fill.  A
terminal order update (and, for a non-zero fill, a matching fill update) is
required before the strategy can establish exposure.
"""
from __future__ import annotations

import json
import copy
import math
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from src.common.comparison import depth_price
from src.common.market_data import age_seconds, normalize_quote_time
from src.common.rate_limit import RateLimitGate
from src.common.discovery import DiscoveryCooldown
from src.execution.orders import (decimal, direction, fresh_quote, unknown,
                                  check_submit_deadline, SubmissionWindowExpired)
from src.execution.precheck import EntryPrecheckMixin, account_transaction
from src.execution.preparation import preparation_book, prepared_book
from src.execution.receipt_diagnostics import DIAGNOSTIC_FIELDS, order_diagnostics
from src.arcus.pricing import PriceGridError, protective_limit, validate_placement_price
from src.arcus.trading_bounds import validate_trading_quote


TERMINAL_STATUSES = {
    "FILLED": "filled",
    "PARTIALLY_FILLED": "partial",
    "PARTIAL": "partial",
    "CANCELED": "canceled",
    "CANCELLED": "canceled",
    "MARGIN_CANCELED": "canceled",
    "REJECTED": "rejected",
    "EXPIRED": "canceled",
}


def _number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _fmt(value) -> str:
    value = Decimal(str(value)).normalize()
    return format(value, "f")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market_base(symbol: str) -> str:
    return str(symbol or "").upper().split("-", 1)[0].replace("/", "")


def _row_value(row: dict, *names, default=None):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def build_order_payload(address: str, account_index: int, market_id: int,
                        side: str, price, quantity, tick_size, step_size,
                        client_order_id: str, reduce_only: bool = False,
                        tif: int = 2, good_til_us: Optional[int] = None,
                        timestamp_ns: Optional[int] = None, tick_tiers=None):
    """Build the official Scheme-1 typed message and its JSON body.

    ``tif=2`` is IOC.  Arcus still requires ``goodTilTime`` for IOC/FOK and
    the signed integer payload uses nanoseconds while the body uses
    microseconds.  The canonical field order is kept explicit to prevent a
    harmless JSON re-serialization from changing the signature.
    """
    side = direction(side)
    validate_placement_price(price, tick_size, tick_tiers)
    now_ns = int(timestamp_ns or time.time_ns())
    expiry_us = int(good_til_us or (time.time() * 1_000_000 + 40 * 86_400 * 1_000_000))
    tick = Decimal(str(tick_size))
    step = Decimal(str(step_size))
    price_units = Decimal(str(price)) / tick
    size_units = Decimal(str(quantity)) / step
    if price_units != price_units.to_integral_value() or size_units != size_units.to_integral_value():
        raise ValueError("Arcus 价格或数量不符合市场步长")
    p, q = int(price_units), int(size_units)
    if p <= 0 or q <= 0:
        raise ValueError("Arcus 订单整数参数必须为正")
    side_int = 0 if side == "buy" else 1
    addr = str(address).lower()
    # c is the client correlation id.  It is optional in the API but is
    # included so a reconnect can identify a 202 acknowledgement safely.
    typed = (f'{{"ad":"{addr}","ai":{int(account_index)},"c":"{client_order_id}",'
             f'"ct":{now_ns},"g":{expiry_us * 1000},"m":{int(market_id)},"op":1,'
             f'"p":{p},"q":{q},"r":{1 if reduce_only else 0},"s":{side_int},"t":{int(tif)},"v":1}}')
    body = {
        "address": address,
        "accountIndex": int(account_index),
        "marketId": int(market_id),
        "orderSide": side.upper(),
        "orderType": "LIMIT",
        "quantity": _fmt(quantity),
        "price": _fmt(price),
        "timeInForce": {0: "GTT", 1: "FOK", 2: "IOC", 3: "ALO"}.get(int(tif), "IOC"),
        "goodTilTime": str(expiry_us),
        "timestamp": now_ns,
        # Arcus calls this field ``clientId``.  The signed payload uses the
        # compact ``c`` key, and the gateway derives that same value from the
        # request body before verifying the signature.
        "clientId": client_order_id,
    }
    if reduce_only:
        body["reduceOnly"] = True
    return typed, body, now_ns


def build_cancel_payload(address: str, account_index: int, market_id: int,
                         order_id: str, timestamp_ns: Optional[int] = None):
    ts = int(timestamp_ns or time.time_ns())
    typed = (f'{{"ad":"{str(address).lower()}","ai":{int(account_index)},"ct":{ts},'
             f'"id":"{order_id}","m":{int(market_id)},"op":2,"v":1}}')
    body = {"address": address, "accountIndex": int(account_index), "marketId": int(market_id),
            "kind": "orderId", "orderId": str(order_id), "timestamp": ts}
    return typed, body, ts


def _levels(rows: Any):
    result = []
    if not isinstance(rows, list):
        return result
    for row in rows:
        if isinstance(row, dict):
            price = _number(_row_value(row, "price", "px"))
            size = _number(_row_value(row, "size", "quantity", "qty", "amount"))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, size = _number(row[0]), _number(row[1])
        else:
            continue
        if price is not None and size is not None and price > 0 and size >= 0:
            result.append({"price": price, "size": size})
    return result


def normalize_order_update(row: dict) -> dict:
    """Normalize both orders-channel rows and REST order rows."""
    if not isinstance(row, dict):
        return {}
    status_raw = str(_row_value(row, "status", "orderStatus", "state", default="")).upper()
    status = TERMINAL_STATUSES.get(status_raw, status_raw.lower() or "open")
    order_id = _row_value(row, "orderId", "orderID", "id")
    filled = _number(_row_value(row, "filledSize", "filledQuantity", "executedQuantity",
                                  "filledQty", "filled", "sizeFilled"), 0.0)
    avg = _number(_row_value(row, "avgFillPrice", "averagePrice", "avgPrice", "fillPrice", "executedPrice"))
    fee = _number(_row_value(row, "fees", "fee", "totalFee", "feeUsd", "tradingFee", "commission"))
    return {"order_id": str(order_id) if order_id is not None else None,
            "status": status, "raw_status": status_raw, "filled_size": filled,
            "average_price": avg, "fees_usdc": fee,
            "client_order_id": _row_value(row, "clientOrderId", "clientId", "client_id"),
            "reduce_only": bool(_row_value(row, "reduceOnly", "reduce_only", default=False)),
            "side": str(_row_value(row, "side", "orderSide", default="")).lower(),
            **order_diagnostics(row)}


def normalize_fill(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    return {"order_id": _row_value(row, "orderId", "orderID", "id"),
            "size": _number(_row_value(row, "fillSize", "size", "quantity", "qty", "fillQty", "originalSize"), 0.0),
            "price": _number(_row_value(row, "fillPrice", "price", "avgPrice", "averagePrice", "execPrice", "px")),
            "fee": _number(_row_value(row, "fee", "fees", "totalFee", "feeUsd", "tradingFee", "commission"))}


class _ArcusWebSocket:
    """Small synchronous receiver that preserves RPC and account events."""
    def __init__(self, url: str, proxy: Optional[str], address: str, account_index: int):
        self.url, self.proxy, self.address, self.account_index = url, proxy, address, int(account_index)
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._send_lock = threading.RLock()
        self._next_id = 1
        self._pending = {}
        self.orders: Dict[str, dict] = {}
        self.fills: Dict[str, dict] = {}
        self._order_updates = threading.Condition()
        self.last_error = None
        self.account_revision = 0

    def start(self, timeout=12):
        if self._thread and self._thread.is_alive():
            if not self._ready.wait(timeout):
                raise RuntimeError("Arcus WebSocket 连接未就绪")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="arcus-order-ws", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(self.last_error or "Arcus WebSocket 连接超时")

    def _run(self):
        try:
            from websockets.sync.client import connect
            with connect(self.url, proxy=self.proxy or None, open_timeout=10,
                         ping_interval=20, ping_timeout=20) as ws:
                self._ws = ws
                self._send_subscriptions()
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1)
                    except TimeoutError:
                        continue
                    if raw is None:
                        break
                    self._handle(raw)
        except Exception as exc:
            self.last_error = f"Arcus WebSocket：{type(exc).__name__}"
            for waiter in list(self._pending.values()):
                try:
                    waiter.put_nowait({"status": "connection_error", "error": self.last_error})
                except queue.Full:
                    pass
        finally:
            self._ws = None
            self._ready.clear()
            self.account_revision += 1

    def _send_subscriptions(self):
        for payload in (
            {"type": "subscribe", "channel": "orders", "id": self.address,
             "accountIndex": self.account_index, "nRecentClosed": 100},
            {"type": "subscribe", "channel": "userFills", "id": self.address,
             "accountIndex": self.account_index},
            {"type": "subscribe", "channel": "positions", "id": self.address,
             "accountIndex": self.account_index},
            {"type": "subscribe", "channel": "account", "id": self.address,
             "accountIndex": self.account_index},
        ):
            self._ws.send(json.dumps(payload, separators=(",", ":")))

    def _handle(self, raw):
        try:
            message = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        msg_id = message.get("id")
        if msg_id in self._pending:
            try:
                self._pending[msg_id].put_nowait(message)
            except queue.Full:
                pass
        channel = message.get("channel")
        contents = message.get("contents") or {}
        if channel in ('orders', 'userFills', 'positions', 'account'):
            # Notifications invalidate REST evidence; partial messages are
            # never interpreted as an authoritative empty account.
            self.account_revision += 1
        if channel == "orders":
            rows = list(contents) if isinstance(contents, list) else []
            if isinstance(contents, dict):
                for key in ("openOrders", "recentClosedOrders", "orders", "data", "items", "records"):
                    if isinstance(contents.get(key), list):
                        rows.extend(contents[key])
                if not rows and any(k in contents for k in ("orderId", "status", "orderStatus")):
                    rows.append(contents)
            with self._order_updates:
                for row in rows:
                    normalized = normalize_order_update(row)
                    if normalized.get("order_id"):
                        self.orders[str(normalized["order_id"])] = normalized
                self._order_updates.notify_all()
        elif channel == "userFills":
            rows = list(contents) if isinstance(contents, list) else []
            if isinstance(contents, dict):
                for key in ("fills", "userFills", "data", "items", "records"):
                    if isinstance(contents.get(key), list):
                        rows.extend(contents[key])
                if not rows and any(k in contents for k in ("orderId", "fillPrice", "price")):
                    rows.append(contents)
            for row in rows:
                normalized = normalize_fill(row)
                if normalized.get("order_id"):
                    self.fills[str(normalized["order_id"])] = normalized

    def rpc(self, method: str, body: dict, typed_payload: str, signing_key,
            api_key: str, timestamp_ns: int, timeout=15, submit_deadline_monotonic=None):
        self.start()
        with self._send_lock:
            check_submit_deadline(submit_deadline_monotonic)
            if self._ws is None:
                raise RuntimeError(self.last_error or "Arcus WebSocket 未连接")
            req_id = self._next_id
            self._next_id += 1
            waiter = queue.Queue(maxsize=1)
            self._pending[req_id] = waiter
            signature = signing_key.sign(typed_payload.encode()).hex()
            request = {"type": "post", "id": req_id, "request": {
                "type": method, "payload": body, "apiKey": api_key,
                "timestamp": str(timestamp_ns), "signature": signature}}
            try:
                check_submit_deadline(submit_deadline_monotonic)
                self._ws.send(json.dumps(request, separators=(",", ":")))
                return waiter.get(timeout=timeout)
            finally:
                self._pending.pop(req_id, None)

    def wait_order(self, order_id: str, timeout: float = 15):
        deadline = time.monotonic() + max(0, timeout)
        with self._order_updates:
            while True:
                order = self.orders.get(str(order_id))
                if order and order.get("status") in ("filled", "partial", "canceled", "rejected"):
                    return dict(order)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._order_updates.wait(remaining)

    def close(self):
        self._stop.set()
        with self._send_lock:
            try:
                if self._ws:
                    self._ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(3)


class ArcusClient:
    """Signed Arcus REST/WS client. It never submits during construction."""
    def __init__(self, wallet_address: str, account_index: int, api_signing_key: str,
                 base_url="https://api.arcus.xyz", ws_url="wss://api.arcus.xyz/v1/ws",
                 proxy_url=None, order_timeout=15):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        self.wallet_address = wallet_address
        self.account_index = int(account_index)
        raw = str(api_signing_key).removeprefix("0x")
        self.signing_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))
        self.api_key = self.signing_key.public_key().public_bytes_raw().hex()
        self.base_url, self.ws_url, self.proxy_url = base_url.rstrip("/"), ws_url, proxy_url
        self.order_timeout = float(order_timeout)
        self.http = httpx.Client(timeout=httpx.Timeout(12.0, connect=5.0), proxy=proxy_url)
        self.ws = _ArcusWebSocket(ws_url, proxy_url, wallet_address, self.account_index)
        self._markets = {}
        self._markets_at = 0.0
        self.rate_limit = RateLimitGate(self.base_url)
        self._public_cache = {}
        self._public_cache_lock = threading.RLock()
        self._public_key_locks = tuple(threading.RLock() for _ in range(32))

    def _get(self, path, params=None, ttl=0, discovery=False):
        if ttl:
            cache_key = (path, tuple(sorted((params or {}).items())))
            with self._public_cache_lock:
                cached = self._public_cache.get(cache_key)
                if cached and time.monotonic() - cached[0] < ttl:
                    return copy.deepcopy(cached[1])
            # Singleflight the same key without holding one global lock over
            # network I/O for every market. Lock stripes keep memory bounded.
            with self._public_key_locks[hash(cache_key) % len(self._public_key_locks)]:
                with self._public_cache_lock:
                    cached = self._public_cache.get(cache_key)
                    if cached and time.monotonic()-cached[0] < ttl:
                        return copy.deepcopy(cached[1])
                result = self._get(path, params, discovery=discovery)
                if isinstance(result, dict):
                    result['_local_fetched_at'] = _iso_now()
                with self._public_cache_lock:
                    if len(self._public_cache) >= 256:
                        self._public_cache.clear()
                    self._public_cache[cache_key] = (time.monotonic(), result)
                return copy.deepcopy(result)
        self.rate_limit.check()
        if discovery:
            from src.execution.preparation import is_preparing
            self.rate_limit.reserve_discovery(priority=is_preparing())
        response = self.http.get(self.base_url + path, params=params,
                                 headers={"X-API-Key": self.api_key,
                                          "Content-Type": "application/json"})
        self.rate_limit.reject(response)
        response.raise_for_status()
        return response.json() if response.content else {}

    def list_markets(self, force=False):
        if not force and self._markets and time.monotonic() - self._markets_at < 60:
            return dict(self._markets)
        payload = self._get("/v1/markets")
        rows = payload.get("markets", []) if isinstance(payload, dict) else []
        result = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("marketDisplayName") or row.get("name") or "").strip()
            base = _market_base(name)
            if not base or row.get("marketId") is None:
                continue
            result[base] = {"market_id": int(row["marketId"]), "symbol": name,
                            "tick_size": row.get("tickSize", "0.01"),
                            "tick_tiers": copy.deepcopy(row.get("tickTiers")),
                            "step_size": row.get("stepSize", "0.000001"),
                            "min_base_amount": row.get("minOrderSize", 0),
                            "min_quote_amount": row.get("minOrderNotional", 0),
                            "trading_bounds": ({'lower': row.get('lowerTradingBound'),
                                                'upper': row.get('upperTradingBound')}
                                               if 'lowerTradingBound' in row or 'upperTradingBound' in row else None),
                            "status": row.get("status", "active")}
        self._markets, self._markets_at = result, time.monotonic()
        return dict(result)

    def invalidate_market_rules(self):
        # A definitive venue rejection invalidates cached band metadata;
        # refresh with the next normal catalog read, never during the hedge.
        self._markets_at = 0.0

    def market(self, symbol):
        base = _market_base(symbol)
        market = self.list_markets().get(base)
        if not market:
            raise ValueError("Arcus 未找到可交易市场：" + base)
        if str(market.get("status")).lower() in ("inactive", "disabled", "closed"):
            raise ValueError("Arcus 市场当前不可交易：" + base)
        return market

    def bbo(self, symbol, discovery=False):
        market = self.market(symbol)
        payload = self._get("/v1/bbo/" + market["symbol"], ttl=.5, discovery=discovery)
        body = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        bid = body.get("bestBid") if isinstance(body, dict) else None
        ask = body.get("bestAsk") if isinstance(body, dict) else None
        def level(value):
            if not isinstance(value, dict): return None, None
            return _number(value.get("price")), _number(value.get("size"))
        bp, bs, ap, ass = (*level(bid), *level(ask))
        stamp = _number(body.get("timestamp")) if isinstance(body, dict) else None
        observed = datetime.fromisoformat(payload['_local_fetched_at']) if payload.get('_local_fetched_at') else datetime.now(timezone.utc)
        exchange_observed = None
        if stamp and stamp > 1e12:
            try:
                candidate = datetime.fromtimestamp(stamp / 1e6, timezone.utc)
                exchange_observed = candidate.isoformat()
                candidate = normalize_quote_time(candidate, observed)
                if (observed - candidate).total_seconds() < 120: observed = candidate
            except (OverflowError, OSError, ValueError): pass
        return {"market": market, "bid": bp, "bid_size": bs, "ask": ap, "ask_size": ass,
                "updated_at": observed.isoformat(), "exchange_updated_at": exchange_observed,
                "received_at": payload.get('_local_fetched_at') or _iso_now()}

    def orderbook(self, symbol, discovery=False):
        market = self.market(symbol)
        payload = self._get("/v1/l2Orderbook/" + market["symbol"], ttl=.2, discovery=discovery)
        body = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        bids = _levels(body.get("bids") if isinstance(body, dict) else [])
        asks = _levels(body.get("asks") if isinstance(body, dict) else [])
        if not bids or not asks:
            bbo = self.bbo(symbol)
            if bbo.get("bid") is not None: bids = [{"price": bbo["bid"], "size": bbo.get("bid_size") or 0}]
            if bbo.get("ask") is not None: asks = [{"price": bbo["ask"], "size": bbo.get("ask_size") or 0}]
            updated = bbo["updated_at"]
        else:
            updated = payload.get('_local_fetched_at') or _iso_now()
        return {"symbol": _market_base(symbol), "fetched_at": updated, "bids": bids, "asks": asks}

    def account(self):
        payload = self._get("/v1/account", {"address": self.wallet_address, "accountIndex": self.account_index})
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    def positions(self):
        payload = self._get("/v1/positions", {"address": self.wallet_address, "accountIndex": self.account_index})
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    def orders(self):
        return self._get("/v1/orders", {"address": self.wallet_address, "accountIndex": self.account_index})

    def order_status(self, order_id, *, collection_only=False):
        """Read-only recovery for a lost WebSocket terminal update.

        Arcus deployments have exposed both a dedicated status route and an
        orders collection over time; probe the dedicated route first, then
        search the authenticated collection.  A failed probe never triggers
        another submission.
        """
        params = {"address": self.wallet_address, "accountIndex": self.account_index,
                  "orderId": str(order_id)}
        for path in (() if collection_only else ("/v1/orderStatus", "/v1/order-status")):
            try:
                payload = self._get(path, params)
                if isinstance(payload, dict):
                    candidate = payload.get("order") or payload.get("data") or payload
                    normalized = normalize_order_update(candidate)
                    if (normalized.get("order_id") == str(order_id) and
                            normalized.get("status") in ("filled", "partial", "canceled", "rejected")):
                        return normalized
            except httpx.HTTPStatusError:
                continue
        payload = self.orders()
        rows = list(payload) if isinstance(payload, list) else []
        if isinstance(payload, dict):
            for key in ("orders", "openOrders", "recentClosedOrders", "data", "items", "records"):
                if isinstance(payload.get(key), list):
                    rows.extend(payload[key])
        for row in rows:
            normalized = normalize_order_update(row)
            if str(normalized.get("order_id")) == str(order_id):
                return normalized
        return None

    def submit(self, symbol, side, quantity, price, client_order_id, reduce_only=False,
               submit_deadline_monotonic=None):
        market = self.market(symbol)
        typed, body, ts = build_order_payload(self.wallet_address, self.account_index, market["market_id"],
                                              side, price, quantity, market["tick_size"], market["step_size"],
                                              client_order_id, reduce_only=reduce_only,
                                              tick_tiers=market.get('tick_tiers'))
        response = self.ws.rpc("placeOrder", body, typed, self.signing_key, self.api_key, ts,
                               timeout=self.order_timeout, submit_deadline_monotonic=submit_deadline_monotonic)
        status = str(response.get("status", ""))
        if status not in ("200", "202"):
            error = response.get("error") or response.get("message") or "Arcus 下单被拒"
            return {"accepted": False, "response": response, "error": str(error)}
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        order_id = result.get("orderId") or result.get("id") or response.get("orderId")
        if not order_id:
            return {"accepted": False, "response": response, "error": "Arcus 受理回执缺少订单号"}
        return {"accepted": True, "order_id": str(order_id), "response": response}

    def cancel(self, symbol, order_id):
        market = self.market(symbol)
        typed, body, ts = build_cancel_payload(self.wallet_address, self.account_index,
                                                market["market_id"], str(order_id))
        response = self.ws.rpc("cancelOrder", body, typed, self.signing_key, self.api_key, ts,
                               timeout=self.order_timeout)
        status = str(response.get("status", ""))
        return {"accepted": status in ("200", "202"), "response": response,
                "error": response.get("error") or response.get("message")}

    def close(self):
        self.ws.close()
        self.http.close()


class ArcusExecutionAdapter(EntryPrecheckMixin):
    supports_close_quote_reuse = True
    supports_grid_additions = True
    venue = "arcus"
    requires_page_slippage = False
    live_blocker = "Arcus 需要 API Signing Key、账户费率、IOC 终态和成交回报核验"

    def __init__(self, client: ArcusClient, journal, account: str, enabled=False,
                 fee_bps=None, poll_seconds=15):
        self.client, self.journal, self.account = client, journal, str(account)
        self.enabled, self.verified_fee_bps, self.poll_seconds = enabled, fee_bps, float(poll_seconds)
        self.lock = threading.RLock()
        self._reference_feed = None
        self._reference_symbols = ()
        self._reference_retry_at = 0
        self._reference_lock = threading.Lock()
        self.discovery_supported_symbols = None
        self._discovery_cooldown = DiscoveryCooldown('Arcus')
        self._account_watch_stop = threading.Event()
        self._account_watch_thread = None

    def start_reference_feed(self, symbols):
        self._reference_symbols = tuple(symbols)
        self._ensure_reference_feed()

    def _ensure_reference_feed(self):
        if (self._reference_feed is not None or not self._reference_symbols or
                time.monotonic() < self._reference_retry_at or
                not self._reference_lock.acquire(blocking=False)):
            return
        try:
            self._reference_retry_at = time.monotonic() + 30
            catalog = self.client.list_markets()
            self.discovery_supported_symbols = frozenset(
                symbol for symbol, market in catalog.items()
                if str(market.get('status')).lower() not in ('inactive', 'disabled', 'closed'))
            markets = {symbol: catalog[symbol]['symbol'] for symbol in self._reference_symbols
                       if symbol in self.discovery_supported_symbols}
            if markets:
                from src.common.live_feed import ArcusFeedGroup
                feed = ArcusFeedGroup(markets, self.client.proxy_url, self.client.ws_url)
                feed.start()
                self._reference_feed = feed
        except Exception as exc:
            self._reference_retry_at = time.monotonic() + max(5, getattr(exc, 'retry_after_seconds', 30))
        finally:
            self._reference_lock.release()

    def close_reference_feed(self):
        with self._reference_lock:
            self._reference_symbols = ()
            self._reference_retry_at = 0
            if self._reference_feed is not None:
                self._reference_feed.stop()
                self._reference_feed = None

    def expand_reference_feed(self, symbols):
        requested = tuple(dict.fromkeys(self._reference_symbols + tuple(symbols)))
        if requested == self._reference_symbols and self._reference_feed is not None:
            return
        self.start_reference_feed(requested)
        if self._reference_feed is not None:
            catalog = self.client.list_markets()
            self.discovery_supported_symbols = frozenset(s for s,market in catalog.items()
                if str(market.get('status')).lower() not in ('inactive', 'disabled', 'closed'))
            self._reference_feed.add_markets({s:catalog[s]['symbol'] for s in requested
                                             if s in self.discovery_supported_symbols})

    @property
    def live_ready(self):
        return bool(self.enabled and self.verified_fee_bps is not None and self.client.api_key)

    def _market(self, symbol): return self.client.market(symbol)

    def account_snapshot_identity(self):
        return (self.venue, self.account, self.client.base_url,
                self.client.wallet_address, self.client.account_index, self.client.api_key)

    def account_snapshot_revision(self):
        ws = self.client.ws
        return ws._ready.is_set(), ws.account_revision

    def start_account_watch(self):
        if self._account_watch_thread and self._account_watch_thread.is_alive():
            return
        self._account_watch_stop.clear()
        def maintain():
            while not self._account_watch_stop.is_set():
                try:
                    self.client.ws.start(timeout=0)
                except RuntimeError:
                    pass  # The connection continues in its own thread.
                self._account_watch_stop.wait(10)
        self._account_watch_thread = threading.Thread(target=maintain, name='arcus-account-watch', daemon=True)
        self._account_watch_thread.start()

    def stop_account_watch(self):
        self._account_watch_stop.set()
        if self._account_watch_thread:
            self._account_watch_thread.join(.2)

    @staticmethod
    def entry_available_balance(state):
        return _number(_row_value(state['account'], 'freeCollateral', 'availableBalance', 'available_balance'))

    def read_entry_account_state(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            account = pool.submit(self.client.account)
            orders = pool.submit(self.client.orders)
            positions = pool.submit(self.client.positions)
            state = dict(account=account.result(), orders=orders.result(), positions=positions.result())
        free = _number(_row_value(state['account'], 'freeCollateral', 'availableBalance', 'available_balance'))
        if free is None or free < 0:
            raise ValueError('Arcus 账户可用保证金字段缺失')
        positions = state['positions']
        if not isinstance(positions, dict) or not isinstance(positions.get('positions', positions), (dict, list)):
            raise ValueError('Arcus 全账户持仓未确认')
        rows = positions.get('positions')
        if rows is None:
            raise ValueError('Arcus 全账户持仓集合缺失')
        rows = list(rows.values()) if isinstance(rows, dict) else rows
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not _row_value(
                    row, 'marketDisplayName', 'symbol', 'market', 'name')):
                raise ValueError('Arcus 持仓合约未确认')
            base = _market_base(_row_value(row, 'marketDisplayName', 'symbol', 'market', 'name'))
            if base in seen:
                raise ValueError('Arcus 同合约多条持仓，需先核对')
            seen.add(base)
            quantity = _number(_row_value(row, 'positionSize', 'size', 'quantity', 'qty'))
            if quantity is None:
                notional = _number(row.get('positionValueNotional'))
                mark = _number(_row_value(row, 'markPx', 'markPrice'))
                if notional is None or (notional != 0 and (mark is None or mark <= 0)):
                    raise ValueError('Arcus 持仓数量未确认')
        self._checked_order_rows(state['orders'])
        return state

    def size_decimals(self, symbol):
        step = Decimal(str(self._market(symbol)["step_size"]))
        return max(0, -step.as_tuple().exponent)

    def get_market_price(self, symbol):
        self._ensure_reference_feed()
        bbo = self._reference_feed.snapshot(symbol) if self._reference_feed is not None else None
        if bbo is not None:
            self._discovery_cooldown.fresh_stream(symbol)
        else:
            issue = self._reference_feed.discovery_issue(symbol) if self._reference_feed is not None else None
            if issue == 'unavailable':
                self._discovery_cooldown.observe(symbol, issue)
            self._discovery_cooldown.check(symbol)
            try:
                bbo = self.client.bbo(symbol, discovery=True)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    self._discovery_cooldown.failed(symbol, 'unavailable')
                    self._discovery_cooldown.check(symbol)
                raise
        prices = [bbo.get("bid"), bbo.get("ask")]
        if not all(isinstance(x, (int, float)) and x > 0 for x in prices):
            raise ValueError("Arcus BBO 缺失")
        self._discovery_cooldown.recovered(symbol)
        return (prices[0] + prices[1]) / 2

    def reference_health(self, symbol):
        return (self._reference_feed.quote_health(symbol) if self._reference_feed is not None
                else {'connected': False, 'quote_status': 'missing'})

    def cached_reference_quote(self, symbol):
        """Discovery estimates reuse the WS snapshot; never add REST traffic."""
        quote = self._reference_feed.snapshot(symbol) if self._reference_feed is not None else None
        if quote is None:
            return None
        def size(value):
            try:
                if isinstance(value, bool):
                    return None
                number = float(value)
                return number if math.isfinite(number) and number >= 0 else None
            except (TypeError, ValueError):
                return None
        return dict(quote, fee_bps=self.verified_fee_bps,
                    bid_size=size(quote.get('bid_size')), ask_size=size(quote.get('ask_size')))

    def reconcile_funding(self, symbol, start_time, end_time):
        """Actual signed settlements, scoped to account, market and µs window."""
        market = self._market(symbol)
        start, end = int(start_time.timestamp()*1_000_000), int(end_time.timestamp()*1_000_000)
        payload = self.client._get('/v1/funding', dict(address=self.client.wallet_address,
            accountIndex=self.client.account_index, market=market['symbol'],
            **{'from': start, 'to': end, 'limit': 1000}))
        rows = payload.get('fundingPayments') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) >= 1000:
            return None
        total = Decimal(0)
        for row in rows:
            if (not isinstance(row, dict) or str(row.get('marketId')) != str(market['market_id'])
                    or type(row.get('time')) is not int or not start <= row['time'] <= end):
                return None
            amount = Decimal(str(row.get('payment')))
            if not amount.is_finite():
                return None
            total += amount
        return dict(complete=True, venue='arcus', symbol=symbol, funding_usdc=float(total),
                    rows=[{k:r.get(k) for k in ('marketId','payment','time')} for r in rows],
                    observed_at=_iso_now())

    def get_position_mark_snapshot(self, symbol):
        bbo = self._reference_feed.snapshot(symbol) if self._reference_feed is not None else None
        bbo = bbo or self.client.bbo(symbol)
        from src.execution.position_monitor import PositionMonitor
        result = {'bid': bbo.get('bid'), 'ask': bbo.get('ask'),
                  'updated_at': bbo.get('updated_at'), 'source': bbo.get('source', 'arcus_rest'),
                  'fee_bps': self.verified_fee_bps}
        PositionMonitor._validate_quote(result)
        return result

    def discovery_admission(self, symbol, *, preparation=False):
        """Inspect local freshness/quota only; never perform a REST request."""
        quote = self.cached_reference_quote(symbol)
        age = age_seconds(quote.get('updated_at')) if quote else None
        if quote and age is not None and 0 <= age <= 2 and (not preparation or all(isinstance(quote.get(k), (int, float)) and quote[k] > 0
                                            for k in ('bid_size', 'ask_size'))):
            return {'wait_seconds': 0, 'rest_hosts': ()}
        gate = self.client.rate_limit
        delay = gate.discovery_delay(priority=preparation)
        if not preparation:
            delay = max(delay, self._discovery_cooldown.remaining(symbol))
        return {'wait_seconds': delay, 'rest_hosts': (str(gate.path),)}

    @preparation_book
    def get_sizing_book(self, symbol):
        from src.execution.preparation import is_preparing
        if is_preparing():
            quote = self.cached_reference_quote(symbol)
            age = age_seconds(quote.get('updated_at')) if quote else None
            if quote and age is not None and 0 <= age <= 2 and all(isinstance(quote.get(k), (int, float)) and quote[k] > 0 for k in ('bid_size', 'ask_size')):
                return dict(symbol=symbol, fetched_at=quote['updated_at'],
                    bids=[dict(price=quote['bid'], size=quote['bid_size'])],
                    asks=[dict(price=quote['ask'], size=quote['ask_size'])])
            return self.client.orderbook(symbol, discovery=True)
        return self.client.orderbook(symbol)

    def cached_sizing_book(self, symbol, quantity):
        """A fresh BBO fully covering BOTH sides is sufficient sized depth."""
        cached = prepared_book(self, symbol)
        if cached is not None:
            return cached
        quote = self.cached_reference_quote(symbol)
        age = age_seconds(quote.get('updated_at')) if quote else None
        if quote and age is not None and 0 <= age <= 2 and all(isinstance(quote.get(k), (int, float)) and quote[k] >= quantity
                         for k in ('bid_size', 'ask_size')):
            return dict(symbol=symbol, fetched_at=quote['updated_at'], source=quote.get('source'),
                        funding=quote.get('funding'),
                        bids=[dict(price=quote['bid'], size=quote['bid_size'])],
                        asks=[dict(price=quote['ask'], size=quote['ask_size'])])
        return None

    def get_executable_quote(self, symbol, quantity):
        book = self.cached_sizing_book(symbol, float(quantity)) or self.get_sizing_book(symbol)
        return self.quote_from_sizing_book(symbol, quantity, book)

    def quote_from_sizing_book(self, symbol, quantity, book):
        """Build a size-bound quote locally; freshness and depth remain required."""
        age = age_seconds(book.get("fetched_at"))
        if age is None or not 0 <= age <= 5:
            raise ValueError("Arcus 盘口过期")
        buy, sell = depth_price(book["asks"], float(quantity)), depth_price(book["bids"], float(quantity))
        if not buy["sufficient"] or not sell["sufficient"]:
            raise ValueError("Arcus 可见深度不足")
        return {"symbol": _market_base(symbol), "size": quantity,
                "bid": sell["worst_price"], "ask": buy["worst_price"],
                "bid_vwap": sell["vwap"], "ask_vwap": buy["vwap"],
                "updated_at": book["fetched_at"], "executable": self.live_ready,
                "instrument_type": "perpetual", "fee_bps": float(self.verified_fee_bps),
                'funding': book.get('funding') or (self.cached_reference_quote(symbol) or {}).get('funding'),
                'sizing_book': book}

    def validate_quote_order(self, symbol, quantity, side, quote, slippage_bps):
        fresh_quote(quote, _market_base(symbol), quantity)
        reference = quote['ask' if direction(side) == 'buy' else 'bid']
        market = self._market(symbol)
        validate_trading_quote(market, side, quote)
        self._order_limit(symbol, side, reference, slippage_bps, market=market)
        return True

    def _order_limit(self, symbol, side, reference, slippage_bps, market=None):
        market = self._market(symbol) if market is None else market
        limit = protective_limit(reference, side, slippage_bps, market['tick_size'], market.get('tick_tiers'))
        # A valid but non-marketable grid point cannot complete the hedge.
        buy = direction(side) == 'buy'
        if ((buy and decimal(limit, True) < decimal(reference, True)) or
                (not buy and decimal(limit, True) > decimal(reference, True))):
            raise PriceGridError('Arcus 价格档位内没有符合预算的可成交限价')
        return limit

    def _position(self, symbol, payload=None):
        payload = self.client.positions() if payload is None else payload
        rows = payload.get("positions", payload) if isinstance(payload, dict) else []
        if isinstance(rows, dict): rows = list(rows.values())
        base = _market_base(symbol)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict): continue
            name = str(_row_value(row, "marketDisplayName", "symbol", "market", "name", default="")).upper()
            if base and _market_base(name) == base:
                signed_qty = _number(_row_value(row, "positionSize", "size", "quantity", "qty", "positionValueNotional"), 0.0)
                if "positionValueNotional" in row and not _row_value(row, "positionSize", "size", "quantity", "qty"):
                    mark = _number(_row_value(row, "markPx", "markPrice")) or 0
                    signed_qty = signed_qty / mark if mark else 0
                side = str(_row_value(row, "side", "positionSide", default="")).lower()
                if side not in ("long", "short"):
                    side = "long" if signed_qty >= 0 else "short"
                return {"qty": abs(signed_qty), "side": side, "raw": row}
        return {"qty": 0.0, "side": None, "raw": None}

    def position_snapshot(self, symbol):
        """Read the current authenticated position without placing an order."""
        snapshot = self._position(symbol)
        snapshot['fetched_at'] = _iso_now()
        return snapshot

    @staticmethod
    def _order_rows(payload):
        """Return the account's order rows without treating history as active.

        Arcus has returned both ``openOrders`` and a mixed ``orders`` history
        from the same endpoint.  The old precheck assumed every row under
        ``orders`` was live, so a previously filled order on the same market
        could block a later reduce-only close forever.
        """
        if not isinstance(payload, dict):
            return []
        rows = []
        for key in ("openOrders", "orders", "data", "items", "records"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = list(value.values())
            if not isinstance(value, list):
                continue
            rows.extend(value)
            # ``openOrders`` is already the authoritative live collection;
            # ``orders`` is the usual mixed collection.  Do not append aliases
            # a second time, which would inflate the displayed count.
            if key in ("openOrders", "orders"):
                break
        return rows

    @classmethod
    def _checked_order_rows(cls, payload):
        # An HTTP/schema failure must not be mistaken for no active orders.
        if (isinstance(payload, dict) and isinstance(payload.get('data'), dict) and
                not any(key in payload for key in ('openOrders', 'orders', 'items', 'records'))):
            payload = payload['data']
        if not isinstance(payload, dict) or not any(
                isinstance(payload.get(k), (list, dict))
                for k in ('openOrders', 'orders', 'data', 'items', 'records')):
            raise ValueError('Arcus 全账户活动订单未确认')
        rows = cls._order_rows(payload)
        if any(not isinstance(row, dict) or (
                normalize_order_update(row).get('status') not in ('filled', 'partial', 'canceled', 'rejected') and
                not _row_value(row, 'marketDisplayName', 'symbol', 'market', 'name')) for row in rows):
            raise ValueError('Arcus 活动订单合约未确认')
        return rows

    def _active_orders(self, symbol, payload=None):
        base = _market_base(symbol)
        active = []
        seen = set()
        for row in self._checked_order_rows(self.client.orders() if payload is None else payload):
            if not isinstance(row, dict):
                continue
            normalized = normalize_order_update(row)
            order_id = normalized.get("order_id")
            key = str(order_id) if order_id is not None else str(id(row))
            if key in seen:
                continue
            seen.add(key)
            market_name = str(_row_value(
                row, "marketDisplayName", "symbol", "market", "name", default=""))
            if _market_base(market_name) != base:
                continue
            if normalized.get("status") in ("filled", "partial", "canceled", "rejected"):
                continue
            active.append({"order": normalized, "raw": row})
        return active

    @staticmethod
    def _active_order_reason(item):
        order = item.get("order") or {}
        raw = item.get("raw") or {}
        order_id = str(order.get("order_id") or "?")
        # Keep diagnostic text bounded and free of arbitrary server content.
        if len(order_id) > 128 or not all(ch.isalnum() or ch in "._:-" for ch in order_id):
            order_id = "?"
        side = str(order.get("side") or "?").lower()
        if side not in ("buy", "sell"):
            side = "?"
        quantity = _number(_row_value(
            raw, "originalSize", "size", "quantity", "qty", "filledSize"), 0.0)
        reduce_only = bool(_row_value(raw, "reduceOnly", "reduce_only", default=False))
        return (f"Arcus 当前市场有活动订单：订单号 {order_id}，方向 {side}，"
                f"数量 {quantity:.12g}，只减仓 {'是' if reduce_only else '否'}")

    def validate_order(self, symbol, quantity, reduce_only=False, side=None, account_state=None):
        market = self._market(symbol)
        amount = decimal(quantity, True)
        reference_price = decimal(self.get_market_price(symbol), True) if not reduce_only else None
        if not reduce_only and (amount < decimal(market["min_base_amount"] or 0) or
                                amount * reference_price < decimal(market["min_quote_amount"] or 0)):
            raise ValueError("未满足 Arcus 最小下单数量或金额")
        if account_state is None or reduce_only:
            with ThreadPoolExecutor(max_workers=3) as pool:
                account_future = pool.submit(self.client.account)
                active_future = pool.submit(self._active_orders, symbol)
                position_future = pool.submit(self._position, symbol)
                account, active_orders, position = (account_future.result(), active_future.result(),
                                                     position_future.result())
        else:
            account = account_state['account']
            active_orders = self._active_orders(symbol, account_state['orders'])
            position = self._position(symbol, account_state['positions'])
        if active_orders:
            raise ValueError(self._active_order_reason(active_orders[0]))
        if reduce_only:
            close_side = "sell" if position["side"] == "long" else "buy"
            if position["qty"] < float(amount) - 1e-12 or direction(side) != close_side:
                raise ValueError("Arcus 只减仓数量或方向与真实持仓不符")
        else:
            self.validate_entry_position(symbol, position['qty'] *
                                         (1 if position['side'] == 'long' else -1), side)
        free = _number(_row_value(account if isinstance(account, dict) else {}, "freeCollateral", "availableBalance", "available_balance"))
        if not reduce_only:
            if free is None:
                raise ValueError("Arcus 账户可用保证金字段缺失，未发送订单")
            if decimal(free) < amount * reference_price:
                raise ValueError("Arcus 可用保证金不足以覆盖保守的一倍名义金额")
            return {'available_balance': str(free), 'min_base_amount': market['min_base_amount'],
                    'min_quote_amount': market['min_quote_amount']}

    def _receipt(self, order, fill, requested):
        status = order.get("status")
        if status not in ("filled", "partial", "canceled", "rejected"):
            return unknown("Arcus 订单仍在处理中，终态未确认")
        filled = _number(order.get("filled_size"), 0.0)
        if fill and not filled: filled = _number(fill.get("size"), 0.0)
        complete_fill = (isinstance(fill, dict) and
                         str(fill.get('order_id')) == str(order.get('order_id')) and
                         abs(float(fill.get('size') or 0) - filled) <= 1e-12)
        average = (_number(fill.get('price')) if complete_fill else None) or _number(order.get("average_price"))
        fee = order.get("fees_usdc")
        fee_source = 'arcus_order'
        if fee is None and complete_fill:
            fee = fill.get("fee")
            if fee is not None:
                fee_source = 'arcus_fills'
        fee_estimated = fee is None
        # Arcus order/fill streams do not always repeat the fee amount.  The
        # account-specific fee evidence is already required before this
        # adapter is enabled, so deriving the quote-currency fee is safe and
        # keeps the strategy from treating a known fill as unknown.
        if fee is None and filled and average is not None and self.verified_fee_bps is not None:
            fee = float(average) * float(filled) * float(self.verified_fee_bps) / 10000.0
            fee_source = 'account_rate_estimate'
        if filled > float(requested) + 1e-12:
            raise ValueError("Arcus 成交数量超过请求数量")
        if filled and (average is None or average <= 0):
            return unknown("Arcus 成交回报缺少成交价")
        if order.get('platform_reason_code') == 'FILL_WILL_EXCEED_TRADING_BOUND':
            self.client.invalidate_market_rules()
        return {"success": filled > 0, "confirmed": True, "status": status,
                "filled_size": float(filled), "average_price": average,
                "order_id": order.get("order_id"), "fees_usdc": fee,
                "fees_verified": fee is not None, "funding_usdc": None,
                "fee_source": fee_source, "fee_estimated": fee_estimated,
                "fee_basis": "Arcus 已核验账户费率 × 成交金额（估算）" if fee_estimated else "Arcus 平台成交实扣手续费",
                **{key: order.get(key) for key in DIAGNOSTIC_FIELDS}}

    def _wait_for_order(self, order_id, symbol):
        """Prefer stream events; recover a missed event without an idle 15s wait.

        One early collection read is allowed, never a polling loop. Keep the
        original final recovery for delayed history, and let the shared HTTP
        cooldown gate every read. An acknowledgement or fill alone is not a
        terminal order and must never trigger a second submission.
        """
        def terminal(order):
            return (isinstance(order, dict) and str(order.get('order_id')) == str(order_id)
                    and order.get('status') in ('filled', 'partial', 'canceled', 'rejected'))

        budget = max(0, self.poll_seconds)
        deadline = time.monotonic() + budget
        with self.transport_stage('Arcus WebSocket 回执等候', symbol):
            observed = self.client.ws.wait_order(order_id, min(1.0, budget))
        if terminal(observed):
            return observed
        if budget > 1:
            try:
                with self.transport_stage('Arcus 订单提前核验', symbol):
                    observed = self.client.order_status(order_id, collection_only=True)
                if terminal(observed):
                    return observed
            except Exception:
                # A failed read (including 429) cannot discard a later WS
                # confirmation. No inline HTTP retry; the final read remains
                # subject to the same cross-process cooldown.
                pass
            with self.transport_stage('Arcus WebSocket 后续等候', symbol):
                observed = self.client.ws.wait_order(order_id, max(0, deadline - time.monotonic()))
            if terminal(observed):
                return observed
        with self.transport_stage('Arcus 订单最终核验', symbol):
            observed = self.client.order_status(order_id)
        return observed if terminal(observed) else None

    def place_order(self, symbol, side, size, price, slippage_bps, client_order_id, quote, reduce_only=False):
        if not self.live_ready:
            return {"success": False, "confirmed": True, "status": "rejected", "filled_size": 0,
                    "submitted": False, "error": self.live_blocker}
        with self.lock:
            fresh_quote(quote, _market_base(symbol), size)
            normalized = direction(side)
            if decimal(price) != decimal(quote["ask" if normalized == "buy" else "bid"]):
                raise ValueError("Arcus 价格与本次报价不符")
            try:
                market = self._market(symbol)
                validate_trading_quote(market, normalized, quote)
                limit = self._order_limit(symbol, normalized, price, slippage_bps, market=market)
            except PriceGridError as exc:
                return {"success": False, "confirmed": True, "status": "rejected", "filled_size": 0,
                        "submitted": False, "error": str(exc)}
            intent = {"symbol": _market_base(symbol), "side": normalized, "size": str(size),
                      "price": limit, "reference_price": str(price), "slippage_bps": str(slippage_bps),
                      "time_in_force": "IOC", "quote_updated_at": str(quote.get('updated_at') or ''),
                      "reduce_only": bool(reduce_only), "client_order_id": client_order_id}
            row, new = self.journal.reserve(self.venue, self.account, client_order_id, intent)
            if not new:
                return json.loads(row["result"]) if row.get("result") else unknown("提交意图已存在，禁止重发")
            order_id = None
            try:
                try:
                    if not self.consume_entry_precheck(symbol, size, normalized, quote, reduce_only):
                        self.validate_order(symbol, size, reduce_only, normalized)
                    # Account reads may take several seconds. A quote that
                    # was fresh on entry must still be fresh at submission.
                    fresh_quote(quote, _market_base(symbol), size)
                except Exception as exc:
                    result = {"success": False, "confirmed": True, "status": "rejected", "filled_size": 0,
                              "submitted": False,
                              "error": "提交前账户检查失败，未发送订单：" + str(exc)}
                    self.journal.finish(row["seq"], result)
                    return result
                with self.account_mutation() as mutation:
                    with self.transport_stage('Arcus 签名发送', symbol):
                        args = (symbol, normalized, size, limit, client_order_id, reduce_only)
                        if quote.get('submit_deadline_monotonic') is not None:
                            accepted = self.client.submit(*args,
                                submit_deadline_monotonic=quote['submit_deadline_monotonic'])
                        else:
                            accepted = self.client.submit(*args)
                    if not accepted.get("accepted"):
                        result = {"success": False, "confirmed": True, "status": "rejected", "filled_size": 0,
                                  "error": accepted.get("error", "Arcus 下单被拒")}
                    else:
                        order_id = accepted["order_id"]
                        with self.transport_stage('Arcus 成交回执查询', symbol):
                            observed = self._wait_for_order(order_id, symbol)
                        if observed is None:
                            result = unknown("Arcus 已受理但订单终态超时，需核对，未重发")
                            result["order_id"] = order_id
                        else:
                            result = self._receipt(observed, self.client.ws.fills.get(order_id), size)
                    mutation['confirmed'] = result.get('confirmed') is True
            except (SubmissionWindowExpired, PriceGridError) as exc:
                self.account_reconciled(True)
                result = {'success': False, 'confirmed': True, 'status': 'rejected',
                          'filled_size': 0, 'submitted': False, 'error': str(exc)}
                self.journal.finish(row['seq'], result)
                return result
            except Exception as exc:
                result = unknown("Arcus 下单结果需核对：" + type(exc).__name__)
                if order_id is not None:
                    result['order_id'] = order_id
            result['submitted'] = True
            result['time_in_force'] = 'IOC'
            result['limit_price'] = float(limit)
            self.journal.finish(row["seq"], result)
            return result

    def close_position(self, symbol, side, size, client_order_id, reduce_only=True, slippage_bps=10):
        quote = self.get_executable_quote(symbol, size)
        close_side = "sell" if direction(side) == "buy" else "buy"
        price = quote["bid"] if close_side == "sell" else quote["ask"]
        return self.place_order(symbol, close_side, size, price, slippage_bps,
                                client_order_id, quote, reduce_only=reduce_only)

    def assert_flat(self, symbol):
        position = self._position(symbol)
        if position["qty"] > 1e-12:
            raise ValueError("Arcus 平仓后仍有持仓")
        return True

    def reconcile_order(self, sequence):
        row = self.journal.get(sequence)
        if not row or row["venue"] != self.venue or row["account"] != self.account:
            raise ValueError("日志订单不属于 Arcus 当前账户")
        result = json.loads(row["result"]) if row.get("result") else {}
        order_id = result.get("order_id")
        if not order_id:
            raise ValueError("Arcus 未保存订单号，需在平台订单/成交记录中核对")
        observed = self.client.order_status(order_id)
        if observed is None:
            raise ValueError("Arcus 订单仍需在平台订单/成交记录中核对")
        receipt = self._receipt(observed, self.client.ws.fills.get(str(order_id)),
                                decimal(json.loads(row["intent"])["size"], True))
        self.journal.finish(sequence, receipt)
        self.account_reconciled(receipt.get('confirmed') is True)
        return receipt

    @account_transaction()
    def cancel_order(self, sequence):
        if not self.live_ready:
            raise ValueError(self.live_blocker)
        row = self.journal.get(sequence)
        if not row or row["venue"] != self.venue or row["account"] != self.account:
            raise ValueError("无法取消其他账户或未记录的 Arcus 订单")
        result = json.loads(row["result"]) if row.get("result") else {}
        if result.get("confirmed") is True:
            return result
        order_id = result.get("order_id")
        if not order_id:
            raise ValueError("未知回执没有可安全撤销的订单号，需先对账")
        intent = json.loads(row["intent"])
        response = self.client.cancel(intent["symbol"], order_id)
        if not response.get("accepted"):
            raise ValueError("Arcus 撤单未受理，需核对订单状态")
        return response
