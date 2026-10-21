from __future__ import annotations

from dataclasses import replace

from xiao_a.ensemble import AdaptiveEnsemble, StrategyPerformance
from xiao_a.models import AdvisorDecision, MarketSnapshot
from xiao_a.risk import RiskContext, RiskEngine
from xiao_a.strategies.base import Strategy


class AdvisorEngine:
    def __init__(
        self,
        strategies: list[Strategy],
        *,
        ensemble: AdaptiveEnsemble | None = None,
        risk_engine: RiskEngine | None = None,
    ):
        self.strategies = strategies
        self.ensemble = ensemble or AdaptiveEnsemble()
        self.risk_engine = risk_engine or RiskEngine()

    def decide(
        self,
        snapshot: MarketSnapshot,
        risk_context: RiskContext,
        performance: dict[str, StrategyPerformance] | None = None,
    ) -> AdvisorDecision:
        signals = [strategy.evaluate(snapshot) for strategy in self.strategies]
        combined = self.ensemble.combine(signals, performance)
        # Keep caller-owned state immutable from the engine's point of view.
        effective_risk = replace(risk_context, data_status=snapshot.data_status)
        risk = self.risk_engine.apply(combined.action, combined.confidence, effective_risk)
        reasons = [*combined.reasons]
        reasons.extend(f"{signal.strategy}: {signal.thesis}" for signal in signals)
        return AdvisorDecision(
            symbol=snapshot.symbol,
            as_of=snapshot.as_of,
            raw_action=combined.action,
            final_action=risk.action,
            score=combined.score,
            confidence=risk.confidence,
            model_confidence=combined.confidence,
            executable=risk.executable,
            reasons=reasons,
            warnings=[*snapshot.data_warnings, *risk.warnings],
            vetoes=list(risk.vetoes),
            strategy_signals=signals,
            strategy_weights=combined.weights,
        )
