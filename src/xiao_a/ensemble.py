from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from xiao_a.models import SignalAction, StrategySignal


@dataclass(frozen=True)
class StrategyPerformance:
    samples: int
    hit_rate: float = 0.5
    mean_excess_return: float = 0.0


@dataclass(frozen=True)
class EnsembleResult:
    action: SignalAction
    score: float
    confidence: float
    weights: dict[str, float]
    reasons: tuple[str, ...]


class AdaptiveEnsemble:
    """Combine replaceable strategies and shrink weak evidence toward neutral."""

    def __init__(self, base_weights: dict[str, float] | None = None):
        self.base_weights = base_weights or {}

    @staticmethod
    def _performance_factor(performance: StrategyPerformance | None) -> float:
        if performance is None:
            return 1.0
        samples = max(0, performance.samples)
        hit = max(0.0, min(1.0, performance.hit_rate))
        posterior_hit = (hit * samples + 0.5 * 30) / (samples + 30)
        factor = 1.0 + (posterior_hit - 0.5) * 2.0
        factor += max(-0.20, min(0.20, performance.mean_excess_return * 4.0))
        return max(0.50, min(1.50, factor))

    def combine(
        self,
        signals: list[StrategySignal],
        performance: dict[str, StrategyPerformance] | None = None,
    ) -> EnsembleResult:
        if not signals:
            return EnsembleResult(SignalAction.HOLD, 0.0, 0.0, {}, ("no strategy signal",))
        performance = performance or {}
        raw_weights: dict[str, float] = {}
        for signal in signals:
            base = max(0.0, self.base_weights.get(signal.strategy, 1.0))
            raw_weights[signal.strategy] = base * max(0.0, signal.confidence) * self._performance_factor(
                performance.get(signal.strategy)
            )
        total = sum(raw_weights.values())
        if total <= 0:
            return EnsembleResult(SignalAction.HOLD, 0.0, 0.0, raw_weights, ("all strategy weights are zero",))

        weights = {key: value / total for key, value in raw_weights.items()}
        score = sum(signal.score * weights[signal.strategy] for signal in signals)
        variance = sum(weights[signal.strategy] * (signal.score - score) ** 2 for signal in signals)
        disagreement = min(1.0, sqrt(max(0.0, variance)))
        evidence_confidence = sum(signal.confidence * weights[signal.strategy] for signal in signals)
        confidence = max(0.0, min(1.0, evidence_confidence * (1.0 - 0.65 * disagreement)))

        if score >= 0.25:
            action = SignalAction.BUY
        elif score <= -0.55:
            action = SignalAction.SELL
        elif score <= -0.20:
            action = SignalAction.REDUCE
        else:
            action = SignalAction.HOLD
        reasons = (
            f"weighted_score={score:+.3f}",
            f"strategy_disagreement={disagreement:.3f}",
            "weights are performance-adjusted and sample-size-shrunk",
        )
        return EnsembleResult(action, score, confidence, weights, reasons)

