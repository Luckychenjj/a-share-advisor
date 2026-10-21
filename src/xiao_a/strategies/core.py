from __future__ import annotations

from xiao_a.models import MarketSnapshot, SignalAction, StrategySignal


def _bounded(value: object, default: float = 0.0) -> float:
    try:
        return max(-1.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _action(score: float) -> SignalAction:
    if score >= 0.25:
        return SignalAction.BUY
    if score <= -0.55:
        return SignalAction.SELL
    if score <= -0.20:
        return SignalAction.REDUCE
    return SignalAction.HOLD


class CycleTrendStrategy:
    """Model 1 as a transparent feature combination, not a sacred formula."""

    name = "cycle_trend"

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal:
        f = snapshot.features
        components = {
            "monthly": _bounded(f.get("monthly_trend")),
            "weekly": _bounded(f.get("weekly_trend")),
            "daily": _bounded(f.get("daily_trend")),
            "relative_strength": _bounded(f.get("relative_strength")),
            "cycle": _bounded(f.get("cycle_position")),
        }
        weights = {"monthly": 0.22, "weekly": 0.26, "daily": 0.18, "relative_strength": 0.22, "cycle": 0.12}
        score = sum(components[key] * weights[key] for key in components)
        available = sum(key in f and f.get(key) is not None for key in (
            "monthly_trend", "weekly_trend", "daily_trend", "relative_strength", "cycle_position"
        ))
        # Missing features mean abstention, not a quietly confident HOLD vote.
        confidence = 0.0 if available == 0 else min(0.95, 0.25 + 0.14 * available)
        labels = {"monthly": "月线", "weekly": "周线", "daily": "日线", "relative_strength": "相对强度", "cycle": "周期位置"}
        evidence = tuple(f"{labels[key]}={value:+.2f}" for key, value in components.items())
        invalidation_details = []
        if components["weekly"] < 0:
            invalidation_details.append({
                "parameter": "周线趋势",
                "value": f"{components['weekly']:+.2f}",
                "normal_range": "≥0.00",
                "reason": "已转负，周线趋势反转",
            })
        if components["relative_strength"] < 0:
            invalidation_details.append({
                "parameter": "相对强度",
                "value": f"{components['relative_strength']:+.2f}",
                "normal_range": "≥0.00",
                "reason": "弱于市场或板块基准",
            })
        invalidation_active = bool(invalidation_details)
        return StrategySignal(
            strategy=self.name,
            score=score,
            confidence=confidence,
            action=_action(score),
            thesis="多周期趋势、相对强度与周期位置的综合判断",
            evidence=evidence,
            invalidation=("周线趋势反转", "相对强度转负"),
            metadata={
                "components": components,
                "invalidation_active": invalidation_active,
                "invalidation_details": invalidation_details,
            },
        )


class LeaderSentimentStrategy:
    """Model 2 using measurable leader and market-emotion inputs."""

    name = "leader_sentiment"

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal:
        f = snapshot.features
        components = {
            "leader": _bounded(f.get("leader_strength")),
            "sector_heat": _bounded(f.get("sector_heat")),
            "breadth": _bounded(f.get("limit_up_breadth")),
            "emotion": _bounded(f.get("emotion_cycle")),
            "broken_board": -_bounded(f.get("broken_board_risk")),
            "liquidity": _bounded(f.get("liquidity_quality")),
            "fund_flow": _bounded(f.get("fund_flow_signal")),
            "limit_up": _bounded(f.get("limit_up_signal")),
        }
        weights = {
            "leader": 0.24,
            "sector_heat": 0.17,
            "breadth": 0.14,
            "emotion": 0.14,
            "broken_board": 0.10,
            "liquidity": 0.07,
            "fund_flow": 0.09,
            "limit_up": 0.05,
        }
        score = sum(components[key] * weights[key] for key in components)
        available = sum(
            key in f and f.get(key) is not None
            for key in (
                "leader_strength", "sector_heat", "limit_up_breadth",
                "emotion_cycle", "broken_board_risk", "liquidity_quality",
                "fund_flow_signal", "limit_up_signal",
            )
        )
        confidence = 0.0 if available == 0 else min(0.95, 0.20 + 0.12 * available)
        labels = {
            "leader": "龙头强度", "sector_heat": "板块热度", "breadth": "市场广度",
            "emotion": "市场情绪", "broken_board": "炸板风险反向项", "liquidity": "流动性",
            "fund_flow": "主力资金", "limit_up": "涨停池状态",
        }
        invalidation_details = []
        broken_board_risk = _bounded(f.get("broken_board_risk"))
        if components["leader"] < 0:
            invalidation_details.append({
                "parameter": "龙头强度",
                "value": f"{components['leader']:+.2f}",
                "normal_range": "≥0.00",
                "reason": "个股强度已弱于板块基准",
            })
        if broken_board_risk >= 0.45:
            invalidation_details.append({
                "parameter": "炸板风险",
                "value": f"{broken_board_risk:+.2f}",
                "normal_range": "<+0.45",
                "reason": "炸板风险偏高，连板承接变弱",
            })
        if components["breadth"] <= -0.35:
            invalidation_details.append({
                "parameter": "市场广度",
                "value": f"{components['breadth']:+.2f}",
                "normal_range": ">-0.35",
                "reason": "涨停扩散度明显收缩",
            })
        invalidation_active = bool(invalidation_details)
        return StrategySignal(
            strategy=self.name,
            score=score,
            confidence=confidence,
            action=_action(score),
            thesis="用板块广度、市场情绪、炸板质量与流动性验证龙头地位",
            evidence=tuple(f"{labels[key]}={value:+.2f}" for key, value in components.items()),
            invalidation=("龙头相对强度丧失", "炸板风险扩大", "板块广度坍塌"),
            metadata={
                "components": components,
                "invalidation_active": invalidation_active,
                "invalidation_details": invalidation_details,
            },
        )


class MultiFactorStrategy:
    """Transparent cross-sectional-style score for a single stock.

    This is intentionally a rules-based factor model first. It can be
    replaced by a fitted model later without changing the decision contract.
    Missing factors are omitted and confidence is reduced accordingly.
    """

    name = "multi_factor"

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal:
        f = snapshot.features
        components = {
            "trend": _bounded(f.get("daily_trend")),
            "momentum_20": _bounded(f.get("momentum_20")),
            "momentum_60": _bounded(f.get("momentum_60")),
            "volume_price": _bounded(f.get("volume_price_signal")),
            "volatility": _bounded(f.get("volatility_score")),
            "liquidity": _bounded(f.get("liquidity_quality")),
            "valuation": _bounded(f.get("valuation_signal")),
            "fund_flow": _bounded(f.get("fund_flow_signal")),
        }
        weights = {
            "trend": 0.18, "momentum_20": 0.14, "momentum_60": 0.14,
            "volume_price": 0.14, "volatility": 0.10, "liquidity": 0.08,
            "valuation": 0.12, "fund_flow": 0.10,
        }
        available = [key for key in components if f.get({
            "trend": "daily_trend", "momentum_20": "momentum_20", "momentum_60": "momentum_60",
            "volume_price": "volume_price_signal", "volatility": "volatility_score",
            "liquidity": "liquidity_quality", "valuation": "valuation_signal",
            "fund_flow": "fund_flow_signal",
        }[key]) is not None]
        total_weight = sum(weights[key] for key in available)
        score = sum(components[key] * weights[key] for key in available) / total_weight if total_weight else 0.0
        confidence = 0.0 if not available else min(0.90, 0.22 + 0.09 * len(available))
        labels = {
            "trend": "趋势", "momentum_20": "20日动量", "momentum_60": "60日动量",
            "volume_price": "量价", "volatility": "波动风险反向项", "liquidity": "流动性",
            "valuation": "估值", "fund_flow": "资金流",
        }
        evidence = tuple(
            f"{labels[key]}={components[key]:+.2f}" if key in available else f"{labels[key]}=缺失"
            for key in components
        )
        positive = sorted(((components[key] * weights[key], labels[key]) for key in available), reverse=True)[:2]
        negative = sorted(((components[key] * weights[key], labels[key]) for key in available))[:2]
        thesis = "多因子评分：" + ("、".join(label for _, label in positive) or "暂无有效因子")
        if negative and negative[0][0] < -0.05:
            thesis += f"偏弱项为{negative[0][1]}"
        trend_and_momentum_weak = all(
            key in available and components[key] < 0
            for key in ("trend", "momentum_20", "momentum_60")
        )
        volume_and_fund_flow_weak = (
            "volume_price" in available
            and "fund_flow" in available
            and components["volume_price"] < 0
            and components["fund_flow"] < 0
        )
        invalidation_details = []
        if trend_and_momentum_weak:
            for key in ("trend", "momentum_20", "momentum_60"):
                invalidation_details.append({
                    "parameter": labels[key],
                    "value": f"{components[key]:+.2f}",
                    "normal_range": "≥0.00",
                    "reason": "同步转负，趋势与动量共振走弱",
                })
        if volume_and_fund_flow_weak:
            invalidation_details.extend([
                {
                    "parameter": "量价",
                    "value": f"{components['volume_price']:+.2f}",
                    "normal_range": "≥0.00",
                    "reason": "量价关系转弱",
                },
                {
                    "parameter": "资金流",
                    "value": f"{components['fund_flow']:+.2f}",
                    "normal_range": "≥0.00",
                    "reason": "资金流同步转弱",
                },
            ])
        return StrategySignal(
            strategy=self.name,
            score=max(-1.0, min(1.0, score)),
            confidence=confidence,
            action=_action(score),
            thesis=thesis,
            evidence=evidence,
            invalidation=("趋势和动量同步转弱", "放量下跌并伴随资金流恶化"),
            metadata={
                "components": components,
                "available_factors": available,
                "factor_weights": weights,
                "invalidation_active": trend_and_momentum_weak or volume_and_fund_flow_weak,
                "invalidation_details": invalidation_details,
            },
        )


class MarketRegimeStrategy:
    """Classify market state and adjust directional conviction conservatively."""

    name = "market_regime"

    def evaluate(self, snapshot: MarketSnapshot) -> StrategySignal:
        f = snapshot.features
        trend = _bounded(f.get("weekly_trend"))
        breadth = _bounded(f.get("limit_up_breadth"))
        emotion = _bounded(f.get("emotion_cycle"))
        sector = _bounded(f.get("sector_heat"))
        broken_board = _bounded(f.get("broken_board_risk"))
        available = [key for key in ("weekly_trend", "limit_up_breadth", "emotion_cycle", "sector_heat", "broken_board_risk") if f.get(key) is not None]
        regime_score = 0.34 * trend + 0.22 * breadth + 0.18 * emotion + 0.16 * sector - 0.10 * broken_board
        regime_score = max(-1.0, min(1.0, regime_score))
        if regime_score >= 0.45:
            regime = "强趋势"
        elif regime_score >= 0.12:
            regime = "震荡偏强"
        elif regime_score <= -0.45:
            regime = "风险收缩"
        elif regime_score <= -0.12:
            regime = "震荡偏弱"
        else:
            regime = "中性震荡"
        confidence = 0.0 if not available else min(0.88, 0.28 + 0.12 * len(available))
        invalidation_details = []
        if breadth <= -0.35:
            invalidation_details.append({
                "parameter": "市场广度",
                "value": f"{breadth:+.2f}",
                "normal_range": ">-0.35",
                "reason": "市场扩散度明显恶化",
            })
        if emotion < 0 and broken_board >= 0.35:
            invalidation_details.extend([
                {
                    "parameter": "市场情绪",
                    "value": f"{emotion:+.2f}",
                    "normal_range": "≥0.00",
                    "reason": "情绪已转弱",
                },
                {
                    "parameter": "炸板风险",
                    "value": f"{broken_board:+.2f}",
                    "normal_range": "<+0.35",
                    "reason": "炸板风险抬升，承接变差",
                },
            ])
        invalidation_active = bool(invalidation_details)
        return StrategySignal(
            strategy=self.name,
            score=regime_score,
            confidence=confidence,
            action=_action(regime_score),
            thesis=f"市场状态：{regime}，由趋势、广度、情绪和炸板风险共同判断",
            evidence=(f"状态={regime}", f"状态分数={regime_score:+.2f}", f"有效输入={len(available)}/5"),
            invalidation=("市场广度快速恶化", "情绪转弱且炸板风险上升"),
            metadata={
                "regime": regime,
                "available_inputs": available,
                "invalidation_active": invalidation_active,
                "invalidation_details": invalidation_details,
            },
        )
