from __future__ import annotations

from dataclasses import dataclass

from xiao_a.models import DailyBar, MarketSnapshot, SignalAction, StrategySignal


@dataclass(frozen=True)
class Fractal:
    index: int
    kind: str
    price: float


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _macd_hist(closes: list[float]) -> list[float]:
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    diff = [a - b for a, b in zip(fast, slow)]
    signal = _ema(diff, 9)
    return [2.0 * (a - b) for a, b in zip(diff, signal)]


def detect_fractals(bars: list[DailyBar]) -> list[Fractal]:
    points: list[Fractal] = []
    for i in range(1, len(bars) - 1):
        left, bar, right = bars[i - 1], bars[i], bars[i + 1]
        if bar.high > left.high and bar.high > right.high and bar.low > left.low and bar.low > right.low:
            points.append(Fractal(i, "top", bar.high))
        elif bar.low < left.low and bar.low < right.low and bar.high < left.high and bar.high < right.high:
            points.append(Fractal(i, "bottom", bar.low))
    return points


def build_pens(fractals: list[Fractal], min_gap: int = 3) -> list[Fractal]:
    """Build an alternating pivot sequence with conservative spacing."""

    pens: list[Fractal] = []
    for point in fractals:
        if not pens:
            pens.append(point)
            continue
        last = pens[-1]
        if point.kind == last.kind:
            more_extreme = point.price > last.price if point.kind == "top" else point.price < last.price
            if more_extreme:
                pens[-1] = point
            continue
        if point.index - last.index >= min_gap:
            pens.append(point)
    return pens


def latest_center(pens: list[Fractal]) -> tuple[float, float] | None:
    """Return the latest overlap of three consecutive pen ranges."""

    center = None
    for i in range(len(pens) - 3):
        segments = []
        for a, b in zip(pens[i:i + 3], pens[i + 1:i + 4]):
            segments.append((min(a.price, b.price), max(a.price, b.price)))
        low = max(item[0] for item in segments)
        high = min(item[1] for item in segments)
        if low < high:
            center = (low, high)
    return center


class ChanStructureStrategy:
    """Deterministic Chan-inspired structure signal.

    This is deliberately labelled an approximation. Its value is repeatability:
    rules can be tested and replaced instead of being inferred ad hoc by an LLM.
    """

    name = "chan_structure"

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal:
        bars = snapshot.bars
        if len(bars) < 30:
            return StrategySignal(
                strategy=self.name,
                score=0.0,
                confidence=0.0,
                action=SignalAction.HOLD,
                thesis="日线数量不足，缠论结构策略弃权",
                evidence=(f"有效日线={len(bars)}",),
                invalidation=("至少提供30根有效日线",),
                metadata={
                    "abstained": True,
                    "approximation": True,
                    "invalidation_active": None,
                    "invalidation_details": [],
                },
            )

        fractals = detect_fractals(bars)
        pens = build_pens(fractals)
        center = latest_center(pens)
        closes = [bar.close for bar in bars]
        histogram = _macd_hist(closes)
        score = 0.0
        evidence: list[str] = [f"分型={len(fractals)}", f"笔={len(pens)}"]

        if len(pens) >= 2:
            rising = pens[-1].price > pens[-2].price
            score += 0.25 if rising else -0.25
            evidence.append("最新一笔=向上" if rising else "最新一笔=向下")

        same_kind = [point for point in fractals if point.kind == (fractals[-1].kind if fractals else "")]
        if len(same_kind) >= 2:
            previous, latest = same_kind[-2], same_kind[-1]
            previous_momentum = abs(histogram[previous.index])
            latest_momentum = abs(histogram[latest.index])
            if latest.kind == "bottom" and latest.price < previous.price and latest_momentum < previous_momentum:
                score += 0.45
                evidence.append("底背驰")
            elif latest.kind == "top" and latest.price > previous.price and latest_momentum < previous_momentum:
                score -= 0.45
                evidence.append("顶背驰")

        if center is not None:
            evidence.append(f"中枢={center[0]:.2f}-{center[1]:.2f}")
            if closes[-1] > center[1]:
                score += 0.20
                evidence.append("价格在中枢上方")
            elif closes[-1] < center[0]:
                score -= 0.20
                evidence.append("价格在中枢下方")

        latest_pen_reversed = len(pens) >= 2 and pens[-1].price <= pens[-2].price
        price_reentered_or_below_center = center is not None and closes[-1] <= center[1]
        invalidation_details = []
        if latest_pen_reversed:
            invalidation_details.append({
                "parameter": "最新一笔",
                "value": "向下",
                "normal_range": "向上延伸",
                "reason": "最新一笔已由上行转为下行",
            })
        if price_reentered_or_below_center and center is not None:
            invalidation_details.append({
                "parameter": "当前价格",
                "value": f"{closes[-1]:.2f}",
                "normal_range": f">{center[1]:.2f}（中枢上沿）",
                "reason": "价格重新进入或跌破最新中枢",
            })

        score = max(-1.0, min(1.0, score))
        if score >= 0.25:
            action = SignalAction.BUY
        elif score <= -0.55:
            action = SignalAction.SELL
        elif score <= -0.20:
            action = SignalAction.REDUCE
        else:
            action = SignalAction.HOLD
        confidence = min(0.85, 0.35 + len(pens) * 0.035 + (0.10 if center else 0.0))
        return StrategySignal(
            strategy=self.name,
            score=score,
            confidence=confidence,
            action=action,
            thesis="可重复计算的分型、笔、中枢与动量背驰近似",
            evidence=tuple(evidence),
            invalidation=("最新一笔反转", "价格重新进入或跌破最新中枢"),
            metadata={
                "center": center,
                "approximation": True,
                "invalidation_active": latest_pen_reversed or price_reentered_or_below_center,
                "invalidation_details": invalidation_details,
            },
        )
