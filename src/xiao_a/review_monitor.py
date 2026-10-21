"""Versioned review signals, not model tuning or trade authorization.

Performance monitoring groups versions/targets, clusters same-day predictions,
and discards overlapping label windows. Thresholds are conservative operational
review rules; they are not evidence that any sample count proves model accuracy.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any


REVIEW_POLICY = {
    "version": "review-monitor-v1",
    "data_dates": 3,
    "minimum_data_units": 5,
    "data_lookback_calendar_days": 30,
    "daily_problem_rate": 0.5,
    "reference_blocks": 8,
    "recent_window_blocks": 8,
    "consecutive_windows": 2,
    "underperformance_margin": 0.05,
    "bootstrap_repetitions": 1000,
    "action": "manual_review_only",
}


def _day(value: Any) -> str | None:
    try:
        stamp = str(value)
        if "T" in stamp:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            return parsed.date().isoformat()
        return date.fromisoformat(stamp[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _problem(record: dict[str, Any]) -> bool:
    quality = record.get("data_quality") or {}
    status = quality.get("status")
    values = list(status.values()) if isinstance(status, dict) else [status]
    return bool(quality.get("errors")) or any(str(x).lower() in {
        "stale", "partial", "unavailable", "invalid",
    } for x in values)


def _bootstrap_upper(values: list[float], seed: str) -> float:
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values)
        for _ in range(REVIEW_POLICY["bootstrap_repetitions"])
    )
    return means[int((len(means) - 1) * 0.975)]


def _performance_group_key(record: dict[str, Any]) -> str:
    # Hard risk is not a tunable component of this monitor.
    return json.dumps({
        "model": record.get("model_id"),
        "versions": record.get("model_versions"),
        "target": record.get("target"),
        "score_type": record.get("score_type"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _nonoverlapping_blocks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for item in sorted(rows, key=lambda x: (str(x.get("as_of")), str(x.get("prediction_id")))):
        day = _day(item.get("information_cutoff"))
        if day:
            # Predefined earliest forecast per symbol/day, never the best result.
            by_date.setdefault(day, {}).setdefault(str(item.get("symbol")), item)
    blocks = []
    last_exit = ""
    for day, symbols in sorted(by_date.items()):
        if day <= last_exit:
            continue
        entries = list(symbols.values())
        last_exit = max(_day(x["settlement"].get("exit_date")) or day for x in entries)
        blocks.append({"date": day, "exit": last_exit, "rows": entries})
    return blocks


def evaluate_review_signals(records: list[dict[str, Any]], events: list[dict[str, Any]],
                            integrity: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    signals: list[dict[str, Any]] = []
    groups_report: list[dict[str, Any]] = []

    def signal(key: str, title: str, category: str, reason: str, evidence: dict,
               first_date: str, *, severity: str = "warning") -> None:
        signals.append({
            "condition_key": key, "title": title, "category": category,
            "severity": severity, "status": "需复核", "reason": reason,
            "evidence": evidence, "first_observed_date": first_date,
            "next_step": "复制复核报告并联系开发者核查；不自动修改模型或放宽账户风控。",
            "policy_version": REVIEW_POLICY["version"],
        })

    if not integrity.get("valid"):
        signal("audit_integrity", "预测台账完整性异常", "audit_integrity",
               "审计哈希链校验失败，评分已暂停；先核对台账文件。",
               {"broken_event_id": integrity.get("broken_event_id")}, today.isoformat(), severity="error")
        return {"policy": REVIEW_POLICY, "status": "需复核", "signals": signals,
                "groups": [], "probability_status": "台账异常，概率监测暂停"}

    formal = [x for x in records if x.get("mode") == "formal"]
    lookback_start = (today - timedelta(days=REVIEW_POLICY["data_lookback_calendar_days"])).isoformat()
    # Unit = symbol/day. Repeated requests/failures cannot inflate the evidence.
    units: dict[str, dict[str, bool]] = {}
    for record in formal:
        day = _day(record.get("as_of"))
        if day and lookback_start <= day <= today.isoformat():
            symbol = str(record.get("symbol"))
            units.setdefault(day, {})[symbol] = units.get(day, {}).get(symbol, False) or _problem(record)
    for event in events:
        if event.get("event_type") == "analysis_attempt_failed":
            day = _day(event.get("requested_at") or event.get("recorded_at"))
            if day and lookback_start <= day <= today.isoformat():
                units.setdefault(day, {})[str(event.get("symbol"))] = True
    dates = sorted(units)[-REVIEW_POLICY["data_dates"]:]
    daily = [{"date": day, "units": len(units[day]),
              "problem_rate": sum(units[day].values()) / len(units[day])} for day in dates]
    if len(dates) == REVIEW_POLICY["data_dates"] and sum(x["units"] for x in daily) >= REVIEW_POLICY["minimum_data_units"] and all(
        x["problem_rate"] >= REVIEW_POLICY["daily_problem_rate"] for x in daily
    ):
        signal("persistent_data_quality", "数据与分析连续异常", "data_quality",
               "最近3个有记录日期均至少一半标的出现过期、缺失或分析失败。先查数据接口与输入口径，不据此改模型规律。",
               {"daily": daily, "sample_units": sum(x["units"] for x in daily)}, dates[0])

    blocked_days: set[str] = set()
    blocked_ids: set[str] = set()
    for record in formal:
        if record.get("settlement"):
            continue
        for attempt in record.get("settlement_attempts") or []:
            reason = str(attempt.get("reason") or "")
            day = _day(attempt.get("attempt_at") or attempt.get("recorded_at"))
            if day and lookback_start <= day <= today.isoformat() and "预测期限尚未完整" not in reason and any(word in reason for word in ("缺失", "无效", "非最新", "不完整")):
                blocked_days.add(day)
                blocked_ids.add(str(record.get("prediction_id")))
    if len(blocked_days) >= 3:
        signal("settlement_data_blocked", "到期结算持续缺少可靠数据", "settlement_quality",
               "待结算记录在至少3个检查日期出现数据质量问题；普通未到期记录不触发此提醒。",
               {"check_dates": sorted(blocked_days), "prediction_ids": sorted(blocked_ids)}, min(blocked_days))

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in formal:
        result = item.get("settlement") or {}
        target = item.get("target") or {}
        score = item.get("rule_score")
        try:
            score = float(score)
            horizon = int(target.get("horizon_sessions") or 0)
        except (TypeError, ValueError):
            continue
        cutoff, exit_day = _day(item.get("information_cutoff")), _day(result.get("exit_date"))
        if target.get("target_id") and horizon > 0 and result.get("status") == "settled" and math.isfinite(score) and abs(score) > 1e-12 and cutoff and exit_day and cutoff < exit_day <= today.isoformat():
            grouped.setdefault(_performance_group_key(item), []).append(item)
    needed = REVIEW_POLICY["reference_blocks"] + 2 * REVIEW_POLICY["recent_window_blocks"]
    for key, rows in sorted(grouped.items()):
        blocks = _nonoverlapping_blocks(rows)
        group_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        summary = {"group_id": group_id, "model_id": rows[0].get("model_id"),
                   "model_versions": rows[0].get("model_versions"), "target": rows[0].get("target"),
                   "settled_predictions": len(rows), "nonoverlapping_date_blocks": len(blocks),
                   "status": "证据不足", "minimum_monitoring_blocks": needed}
        groups_report.append(summary)
        if len(blocks) < needed:
            continue
        width = REVIEW_POLICY["recent_window_blocks"]
        reference = blocks[:-2 * width]
        reference_rate = sum(sum(bool(x["settlement"]["event_occurred"]) for x in b["rows"]) / len(b["rows"]) for b in reference) / len(reference)
        # Fixed before BOTH recent windows. Never use their labels to choose baseline.
        baseline_up = reference_rate >= 0.5
        windows = []
        for idx, window in enumerate((blocks[-2 * width:-width], blocks[-width:])):
            deltas = []
            for block in window:
                delta = sum(
                    int((float(x["rule_score"]) > 0) == bool(x["settlement"]["event_occurred"]))
                    - int(baseline_up == bool(x["settlement"]["event_occurred"]))
                    for x in block["rows"]
                ) / len(block["rows"])
                deltas.append(delta)
            upper = _bootstrap_upper(deltas, f"{group_id}:{idx}")
            windows.append({"start": window[0]["date"], "end": window[-1]["date"],
                            "blocks": len(window), "predictions": sum(len(b["rows"]) for b in window),
                            "model_minus_baseline_hit_rate": sum(deltas) / len(deltas),
                            "bootstrap_95_upper": upper})
        summary.update(status="持续监测中；未发现预设复核信号", reference_event_rate=reference_rate,
                       baseline_direction="上涨" if baseline_up else "不涨", windows=windows)
        if all(x["bootstrap_95_upper"] < -REVIEW_POLICY["underperformance_margin"] for x in windows):
            summary["status"] = "需复核"
            signal(f"direction_underperformance:{group_id}", "模型方向持续弱于事前固定基准", "model_performance",
                   "相同模型版本和目标的两个连续非重叠观察窗口，方向表现均弱于仅用此前事件率确定的固定方向基准。先复核数据和适用环境，再决定是否提候选改进。",
                   {**summary, "reference_blocks": len(reference)}, windows[0]["start"])
    return {
        "policy": REVIEW_POLICY, "status": "需复核" if signals else "持续监测中" if any(
            x["nonoverlapping_date_blocks"] >= needed for x in groups_report
        ) else "证据不足",
        "signals": signals, "groups": groups_report,
        "probability_status": "未启用概率偏差报警：现有生产模型只输出规则分，没有经过验证的概率。",
        "limitations": "阈值仅决定何时人工复核，不证明准确或失效。同日按组、重叠期限剔除、区块重采样仍无法消除市场相关性；反复监测也可能误报。",
    }
