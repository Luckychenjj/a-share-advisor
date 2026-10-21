from __future__ import annotations

from typing import Protocol

from xiao_a.models import MarketSnapshot, StrategySignal


class Strategy(Protocol):
    name: str

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal: ...

