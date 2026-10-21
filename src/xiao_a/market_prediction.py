"""Market-only forecast representation; no holdings or account inputs."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from math import isfinite
from typing import Any

from xiao_a.prediction_audit import TARGET_DEFINITION


def independent_market_prediction(decision: Any) -> dict[str, Any]:
    values = asdict(decision) if is_dataclass(decision) else decision
    values = values if isinstance(values, dict) else {}
    try:
        score = float(values.get("score"))
        if not isfinite(score):
            score = None
    except (TypeError, ValueError):
        score = None
    action = values.get("raw_action")
    action = getattr(action, "value", action)
    direction = "unknown" if score is None else "up" if score > 1e-12 else "down" if score < -1e-12 else "neutral"
    if direction == "up":
        label = "看涨" if action == "buy" else "偏多观察（信号较弱）"
    elif direction == "down":
        label = "看跌" if action == "sell" else "偏弱" if action == "reduce" else "偏空观察（信号较弱）"
    else:
        label = "中性观察" if direction == "neutral" else "独立预测未取得"
    return {
        "scope": "market_only", "direction": direction, "direction_label": label,
        "direction_score": score, "rule_conformance_score": values.get("model_confidence"),
        "score_type": "rule_conformance_score", "model_action": action,
        "target": dict(TARGET_DEFINITION),
        "raw_probability": None, "calibrated_probability": None,
        "expected_return": None, "return_interval": None,
        "return_estimate_status": "尚未验证：现有规则模型没有经过独立验证的未来收益幅度估计。",
        "account_independent": True,
        "description": "仅以市场行情和因子评价方向；持仓、成本、数量、现金、仓位和账户回撤不参与该预测。上涨概率与收益幅度均未验证，方向不等于交易授权。",
    }
