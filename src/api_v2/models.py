from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class CatalogMode(str, Enum):
    full = "full"
    configured_only = "configured_only"
    unavailable = "unavailable"


class VenueCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_data: List[str] = Field(default_factory=list)
    catalog_mode: CatalogMode
    executable_quotes: bool = False
    order_submission: bool = False
    order_status_stream: bool = False
    fee_schedule: bool = False
    funding: bool = False


class VenueStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str
    display_name: str
    status: str
    transport: List[str] = Field(default_factory=list)
    capabilities: VenueCapability
    configured_symbols: List[str] = Field(default_factory=list)
    last_error: Optional[str] = None
    observed_at: datetime = Field(default_factory=now_utc)


class Instrument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str
    venue_symbol: str
    canonical_symbol: str
    instrument_type: str = "perpetual"
    base_asset: Optional[str] = None
    quote_asset: Optional[str] = None
    enabled: bool = True
    size_decimals: Optional[int] = None
    price_decimals: Optional[int] = None
    min_base_amount: Optional[float] = None
    min_quote_amount: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QuoteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str
    symbol: str
    venue_symbol: str
    instrument_type: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    mark_price: Optional[float] = None
    quote_asset: Optional[str] = None
    source: str
    executable: bool = False
    stale: bool = True
    age_seconds: Optional[float] = None
    fetched_at: datetime = Field(default_factory=now_utc)
    reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_symbol: str
    buy_venue: str
    sell_venue: str
    buy_price: float
    sell_price: float
    gross_bps: float
    net_bps: Optional[float] = None
    return_inputs: dict = Field(default_factory=dict)
    return_preview: dict = Field(default_factory=dict)
    max_base_amount: Optional[float] = None
    max_notional_usdc: Optional[float] = None
    depth_reason: Optional[str] = None
    wear_bps: Optional[float] = None
    wear_summary: Optional[str] = None
    wear_breakdown: Dict[str, Optional[float]] = Field(default_factory=dict)
    buy_age_seconds: Optional[float] = None
    sell_age_seconds: Optional[float] = None
    buy_stale: bool = True
    sell_stale: bool = True
    buy_source: Optional[str] = None
    sell_source: Optional[str] = None
    executable: bool = False
    reason: Optional[str] = None
    # Observed gross spread range for this directed route.  These are
    # diagnostics from the last 24 hours of collected snapshots, not a
    # promise that the extremes were executable or will recur.
    history_min_bps: Optional[float] = None
    history_max_bps: Optional[float] = None
    history_avg_bps: Optional[float] = None
    history_samples: int = 0
    history_raw_min_bps: Optional[float] = None
    history_raw_max_bps: Optional[float] = None
    history_raw_samples: int = 0
    history_anomaly_count: int = 0
    history_anomaly_threshold_bps: Optional[float] = None
    history_7d_min_bps: Optional[float] = None
    history_7d_max_bps: Optional[float] = None
    history_7d_avg_bps: Optional[float] = None
    history_7d_samples: int = 0
    history_7d_raw_min_bps: Optional[float] = None
    history_7d_raw_max_bps: Optional[float] = None
    history_7d_raw_samples: int = 0
    history_7d_anomaly_count: int = 0
    history_7d_anomaly_threshold_bps: Optional[float] = None
    # Route-specific mean-reversion diagnostics.  The target is the median
    # gross spread observed for this symbol and directed venue pair; it is
    # deliberately kept separate from the current signed net edge.
    history_center_bps: Optional[float] = None
    history_p10_bps: Optional[float] = None
    history_p90_bps: Optional[float] = None
    history_reversion_count: Optional[int] = None
    history_last_reversion_age_seconds: Optional[float] = None
    history_median_interval_seconds: Optional[float] = None
    reversion_target_bps: Optional[float] = None
    favorable_deviation_bps: Optional[float] = None
    estimated_reversion_profit_usdc: Optional[float] = None
    reversion_ready: bool = False
    observed_at: datetime = Field(default_factory=now_utc)
