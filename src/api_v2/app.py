from __future__ import annotations

import asyncio
import json
import math
from itertools import combinations
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Dict, List, Optional, Sequence, Tuple

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .adapters import VenueAdapter, build_adapters, canonical_perpetual
from .history import MAX_HISTORY_SECONDS, MultiVenueHistory
from .models import Instrument, Opportunity, QuoteSnapshot
from .return_estimates import attach_return_previews, display_policy
from src.common.market_data import MarketDataError


class ApiState:
    def __init__(self) -> None:
        self.adapters: Dict[str, VenueAdapter] = {}
        self.scan_offsets: Dict[str, int] = {}
        self.quote_slots = asyncio.Semaphore(32)
        self.history = MultiVenueHistory(Path(__file__).resolve().parents[2] / "data" / "multi_venue_history.sqlite")
        self.reference_history_cache: Dict[tuple, tuple[float, dict]] = {}


state = ApiState()

# A cross-venue opportunity is only meaningful when both feeds use a
# comparable price unit. A stale/incorrect contract mapping can otherwise
# turn a 10x quote difference into a fake 900% arbitrage. Large deviations are
# excluded from the opportunity list and remain visible in the raw quote API.
MAX_CROSS_VENUE_PRICE_DEVIATION = 0.50
FEE_EVIDENCE_MAX_AGE_SECONDS = 300.0
FEE_DISPLAY_MAX_AGE_SECONDS = 86400.0
# A centre-based score is useful only after a small amount of route history.
# Keep rows with less history visible, but let well-sampled mean-reversion
# opportunities win ties over a plain current-spread ranking.
REVERSION_SCORE_MIN_SAMPLES = 30


def _fee_evidence(venue: str) -> tuple[Optional[float], bool]:
    """Return the last verified fee and whether it needs a fresh recheck.

    Credentials are never read here.  The web process writes a sanitized fee
    result after its account verification.  A stale value can remain visible
    as an informational estimate for one day, but the execution gate still
    requires a fresh five-minute verification.
    """
    root = Path(__file__).resolve().parents[2]
    try:
        if venue == "lighter":
            payload = json.loads((root / "config" / "variational-execution.local.json").read_text())
            if payload.get("lighter_fee_verified") is not True:
                return None, False
            value = payload.get("lighter_fee_bps")
            checked_at = payload.get("lighter_fee_checked_at")
        elif venue in ("lighter_rh", "standx", "arcus"):
            reports = json.loads((root / "data" / "platform-verification.json").read_text())
            payload = reports.get(venue, {}) if isinstance(reports, dict) else {}
            if payload.get("authenticated") is not True:
                return None, False
            if venue == "arcus" and payload.get("fee_verified") is not True:
                return None, False
            value = payload.get("taker_fee_bps")
            checked_at = payload.get("checked_at")
        else:
            return None, False
        if value is None or not checked_at:
            return None, False
        checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            return None, False
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        if age < 0 or age > FEE_DISPLAY_MAX_AGE_SECONDS:
            return None, False
        if isinstance(value, bool):
            return None, False
        fee = float(value)
        return (fee, age > FEE_EVIDENCE_MAX_AGE_SECONDS) if math.isfinite(fee) and fee >= 0 else (None, False)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, False


def _verified_fee_bps(venue: str) -> Optional[float]:
    value, _ = _fee_evidence(venue)
    return value


async def bounded(awaitable, seconds: float):
    """Bound a connector call so a dead proxy cannot stall the API surface."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError:
        return None


def adapter_timeout(adapter: VenueAdapter) -> float:
    # A cold Lighter catalog request can take around 10s through the local
    # proxy. The adapter single-flights and caches it, so this remains a hard
    # bound rather than a page-wide hang; later polls are immediate.
    # Public exchange endpoints occasionally take a few seconds through the
    # local proxy on a cold connection. Keep the bound finite, but do not turn
    # a normal first catalog/book request into a false "timeout" quote.
    if adapter.venue in ("lighter", "lighter_rh"):
        return 15.0
    if adapter.venue in ("binance", "gate", "okx"):
        return 8.0
    return 6.0


@asynccontextmanager
async def lifespan(_: FastAPI):
    from src.common.quote_clock import quote_clock
    quote_clock.start()
    try:
        state.adapters = build_adapters()
        yield
    finally:
        quote_clock.stop()
        from .center_statistics import close_service
        close_service(state.history)
        await asyncio.gather(*(adapter.close() for adapter in state.adapters.values()),
                             return_exceptions=True)


app = FastAPI(
    title="Arbitrage Desk API",
    version="2.0.0-readonly",
    description="统一平台目录、报价与能力接口。当前版本只读，不提交真实订单。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000", "http://localhost:3000",
        "http://127.0.0.1:5001", "http://localhost:5001",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["X-Scan-Coverage"],
)


def get_adapter(venue: str) -> VenueAdapter:
    adapter = state.adapters.get(venue)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"未知平台: {venue}")
    return adapter


async def calculate_opportunities(symbol: str, pairs: Sequence[Tuple[str, str]], notional: float = 1000.0, record: bool = True) -> List[Opportunity]:
    requested = {name for pair in pairs for name in pair}
    async def fetch(name: str):
        # All scan modes share one small gate so a full-catalog refresh cannot
        # burst every venue at once or overlap with a selection probe.
        async with state.quote_slots:
            return await bounded(get_adapter(name).get_quote(symbol), adapter_timeout(get_adapter(name)))
    fetched = await asyncio.gather(*(fetch(name) for name in requested))
    quotes_by_venue = dict(zip(requested, fetched))
    return build_opportunities(symbol, pairs, quotes_by_venue, record=record, notional=notional)


def _metadata_number(quote: QuoteSnapshot, key: str) -> Optional[float]:
    try:
        value = quote.metadata.get(key)
        number = None if value is None or isinstance(value, bool) else float(value)
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _book_impact_bps(quote: QuoteSnapshot, side: str, quantity: float) -> Optional[float]:
    """Estimate adverse VWAP impact for the requested equal hedge size."""
    if quote.stale or not math.isfinite(quantity) or quantity <= 0:
        return None
    levels = quote.metadata.get('book_asks' if side == 'buy' else 'book_bids')
    if not isinstance(levels, list) or not levels:
        # A fresh, normalized BBO is sufficient when the whole requested leg
        # fits at the best price. Do not extrapolate beyond its visible size.
        size = _metadata_number(quote, 'ask_size_base' if side == 'buy' else 'bid_size_base')
        price = quote.ask if side == 'buy' else quote.bid
        age = quote.age_seconds
        if (age is not None and math.isfinite(age) and 0 <= age <= 10
                and price is not None and math.isfinite(price) and price > 0
                and size is not None and size > 0 and quantity <= size):
            return 0.0
        return None
    prices = []
    remaining = quantity
    cost = 0.0
    for level in levels:
        try:
            price = float(level.get('price'))
            size = float(level.get('size'))
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size <= 0:
            continue
        prices.append(price)
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= max(1e-12, quantity * 1e-10):
            break
    if remaining > max(1e-12, quantity * 1e-10) or not prices:
        return None
    top = prices[0]
    vwap = cost / quantity
    return ((vwap / top) - 1.0) * 10000 if side == 'buy' else (1.0 - vwap / top) * 10000


def _direction_costs(buy: QuoteSnapshot, sell: QuoteSnapshot, gross_bps: float, notional: float = 1000.0) -> dict:
    preview_quantity = notional / max(buy.ask or 0, sell.ask or 0, buy.bid or 0, sell.bid or 0)
    buy_size = _metadata_number(buy, "ask_size_base")
    sell_size = _metadata_number(sell, "bid_size_base")
    sizes = [value for value in (buy_size, sell_size) if value is not None and value >= 0]
    max_base = min(sizes) if len(sizes) == 2 else None
    depth_reason = None if max_base is not None else "至少一侧未提供首档数量或合约数量单位未核实"
    if buy.stale or sell.stale:
        max_base = None
        depth_reason = "至少一侧报价已过期，等待新盘口"
    max_notional = max_base * buy.ask if max_base is not None and buy.ask else None
    buy_fee = _metadata_number(buy, "taker_fee_bps")
    sell_fee = _metadata_number(sell, "taker_fee_bps")
    buy_fee_stale = sell_fee_stale = False
    if buy_fee is None:
        buy_fee, buy_fee_stale = _fee_evidence(buy.venue)
    if sell_fee is None:
        sell_fee, sell_fee_stale = _fee_evidence(sell.venue)
    # Normalize both legs to the buy notional used by gross_bps.
    price_ratio = sell.bid / buy.ask
    fee_bps = buy_fee + sell_fee * price_ratio if buy_fee is not None and sell_fee is not None else None
    requested_quantity = notional / buy.ask if buy.ask and notional > 0 else None
    buy_slippage = _metadata_number(buy, "slippage_bps")
    sell_slippage = _metadata_number(sell, "slippage_bps")
    if buy_slippage is None and requested_quantity is not None:
        buy_slippage = _book_impact_bps(buy, 'buy', requested_quantity)
    if sell_slippage is None and requested_quantity is not None:
        sell_slippage = _book_impact_bps(sell, 'sell', requested_quantity)
    slippage_bps = buy_slippage + sell_slippage * price_ratio if buy_slippage is not None and sell_slippage is not None else None
    wear_bps = fee_bps + slippage_bps if fee_bps is not None and slippage_bps is not None else None
    breakdown = {"fees_bps": fee_bps, "slippage_bps": slippage_bps, "funding_bps": None}
    summary = "手续费未知" if fee_bps is None else f"手续费 {fee_bps:.2f} bps"
    if fee_bps is not None and (buy_fee_stale or sell_fee_stale):
        summary += "（已自动复查前的最近核验值）"
    summary += "；盘口滑点待测" if slippage_bps is None else f"；盘口滑点 {slippage_bps:.2f} bps"
    summary += "；盘口冲击按当前同数量快照估算，不保证最终成交滑点；资金费/平仓成本未计"
    # Net spread uses every verified entry cost that is available. When both
    # fee and same-size order-book impact are known, subtract the complete
    # wear total; when only fees are known, keep the partial net value and
    # clearly label the missing slippage in the summary.
    net_note = None
    if fee_bps is not None and not buy.stale and not sell.stale:
        if wear_bps is not None:
            net_bps = gross_bps - wear_bps
            net_note = "净价差已扣手续费及已知盘口滑点"
        else:
            net_bps = gross_bps - fee_bps
            net_note = "净价差已扣手续费，盘口滑点待测"
    else:
        net_bps = None
    return {
        "net_bps": net_bps,
        "return_inputs": {
            "quantity": preview_quantity,
            "buy": {"bid": buy.bid, "ask": buy.ask, "fee_bps": buy_fee,
                    'funding': buy.metadata.get('funding'),
                    "buy_impact_bps": _book_impact_bps(buy, 'buy', preview_quantity),
                    "sell_impact_bps": _book_impact_bps(buy, 'sell', preview_quantity)},
            "sell": {"bid": sell.bid, "ask": sell.ask, "fee_bps": sell_fee,
                     'funding': sell.metadata.get('funding'),
                     "buy_impact_bps": _book_impact_bps(sell, 'buy', preview_quantity),
                     "sell_impact_bps": _book_impact_bps(sell, 'sell', preview_quantity)},
        },
        "max_base_amount": max_base,
        "max_notional_usdc": max_notional,
        "depth_reason": depth_reason,
        "wear_bps": wear_bps,
        "wear_summary": summary + (("；" + net_note) if net_note else ""),
        "wear_breakdown": breakdown,
    }


def build_opportunities(symbol: str, pairs: Sequence[Tuple[str, str]], quotes_by_venue: Dict[str, Optional[QuoteSnapshot]], record: bool = True, notional: float = 1000.0) -> List[Opportunity]:
    result: List[Opportunity] = []
    for first, second in pairs:
        rows = (quotes_by_venue.get(first), quotes_by_venue.get(second))
        if any(row is None for row in rows):
            continue
        # Cached snapshots and earlier batch results age while waiting for
        # the other leg; never present their original age as current.
        now = datetime.now(timezone.utc)
        rows = tuple(row.model_copy(update={
            "age_seconds": max(row.age_seconds, (now - row.fetched_at).total_seconds(), 0)
                if row.age_seconds is not None else None,
            "stale": row.stale or row.age_seconds is None
                or max(row.age_seconds or 0, (now - row.fetched_at).total_seconds()) > 10,
        }) for row in rows)
        mids = [((row.bid or 0) + (row.ask or 0)) / 2 for row in rows]
        if any(mid <= 0 for mid in mids):
            continue
        if min(mids) / max(mids) < MAX_CROSS_VENUE_PRICE_DEVIATION:
            # Different contract units, pre-market symbols, or a bad feed;
            # never present this as an executable spread.
            continue
        for buy, sell in ((rows[0], rows[1]), (rows[1], rows[0])):
            if buy.ask is None or sell.bid is None or buy.ask <= 0:
                continue
            gross = (sell.bid / buy.ask - 1) * 10000
            costs = _direction_costs(buy, sell, gross, notional=notional)
            executable = bool(buy.executable and sell.executable and not buy.stale and not sell.stale)
            net_bps = costs.get('net_bps')
            if net_bps is not None and net_bps <= 0:
                reason = '当前入场净价差不为正；是否有回归机会需按目标及整轮成本复核'
            elif not executable:
                reason = '当前至少一侧不是可执行实时报价'
            else:
                reason = None
            result.append(Opportunity(
                canonical_symbol=canonical_perpetual(symbol), buy_venue=buy.venue, sell_venue=sell.venue,
                buy_price=buy.ask, sell_price=sell.bid, gross_bps=gross,
                **costs,
                buy_age_seconds=buy.age_seconds, sell_age_seconds=sell.age_seconds,
                buy_stale=buy.stale, sell_stale=sell.stale,
                buy_source=buy.source, sell_source=sell.source,
                executable=executable,
                reason=reason,
            ))
    result.sort(key=opportunity_sort_key, reverse=True)
    if record:
        state.history.append(canonical_perpetual(symbol), result)
    return result


def attach_history_stats(rows: List[Opportunity], notional: float = 1000.0,
                         target_mode: str = 'center', target_window_seconds: int = 604800,
                         history_window_seconds: int = 604800) -> List[Opportunity]:
    """Observed ranges plus the execution model at the selected filter target."""
    from .center_statistics import center_stats_for
    keys = {(row.canonical_symbol, state.history.pair_key(row.buy_venue, row.sell_venue)) for row in rows}
    stats_24h = {key:center_stats_for(state.history, *key, 86400) for key in keys}
    stats_7d = {key:center_stats_for(state.history, *key, 604800) for key in keys}
    by_window = {86400: stats_24h, 604800: stats_7d}
    for window in {int(target_window_seconds), int(history_window_seconds)} - by_window.keys():
        by_window[window] = {key:center_stats_for(state.history, *key, window) for key in keys}
    stats_target = by_window[int(target_window_seconds)]
    stats_display = by_window[int(history_window_seconds)]
    route_stats = {}
    for row in rows:
        key = (row.canonical_symbol, state.history.pair_key(row.buy_venue, row.sell_venue))
        if key not in route_stats:
            route_stats[key] = stats_target[key]
    enriched = []
    for row in rows:
        key = (row.canonical_symbol, state.history.pair_key(row.buy_venue, row.sell_venue))
        current = stats_24h.get(key, {})
        weekly = stats_7d.get(key, {})
        reversion = route_stats.get(key, {})
        display = stats_display.get(key, {})
        center = reversion.get('center_bps')
        samples = int(reversion.get('samples') or 0)
        ready = center is not None and samples >= REVERSION_SCORE_MIN_SAMPLES
        deviation = None
        estimate = None
        enriched.append(row.model_copy(update={
            **current,
            'history_7d_samples': weekly.get('history_samples', 0),
            'history_7d_min_bps': weekly.get('history_min_bps'),
            'history_7d_max_bps': weekly.get('history_max_bps'),
            'history_7d_avg_bps': weekly.get('history_avg_bps'),
            'history_raw_min_bps': current.get('history_raw_min_bps'),
            'history_raw_max_bps': current.get('history_raw_max_bps'),
            'history_raw_samples': current.get('history_raw_samples', 0),
            'history_anomaly_count': current.get('history_anomaly_count', 0),
            'history_anomaly_threshold_bps': current.get('history_anomaly_threshold_bps'),
            'history_7d_raw_min_bps': weekly.get('history_raw_min_bps'),
            'history_7d_raw_max_bps': weekly.get('history_raw_max_bps'),
            'history_7d_raw_samples': weekly.get('history_raw_samples', 0),
            'history_7d_anomaly_count': weekly.get('history_anomaly_count', 0),
            'history_7d_anomaly_threshold_bps': weekly.get('history_anomaly_threshold_bps'),
            'history_target_window_seconds': int(history_window_seconds),
            'history_target_min_bps': display.get('history_min_bps'),
            'history_target_max_bps': display.get('history_max_bps'),
            'history_target_avg_bps': display.get('history_avg_bps'),
            'history_target_samples': display.get('history_samples', 0),
            'history_target_raw_min_bps': display.get('history_raw_min_bps'),
            'history_target_raw_max_bps': display.get('history_raw_max_bps'),
            'history_target_raw_samples': display.get('history_raw_samples', 0),
            'history_target_anomaly_count': display.get('history_anomaly_count', 0),
            'history_target_anomaly_threshold_bps': display.get('history_anomaly_threshold_bps'),
            'history_center_bps': center,
            'history_p10_bps': reversion.get('p10_bps'),
            'history_p90_bps': reversion.get('p90_bps'),
            'history_reversion_count': None,
            'history_last_reversion_age_seconds': reversion.get('last_reversion_age_seconds'),
            'history_median_interval_seconds': reversion.get('median_interval_seconds'),
            'reversion_target_bps': center,
            'favorable_deviation_bps': deviation,
            'estimated_reversion_profit_usdc': estimate,
            'reversion_ready': bool(ready),
        }))
    return attach_return_previews(enriched, state.history, display_policy(Path(__file__).resolve().parents[2]),
                                  target_mode, target_window_seconds)


def opportunity_sort_key(item: Opportunity):
    """Rank by estimated route-specific convergence profit, then net edge."""
    estimate = item.estimated_reversion_profit_usdc
    if estimate is not None and math.isfinite(float(estimate)):
        return (3 if item.return_preview.get('meets_floor') else 2, float(estimate),
                -max(item.buy_age_seconds if item.buy_age_seconds is not None else math.inf,
                     item.sell_age_seconds if item.sell_age_seconds is not None else math.inf),
                item.return_preview.get('net_bps', -math.inf))
    if item.net_bps is not None and math.isfinite(float(item.net_bps)):
        return (1, float(item.net_bps))
    return (0, float(item.gross_bps))


def apply_gross_spread_filter(rows: List[Opportunity], minimum: float = 0,
                              maximum: float = 0) -> List[Opportunity]:
    """Filter discovery rows by absolute gross spread before serialization."""
    return [row for row in rows
            if abs(float(row.gross_bps)) >= minimum and (not maximum or abs(float(row.gross_bps)) <= maximum)]


@app.get("/api/v2/health")
async def health() -> dict:
    from src.common.quote_clock import quote_clock
    return {"service": "arbitrage-desk-api", "version": app.version, "mode": "readonly",
            "quote_clock": quote_clock.status()}


@app.get("/api/v2/venues")
async def venues() -> list:
    result = []
    for adapter in state.adapters.values():
        try:
            result.append(adapter.status().model_dump(mode="json"))
        except Exception as exc:
            result.append(adapter.status(str(exc)).model_dump(mode="json"))
    return result


@app.get("/api/v2/instruments", response_model=List[Instrument])
async def instruments(venue: Optional[str] = Query(default=None)) -> List[Instrument]:
    adapters = [get_adapter(venue)] if venue else list(state.adapters.values())
    rows: List[Instrument] = []
    responses = await asyncio.gather(*(bounded(adapter.list_instruments(), adapter_timeout(adapter)) for adapter in adapters),
                                     return_exceptions=True)
    for adapter, response in zip(adapters, responses):
        if response is None or isinstance(response, Exception):
            if venue:
                if response is None:
                    raise HTTPException(status_code=504, detail=f"{adapter.display_name} 品种目录请求超时")
                # PublicClient sanitizes transport failures. Never expose
                # arbitrary connector exception text (URLs may contain secrets).
                reason = str(response) if isinstance(response, MarketDataError) else type(response).__name__
                raise HTTPException(status_code=503, detail=f"{adapter.display_name} 品种目录暂不可用：{reason}")
            # A venue outage should not hide instruments from other venues.
            continue
        rows.extend(response)
    return rows


@app.get("/api/v2/quotes", response_model=List[QuoteSnapshot])
async def quotes(symbol: str = Query(min_length=1), venue: Optional[str] = Query(default=None)) -> List[QuoteSnapshot]:
    adapters = [get_adapter(venue)] if venue else list(state.adapters.values())
    rows = await asyncio.gather(*(bounded(adapter.get_quote(symbol), adapter_timeout(adapter)) for adapter in adapters))
    return [row if row is not None else QuoteSnapshot(
        venue=adapter.venue, symbol=symbol, venue_symbol=symbol,
        instrument_type="unknown", source=adapter.venue, stale=True,
        reason="平台请求超时") for adapter, row in zip(adapters, rows)]


def requested_symbols(symbol: Optional[str], symbols: Optional[str]) -> list[str]:
    values = []
    if symbols:
        values.extend(item.strip() for item in symbols.split(","))
    if symbol:
        values.append(symbol.strip())
    result = []
    for value in values:
        if value and value.upper() not in {item.upper() for item in result}:
            result.append(value.upper())
    return result


@app.get("/api/v2/opportunities", response_model=List[Opportunity])
async def opportunities(symbol: Optional[str] = Query(default=None),
                        symbols: Optional[str] = Query(default=None),
                        venues: Optional[str] = Query(default=None),
                        venues_pair: Optional[str] = Query(default=None, alias="pair"),
                        notional: float = Query(default=1000.0, ge=1.0, le=1000000.0),
                        gross_min_bps: float = Query(default=0, ge=0),
                        gross_max_bps: float = Query(default=0, ge=0),
                        target_mode: Annotated[str, Query(pattern='^(center|zero|either)$')] = 'center',
                        target_window_seconds: Annotated[int, Query(ge=86400, le=MAX_HISTORY_SECONDS)] = 604800, history_window_seconds: Annotated[int, Query(ge=86400, le=MAX_HISTORY_SECONDS)] = 604800) -> List[Opportunity]:
    if gross_max_bps and gross_max_bps < gross_min_bps:
        raise HTTPException(status_code=400, detail="毛价差上限不能小于下限")
    symbols_list = requested_symbols(symbol, symbols)
    if not symbols_list:
        raise HTTPException(status_code=400, detail="至少指定一个品种")
    if venues:
        names = [item.strip() for item in venues.split(",") if item.strip()]
        pairs = list(combinations(dict.fromkeys(names), 2))
    elif venues_pair:
        names = venues_pair.split(",", 1)
        pairs = [tuple(names)] if len(names) == 2 else []
    else:
        pairs = list(combinations(state.adapters.keys(), 2))
    if not pairs or any(any(name not in state.adapters for name in pair) for pair in pairs):
        return []
    batches = await asyncio.gather(*(calculate_opportunities(item, pairs, notional) for item in symbols_list))
    result = apply_gross_spread_filter([row for batch in batches for row in batch], gross_min_bps, gross_max_bps)
    result.sort(key=opportunity_sort_key, reverse=True)
    return sorted(attach_history_stats(result, notional, target_mode, target_window_seconds, history_window_seconds), key=opportunity_sort_key, reverse=True)


@app.get("/api/v2/best-opportunities", response_model=List[Opportunity])
async def best_opportunities(response: Response, venues: Optional[str] = Query(default=None), venues_pair: Optional[str] = Query(default=None, alias="pair"), limit: int = Query(default=20, ge=1, le=100), notional: float = Query(default=1000.0, ge=1.0, le=1000000.0), target_mode: Annotated[str, Query(pattern='^(center|zero|either)$')] = 'center', target_window_seconds: Annotated[int, Query(ge=86400, le=MAX_HISTORY_SECONDS)] = 604800, history_window_seconds: Annotated[int, Query(ge=86400, le=MAX_HISTORY_SECONDS)] = 604800) -> List[Opportunity]:
    if venues:
        names = [item.strip() for item in venues.split(",") if item.strip()]
        pairs = list(combinations(dict.fromkeys(names), 2))
    elif venues_pair:
        names = venues_pair.split(",", 1)
        pairs = [tuple(names)] if len(names) == 2 else []
    else:
        pairs = list(combinations(state.adapters.keys(), 2))
    if not pairs or any(any(name not in state.adapters for name in pair) for pair in pairs):
        return []
    # Only load catalogs for the venues participating in this scan. Loading
    # every configured venue here made an opt-in public venue compete with
    # slow browser/Lighter requests and could make its own catalog appear
    # empty even when the exchange was healthy.
    selected_names = list(dict.fromkeys(name for pair in pairs for name in pair))
    catalogs = await asyncio.gather(
        *(bounded(get_adapter(name).list_instruments(), adapter_timeout(get_adapter(name)))
          for name in selected_names),
        return_exceptions=True,
    )
    by_venue = {name: (rows if isinstance(rows, list) else []) for name, rows in zip(selected_names, catalogs)}
    jobs = []
    for first, second in pairs:
        first_symbols = {row.canonical_symbol for row in by_venue.get(first, [])}
        second_symbols = {row.canonical_symbol for row in by_venue.get(second, [])}
        jobs.extend((symbol, (first, second)) for symbol in sorted(first_symbols & second_symbols))

    # Fetch each venue/symbol once, regardless of how many pairs use it.
    # Bulk feeds remain bulk even when combined with a legacy connector.
    symbols_by_venue = {name: set() for name in selected_names}
    for symbol, pair in jobs:
        for name in pair:
            symbols_by_venue[name].add(symbol)
    quote_maps = {name: {} for name in selected_names}
    catalog_complete = all(isinstance(rows, list) and rows for rows in catalogs)
    async def collect(name):
        adapter = get_adapter(name)
        symbols = sorted(symbols_by_venue[name])
        if not symbols:
            return
        bulk = getattr(adapter, 'get_quotes', None)
        if bulk:
            rows = await bulk(symbols)
            quote_maps[name].update({canonical_perpetual(k): v for k, v in rows.items()})
            return
        # Rotate bounded legacy scans so slow large catalogs cannot starve
        # symbols at the end or block public feeds from returning results.
        offset = state.scan_offsets.get(name, 0) % len(symbols)
        symbols = symbols[offset:] + symbols[:offset]
        async def one(symbol):
            async with state.quote_slots:
                state.scan_offsets[name] = (state.scan_offsets.get(name, 0) + 1) % len(symbols)
                quote_maps[name][symbol] = await bounded(adapter.get_quote(symbol), adapter_timeout(adapter))
        await asyncio.gather(*(one(symbol) for symbol in symbols))

    tasks = [asyncio.create_task(collect(name)) for name in selected_names]
    try:
        done, pending = await asyncio.wait(tasks, timeout=10.0)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    complete = catalog_complete and all(not isinstance(item, BaseException) for item in outcomes)
    complete = complete and all(
        (quote_maps[name].get(symbol) is not None
         and quote_maps[name][symbol].bid is not None
         and quote_maps[name][symbol].ask is not None)
        for name, symbols in symbols_by_venue.items() for symbol in symbols
    )
    response.headers['X-Scan-Coverage'] = 'complete' if complete else 'partial'
    result = []
    for symbol, pair in jobs:
        result.extend(build_opportunities(symbol, [pair],
                      {name: quote_maps[name].get(symbol) for name in pair}, record=False, notional=notional))
    # One transaction off the event loop, rather than one SQLite transaction
    # for every market/pair in a full exchange scan.
    await asyncio.to_thread(state.history.append_many, result)
    result.sort(key=opportunity_sort_key, reverse=True)
    return sorted(attach_history_stats(result, notional, target_mode, target_window_seconds, history_window_seconds), key=opportunity_sort_key, reverse=True)[:limit]


@app.get("/api/v2/history")
async def history(symbol: str = Query(min_length=1),
                  seconds: int = Query(default=3600, ge=300, le=MAX_HISTORY_SECONDS),
                  pair: Optional[str] = Query(default=None)) -> list[dict]:
    """Return history keyed by the selected buy/sell route."""
    if seconds not in (300, 900, 3600, 21600, 86400, 604800, 2592000, MAX_HISTORY_SECONDS):
        raise HTTPException(status_code=400, detail="时间范围无效")
    return state.history.read(canonical_perpetual(symbol), seconds, pair)


@app.get("/api/v2/reference-history")
async def reference_history(symbol: str = Query(min_length=1),
                            buy_venue: str = Query(min_length=1),
                            sell_venue: str = Query(min_length=1),
                            seconds: int = Query(default=86400, ge=300, le=604800),
                            interval_seconds: int = Query(default=300, ge=60, le=3600)) -> dict:
    """Build a non-executable reference spread from exchange candle closes.

    This is intentionally separate from ``/history``: candle closes do not
    prove historical best bid/ask execution. Missing candles stay missing and
    are never filled with local BBO observations.
    """
    allowed_intervals = (60, 300, 900, 3600)
    if interval_seconds not in allowed_intervals:
        raise HTTPException(status_code=400, detail="参考历史粒度只支持 1m、5m、15m、1h")
    if buy_venue == sell_venue or buy_venue not in state.adapters or sell_venue not in state.adapters:
        raise HTTPException(status_code=400, detail="请选择两个不同的已接入平台")
    canonical = canonical_perpetual(symbol)
    cache_key = (canonical, buy_venue, sell_venue, seconds, interval_seconds)
    cached = state.reference_history_cache.get(cache_key)
    if cached and (asyncio.get_running_loop().time() - cached[0]) < 30:
        return cached[1]
    requested = (state.adapters[buy_venue], state.adapters[sell_venue])
    results = await asyncio.gather(*(
        adapter.get_reference_candles(canonical, seconds, interval_seconds)
        for adapter in requested
    ), return_exceptions=True)
    maps = []
    errors = []
    for venue, result in zip((buy_venue, sell_venue), results):
        if isinstance(result, BaseException):
            maps.append({})
            errors.append({'venue': venue, 'error': type(result).__name__})
            continue
        values = {}
        for point in result if isinstance(result, list) else []:
            try:
                timestamp = int(float(point.get('timestamp')) // interval_seconds * interval_seconds)
                close = float(point.get('close'))
                if timestamp > 0 and math.isfinite(close) and close > 0:
                    values[timestamp] = close
            except (AttributeError, TypeError, ValueError):
                continue
        maps.append(values)
    common = sorted(set(maps[0]).intersection(maps[1]))
    points = [{
        'timestamp': timestamp,
        'reference_bps': (maps[1][timestamp] / maps[0][timestamp] - 1.0) * 10000.0,
        'buy_close': maps[0][timestamp],
        'sell_close': maps[1][timestamp],
        'reference_kind': 'exchange_candle_close',
    } for timestamp in common[-500:]]
    response = {
        'symbol': canonical, 'pair_key': buy_venue + '->' + sell_venue,
        'seconds': seconds, 'interval_seconds': interval_seconds,
        'source': 'exchange_candles', 'executable': False,
        'points': points, 'sample_count': len(points),
        'buy_samples': len(maps[0]), 'sell_samples': len(maps[1]),
        'errors': errors,
        'note': '交易所历史 K 线收盘价重建，仅作参考；不代表历史可成交买一卖一。',
    }
    state.reference_history_cache[cache_key] = (asyncio.get_running_loop().time(), response)
    if len(state.reference_history_cache) > 256:
        oldest = min(state.reference_history_cache, key=lambda key: state.reference_history_cache[key][0])
        state.reference_history_cache.pop(oldest, None)
    return response


@app.get("/api/v2/reversion-stats")
async def reversion_stats(symbol: str = Query(min_length=1),
                          pair: str = Query(min_length=3),
                          seconds: int = Query(default=604800, ge=300, le=MAX_HISTORY_SECONDS),
                          band_bps: float = Query(default=2.0, ge=0.1, le=100.0),
                          min_deviation_bps: float = Query(default=0.0, ge=0.0, le=100000.0),
                          target_mode: str = Query(default='center')) -> dict:
    """Return on-demand local statistics for returns into a route target band."""
    if '->' not in pair or pair.startswith('->') or pair.endswith('->'):
        raise HTTPException(status_code=400, detail="路线格式应为 buy_venue->sell_venue")
    if target_mode not in ('center', 'zero', 'either'):
        raise HTTPException(status_code=400, detail="回归目标必须是 center、zero 或 either")
    return await asyncio.to_thread(state.history.reversion_stats, canonical_perpetual(symbol),
                                   pair, seconds, band_bps, target_mode, min_deviation_bps, execution_basis=True)


@app.get("/api/v2/reversion-quality")
async def reversion_quality(symbol: str = Query(min_length=1, max_length=64),
                            pair: str = Query(min_length=3, max_length=128),
                            seconds: int = Query(default=604800, ge=300, le=MAX_HISTORY_SECONDS),
                            target_mode: str = Query(default='center'),
                            min_deviation_bps: float = Query(default=0., ge=0., le=100000.),
                            channel: Optional[str] = Query(default=None, max_length=4096)) -> dict:
    """Cached diagnostics only; no exchange reads or execution state changes."""
    if pair.count('->') != 1 or not all(pair.split('->')) or len(set(pair.split('->'))) != 2:
        raise HTTPException(status_code=400, detail='需指定两个不同平台的有向路线')
    from .center_statistics import service_for
    try:
        policy = json.loads(channel) if channel is not None else None
        if channel is not None and not isinstance(policy, dict):
            raise ValueError('通道统计参数须为对象')
        return await asyncio.to_thread(service_for(state.history).get_quality,
            canonical_perpetual(symbol), pair, seconds, target_mode, min_deviation_bps, policy)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v2/reversion-stats-batch")
async def reversion_stats_batch(
    items: str = Query(default="", description="逗号分隔的 symbol|buy_venue->sell_venue"),
    seconds: int = Query(default=604800, ge=300, le=MAX_HISTORY_SECONDS),
    band_bps: float = Query(default=2.0, ge=0.1, le=100.0),
    min_deviation_bps: float = Query(default=0.0, ge=0.0, le=100000.0),
    target_mode: str = Query(default='center'),
) -> dict:
    """Return on-demand reversion summaries for a bounded set of routes.

    The scanner already stores one 30-second observation per route. This
    endpoint only reads those samples when the user enables reversion filters;
    it does not start another market-data poller or add a new table.
    """
    requested: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in items.split(',') if items else []:
        symbol, separator, pair = raw.partition('|')
        if not separator or not symbol or '->' not in pair or pair.startswith('->') or pair.endswith('->'):
            continue
        key = (canonical_perpetual(symbol), pair)
        if key not in seen:
            seen.add(key)
            requested.append(key)
        if len(requested) >= 80:
            break
    if not requested:
        return {"items": []}
    if target_mode not in ('center', 'zero', 'either'):
        raise HTTPException(status_code=400, detail="回归目标必须是 center、zero 或 either")
    from .center_statistics import reversion_stats_for
    # Snapshot reads only: one bounded worker computes missing routes and
    # shares results across the API and live process.
    stats = await asyncio.to_thread(lambda: [
        reversion_stats_for(state.history, symbol, pair, seconds, band_bps, target_mode, min_deviation_bps)
        for symbol, pair in requested])
    return {"items": stats}


@app.websocket("/api/v2/ws/quotes")
async def quote_stream(websocket: WebSocket, symbol: str = Query(min_length=1), pair: Optional[str] = None):
    await websocket.accept()
    names = pair.split(",", 1) if pair else list(state.adapters.keys())
    adapters = [state.adapters[name] for name in names if name in state.adapters]
    if not adapters:
        await websocket.send_json({"error": "没有可用平台"})
        await websocket.close(code=1008)
        return
    try:
        while True:
            rows = await asyncio.gather(*(bounded(adapter.get_quote(symbol), adapter_timeout(adapter)) for adapter in adapters))
            payload = []
            for adapter, row in zip(adapters, rows):
                payload.append((row or QuoteSnapshot(
                    venue=adapter.venue, symbol=symbol, venue_symbol=symbol,
                    instrument_type="unknown", source=adapter.venue, stale=True,
                    reason="平台请求超时")).model_dump(mode="json"))
            await websocket.send_json({"symbol": symbol, "quotes": payload})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return


@app.websocket("/api/v2/ws/opportunities")
async def opportunity_stream(websocket: WebSocket, symbols: str = Query(min_length=1),
                             venues: str = Query(min_length=1),
                             notional: float = Query(default=1000.0, ge=1.0, le=1000000.0),
                             gross_min_bps: float = Query(default=0, ge=0),
                             gross_max_bps: float = Query(default=0, ge=0),
                             target_mode: Annotated[str, Query(pattern='^(center|zero|either)$')] = 'center',
                             target_window_seconds: Annotated[int, Query(ge=86400, le=MAX_HISTORY_SECONDS)] = 604800):
    """Push independent refreshed markets from the requested catalog.

    Catalog discovery remains an HTTP scan, while this stream keeps the visible
    rows fresh from the adapter caches.  It is deliberately capped so a full
    catalog cannot create an unbounded number of exchange requests.
    """
    # Full-catalog mode can contain more than a thousand instruments. Keep a
    # bounded set of pending markets so one slow market cannot hold back
    # every other quote. The browser merges each completed subset into its table.
    symbol_list = requested_symbols(None, symbols)
    names = list(dict.fromkeys(item.strip() for item in venues.split(",") if item.strip()))
    pairs = list(combinations(names, 2))
    if not symbol_list or not pairs or any(name not in state.adapters for name in names):
        await websocket.accept()
        await websocket.send_json({"error": "实时机会流参数无效"})
        await websocket.close(code=1008)
        return
    if gross_max_bps and gross_max_bps < gross_min_bps:
        await websocket.accept()
        await websocket.send_json({"error": "毛价差上限不能小于下限"})
        await websocket.close(code=1008)
        return
    await websocket.accept()
    # Use the adapter's declared transport capability instead of hard-coding
    # a venue list.  Lighter, RH and Arcus all maintain websocket-capable
    # quote caches, so selecting them together must not silently downgrade the
    # visible stream to a five-second REST-style poll.  A REST-only venue still
    # gets the conservative interval to avoid turning a browser tab into an
    # exchange polling loop.
    websocket_capable = all(
        "websocket" in getattr(state.adapters[name].capability, "market_data", [])
        for name in names
    )
    interval = 1.0 if websocket_capable else 5.0
    window_size = 100
    window_offset = 0
    pending = {}
    try:
        while True:
            # Tell the browser that the socket is alive before starting a
            # potentially slow full-catalog refresh.  Without this heartbeat
            # a healthy connection looked identical to a dead one while a
            # batch was waiting on a cold REST fallback.
            await websocket.send_json({"rows": [], "heartbeat": True,
                                       "interval_seconds": interval,
                                       "observed_at": datetime.now(timezone.utc).isoformat()})
            started = asyncio.get_running_loop().time()
            # Refresh completed markets independently. A cold REST fallback
            # must not hold fresh books behind a full-window gather barrier.
            # Keep at most one outstanding calculation per market and a
            # bounded total, rotating fairly through larger catalogs.
            for _ in range(len(symbol_list)):
                if len(pending) >= window_size:
                    break
                symbol = symbol_list[window_offset]
                window_offset = (window_offset + 1) % len(symbol_list)
                if symbol not in pending:
                    pending[symbol] = asyncio.create_task(
                        calculate_opportunities(symbol, pairs, notional, record=False))
            done, _ = await asyncio.wait(pending.values(), timeout=interval)
            window_symbols = [symbol for symbol, task in pending.items() if task in done]
            batches = []
            for symbol in window_symbols:
                task = pending.pop(symbol)
                try:
                    batches.append(task.result())
                except Exception:
                    # Remove only this market's old rows; other markets keep
                    # streaming and the failed market is retried next turn.
                    pass
            if not window_symbols:
                continue
            filtered = apply_gross_spread_filter([row for batch in batches for row in batch],
                                                 gross_min_bps, gross_max_bps)
            estimated = await asyncio.to_thread(attach_return_previews, filtered, state.history,
                display_policy(Path(__file__).resolve().parents[2]), target_mode, target_window_seconds)
            rows = [row.model_dump(mode="json") for row in sorted(estimated, key=opportunity_sort_key, reverse=True)]
            await websocket.send_json({"rows": rows[:200], "window_symbols": window_symbols,
                                       "window_offset": window_offset, "window_total": len(symbol_list),
                                       "interval_seconds": interval,
                                       "observed_at": datetime.now(timezone.utc).isoformat()})
            remaining = interval - (asyncio.get_running_loop().time() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
    except WebSocketDisconnect:
        return
    except Exception:
        # A single connector exception must close this client connection
        # cleanly; the browser has an automatic reconnect path and the next
        # scan can continue using the REST fallback.
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        for task in pending.values():
            task.cancel()
        if pending:
            await asyncio.gather(*pending.values(), return_exceptions=True)
