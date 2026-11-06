"""Read-only research diagnostics for the prediction ledger.

This module does not settle predictions, append audit events, rewrite
history, or change production strategy weights. Rule scores stay rule
conformance scores. Direction hit rate is not a buy win rate and is not
net PnL. A chronological comparison on stored scores is descriptive only.
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xiao_a.backtest import AShareBacktester, FeeSchedule, execution_assumptions
from xiao_a.models import DailyBar, SignalAction
from xiao_a.prediction_audit import TARGET_DEFINITION, _canonical, _roc_auc, _safe_float
from xiao_a.review_monitor import REVIEW_POLICY


SCHEMA = "xiao_a_research_diagnostics_v1"
BACKTEST_SCHEMA = "xiao_a_research_backtest_v1"
SCORE_EPSILON = 1e-12
DUCKDB_AUTO_BYTES = 1_000_000
BOOTSTRAP_REPETITIONS = 1000
BOOTSTRAP_SEED = "research-diagnostics-v1"
TARGET_ID = TARGET_DEFINITION["target_id"]
_SHANGHAI = timezone(timedelta(hours=8))


def monitoring_block_minimum() -> int:
    """Blocks required before review_monitor will score a direction comparison.

    The production gate is one reference span plus two later observation
    windows. Each span is ``recent_window_blocks`` long, so the current policy
    is 8 + 2 * 8 = 24. A single window of 8 is not the gate.
    """
    return int(REVIEW_POLICY["reference_blocks"]) + 2 * int(REVIEW_POLICY["recent_window_blocks"])
_REQUIRED_FEES = ("commission_rate", "minimum_commission", "stamp_duty_sell", "transfer_fee_rate")
_FORBIDDEN_SPEC_KEYS = {"account_fees", "account_fees_path", "assets_ledger", "portfolio_path", "userdata"}


def shanghai_day(value: Any) -> str | None:
    """Calendar day used for cutoffs.

    Timezone-aware timestamps convert to Asia/Shanghai. Naive timestamps keep
    the calendar date written in the string. Date-only values use that date.
    """
    if value is None:
        return None
    stamp = str(value).strip()
    if not stamp:
        return None
    try:
        if "T" in stamp or " " in stamp:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(_SHANGHAI)
            return parsed.date().isoformat()
        return date.fromisoformat(stamp[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def chain_integrity(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Same hash rule as PredictionLedger.integrity, without opening a writer."""
    previous = "0" * 64
    for index, event in enumerate(events):
        supplied = event.get("event_hash")
        payload = {key: value for key, value in event.items() if key != "event_hash"}
        expected = hashlib.sha256(previous.encode("ascii") + _canonical(payload)).hexdigest()
        if event.get("previous_hash") != previous or supplied != expected:
            return {"valid": False, "events": index, "broken_event_id": event.get("event_id"), "head": None}
        previous = str(supplied)
    return {"valid": True, "events": len(events), "broken_event_id": None, "head": previous}


def read_audit_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"审计文件不存在或不是文件: {path.name}")
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"审计文件第 {line_number} 行不是有效 JSON") from exc
            if not isinstance(event, dict):
                raise ValueError(f"审计文件第 {line_number} 行不是 JSON 对象")
            events.append(event)
    return events


def read_annotations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"cohort 注释文件不存在: {path.name}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        items: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"cohort 注释第 {line_number} 行不是有效 JSON") from exc
            if not isinstance(item, dict):
                raise ValueError(f"cohort 注释第 {line_number} 行不是 JSON 对象")
            items.append(item)
        return items
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("cohort 注释不是有效 JSON") from exc
    if isinstance(data, dict) and isinstance(data.get("annotations"), list):
        data = data["annotations"]
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("cohort 注释必须是对象数组")
    return data


def fold_records(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize predictions the same way as PredictionLedger.records.

    A later prediction_created with the same id replaces the body, matching
    the ledger. Extra settlement events are remembered and then withheld from
    scored labels instead of being treated as a rewrite.
    """
    records: dict[str, dict[str, Any]] = {}
    duplicate_created = 0
    multiple_settlements: set[str] = set()
    for event in events:
        event_type = event.get("event_type")
        prediction_id = event.get("prediction_id")
        if event_type == "prediction_created":
            key = str(prediction_id)
            if key in records and records[key].get("event_type") == "prediction_created":
                duplicate_created += 1
            records[key] = {**event, "queries": [], "settlement": None, "corrections": []}
        elif prediction_id is not None and str(prediction_id) in records:
            record = records[str(prediction_id)]
            if event_type == "prediction_repeated":
                record["queries"].append(event)
            elif event_type == "prediction_settled":
                if record.get("settlement") is not None:
                    multiple_settlements.add(str(prediction_id))
                record["settlement"] = event
            elif event_type == "prediction_correction":
                record["corrections"].append(event)
            elif event_type == "execution_recorded":
                record.setdefault("executions", []).append(event)
            elif event_type == "settlement_attempted":
                record["last_settlement_attempt"] = event
                record.setdefault("settlement_attempts", []).append(event)
    meta = {
        "duplicate_prediction_created": duplicate_created,
        "multiple_settlement_predictions": len(multiple_settlements),
    }
    return list(records.values()), {**meta, "_multiple_ids": multiple_settlements}


def _sequence(record: dict[str, Any]) -> int:
    value = record.get("sequence")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 10**18


def _slice_events(events: list[dict[str, Any]], as_of: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if as_of is None:
        return events, {
            "as_of": None,
            "applied": False,
            "excluded_after_cutoff": 0,
            "excluded_missing_recorded_at": 0,
            "chain_prefix_aligned": True,
            "slice_hash_chain": "full_file",
            "note": "未设置 as-of 时诊断使用整份当前文件。事件数比旧报告多，只说明后来追加了事件，本身不是损坏。",
            "decision_date_rule": "not_applied",
            "excluded_decision_after_report_as_of": 0,
            "excluded_missing_decision_date": 0,
        }
    try:
        cutoff = date.fromisoformat(as_of).isoformat()
    except ValueError as exc:
        raise ValueError("as-of 必须是 YYYY-MM-DD") from exc
    kept: list[dict[str, Any]] = []
    missing = 0
    excluded_after = 0
    prefix_aligned = True
    seen_drop = False
    for event in events:
        day = shanghai_day(event.get("recorded_at"))
        if day is None:
            missing += 1
            seen_drop = True
            continue
        if day <= cutoff:
            if seen_drop:
                prefix_aligned = False
            kept.append(event)
        else:
            excluded_after += 1
            seen_drop = True
    return kept, {
        "as_of": cutoff,
        "applied": True,
        "timezone": "带时区的 recorded_at 换成 Asia/Shanghai 日期；没有时区的时间戳保留字符串里的日期",
        "excluded_after_cutoff": excluded_after,
        "excluded_missing_recorded_at": missing,
        "chain_prefix_aligned": prefix_aligned,
        "slice_hash_chain": "prefix_of_full_chain" if prefix_aligned else "not_a_prefix_timestamp_slice_still_reported",
        "decision_date_rule": "prediction as_of and information_cutoff must both be on or before the report date; recorded_at only decides which events are visible",
        "excluded_decision_after_report_as_of": 0,
        "excluded_missing_decision_date": 0,
        "note": "recorded_at 只决定事件是否已经写入文件。预测自己的 as_of 或 information_cutoff 晚于报告日时，即使事件时间更早也不进入评分。退出日晚于报告日的标签也不使用。缺少 recorded_at 的事件不能证明落在截止日内。事件数变化本身不是损坏。",
    }


def _classify_label(record: dict[str, Any], multiple_ids: set[str], report_as_of: str | None = None) -> str:
    if str(record.get("prediction_id")) in multiple_ids:
        return "ambiguous_multiple_settlements"
    settlement = record.get("settlement")
    if not settlement:
        return "pending"
    if settlement.get("status") != "settled":
        return "unsettled_other"
    target = record.get("target") if isinstance(record.get("target"), dict) else {}
    target_id = target.get("target_id")
    if target_id is None:
        return "legacy_target_unspecified"
    if target_id != TARGET_ID:
        return "unmatched_target"
    if target.get("horizon_sessions") not in {None, 5}:
        return "unmatched_target"
    event = settlement.get("event_occurred")
    if type(event) is not bool:
        return "ambiguous_label"
    target_return = _safe_float(settlement.get("target_return"))
    if target_return is None or event != (target_return > 0):
        return "ambiguous_label"
    entry = shanghai_day(settlement.get("entry_date"))
    exit_day = shanghai_day(settlement.get("exit_date"))
    cutoff = shanghai_day(record.get("information_cutoff"))
    resolved = shanghai_day(settlement.get("resolved_at"))
    if not entry or not exit_day or not cutoff or not resolved:
        return "ambiguous_label"
    if not (cutoff < entry <= exit_day) or resolved != exit_day:
        return "ambiguous_label"
    if report_as_of is not None:
        settlement_recorded = shanghai_day(settlement.get("recorded_at"))
        if (
            settlement_recorded is None
            or settlement_recorded > report_as_of
            or entry > report_as_of
            or exit_day > report_as_of
        ):
            return "label_not_knowable_at_as_of"
    return "resolvable"


def _apply_decision_as_of(rows: list[dict[str, Any]], report_as_of: str | None) -> dict[str, Any]:
    """Drop predictions whose own dates are after, or not comparable with, the report date.

    Event ``recorded_at`` can be earlier than the prediction date. Scoring that
    row would treat a later decision as if it belonged to the earlier report.
    """
    if report_as_of is None:
        return {"rows": rows, "excluded_decision_after_report_as_of": 0, "excluded_missing_decision_date": 0}
    kept: list[dict[str, Any]] = []
    after = 0
    missing = 0
    for row in rows:
        decision_day = shanghai_day(row.get("as_of"))
        info_day = shanghai_day(row.get("information_cutoff"))
        if decision_day is None or info_day is None:
            missing += 1
            continue
        if decision_day > report_as_of or info_day > report_as_of:
            after += 1
            continue
        kept.append(row)
    return {
        "rows": kept,
        "excluded_decision_after_report_as_of": after,
        "excluded_missing_decision_date": missing,
    }


def _dedupe_formal(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    missing: list[dict[str, Any]] = []
    formal = [record for record in records if record.get("mode") == "formal"]
    for record in formal:
        symbol = record.get("symbol")
        day = shanghai_day(record.get("as_of"))
        if symbol is None or day is None or str(symbol).strip() == "":
            missing.append(record)
            continue
        grouped.setdefault((str(symbol), day), []).append(record)
    kept: list[dict[str, Any]] = []
    excluded = 0
    for items in grouped.values():
        items.sort(key=lambda record: (_sequence(record), str(record.get("prediction_id") or "")))
        kept.append(items[0])
        excluded += len(items) - 1
    kept.sort(key=lambda record: (str(record.get("as_of") or ""), str(record.get("prediction_id") or "")))
    return {
        "rows": kept,
        "excluded_duplicate_symbol_as_of_date": excluded,
        "excluded_missing_symbol_or_as_of": len(missing),
    }


def _symbol_namespace_ambiguous(records: list[dict[str, Any]]) -> bool:
    symbols = {str(record.get("symbol")) for record in records if record.get("symbol")}
    for symbol in symbols:
        if "." in symbol and symbol.split(".", 1)[1] in symbols:
            return True
    return False


def _same_cutoff_day(records: list[dict[str, Any]]) -> int:
    count = 0
    for record in records:
        as_of = shanghai_day(record.get("as_of"))
        cutoff = shanghai_day(record.get("information_cutoff"))
        if as_of is not None and as_of == cutoff:
            count += 1
    return count


def _execution_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"not_applied": 0, "absent_not_backfilled": 0, "unexpected": 0}
    for row in rows:
        settlement = row.get("settlement") or {}
        value = settlement.get("execution_simulation") if settlement else None
        if not settlement:
            continue
        if value is None:
            counts["absent_not_backfilled"] += 1
        elif value == "not_applied":
            counts["not_applied"] += 1
        else:
            counts["unexpected"] += 1
    return counts


def _direction_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    finite = []
    for row in rows:
        if row.get("score_type") != "rule_conformance_score":
            continue
        score = _safe_float(row.get("rule_score"))
        if score is None:
            continue
        finite.append((score, bool(row["settlement"]["event_occurred"])))
    nonzero = [(score, event) for score, event in finite if abs(score) > SCORE_EPSILON]
    return {
        "all_finite": _binary_direction(finite, near_zero_counted_as_not_up=True),
        "nonzero_production_definition": _binary_direction(nonzero, near_zero_counted_as_not_up=False),
    }


def _binary_direction(pairs: list[tuple[float, bool]], *, near_zero_counted_as_not_up: bool) -> dict[str, Any]:
    if not pairs:
        return {
            "rows": 0,
            "hits": 0,
            "hit_rate": None,
            "positive_events": 0,
            "negative_events": 0,
            "positive_event_rate": None,
            "constant_negative_hits": 0,
            "constant_negative_hit_rate": None,
            "constant_positive_hit_rate": None,
            "majority_class_hit_rate": None,
            "beats_constant_negative": None,
            "roc_auc": None,
            "near_zero_counted_as_not_up": near_zero_counted_as_not_up,
            "status": "证据不足",
        }
    hits = sum((score > 0) == event for score, event in pairs)
    positives = sum(event for _, event in pairs)
    negatives = len(pairs) - positives
    constant_negative = negatives / len(pairs)
    constant_positive = positives / len(pairs)
    hit_rate = hits / len(pairs)
    labels = [event for _, event in pairs]
    return {
        "rows": len(pairs),
        "hits": hits,
        "hit_rate": hit_rate,
        "positive_events": positives,
        "negative_events": negatives,
        "positive_event_rate": constant_positive,
        "constant_negative_hits": negatives,
        "constant_negative_hit_rate": constant_negative,
        "constant_positive_hit_rate": constant_positive,
        "majority_class_hit_rate": max(constant_negative, constant_positive),
        "beats_constant_negative": (hits > negatives) if len(pairs) >= 2 else None,
        "roc_auc": _roc_auc([score for score, _ in pairs], labels),
        "near_zero_counted_as_not_up": near_zero_counted_as_not_up,
        "status": "证据不足" if len(pairs) < 2 or len(set(labels)) < 2 else "描述性统计",
    }


def _submodel_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    names: set[str] = set()
    universe = [row for row in rows if row.get("score_type") == "rule_conformance_score"]
    for row in universe:
        scores = row.get("submodel_scores")
        if isinstance(scores, dict):
            names.update(str(key) for key in scores)
    report: dict[str, Any] = {}
    for name in sorted(names):
        finite: list[tuple[float, bool]] = []
        missing = 0
        for row in universe:
            raw = (row.get("submodel_scores") or {}).get(name) if isinstance(row.get("submodel_scores"), dict) else None
            score = _safe_float(raw)
            if score is None:
                missing += 1
                continue
            finite.append((score, bool(row["settlement"]["event_occurred"])))
        nonzero = [(score, event) for score, event in finite if abs(score) > SCORE_EPSILON]
        near_zero = len(finite) - len(nonzero)
        report[name] = {
            "rows_in_rule_score_universe": len(universe),
            "float_scores": len(finite),
            "missing_or_non_finite": missing,
            "near_zero_excluded_from_direction_sample": near_zero,
            "direction_samples": len(nonzero),
            "roc_auc_direction_sample": _roc_auc([score for score, _ in nonzero], [event for _, event in nonzero]) if nonzero else None,
            "roc_auc_all_finite_scores": _roc_auc([score for score, _ in finite], [event for _, event in finite]) if finite else None,
            "note": (
                "方向样本少于浮点分数，是因为接近零的分数不进入方向命中样本；这不是另一套标签，也不把缺失分数当成零。"
                if near_zero or missing
                else "浮点分数都进入了非零方向样本。"
            ),
        }
    return report


def _normalize_action(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().casefold()
    return text or None


def _action_of(record: dict[str, Any], field: str) -> str | None:
    recommendation = record.get("recommendation") if isinstance(record.get("recommendation"), dict) else {}
    if field == "model_action":
        return _normalize_action(recommendation.get("model_action"))
    if field == "account_action":
        return _normalize_action(recommendation.get("account_action"))
    nested = recommendation.get("position_aware_action")
    if isinstance(nested, dict):
        return _normalize_action(nested.get("action"))
    return None


def _action_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("model_action", "account_action", "position_aware_action")
    by_field: dict[str, Any] = {}
    for field in fields:
        grouped: dict[str, list[dict[str, Any]]] = {}
        missing = 0
        for row in rows:
            action = _action_of(row, field)
            if action is None:
                missing += 1
                continue
            grouped.setdefault(action, []).append(row)
        actions = {}
        for action, items in sorted(grouped.items()):
            positives = sum(bool(item["settlement"]["event_occurred"]) for item in items)
            actions[action] = {
                "rows": len(items),
                "distinct_symbols": len({str(item.get("symbol")) for item in items}),
                "positive_events": positives,
                "positive_event_rate": positives / len(items),
                "metric_name": "forward_event_rate_given_action",
                "this_is_not_direction_accuracy": True,
                "this_is_not_net_pnl": True,
            }
        by_field[field] = {"missing_or_blank": missing, "actions": actions}
    buy_rates = {
        field: (by_field[field]["actions"].get("buy"))
        for field in fields
    }
    return {
        "fields_are_not_interchangeable": True,
        "model_action_field": "recommendation.model_action",
        "account_action_field": "recommendation.account_action",
        "position_aware_field": "recommendation.position_aware_action.action",
        "by_field": by_field,
        "buy_event_rates": buy_rates,
        "note": "买入事件率是该动作子集里五日收益大于零的比例。它不是方向命中率，也不是扣费后的净收益。model_action 与 account_action 必须分开读。",
    }


def _nonoverlapping_blocks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, dict[str, dict[str, Any]]] = {}
    collapsed = 0
    missing_cutoff = 0
    ordered = sorted(rows, key=lambda row: (str(row.get("as_of") or ""), str(row.get("prediction_id") or "")))
    for row in ordered:
        day = shanghai_day(row.get("information_cutoff"))
        if not day:
            missing_cutoff += 1
            continue
        bucket = by_date.setdefault(day, {})
        symbol = str(row.get("symbol"))
        if symbol in bucket:
            collapsed += 1
            continue
        bucket[symbol] = row
    blocks = []
    last_exit = ""
    skipped_representatives = 0
    for day, symbols in sorted(by_date.items()):
        if last_exit and day <= last_exit:
            skipped_representatives += len(symbols)
            continue
        entries = list(symbols.values())
        exits = [shanghai_day(entry["settlement"].get("exit_date")) or day for entry in entries]
        last_exit = max(exits)
        hits = 0
        total = 0
        for entry in entries:
            score = _safe_float(entry.get("rule_score"))
            if entry.get("score_type") != "rule_conformance_score" or score is None or abs(score) <= SCORE_EPSILON:
                continue
            total += 1
            if (score > 0) == bool(entry["settlement"]["event_occurred"]):
                hits += 1
        blocks.append({
            "date": day,
            "exit": last_exit,
            "representative_rows": len(entries),
            "direction_rows": total,
            "direction_hits": hits,
        })
    return {
        "blocks": blocks,
        "collapsed_same_symbol_cutoff_day": collapsed,
        "missing_information_cutoff": missing_cutoff,
        "skipped_representatives": skipped_representatives,
    }


def _overlapping_pairs(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_symbol: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        settlement = row["settlement"]
        start = shanghai_day(settlement.get("entry_date"))
        end = shanghai_day(settlement.get("exit_date"))
        if not start or not end:
            continue
        by_symbol.setdefault(str(row.get("symbol")), []).append((start, end))
    pairs = 0
    repeated = 0
    for intervals in by_symbol.values():
        if len(intervals) > 1:
            repeated += 1
        intervals.sort()
        active: list[str] = []
        for start, end in intervals:
            active = [item for item in active if item >= start]
            pairs += len(active)
            active.append(end)
    return {
        "distinct_symbols": len(by_symbol),
        "symbols_with_repeated_rows": repeated,
        "overlapping_same_symbol_window_pairs": pairs,
    }


def _monitoring_gate_reason(blocks: int) -> str:
    minimum = monitoring_block_minimum()
    return (
        f"非重叠日期块为 {blocks}，生产监测门槛是 {minimum}"
        f"（{int(REVIEW_POLICY['reference_blocks'])} 个参考块 + 两个各 "
        f"{int(REVIEW_POLICY['recent_window_blocks'])} 块的观察窗口）。"
        "单个 8 块窗口只是其中一段，不能单独当成证据充足。"
    )


def _block_bootstrap(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    minimum = monitoring_block_minimum()
    usable = [block for block in blocks if block["direction_rows"] > 0]
    if len(usable) < minimum:
        return {
            "status": "证据不足",
            "minimum_monitoring_blocks": minimum,
            "reason": _monitoring_gate_reason(len(usable)) + "行数重采样会把重叠的五个交易日窗口伪装成独立样本，因此这里不给区间。",
            "repetitions": 0,
            "percentile_2_5": None,
            "percentile_97_5": None,
        }
    rng = random.Random(BOOTSTRAP_SEED)
    stats: list[float] = []
    for _ in range(BOOTSTRAP_REPETITIONS):
        sample = [rng.choice(usable) for _ in usable]
        total = sum(block["direction_rows"] for block in sample)
        hits = sum(block["direction_hits"] for block in sample)
        stats.append(hits / total)
    stats.sort()

    def percentile(fraction: float) -> float:
        index = int(round((len(stats) - 1) * fraction))
        return stats[min(len(stats) - 1, max(0, index))]

    return {
        "status": "描述性日期块重采样；不能消除金融时序相关，也不是预测改进的证明",
        "minimum_monitoring_blocks": minimum,
        "seed": BOOTSTRAP_SEED,
        "repetitions": BOOTSTRAP_REPETITIONS,
        "percentile_2_5": percentile(0.025),
        "percentile_97_5": percentile(0.975),
    }


def _predict(rule: str, row: dict[str, Any]) -> bool | None:
    if rule == "constant_negative":
        return False
    if rule == "constant_positive":
        return True
    if rule == "ensemble_positive_sign":
        score = _safe_float(row.get("rule_score"))
    elif rule.startswith("submodel_positive_sign:"):
        name = rule.split(":", 1)[1]
        scores = row.get("submodel_scores") if isinstance(row.get("submodel_scores"), dict) else {}
        score = _safe_float(scores.get(name))
    else:
        raise ValueError(f"未知的预注册规则: {rule}")
    if score is None or abs(score) <= SCORE_EPSILON:
        return None
    return score > 0


def _rule_metrics(rows: list[dict[str, Any]], rule: str) -> dict[str, Any]:
    compared: list[tuple[bool, bool]] = []
    abstentions = 0
    for row in rows:
        prediction = _predict(rule, row)
        if prediction is None:
            abstentions += 1
            continue
        compared.append((prediction, bool(row["settlement"]["event_occurred"])))
    hits = sum(prediction == event for prediction, event in compared)
    negatives = sum(not event for _, event in compared)
    rows_n = len(compared)
    enough = rows_n >= 2 and len({event for _, event in compared}) >= 2
    sign_rule = not rule.startswith("constant_")
    return {
        "rule": rule,
        "rows": rows_n,
        "abstentions": abstentions,
        "near_zero_or_missing_scores_excluded": abstentions if sign_rule else 0,
        "denominator_excludes_near_zero_scores": sign_rule,
        "hits": hits,
        "hit_rate": hits / rows_n if rows_n else None,
        "constant_negative_hit_rate_on_same_rows": negatives / rows_n if rows_n else None,
        "beats_constant_negative_on_these_rows": (hits > negatives) if enough else None,
        "status": "描述性统计" if enough else "证据不足",
        "promoted": False,
    }


def _decision_day(row: dict[str, Any]) -> str | None:
    """Later of the prediction timestamp and the information cutoff.

    Ordering by only one of them can place a decision made on a later date into
    an earlier research slice.
    """
    days = [day for day in (shanghai_day(row.get("as_of")), shanghai_day(row.get("information_cutoff"))) if day]
    return max(days) if days else None


def _nonzero_rule_row(row: dict[str, Any]) -> bool:
    score = _safe_float(row.get("rule_score"))
    return row.get("score_type") == "rule_conformance_score" and score is not None and abs(score) > SCORE_EPSILON


def _out_of_sample(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Chronological sign-rule comparison with label windows purged at the cut.

    No threshold is searched. The stored exit date is the fifth trading-session
    close. A reference label is kept only when that close is strictly before
    the test boundary. Beating constant-negative here is not a promotion, and
    the comparison stays insufficient until the test segment itself has the
    production monitoring block count.
    """
    minimum = monitoring_block_minimum()
    dated = []
    for row in rows:
        day = _decision_day(row)
        if day:
            dated.append((day, row))
    dates = sorted({day for day, _ in dated})
    base = {
        "method": "chronological_last_30_percent_of_decision_dates",
        "test_start_rule": "sorted_decision_dates[floor(n * 0.7)]",
        "decision_date": "max(as_of, information_cutoff) in Asia/Shanghai",
        "purge": "reference row is removed unless the fifth-session exit_date is strictly before test_start",
        "embargo_sessions": 5,
        "embargo_unit": "trading_sessions_represented_by_settlement_entry_and_exit_dates",
        "minimum_monitoring_blocks": minimum,
        "in_sample_threshold_search": "not_performed",
        "selection_performed": False,
        "selected_rule": None,
        "promoted": False,
        "calibrated_probability_available_for_production": False,
        "production_weights_unchanged": True,
        "predictive_improvement_claim_allowed": False,
        "insufficient_evidence": True,
    }
    if len(dates) < 6:
        return {
            **base,
            "status": "证据不足",
            "reason": "时间外比较至少需要6个不同的决策日，并且不在样本内搜索阈值。",
            "distinct_cutoff_dates": len(dates),
        }
    test_start = dates[int(len(dates) * 0.7)]
    reference: list[dict[str, Any]] = []
    purged: list[dict[str, Any]] = []
    test: list[dict[str, Any]] = []
    for day, row in dated:
        if day >= test_start:
            test.append(row)
            continue
        exit_day = shanghai_day((row.get("settlement") or {}).get("exit_date"))
        entry_day = shanghai_day((row.get("settlement") or {}).get("entry_date"))
        if exit_day is not None and entry_day is not None and exit_day < test_start and entry_day < test_start:
            reference.append(row)
        else:
            purged.append(row)
    names = ["constant_negative", "constant_positive", "ensemble_positive_sign"]
    discovered: set[str] = set()
    for _day, row in dated:
        scores = row.get("submodel_scores")
        if isinstance(scores, dict):
            discovered.update(str(key) for key in scores)
    names.extend(f"submodel_positive_sign:{name}" for name in sorted(discovered))
    test_gate_rows = [row for row in test if _nonzero_rule_row(row)]
    test_blocks = _nonoverlapping_blocks(test_gate_rows)["blocks"]
    test_classes = {bool(row["settlement"]["event_occurred"]) for row in test_gate_rows}
    insufficient = len(test_blocks) < minimum or len(test_gate_rows) < 2 or len(test_classes) < 2
    reference_rules = [_rule_metrics(reference, rule) for rule in names]
    test_rules = [_rule_metrics(test, rule) for rule in names]
    if insufficient:
        for rule in (*reference_rules, *test_rules):
            rule["status"] = "证据不足"
            rule["beats_constant_negative_on_these_rows"] = None
    return {
        **base,
        "insufficient_evidence": insufficient,
        "status": "证据不足" if insufficient else "时间外描述性对照；不是已验证的预测改进",
        "reason": (
            _monitoring_gate_reason(len(test_blocks)) + "接近零的规则分不进入符号规则分母，也不增加监测日期块。"
            if insufficient
            else "规则和符号阈值在看测试标签之前就已固定。即便测试段高于常数看空，也不能升级生产权重。"
        ),
        "distinct_cutoff_dates": len(dates),
        "test_start": test_start,
        "reference_rows_after_purge": len(reference),
        "purged_rows": len(purged),
        "test_rows": len(test),
        "test_nonzero_rule_rows": len(test_gate_rows),
        "test_nonoverlapping_date_blocks": len(test_blocks),
        "in_sample_reference_not_a_result": {
            "label": "样本内参照只用于核对，不能拿来挑选规则或宣称改进",
            "rules": reference_rules,
        },
        "out_of_sample_descriptive": {
            "label": "测试段只作描述。符号规则与常数看空比的是同一批非零分数；本模块不据此选择或发布候选。",
            "rules": test_rules,
        },
    }


def _cohorts(
    rows: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    all_ids: set[str],
    research_ids: set[str],
) -> dict[str, Any]:
    by_id: dict[str, tuple[str, str]] = {}
    conflicts: set[str] = set()
    invalid = 0
    for item in annotations:
        prediction_id = item.get("prediction_id")
        bucket = item.get("cohort_bucket")
        provenance = item.get("cohort_provenance")
        if not isinstance(prediction_id, str) or not prediction_id.strip():
            invalid += 1
            continue
        if not isinstance(bucket, str) or not bucket.strip() or not isinstance(provenance, str) or not provenance.strip():
            invalid += 1
            continue
        payload = (bucket.strip(), provenance.strip())
        key = prediction_id.strip()
        if key in by_id and by_id[key] != payload:
            conflicts.add(key)
        by_id[key] = payload
    provenance_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    ambiguous = 0
    legacy = 0
    annotated = 0
    for row in rows:
        prediction_id = str(row.get("prediction_id") or "")
        record_cohort = row.get("research_cohort") if isinstance(row.get("research_cohort"), dict) else None
        record_payload = None
        partial_record_cohort = False
        if record_cohort is not None:
            bucket = record_cohort.get("cohort_bucket")
            provenance = record_cohort.get("cohort_provenance")
            if isinstance(bucket, str) and bucket.strip() and isinstance(provenance, str) and provenance.strip():
                record_payload = (bucket.strip(), provenance.strip())
            else:
                partial_record_cohort = True
        external = by_id.get(prediction_id)
        if prediction_id in conflicts or (external and record_payload and external != record_payload):
            ambiguous += 1
            provenance_counts["ambiguous"] += 1
            bucket_counts["ambiguous"] += 1
            continue
        chosen = external or record_payload
        if chosen is None:
            if partial_record_cohort:
                ambiguous += 1
                provenance_counts["ambiguous"] += 1
                bucket_counts["ambiguous"] += 1
            else:
                legacy += 1
                provenance_counts["legacy_unspecified"] += 1
                bucket_counts["unspecified"] += 1
            continue
        annotated += 1
        provenance_counts[chosen[1]] += 1
        bucket_counts[chosen[0]] += 1
    unmatched = sum(1 for key in by_id if key not in all_ids)
    outside_denominator = sum(1 for key in by_id if key in all_ids and key not in research_ids and key not in conflicts)
    return {
        "backfilled_legacy_records": False,
        "legacy_unspecified_rows": legacy,
        "annotated_rows": annotated,
        "ambiguous_rows": ambiguous,
        "invalid_annotation_objects": invalid,
        "unmatched_annotation_count": unmatched,
        "annotations_outside_research_denominator": outside_denominator,
        "rows_by_provenance": dict(sorted(provenance_counts.items())),
        "rows_by_bucket": dict(sorted(bucket_counts.items())),
        "note": "没有 cohort 字段的旧记录保持 legacy_unspecified。诊断不会把它们回填成持仓、股票池或随机样本。",
    }


def _duckdb_counts(path: Path) -> dict[str, Any]:
    try:
        import duckdb
    except ImportError:
        return {
            "status": "unavailable",
            "matches_python": None,
            "persistent_sidecar_written": False,
            "reason": "未安装 duckdb。Python 读取是权威结果；服务启动不依赖这个可选包。",
        }
    try:
        connection = duckdb.connect(database=":memory:")
        try:
            rows = connection.execute(
                """
                SELECT COALESCE(CAST(event_type AS VARCHAR), '<missing>') AS event_type,
                       COUNT(*)::BIGINT AS n
                FROM read_ndjson(?)
                GROUP BY 1
                ORDER BY 1
                """,
                [str(path)],
            ).fetchall()
        finally:
            connection.close()
    except Exception as exc:
        return {
            "status": "error",
            "matches_python": False,
            "persistent_sidecar_written": False,
            "authoritative": "python",
            "reason": f"{type(exc).__name__}: DuckDB 投影失败，已忽略。",
        }
    return {
        "status": "ok",
        "counts": {str(name): int(count) for name, count in rows},
        "persistent_sidecar_written": False,
        "note": "只在内存里按事件类型交叉计数，不把私人台账复制成 DuckDB 文件。",
    }


def _engine_report(path: Path | None, engine: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    python_counts = dict(sorted(Counter(str(event.get("event_type") or "<missing>") for event in events).items()))
    report: dict[str, Any] = {
        "requested": engine,
        "authoritative": "python",
        "python_event_type_counts": python_counts,
        "vectorbt_used": False,
        "quantstats_used": False,
        "persistent_sidecar_written": False,
    }
    if path is None or engine == "python":
        report["duckdb"] = {"status": "not_requested", "persistent_sidecar_written": False}
        return report
    if engine == "auto" and path.stat().st_size < DUCKDB_AUTO_BYTES:
        report["duckdb"] = {
            "status": "skipped_small_file",
            "threshold_bytes": DUCKDB_AUTO_BYTES,
            "persistent_sidecar_written": False,
        }
        return report
    duckdb_report = _duckdb_counts(path)
    if duckdb_report.get("status") == "ok":
        duckdb_report["matches_python"] = duckdb_report.get("counts") == python_counts
        if not duckdb_report["matches_python"]:
            duckdb_report["authoritative"] = "python"
            duckdb_report["warning"] = "DuckDB 事件计数与 Python 不一致，以 Python 为准。"
    report["duckdb"] = duckdb_report
    return report


def _paused_report(events: list[dict[str, Any]], integrity: dict[str, Any], cutoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "read_only": True,
        "scoring_paused": True,
        "audit_rewritten": False,
        "historical_settlement_rewritten": False,
        "production_weights_unchanged": True,
        "candidate_promoted": False,
        "calibrated_probability_available_for_production": False,
        "execution_simulation": "not_applied",
        "predictive_improvement_claim_allowed": False,
        "direction_accuracy_is_not_buy_win_rate": True,
        "direction_accuracy_is_not_net_pnl": True,
        "full_file_events": len(events),
        "integrity": integrity,
        "cutoff": cutoff,
        "direction": {"status": "哈希链异常，评分已暂停", "direction_hit_rate": None, "roc_auc": None},
        "net_pnl": None,
        "limitations": [
            "哈希链异常，已停止评分。",
            "当前文件事件数和某次旧报告不同，本身不能证明损坏；损坏以哈希链为准。",
        ],
    }


def build_research_report(
    events: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    annotations: list[dict[str, Any]] | None = None,
    source_path: Path | None = None,
    engine: str = "auto",
) -> dict[str, Any]:
    integrity = chain_integrity(events)
    sliced, cutoff = _slice_events(events, as_of)
    if not integrity["valid"]:
        report = _paused_report(events, integrity, cutoff)
        report["engine"] = _engine_report(source_path, engine, events)
        return report
    records, fold_meta = fold_records(sliced)
    multiple_ids = fold_meta.pop("_multiple_ids")
    mode_counts = Counter(record.get("mode") if record.get("mode") in {"formal", "experiment"} else "legacy_mode_unspecified" for record in records)
    deduped = _dedupe_formal(records)
    decision_filter = _apply_decision_as_of(deduped["rows"], cutoff.get("as_of"))
    research_rows = decision_filter["rows"]
    cutoff["excluded_decision_after_report_as_of"] = decision_filter["excluded_decision_after_report_as_of"]
    cutoff["excluded_missing_decision_date"] = decision_filter["excluded_missing_decision_date"]
    labels = [_classify_label(record, multiple_ids, cutoff.get("as_of")) for record in research_rows]
    label_counts = dict(sorted(Counter(labels).items()))
    resolvable = [record for record, label in zip(research_rows, labels) if label == "resolvable"]
    rule_rows = [record for record in resolvable if record.get("score_type") == "rule_conformance_score"]
    legacy_score_type = sum(1 for record in resolvable if record.get("score_type") is None)
    other_score_type = sum(1 for record in resolvable if record.get("score_type") not in {None, "rule_conformance_score"})
    direction = _direction_table(rule_rows)
    nonzero = direction["nonzero_production_definition"]
    inclusive = direction["all_finite"]
    monitoring_rows = [row for row in rule_rows if _nonzero_rule_row(row)]
    block_info = _nonoverlapping_blocks(monitoring_rows)
    blocks = block_info["blocks"]
    resolvable_blocks = _nonoverlapping_blocks(resolvable)["blocks"]
    overlap = _overlapping_pairs(monitoring_rows)
    minimum_blocks = monitoring_block_minimum()
    insufficient = len(blocks) < minimum_blocks or nonzero["rows"] < 2 or nonzero["status"] == "证据不足"
    corrections = sum(len(record.get("corrections") or []) for record in records)
    created = sum(event.get("event_type") == "prediction_created" for event in sliced)
    repeated = sum(event.get("event_type") == "prediction_repeated" for event in sliced)
    horizon_absent = sum(
        1
        for record in resolvable
        if not isinstance(record.get("target"), dict) or record["target"].get("horizon_sessions") is None
    )
    claim_blocked = (
        "历史台账上的描述统计和时间外符号规则对照都不是前瞻验证。"
        "五日窗口重叠、标的重复，且非重叠日期块通常远少于行数。"
        "方向命中率高于或低于常数看空，都不能换成买入胜率、净收益或生产权重。"
    )
    report = {
        "schema": SCHEMA,
        "read_only": True,
        "scoring_paused": False,
        "audit_rewritten": False,
        "historical_settlement_rewritten": False,
        "production_weights_unchanged": True,
        "candidate_promoted": False,
        "calibrated_probability_available_for_production": False,
        "execution_simulation": "not_applied",
        "net_pnl": None,
        "net_pnl_reason": "审计结算没有做成交模拟。方向命中率不能换算成净收益。",
        "score_type_interpretation": "rule_conformance_score_is_not_a_probability",
        "direction_accuracy_is_not_buy_win_rate": True,
        "direction_accuracy_is_not_net_pnl": True,
        "predictive_improvement_claim_allowed": False,
        "predictive_improvement_block_reason": claim_blocked,
        "target": {
            "target_id": TARGET_ID,
            "label_rule": "next_session_open_through_fifth_trading_session_close_gt_0",
            "definition": dict(TARGET_DEFINITION),
            "definition_modified": False,
        },
        "integrity": integrity,
        "event_count_difference_is_not_automatically_corruption": True,
        "cutoff": cutoff,
        "full_file_events": len(events),
        "engine": _engine_report(source_path, engine, events),
        "denominator_ladder": [
            {"name": "full_file_events", "count": len(events), "definition": "文件里的全部事件，包括 as-of 之后追加的事件"},
            {"name": "slice_events", "count": len(sliced), "definition": "本次分母使用的事件。设置 as-of 时只保留 recorded_at 的上海日期不晚于截止日的事件"},
            {"name": "slice_prediction_created", "count": created, "definition": "切片中的 prediction_created 条数。prediction_repeated 不是新预测"},
            {"name": "slice_prediction_repeated", "count": repeated, "definition": "重复查询，不增加正式预测分母"},
            {"name": "slice_materialized_predictions", "count": len(records), "definition": "按 prediction_id 折叠后的预测数"},
            {"name": "formal_mode", "count": mode_counts.get("formal", 0), "definition": "mode 明确为 formal 的预测"},
            {"name": "experiment_mode", "count": mode_counts.get("experiment", 0), "definition": "实验记录，不进入正式研究分母"},
            {"name": "legacy_mode_unspecified", "count": mode_counts.get("legacy_mode_unspecified", 0), "definition": "没有 mode 的旧记录。不回填成 formal，也不进入正式研究分母"},
            {"name": "excluded_duplicate_symbol_as_of_date", "count": deduped["excluded_duplicate_symbol_as_of_date"], "definition": "同一标的、同一 as_of 上海日期的正式记录只保留 sequence 最早的一条"},
            {"name": "excluded_missing_symbol_or_as_of", "count": deduped["excluded_missing_symbol_or_as_of"], "definition": "正式记录缺少 symbol 或 as_of，不能进入按日去重后的研究分母"},
            {"name": "excluded_decision_after_report_as_of", "count": decision_filter["excluded_decision_after_report_as_of"], "definition": "事件 recorded_at 已可见，但预测 as_of 或 information_cutoff 晚于报告日"},
            {"name": "excluded_missing_decision_date", "count": decision_filter["excluded_missing_decision_date"], "definition": "设置了 as-of，但预测缺少 as_of 或 information_cutoff，不能证明没有穿越"},
            {"name": "formal_research_rows", "count": len(research_rows), "definition": "去重后且决策日不晚于报告日的正式研究行。as_of 与 information_cutoff 在同一天不会因此被剔除"},
            {"name": "labels_not_knowable_at_as_of", "count": label_counts.get("label_not_knowable_at_as_of", 0), "definition": "五个交易日窗口的退出日或结算写入日晚于报告日，标签不进入方向分母"},
            {"name": "research_resolvable_v1", "count": len(resolvable), "definition": "目标、收益符号、入场日和退出日都能对上 forward_positive_return_5_sessions_v1，且在报告日已经可知"},
            {"name": "direction_nonzero_rule_score", "count": nonzero["rows"], "definition": "生产报告同口径：规则符合分绝对值大于 1e-12 的可解析行。接近零的分数不进入这个分母"},
        ],
        "same_calendar_day_as_information_cutoff": {
            "rows": _same_cutoff_day(research_rows),
            "excluded": False,
            "note": "as_of 与 information_cutoff 落在同一上海日期是正常的，不是重复记录剔除，也不是损坏。",
        },
        "symbol_namespace_ambiguous": _symbol_namespace_ambiguous(research_rows),
        "structural_warnings": {
            "duplicate_prediction_created": fold_meta["duplicate_prediction_created"],
            "multiple_settlement_predictions": fold_meta["multiple_settlement_predictions"],
            "corrections_not_applied_to_scores": corrections,
        },
        "sampling": {
            "design": "not_a_probability_sample",
            "daily_sample_lists_are_imperfect": True,
            "reasons": [
                "额外评估会在同一标的的同一 as_of 日留下多条记录；研究分母只保留事先规定的最早一条。",
                "持仓和股票池可能重叠。没有 cohort bucket 的旧记录不能回填来源。",
                "五日窗口重叠且标的重复，所以行数不是独立样本数。",
            ],
        },
        "labels": {
            "counts": label_counts,
            "horizon_sessions_absent_but_target_id_matches": horizon_absent,
            "legacy_score_type_rows_excluded_from_direction": legacy_score_type,
            "other_score_type_rows_excluded_from_direction": other_score_type,
            "execution_simulation_field_counts": _execution_counts(research_rows),
        },
        "direction": {
            "definition": "rule_score > 0 是否等于 event_occurred。接近零的分数在生产口径里弃权，不算看空。",
            "production_report_excludes_near_zero": True,
            "score_epsilon": SCORE_EPSILON,
            "zero_scores_are_not_in_the_direction_denominator": True,
            "denominators_match": nonzero["rows"] == inclusive["rows"],
            "nonzero_production_definition": nonzero,
            "all_finite_scores_nonpositive_means_not_up": inclusive,
            "beats_constant_negative_direction_baseline": nonzero["beats_constant_negative"],
            "outperformance_of_constant_negative_is_not_a_verified_edge": True,
            "insufficient_evidence": insufficient,
            "minimum_monitoring_blocks": minimum_blocks,
            "status": "证据不足" if insufficient else "描述性统计；不是买入胜率或净收益",
        },
        "class_balance": {
            "on_nonzero_direction_rows": {
                "positive_events": nonzero["positive_events"],
                "negative_events": nonzero["negative_events"],
                "positive_event_rate": nonzero["positive_event_rate"],
            },
            "note": "下跌事件占多数时，常数看空的方向命中率会很高。这不是可交易优势，也不能解释成模型失效以外的净收益。",
        },
        "auc": {
            "score_meaning": "rule_conformance_score",
            "probability": False,
            "ensemble_rule_score_direction_sample": nonzero["roc_auc"],
            "ensemble_rule_score_all_finite": inclusive["roc_auc"],
            "submodels": _submodel_table(rule_rows),
        },
        "actions": _action_report(resolvable),
        "overlap": {
            "horizon_sessions": 5,
            "horizon_unit": "trading_sessions_from_settlement_entry_date_through_exit_date",
            "minimum_monitoring_blocks": minimum_blocks,
            "monitoring_policy_version": REVIEW_POLICY["version"],
            "reference_blocks": int(REVIEW_POLICY["reference_blocks"]),
            "recent_window_blocks": int(REVIEW_POLICY["recent_window_blocks"]),
            "nonoverlapping_date_blocks": len(blocks),
            "resolvable_blocks_including_near_zero_scores": len(resolvable_blocks),
            "near_zero_scores_do_not_add_monitoring_blocks": True,
            "representative_rows_in_blocks": sum(block["representative_rows"] for block in blocks),
            "representative_rows_skipped_because_cutoff_falls_inside_prior_window": block_info["skipped_representatives"],
            "extra_rows_collapsed_on_same_symbol_and_cutoff_day": block_info["collapsed_same_symbol_cutoff_day"],
            "distinct_symbols": overlap["distinct_symbols"],
            "symbols_with_repeated_rows": overlap["symbols_with_repeated_rows"],
            "overlapping_same_symbol_window_pairs": overlap["overlapping_same_symbol_window_pairs"],
            "independent_sample_proxy": len(blocks),
            "row_count_is_not_independent_sample_size": True,
            "insufficient_evidence": len(blocks) < minimum_blocks or len(monitoring_rows) < 2,
            "block_bootstrap": _block_bootstrap(blocks),
            "note": "监测块只使用非零规则分。下一块的信息截止日必须晚于上一块第五个交易日退出日；截止日等于退出日仍然重叠。接近零的分数不把块数凑过生产门槛。",
        },
        "cohorts": _cohorts(
            research_rows,
            annotations or [],
            {str(record.get("prediction_id")) for record in records},
            {str(record.get("prediction_id")) for record in research_rows},
        ),
        "out_of_sample_rule_comparison": _out_of_sample(rule_rows),
        "strategy_value": {
            "status": "尚未验证",
            "net_return": None,
            "reason": "市场事件标签不是策略收益。约束回测必须另附日线规格，并且不会回写这条审计结论。",
        },
        "limitations": [
            "规则符合分不是成功概率，也不会在这里被拟合成可上线概率。",
            "方向命中率不是买入胜率，也不是净收益。",
            "常数看空在下跌样本占多数时可以有更高的方向命中率，这不构成交易优势。",
            "同一标的同一 as_of 日的重复正式记录被剔除并单列；as_of 与 information_cutoff 同一天不会被剔除。",
            "样本不是随机样本。旧记录缺少 cohort bucket 时保持未指定，不回填。",
            "五个交易日窗口按结算记录的入场日到退出日重叠。证据门槛与生产监测相同：8个参考块加两个各8块的观察窗口，当前是24块。单个8块窗口不足。",
            "时间外规则比较不做阈值搜索，也不自动晋升候选。",
            "本报告不重写台账、不结算、不修改生产权重、不下单。",
            "费用只出现在单独的研究回测规格里，不读取账户费率文件。",
        ],
    }
    return report


def diagnose_audit_file(
    path: Path,
    *,
    as_of: str | None = None,
    cohort_annotations: Path | None = None,
    engine: str = "auto",
) -> dict[str, Any]:
    events = read_audit_events(path)
    annotations = read_annotations(cohort_annotations) if cohort_annotations else []
    report = build_research_report(events, as_of=as_of, annotations=annotations, source_path=path, engine=engine)
    report["audit_basename"] = path.name
    return report


def _json_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} 必须是 JSON 布尔值")


def run_research_backtest_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("回测规格必须是 JSON 对象")
    if _FORBIDDEN_SPEC_KEYS & set(spec):
        raise ValueError("回测规格不得引用账户费率、资产台账或个人数据路径")
    fees_raw = spec.get("fees")
    if not isinstance(fees_raw, dict) or any(key not in fees_raw for key in _REQUIRED_FEES):
        raise ValueError("必须显式提供 commission_rate、minimum_commission、stamp_duty_sell、transfer_fee_rate")
    fee_values = {}
    for key in _REQUIRED_FEES:
        value = _safe_float(fees_raw[key])
        if value is None or value < 0:
            raise ValueError(f"费率无效: {key}")
        fee_values[key] = value
    initial_cash = _safe_float(spec.get("initial_cash"))
    slippage_bps = _safe_float(spec.get("slippage_bps"))
    target_pct = _safe_float(spec.get("target_position_pct", 0.95))
    if initial_cash is None or initial_cash <= 0 or slippage_bps is None or slippage_bps < 0:
        raise ValueError("initial_cash 必须为正，slippage_bps 必须为非负")
    if target_pct is None or not 0 < target_pct <= 1:
        raise ValueError("target_position_pct 必须在 (0, 1] 内")
    if not isinstance(spec.get("bars"), list) or not spec["bars"]:
        raise ValueError("bars 必须是非空数组")
    if not isinstance(spec.get("signals"), dict):
        raise ValueError("signals 必须是日期到动作的对象")
    bars: list[DailyBar] = []
    seen_dates: set[date] = set()
    for item in spec["bars"]:
        if not isinstance(item, dict):
            raise ValueError("每根日线必须是对象")
        trading_date = date.fromisoformat(str(item["trading_date"]))
        if trading_date in seen_dates:
            raise ValueError("日线日期重复")
        if bars and trading_date <= bars[-1].trading_date:
            raise ValueError("日线必须按交易日严格递增，诊断不会悄悄排序")
        seen_dates.add(trading_date)
        previous = item.get("previous_close")
        limit_rate = item.get("limit_rate")
        bars.append(DailyBar(
            trading_date=trading_date,
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            volume=float(item.get("volume") or 0.0),
            previous_close=None if previous is None else float(previous),
            suspended=_json_bool(item.get("suspended", False), "suspended"),
            is_st=_json_bool(item.get("is_st", False), "is_st"),
            limit_rate=None if limit_rate is None else float(limit_rate),
        ))
    signals: dict[date, SignalAction] = {}
    for raw_day, raw_action in spec["signals"].items():
        signal_day = date.fromisoformat(str(raw_day))
        if signal_day not in seen_dates:
            raise ValueError(f"信号日期不在日线里: {raw_day}")
        try:
            signals[signal_day] = SignalAction(str(raw_action))
        except ValueError as exc:
            raise ValueError(f"无法识别的信号动作: {raw_action}") from exc
    fees = FeeSchedule(
        commission_rate=fee_values["commission_rate"],
        minimum_commission=fee_values["minimum_commission"],
        stamp_duty_sell=fee_values["stamp_duty_sell"],
        transfer_fee_rate=fee_values["transfer_fee_rate"],
    )
    result = AShareBacktester(
        initial_cash=initial_cash,
        fees=fees,
        slippage_bps=slippage_bps,
    ).run(bars, signals, target_position_pct=target_pct)
    return {
        "schema": BACKTEST_SCHEMA,
        "read_only": True,
        "authority": "AShareBacktester",
        "vectorbt_used": False,
        "quantstats_used": False,
        "promoted_to_production": False,
        "production_weights_unchanged": True,
        "candidate_promoted": False,
        "fees_source": "explicit_spec_not_account_fees_file",
        "execution_simulation": "research_constrained_simulation",
        "uses_audit_direction_hit_as_pnl": False,
        "symbol": spec.get("symbol"),
        "parameters": {
            "initial_cash": initial_cash,
            "slippage_bps": slippage_bps,
            "target_position_pct": target_pct,
            "fees": fee_values,
        },
        "assumptions": result.assumptions or execution_assumptions(fees, slippage_bps, 100),
        "initial_cash": result.initial_cash,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "max_drawdown": result.max_drawdown,
        "ending_cash": result.ending_cash,
        "ending_quantity": result.ending_quantity,
        "trade_count": len(result.trades),
        "trades": [
            {
                "date": trade.trading_date.isoformat(),
                "side": trade.side.value,
                "quantity": trade.quantity,
                "price": trade.price,
                "fees": trade.fees,
            }
            for trade in result.trades
        ],
        "rejected_orders": list(result.rejected_orders),
        "limitations": [
            "这是单标的约束研究模拟，不是审计标签的成交重放。",
            "方向命中率不能换算成这里的净收益。",
            "未模拟分红、历史 ST 变更、队列位置和部分成交。",
            "VectorBT 与 QuantStats 未使用，也不能替代本回测器。",
            "结果不会写回生产策略或审计台账。",
        ],
    }


def _write_report(output: Path, text: str, protected: list[Path]) -> None:
    resolved = output.resolve()
    for item in protected:
        if item.resolve() == resolved:
            raise ValueError("拒绝把研究报告写回审计文件或注释文件")
    if output.suffix == ".jsonl":
        raise ValueError("拒绝把研究报告写成 jsonl，以免和追加式台账混淆")
    output.write_text(text + "\n", encoding="utf-8")


def run_research_command(args: Any) -> int:
    try:
        protected: list[Path] = []
        if args.research_command == "audit":
            protected.append(args.audit)
            if args.cohort_annotations is not None:
                protected.append(args.cohort_annotations)
            report = diagnose_audit_file(
                args.audit,
                as_of=args.as_of,
                cohort_annotations=args.cohort_annotations,
                engine=args.engine,
            )
        elif args.research_command == "backtest":
            protected.append(args.spec)
            try:
                spec = json.loads(args.spec.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("回测规格不是有效 JSON") from exc
            report = run_research_backtest_spec(spec)
        elif args.research_command == "candidates":
            from xiao_a.research_candidates import diagnose_candidates_file
            protected.append(args.audit)
            if args.cohort_annotations is not None:
                protected.append(args.cohort_annotations)
            if args.execution_spec is not None:
                protected.append(args.execution_spec)
            report = diagnose_candidates_file(
                args.audit,
                as_of=args.as_of,
                cohort_annotations=args.cohort_annotations,
                execution_spec=args.execution_spec,
            )
        else:
            raise ValueError("未知的研究子命令")
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if getattr(args, "output", None):
            _write_report(args.output, text, protected)
        else:
            print(text)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
