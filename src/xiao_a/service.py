from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from xiao_a.decision import AdvisorEngine
from xiao_a.ensemble import StrategyPerformance
from xiao_a.models import DailyBar, DataStatus, MarketSnapshot
from xiao_a.risk import RiskContext
from xiao_a.strategies.chan import ChanStructureStrategy
from xiao_a.strategies.core import CycleTrendStrategy, LeaderSentimentStrategy, MarketRegimeStrategy, MultiFactorStrategy


def jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_payload(raw: dict[str, Any]) -> tuple[MarketSnapshot, RiskContext, dict[str, StrategyPerformance]]:
    bars = [
        DailyBar(
            trading_date=date.fromisoformat(item.pop("trading_date")),
            **item,
        )
        for item in (dict(bar) for bar in raw.get("bars", []))
    ]
    status = DataStatus(raw.get("data_status", DataStatus.FRESH.value))
    snapshot = MarketSnapshot(
        symbol=raw["symbol"],
        as_of=_parse_datetime(raw.get("as_of")),
        bars=bars,
        features=raw.get("features", {}),
        data_status=status,
        data_warnings=raw.get("data_warnings", []),
    )

    risk_raw = raw.get("risk", {})
    allowed = {item.name for item in fields(RiskContext)} - {"data_status"}
    risk = RiskContext(**{key: value for key, value in risk_raw.items() if key in allowed})
    performance = {
        name: StrategyPerformance(**values)
        for name, values in raw.get("performance", {}).items()
    }
    return snapshot, risk, performance


def evaluate_payload(raw: dict[str, Any]):
    snapshot, risk, performance = parse_payload(raw)
    engine = AdvisorEngine([
        CycleTrendStrategy(),
        LeaderSentimentStrategy(),
        ChanStructureStrategy(),
        MultiFactorStrategy(),
        MarketRegimeStrategy(),
    ])
    return engine.decide(snapshot, risk, performance)
