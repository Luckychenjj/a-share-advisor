from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class DataStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class SignalAction(str, Enum):
    BUY = "buy"
    HOLD = "hold"
    REDUCE = "reduce"
    SELL = "sell"


@dataclass(frozen=True)
class Provenance:
    source: str
    source_url: str
    fetched_at: datetime
    as_of: datetime | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    previous_close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None  # normalized to lots (手) by quote providers
    amount: float | None = None
    turnover_pct: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    status: DataStatus = DataStatus.FRESH
    provenance: Provenance | None = None

    # Optional ratio supplied in the same quote, not recomputed at fetch time.
    volume_ratio: float | None = None
    # Tencent quote fields 44/45 report circulating/total market value in 亿元.
    market_cap: float | None = None  # normalized to CNY
    float_market_cap: float | None = None  # normalized to CNY


@dataclass(frozen=True)
class DailyBar:
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0  # normalized to lots (手) by kline providers
    previous_close: float | None = None
    suspended: bool = False
    is_st: bool = False
    # Explicitly supplied by the market-data layer when known. This supports
    # board- and period-specific rules instead of guessing every non-ST stock
    # has a 10% limit.
    limit_rate: float | None = None


@dataclass
class MarketSnapshot:
    symbol: str
    as_of: datetime
    quote: Quote | None = None
    bars: list[DailyBar] = field(default_factory=list)
    features: dict[str, float | int | str | bool | None] = field(default_factory=dict)
    data_status: DataStatus = DataStatus.FRESH
    data_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StrategySignal:
    strategy: str
    score: float
    confidence: float
    action: SignalAction
    thesis: str
    evidence: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdvisorDecision:
    symbol: str
    as_of: datetime
    raw_action: SignalAction
    final_action: SignalAction
    score: float
    confidence: float
    executable: bool
    reasons: list[str]
    warnings: list[str]
    vetoes: list[str]
    strategy_signals: list[StrategySignal]
    strategy_weights: dict[str, float]
    # This field is a deterministic rule-fit / signal-agreement score, never a
    # probability unless a separate, time-tested calibrator is explicitly used.
    confidence_type: str = "rule_conformance_score"
    model_confidence: float | None = None  # Raw rule agreement before account/data risk caps.
