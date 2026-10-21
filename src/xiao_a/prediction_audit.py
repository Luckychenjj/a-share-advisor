"""Append-only prediction audit, forward settlement, and conservative reports.

The current advisor emits deterministic rule/factor conformance scores, not
probabilities. This module preserves that distinction and never treats a
negative market view as a short position.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from xiao_a.review_monitor import evaluate_review_signals


TARGET_DEFINITION = {
    "target_id": "forward_positive_return_5_sessions_v1",
    "kind": "market_event",
    "event": "标的在预测后第5个可用交易日收盘相对下一交易日开盘收益为正",
    "horizon_sessions": 5,
    "entry_price": "预测信息截止日之后首个可用交易日开盘价",
    "exit_price": "其后第5个可用交易日收盘价",
    "benchmark": "同期沪深300下一交易日开盘至第5个交易日收盘收益方向",
    "price_basis": "供应商返回的前复权日线；缺少数据或日期不适用时不强制结算",
    "definition_version": 1,
}


def select_future_bar_window(bars: list[Any], *, information_cutoff_day: date, horizon_sessions: int, before_day: date) -> list[Any]:
    """Return only complete exchange bars strictly after cutoff and before today."""
    if horizon_sessions <= 0:
        raise ValueError("horizon_sessions must be positive")
    by_date = {
        bar.trading_date: bar
        for bar in bars
        if information_cutoff_day < bar.trading_date < before_day
    }
    ordered = [by_date[key] for key in sorted(by_date)]
    return ordered[:horizon_sessions] if len(ordered) >= horizon_sessions else []

MODEL_CATALOG = {
    "cycle_trend": {"type": "规则模型", "version": "cycle-trend-v1"},
    "leader_sentiment": {"type": "规则/因子模型", "version": "leader-sentiment-v1"},
    "chan_structure": {"type": "确定性规则模型", "version": "chan-structure-v1"},
    "multi_factor": {"type": "规则因子模型", "version": "multi-factor-v1"},
    "market_regime": {"type": "规则模型", "version": "market-regime-v1"},
    "adaptive_ensemble": {"type": "规则加权混合模型", "version": "adaptive-ensemble-v1"},
}

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class PredictionLedger:
    """Hash-chained JSONL events; corrections and settlements are new events."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for(self.path)

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            result = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("预测台账包含无效事件")
                result.append(event)
            return result

    def integrity(self) -> dict[str, Any]:
        previous = "0" * 64
        count = 0
        for event in self.events():
            supplied = event.get("event_hash")
            payload = {key: value for key, value in event.items() if key != "event_hash"}
            expected = hashlib.sha256(previous.encode("ascii") + _canonical(payload)).hexdigest()
            if event.get("previous_hash") != previous or supplied != expected:
                return {"valid": False, "events": count, "broken_event_id": event.get("event_id")}
            previous = supplied
            count += 1
        return {"valid": True, "events": count, "head": previous}

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self.events()
            check = self.integrity()
            if not check["valid"]:
                raise RuntimeError("预测台账哈希链异常；已停止追加和评分，请先人工复核原始文件")
            previous = existing[-1].get("event_hash", "0" * 64) if existing else "0" * 64
            event = {
                "event_id": str(uuid.uuid4()),
                "event_type": event_type,
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "sequence": len(existing) + 1,
                "previous_hash": previous,
                **payload,
            }
            event["event_hash"] = hashlib.sha256(previous.encode("ascii") + _canonical(event)).hexdigest()
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def records(self) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for event in self.events():
            event_type = event.get("event_type")
            prediction_id = event.get("prediction_id")
            if event_type == "prediction_created":
                records[str(prediction_id)] = {**event, "queries": [], "settlement": None, "corrections": []}
            elif prediction_id in records:
                record = records[str(prediction_id)]
                if event_type == "prediction_repeated":
                    record["queries"].append(event)
                elif event_type == "prediction_settled":
                    record["settlement"] = event
                elif event_type == "prediction_correction":
                    record["corrections"].append(event)
                elif event_type == "execution_recorded":
                    record.setdefault("executions", []).append(event)
                elif event_type == "settlement_attempted":
                    record["last_settlement_attempt"] = event
                    record.setdefault("settlement_attempts", []).append(event)
        return list(records.values())

    def create_prediction(self, record: dict[str, Any], *, mode: str = "formal") -> tuple[dict[str, Any], bool]:
        if mode not in {"formal", "experiment"}:
            raise ValueError("记录类型必须是 formal 或 experiment")
        fingerprint = hashlib.sha256(_canonical({
            "symbol": record.get("symbol"),
            "info_cutoff": record.get("information_cutoff"), "model_versions": record.get("model_versions"),
            "features": (record.get("input_snapshot") or {}).get("features"),
            "quote_price": ((record.get("input_snapshot") or {}).get("quote") or {}).get("price"),
            "score": record.get("rule_score"), "action": (record.get("recommendation") or {}).get("model_action"),
            "configuration": record.get("configuration"), "account_snapshot": record.get("account_snapshot"),
            "target": record.get("target"),
        })).hexdigest()
        if mode == "formal":
            duplicate = next((item for item in reversed(self.records()) if item.get("fingerprint") == fingerprint), None)
            if duplicate:
                self.append("prediction_repeated", {"prediction_id": duplicate["prediction_id"], "query_at": datetime.now(timezone.utc).isoformat()})
                return duplicate, False
        prediction_id = str(uuid.uuid4())
        stored = {
            "prediction_id": prediction_id,
            "mode": mode,
            "fingerprint": fingerprint,
            "parent_prediction_id": record.get("parent_prediction_id"),
            **record,
            "raw_probability": None,
            "calibrated_probability": None,
            "probability_status": "未校准；现有 confidence 仅为规则符合分",
        }
        self.append("prediction_created", stored)
        return stored, True

    def settle(self, prediction_id: str, result: dict[str, Any]) -> dict[str, Any]:
        record = next((item for item in self.records() if item["prediction_id"] == prediction_id), None)
        if record is None:
            raise KeyError("预测记录不存在")
        if record.get("settlement"):
            raise ValueError("预测已结算；如结果需更正，请追加更正事件并说明原因")
        if result.get("status") == "settled":
            try:
                cutoff = date.fromisoformat(str(record["information_cutoff"])[:10])
                entry_day = date.fromisoformat(str(result["entry_date"]))
                exit_day = date.fromisoformat(str(result["exit_date"]))
                resolved_day = date.fromisoformat(str(result["resolved_at"])[:10])
                target_return = float(result["target_return"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("结算必须提供截止日之后的入场日、退出日、结果日期和收益") from exc
            if not cutoff < entry_day <= exit_day or resolved_day != exit_day:
                raise ValueError("未到期标签不得结算；退出和结算日期须严格晚于信息截止日")
            if not math.isfinite(target_return) or bool(result.get("event_occurred")) != (target_return > 0):
                raise ValueError("事件标签必须严格按收益是否大于零计算")
        event = self.append("prediction_settled", {"prediction_id": prediction_id, **result})
        return event

    def correct(self, prediction_id: str, replacement: dict[str, Any], reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("更正必须填写原因")
        if not any(item["prediction_id"] == prediction_id for item in self.records()):
            raise KeyError("预测记录不存在")
        return self.append("prediction_correction", {
            "prediction_id": prediction_id, "replacement": replacement,
            "reason": reason.strip(),
        })

    def record_execution(self, prediction_id: str, execution: dict[str, Any]) -> dict[str, Any]:
        record = next((item for item in self.records() if item["prediction_id"] == prediction_id), None)
        if record is None:
            raise KeyError("预测记录不存在")
        return self.append("execution_recorded", {"prediction_id": prediction_id, "execution": execution})

    def candidates(self) -> list[dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        for event in self.events():
            candidate_id = event.get("candidate_id")
            if event.get("event_type") == "candidate_proposed":
                items[str(candidate_id)] = {**event, "reviews": [], "status": "shadow_only"}
            elif candidate_id in items and event.get("event_type") in {"candidate_reviewed", "candidate_rolled_back"}:
                items[str(candidate_id)]["reviews"].append(event)
                items[str(candidate_id)]["status"] = event.get("status", items[str(candidate_id)]["status"])
        return list(items.values())

    def propose_candidate(self, *, version: str, rationale: str, allowed_change: dict[str, Any], evaluation_plan: dict[str, Any]) -> dict[str, Any]:
        if not version.strip() or not rationale.strip():
            raise ValueError("候选版本和改进原因不能为空")
        if not isinstance(allowed_change, dict) or not allowed_change:
            raise ValueError("必须提供可审计的候选变更")
        forbidden = {"risk", "risk_policy", "hard_risk", "short", "leverage", "margin", "execution_permission", "account_limit"}
        def nested_keys(value: Any) -> set[str]:
            if isinstance(value, dict):
                return {str(key).lower() for key in value} | set().union(*(nested_keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(nested_keys(item) for item in value)) if value else set()
            return set()

        keys = nested_keys(allowed_change)
        if keys & forbidden:
            raise ValueError("候选改进不得修改只做多约束、硬风控、账户限制或实盘权限")
        event = self.append("candidate_proposed", {
            "candidate_id": str(uuid.uuid4()), "version": version.strip(), "rationale": rationale.strip(),
            "allowed_change": allowed_change, "evaluation_plan": evaluation_plan,
            "baseline_version": "advisor-rules-v1", "status": "shadow_only", "active": False,
            "hard_risk_change_allowed": False,
        })
        return event

    def review_candidate(self, candidate_id: str, *, decision: str, reviewer: str, rationale: str,
                         time_test_status: str | None = None, shadow_status: str | None = None) -> dict[str, Any]:
        candidate = next((item for item in self.candidates() if item["candidate_id"] == candidate_id), None)
        if candidate is None:
            raise KeyError("候选版本不存在")
        if candidate.get("status") in {"rejected", "rolled_back"}:
            raise ValueError("该候选版本已结束审核状态")
        if candidate.get("status") == "approved_pending_manual_release" and decision == "approve":
            raise ValueError("该候选已审核通过，不能重复批准")
        if not reviewer.strip() or not rationale.strip():
            raise ValueError("审核人和审核说明不能为空")
        if decision not in {"approve", "reject", "rollback"}:
            raise ValueError("审核决定必须是 approve、reject 或 rollback")
        if decision == "approve" and (time_test_status != "passed" or shadow_status != "passed"):
            raise ValueError("只有时间外验证和前瞻影子观察均通过后才能登记审核通过")
        status = {"approve": "approved_pending_manual_release", "reject": "rejected", "rollback": "rolled_back"}[decision]
        event_type = "candidate_rolled_back" if decision == "rollback" else "candidate_reviewed"
        return self.append(event_type, {
            "candidate_id": candidate_id, "decision": decision, "status": status,
            "reviewer": reviewer.strip(), "rationale": rationale.strip(),
            "time_test_status": time_test_status, "shadow_status": shadow_status,
            "active": False,
            "note": "该记录只保留审核轨迹，不会改写当前生产模型、硬风控或真实下单权限。",
        })

    def review_history(self) -> list[dict[str, Any]]:
        history: dict[str, dict[str, Any]] = {}
        for event in self.events():
            review_id = str(event.get("review_id") or "")
            kind = event.get("event_type")
            if kind == "review_alert_opened":
                history[review_id] = {**event, "acknowledged": False, "resolved": False}
            elif review_id in history and kind == "review_acknowledged":
                history[review_id].update(acknowledged=True, acknowledgement=event)
            elif review_id in history and kind == "review_alert_resolved":
                history[review_id].update(resolved=True, resolution=event)
        return list(history.values())

    def review_monitor(self, *, records: list[dict[str, Any]] | None = None,
                       integrity: dict[str, Any] | None = None) -> dict[str, Any]:
        chain = self.integrity() if integrity is None else integrity
        monitor = evaluate_review_signals(
            self.records() if records is None else records, self.events(),
            chain,
        )
        history = self.review_history() if chain.get("valid") else []
        active = {item["condition_key"]: item for item in history if not item["resolved"]}
        alerts = []
        for signal in monitor.pop("signals"):
            prior = active.get(signal["condition_key"])
            previous_episode = next((x for x in reversed(history) if x["condition_key"] == signal["condition_key"]), None)
            review_id = prior["review_id"] if prior else hashlib.sha256(_canonical({
                "condition": signal["condition_key"], "policy": signal["policy_version"],
                "first_date": signal["first_observed_date"],
                "previous_resolution": (previous_episode or {}).get("resolution", {}).get("event_id"),
            })).hexdigest()[:24]
            alerts.append({**signal, "review_id": review_id,
                           "acknowledged": bool(prior and prior["acknowledged"]),
                           "acknowledgement": prior.get("acknowledgement") if prior else None,
                           "recorded": prior is not None})
        return {**monitor, "alerts": alerts, "active_count": len(alerts),
                "unacknowledged_count": sum(not x["acknowledged"] for x in alerts),
                "history": list(reversed(history[-50:]))}

    def check_review_reminders(self) -> dict[str, Any]:
        """Record state transitions once per episode; never tune the model."""
        with self._lock:
            monitor = self.review_monitor()
            if not self.integrity()["valid"]:
                return monitor  # The damaged ledger must remain untouched.
            active_keys = {x["condition_key"] for x in monitor["alerts"]}
            for alert in monitor["alerts"]:
                if not alert["recorded"]:
                    self.append("review_alert_opened", alert)
            for item in self.review_history():
                if not item["resolved"] and item["condition_key"] not in active_keys:
                    self.append("review_alert_resolved", {
                        "review_id": item["review_id"], "condition_key": item["condition_key"],
                        "reason": "本次检查已不再触发该复核条件；不代表模型已验证有效。",
                    })
            return self.review_monitor()

    def acknowledge_review(self, review_id: str, *, reviewer: str, reason: str) -> dict[str, Any]:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("确认复核提醒须记录确认人和原因")
        with self._lock:
            monitor = self.check_review_reminders()
            item = next((x for x in monitor["alerts"] if x["review_id"] == review_id), None)
            if item is None:
                raise ValueError("该提醒已不再处于活动状态，请刷新后核对")
            if item["acknowledged"]:
                return item["acknowledgement"]
            return self.append("review_acknowledged", {
                "review_id": review_id, "condition_key": item["condition_key"],
                "reviewer": reviewer.strip(), "reason": reason.strip(),
                "meaning": "已知晓提醒；不代表已修复、通过验证或批准模型上线。",
            })

    def report(self) -> dict[str, Any]:
        records = self.records()
        chain = self.integrity()
        formal = [item for item in records if item.get("mode") == "formal"]
        settled = [item for item in formal if (item.get("settlement") or {}).get("status") == "settled"]
        pending = [item for item in formal if not item.get("settlement")]
        unscored = [item for item in formal if item.get("settlement") and item["settlement"].get("status") != "settled"]
        count = len(settled)
        directional = [item for item in settled if item.get("score_type") == "rule_conformance_score" and abs(float(item.get("rule_score") or 0.0)) > 1e-12]
        direction_hits = [(float(item.get("rule_score") or 0.0) > 0) == bool(item["settlement"]["event_occurred"]) for item in directional]
        event_rate = _rate([bool(item["settlement"]["event_occurred"]) for item in directional])
        benchmark_hits = [
            (float(item.get("rule_score") or 0.0) > 0) == bool(item["settlement"]["benchmark_event_occurred"])
            for item in directional if item["settlement"].get("benchmark_event_occurred") is not None
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        submodels: dict[str, list[dict[str, Any]]] = {}
        for item in settled:
            key = f"{item.get('model_id')}|{item.get('target', {}).get('target_id')}|{item.get('target', {}).get('horizon_sessions')}"
            grouped.setdefault(key, []).append(item)
            for submodel, score in (item.get("submodel_scores") or {}).items():
                if _safe_float(score) is not None:
                    submodels.setdefault(f"{submodel}|{item.get('target', {}).get('target_id')}|{item.get('target', {}).get('horizon_sessions')}", []).append({**item, "rule_score": score})
        probabilities = [
            item for item in settled
            if item.get("scored_probability") is not None
        ]
        brier = None
        log_loss = None
        if probabilities:
            brier = sum((float(item["scored_probability"]) - float(item["settlement"]["event_occurred"])) ** 2 for item in probabilities) / len(probabilities)
            log_loss = _log_loss(
                [float(item["scored_probability"]) for item in probabilities],
                [bool(item["settlement"]["event_occurred"]) for item in probabilities],
            )
        stale_status = []
        for item in formal:
            status = item.get("data_quality", {}).get("status")
            statuses = list(status.values()) if isinstance(status, dict) else [status]
            stale_status.append(any(value not in {None, "fresh", "FRESH"} for value in statuses))
        result = {
            "counts": {"formal_predictions": len(formal), "settled": count, "pending": len(pending), "unsettled_with_reason": len(unscored), "experiments": sum(item.get("mode") == "experiment" for item in records), "failed_analysis_attempts": sum(event.get("event_type") == "analysis_attempt_failed" for event in self.events())},
            "unsettled_reasons": [item.get("settlement", {}).get("reason") for item in unscored],
            "market_event": {
                "directional_samples": len(direction_hits),
                "direction_hit_rate": sum(direction_hits) / len(direction_hits) if direction_hits else None,
                "event_rate_baseline": event_rate,
                "majority_direction_baseline": max(event_rate, 1 - event_rate) if event_rate is not None else None,
                "model_vs_contemporaneous_hs300_direction": sum(benchmark_hits) / len(benchmark_hits) if benchmark_hits else None,
                "groups": {key: _direction_metrics(items) for key, items in grouped.items()},
                "submodels": {key: _direction_metrics(items) for key, items in submodels.items()},
                "status": "证据不足" if len(direction_hits) < 2 else "描述性统计；非盈利保证",
            },
            "probability": {
                "status": "未校准：当前没有经时间外验证的概率映射" if not probabilities else "仅统计具有明确概率字段的已结算记录",
                "samples": len(probabilities), "brier": brier, "log_loss": log_loss,
                "calibration_curve": _reliability_bins(probabilities),
            },
            "integrity": chain,
            "model_registry": {
                "active_version": "advisor-rules-v1",
                "active_model_type": "规则/因子混合；没有基础大模型训练",
                "candidates": self.candidates(),
                "activation_status": "候选版本不自动上线；需独立部署并重新验证",
            },
            "strategy_value": {"status": "尚未验证", "net_return": None, "max_drawdown": None, "turnover": None, "reason": "尚未定义统一仓位规模、交易触发、有效期及成交模拟口径；市场事件结算不是策略收益。"},
            "account_risk_value": {"status": "尚未验证", "reason": "需有可追溯的账户权益、逐笔出入金、当时可卖数量与规则触发历史。"},
            "data_quality": {
                "stale_rate": _rate(stale_status),
                "missing_or_failed_rate": _rate([bool(item.get("data_quality", {}).get("errors")) for item in formal]),
                "unsettled_rate": len(unscored) / len(formal) if formal else None,
                "pending_rate": len(pending) / len(formal) if formal else None,
            },
            "limitations": [
                "规则符合分不是成功概率。", "方向命中率不等于策略收益或建议正确率。",
                "当前台账尚未自动推导成交；避免的损失不计为盈利。",
                "正式的策略价值和账户级风控评价须有可追溯的执行及资金流事件。",
            ],
        }
        if not chain.get("valid"):
            result["market_event"] = {"directional_samples": None, "direction_hit_rate": None, "status": "哈希链异常，评分已暂停"}
            result["probability"] = {"status": "哈希链异常，校准评价已暂停", "samples": None, "brier": None, "log_loss": None, "calibration_curve": []}
            result["calibration"] = {"status": "哈希链异常，时间校准已暂停"}
            result["scoring_paused"] = True
        else:
            result["scoring_paused"] = False
        result["review_monitor"] = self.review_monitor(records=records, integrity=chain)
        return result


def _direction_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [item for item in items if (item.get("settlement") or {}).get("status") == "settled" and abs(float(item.get("rule_score") or 0.0)) > 1e-12]
    labels = [bool(item["settlement"]["event_occurred"]) for item in rows]
    positive_calls = [float(item.get("rule_score") or 0.0) > 0 for item in rows]
    tp = sum(call and label for call, label in zip(positive_calls, labels))
    fp = sum(call and not label for call, label in zip(positive_calls, labels))
    fn = sum(not call and label for call, label in zip(positive_calls, labels))
    return {
        "samples": len(rows),
        "event_rate": sum(labels) / len(labels) if labels else None,
        "direction_hit_rate": sum(call == label for call, label in zip(positive_calls, labels)) / len(rows) if rows else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "roc_auc": _roc_auc([float(item.get("rule_score") or 0.0) for item in rows], labels),
        "status": "描述性统计；需结合样本区间及日期依赖解读" if len(rows) >= 2 else "证据不足",
    }


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _roc_auc(scores: list[float], labels: list[bool]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    order = sorted(range(len(scores)), key=lambda index: scores[index])
    rank_sum = 0.0
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and scores[order[end]] == scores[order[start]]:
            end += 1
        average_rank = (start + 1 + end) / 2
        rank_sum += average_rank * sum(bool(labels[order[idx]]) for idx in range(start, end))
        start = end
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _reliability_bins(records: list[dict[str, Any]], bins: int = 5) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = [[] for _ in range(bins)]
    for item in records:
        probability = min(1.0, max(0.0, float(item["scored_probability"])))
        groups[min(bins - 1, int(probability * bins))].append(item)
    result = []
    for index, group in enumerate(groups):
        if not group:
            continue
        event_rate = sum(bool(item["settlement"]["event_occurred"]) for item in group) / len(group)
        # Wilson 95% interval: descriptive only, does not imply independent samples.
        n = len(group)
        z = 1.959963984540054
        den = 1 + z * z / n
        center = (event_rate + z * z / (2 * n)) / den
        radius = z * math.sqrt(event_rate * (1 - event_rate) / n + z * z / (4 * n * n)) / den
        result.append({
            "range": [index / bins, (index + 1) / bins], "samples": n,
            "mean_probability": sum(float(item["scored_probability"]) for item in group) / n,
            "observed_rate": event_rate, "wilson_95_interval": [max(0.0, center - radius), min(1.0, center + radius)],
        })
    return result


def chronological_calibration(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Use three chronological ranges and purge labels crossing boundaries."""
    rows = [item for item in records if (item.get("settlement") or {}).get("status") == "settled" and _safe_float(item.get("rule_score")) is not None]
    rows.sort(key=lambda item: (str(item.get("as_of", "")), str(item.get("prediction_id", ""))))
    dates = sorted({str(item.get("as_of", ""))[:10] for item in rows})
    if len(dates) < 12:
        return {"status": "证据不足", "reason": "需要按日期分成开发、校准和最终测试三段，并清除跨界标签；当前前瞻日期不足。", "samples": len(rows), "distinct_prediction_dates": len(dates)}
    calibration_start = dates[int(len(dates) * 0.5)]
    test_start = dates[int(len(dates) * 0.7)]
    development = [item for item in rows if str(item.get("as_of", ""))[:10] < calibration_start and str(item["settlement"].get("resolved_at", ""))[:10] < calibration_start]
    calibration = [item for item in rows if calibration_start <= str(item.get("as_of", ""))[:10] < test_start and str(item["settlement"].get("resolved_at", ""))[:10] < test_start]
    test = [item for item in rows if str(item.get("as_of", ""))[:10] >= test_start]
    calibration_labels = [bool(item["settlement"]["event_occurred"]) for item in calibration]
    test_labels = [bool(item["settlement"]["event_occurred"]) for item in test]
    if len(calibration) < 2 or len(test) < 2 or len(set(calibration_labels)) < 2:
        return {"status": "证据不足", "reason": "校准段或最终测试段样本/标签类别不足；未生成生产校准概率。", "samples": len(rows), "development_samples": len(development), "calibration_samples": len(calibration), "test_samples": len(test), "calibration_start": calibration_start, "test_start": test_start}
    slope, intercept = _fit_platt(calibration)
    raw_scores = [min(1.0, max(0.0, (float(item["rule_score"]) + 1.0) / 2.0)) for item in test]
    calibrated = [_sigmoid(slope * float(item["rule_score"]) + intercept) for item in test]
    raw_brier = sum((p - int(y)) ** 2 for p, y in zip(raw_scores, test_labels)) / len(test)
    calibrated_brier = sum((p - int(y)) ** 2 for p, y in zip(calibrated, test_labels)) / len(test)
    raw_ll = _log_loss(raw_scores, test_labels)
    calibrated_ll = _log_loss(calibrated, test_labels)
    enough_class_support = len(set(test_labels)) == 2
    calibration_curve = _reliability_bins([
        {**item, "scored_probability": probability}
        for item, probability in zip(test, calibrated)
    ])
    return {
        "status": "时间外描述性对照；不代表校准可靠或有盈利优势" if enough_class_support else "测试集类别单一，校准证据不足",
        "method": "Platt逻辑校准；规则分映射至单调logit",
        "split": "chronological_50_20_30_with_label_end_purge", "calibration_start": calibration_start, "test_start": test_start,
        "development_samples": len(development), "calibration_samples": len(calibration), "test_samples": len(test),
        "development_end": max((str(x.get("as_of"))[:10] for x in development), default=None),
        "calibration_end": max(str(x.get("as_of"))[:10] for x in calibration),
        "test_start": min(str(x.get("as_of"))[:10] for x in test),
        "development_used_for_calibration_fit": False,
        "calibration_used_only_for_fitting": True, "test_used_only_for_evaluation": True,
        "calibrator": {"slope": slope, "intercept": intercept},
        "score_transform_brier": raw_brier, "calibrated_brier": calibrated_brier,
        "score_transform_log_loss": raw_ll, "calibrated_log_loss": calibrated_ll,
        "score_transform": "仅作诊断基线：将规则分线性映射为 (score + 1) / 2；这不是模型原始概率",
        "calibration_curve": calibration_curve,
        "calibrated_probability_available_for_production": False,
        "note": "原模型没有概率输出；此处线性映射仅作明确标注的诊断基线，Platt结果仅为时间外研究输出，不进入生产。上线仍须人工审核并进行前瞻影子对照。相同标的、重叠期限与日期相关性仍会降低有效独立样本量。",
    }


def _fit_platt(rows: list[dict[str, Any]]) -> tuple[float, float]:
    slope = 0.0
    positives = sum(bool(item["settlement"]["event_occurred"]) for item in rows)
    rate = min(1 - 1e-6, max(1e-6, positives / len(rows)))
    intercept = math.log(rate / (1 - rate))
    for _ in range(2000):
        grad_slope = 0.0
        grad_intercept = 0.0
        for item in rows:
            x = float(item["rule_score"])
            y = int(bool(item["settlement"]["event_occurred"]))
            p = _sigmoid(slope * x + intercept)
            grad_slope += (p - y) * x
            grad_intercept += p - y
        rate_step = 0.02 / len(rows)
        next_slope = slope - rate_step * grad_slope
        next_intercept = intercept - rate_step * grad_intercept
        if max(abs(next_slope - slope), abs(next_intercept - intercept)) < 1e-9:
            slope, intercept = next_slope, next_intercept
            break
        slope, intercept = next_slope, next_intercept
    return slope, intercept


def _sigmoid(value: float) -> float:
    value = min(35.0, max(-35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _log_loss(probabilities: list[float], labels: list[bool]) -> float:
    epsilon = 1e-12
    return -sum(int(y) * math.log(max(epsilon, min(1 - epsilon, p))) + (1 - int(y)) * math.log(max(epsilon, 1 - min(1 - epsilon, p))) for p, y in zip(probabilities, labels)) / len(labels)
