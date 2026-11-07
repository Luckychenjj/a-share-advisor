"""Frozen retrospective candidate comparison. It does not write the audit.

Rules below copy the existing production action cuts. They are not fit on
outcomes. Multi-factor was already singled out from this research family, so
every split of the saved ledger is retrospective. A prospective shadow starts
only after the frozen cutoff, on rows that are not in this file.

Executable net PnL stays unavailable unless a separate spec proves a future
execution path. Label returns, endpoint prices, pre-prediction bars, false
suspension flags, and prediction-time quote limits are not that path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from xiao_a.backtest import AShareBacktester, FeeSchedule
from xiao_a.models import DailyBar, SignalAction
from xiao_a.prediction_audit import _canonical, _safe_float
from xiao_a.research_diagnostics import (
    SCORE_EPSILON,
    _apply_decision_as_of,
    _classify_label,
    _cohorts,
    _decision_day,
    _dedupe_formal,
    _json_bool,
    _nonoverlapping_blocks,
    _nonzero_rule_row,
    _slice_events,
    chain_integrity,
    fold_records,
    monitoring_block_minimum,
    read_annotations,
    read_audit_events,
    shanghai_day,
)


SCHEMA = "xiao_a_research_candidates_v1"
SPEC_ID = "research-candidate-v1"
PARTICIPANTS = ("cycle_trend", "leader_sentiment", "chan_structure", "multi_factor", "market_regime")
BUY_AT = 0.25
REDUCE_AT = -0.20
SELL_AT = -0.55
FEE_KEYS = ("commission_rate", "minimum_commission", "stamp_duty_sell", "transfer_fee_rate")

FROZEN_SPEC: dict[str, Any] = {
    "spec_id": SPEC_ID,
    "threshold_search": "not_performed",
    "thresholds_source": "strategies.core._action and AdaptiveEnsemble.combine",
    "buy_at": BUY_AT,
    "reduce_at": REDUCE_AT,
    "sell_at": SELL_AT,
    "score_epsilon": SCORE_EPSILON,
    "participants": list(PARTICIPANTS),
    "ensemble_baseline": "saved rule_score only; submodel scores do not vote",
    "multi_factor_alone": "saved submodel_scores.multi_factor only; missing or near zero abstains; components are not recomputed",
    "consensus_only": "at least two non-zero participant scores must share one strict sign; missing and near-zero scores are not votes; buy uses production action on the weakest participant",
    "regime_source": "strategy_signals.market_regime.metadata.regime plus available_inputs and invalidation_active; not inferred from later prices",
    "allows_short": False,
    "retrospective_because": "multi_factor was already identified from this ledger family; old slices are not prospective confirmation",
    "net_pnl": "unavailable unless a separate spec proves future execution bars, adjustment factors, tradability, and historical fee applicability",
}


def config_fingerprint() -> str:
    return hashlib.sha256(_canonical(FROZEN_SPEC)).hexdigest()


def production_action(score: float) -> str:
    if score >= BUY_AT:
        return "buy"
    if score <= SELL_AT:
        return "sell"
    if score <= REDUCE_AT:
        return "reduce"
    return "hold"


def _parse_signals(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        parsed = []
        for name, payload in value.items():
            if isinstance(payload, dict):
                item = dict(payload)
                item.setdefault("strategy", str(name))
                parsed.append(item)
        return parsed
    return []


def _signal_sources(record: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    sources = []
    own = _parse_signals(record.get("strategy_signals"))
    if own:
        sources.append(("strategy_signals", own))
    raw = record.get("raw_output")
    if isinstance(raw, dict):
        nested = _parse_signals(raw.get("strategy_signals"))
        if nested:
            sources.append(("raw_output.strategy_signals", nested))
    return sources


def _named_signal(signals: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [item for item in signals if str(item.get("strategy")) == name]
    if len(matches) == 1:
        return matches[0]
    return None


def contemporaneous_regime(record: dict[str, Any]) -> dict[str, Any]:
    """Read the saved regime label. Do not rebuild it from score or later prices."""
    found: list[tuple[str, str | None, Any, Any]] = []
    for source, signals in _signal_sources(record):
        item = _named_signal(signals, "market_regime")
        if item is None:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        regime = metadata.get("regime")
        regime_text = regime.strip() if isinstance(regime, str) and regime.strip() else None
        available = metadata.get("available_inputs")
        invalid = metadata.get("invalidation_active")
        found.append((source, regime_text, available if isinstance(available, list) else None, invalid if isinstance(invalid, bool) else None))
    if not found:
        return {
            "regime": None,
            "status": "missing",
            "available_inputs_present": False,
            "available_input_count": None,
            "invalidation_active": None,
            "source": None,
        }
    labels = {item[1] for item in found}
    if len(found) > 1 and len(labels) > 1:
        return {
            "regime": None,
            "status": "ambiguous",
            "available_inputs_present": False,
            "available_input_count": None,
            "invalidation_active": None,
            "source": "conflicting_strategy_signal_copies",
        }
    source, regime, available, invalid = found[0]
    if regime is None:
        status = "missing"
    else:
        status = "saved_metadata"
    return {
        "regime": regime,
        "status": status,
        "available_inputs_present": available is not None,
        "available_input_count": len(available) if available is not None else None,
        "invalidation_active": invalid,
        "source": source,
    }


def _participant_scores(record: dict[str, Any]) -> dict[str, float | None]:
    raw = record.get("submodel_scores") if isinstance(record.get("submodel_scores"), dict) else {}
    scores: dict[str, float | None] = {}
    for name in PARTICIPANTS:
        scores[name] = _safe_float(raw.get(name))
    return scores


def _call_from_score(score: float | None, *, missing_reason: str) -> dict[str, Any]:
    if score is None:
        return {"direction": "abstain", "action": "abstain", "score": None, "reason": missing_reason}
    if abs(score) <= SCORE_EPSILON:
        return {"direction": "abstain", "action": "abstain", "score": score, "reason": "near_zero"}
    return {
        "direction": "up" if score > 0 else "down",
        "action": production_action(score),
        "score": score,
        "reason": "production_action_thresholds",
    }


def evaluate_candidate(name: str, record: dict[str, Any]) -> dict[str, Any]:
    if name == "ensemble_baseline":
        return _call_from_score(_safe_float(record.get("rule_score")), missing_reason="missing_rule_score")
    if name == "multi_factor_alone":
        return _call_from_score(_participant_scores(record)["multi_factor"], missing_reason="missing_multi_factor")
    if name == "consensus_only":
        scores = _participant_scores(record)
        votes = [value for value in scores.values() if value is not None and abs(value) > SCORE_EPSILON]
        if len(votes) < 2:
            return {"direction": "abstain", "action": "abstain", "score": None, "reason": "insufficient_participants"}
        if any(value > 0 for value in votes) and any(value < 0 for value in votes):
            return {"direction": "abstain", "action": "abstain", "score": None, "reason": "disagreement"}
        weakest = min(votes) if votes[0] > 0 else max(votes)
        call = _call_from_score(weakest, missing_reason="insufficient_participants")
        call["reason"] = "unanimous_participants_weakest_score"
        call["participants"] = len(votes)
        return call
    raise ValueError(f"未知候选: {name}")


def _event(row: dict[str, Any]) -> bool:
    return bool(row["settlement"]["event_occurred"])


def _benchmark_call(name: str) -> dict[str, Any]:
    if name == "always_positive":
        return {"direction": "up", "action": "buy", "score": None, "reason": "benchmark"}
    if name == "always_negative":
        return {"direction": "down", "action": "sell", "score": None, "reason": "benchmark_no_short"}
    if name == "no_signal":
        return {"direction": "abstain", "action": "abstain", "score": None, "reason": "benchmark"}
    raise ValueError(name)


def _metrics(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    universe = len(rows)
    directional = [(row, call) for row, call in zip(rows, calls) if call["direction"] in {"up", "down"}]
    buys = [(row, call) for row, call in zip(rows, calls) if call["action"] == "buy"]
    actions: dict[str, int] = {}
    for call in calls:
        actions[call["action"]] = actions.get(call["action"], 0) + 1
    hits = sum((call["direction"] == "up") == _event(row) for row, call in directional)
    buy_hits = sum(_event(row) for row, _call in buys)
    positives = sum(_event(row) for row in rows)
    missed = sum(_event(row) and call["action"] != "buy" for row, call in zip(rows, calls))
    negatives = sum(not _event(row) for row, _call in directional)
    return {
        "universe_rows": universe,
        "directional_rows": len(directional),
        "abstentions": universe - len(directional),
        "coverage": len(directional) / universe if universe else None,
        "direction_hits": hits,
        "direction_accuracy": hits / len(directional) if directional else None,
        "constant_negative_direction_accuracy_on_same_rows": negatives / len(directional) if directional else None,
        "constant_positive_direction_accuracy_on_same_rows": (len(directional) - negatives) / len(directional) if directional else None,
        "eligible_action_rows": sum(count for action, count in actions.items() if action != "abstain"),
        "buy_rows": len(buys),
        "buy_coverage": len(buys) / universe if universe else None,
        "positive_event_precision": buy_hits / len(buys) if buys else None,
        "positive_event_precision_is_not_direction_accuracy": True,
        "universe_positive_event_rate": positives / universe if universe else None,
        "missed_positive_events": missed,
        "actions": dict(sorted(actions.items())),
        "opens_short": False,
    }


def _candidate_names() -> tuple[str, ...]:
    return ("ensemble_baseline", "multi_factor_alone", "consensus_only")


def _benchmark_names() -> tuple[str, ...]:
    return ("always_positive", "always_negative", "no_signal")


def _evaluate_universe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = {}
    for name in _candidate_names():
        calls = [evaluate_candidate(name, row) for row in rows]
        candidates[name] = _metrics(rows, calls)
    benchmarks = {}
    for name in _benchmark_names():
        call = _benchmark_call(name)
        benchmarks[name] = _metrics(rows, [dict(call) for _ in rows])
    return {"candidates": candidates, "benchmarks": benchmarks}


def _split_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    minimum = monitoring_block_minimum()
    dated = [(day, row) for row in rows if (day := _decision_day(row))]
    dates = sorted({day for day, _row in dated})
    base = {
        "minimum_monitoring_blocks": minimum,
        "training_choices": "none",
        "selected_candidate": None,
        "winner_chosen_from_outcomes": False,
        "retrospective_confirmation_only": True,
        "genuine_confirmation": "需要截止日之后、且未参与本次候选定义的新预测；本文件里的切片不是前瞻确认。",
        "predictive_improvement_claim_allowed": False,
        "insufficient_evidence": True,
    }
    if len(dates) < 6:
        return {**base, "status": "证据不足", "reason": "决策日少于6个，不能做带清洗的时间外对照。", "distinct_decision_dates": len(dates)}
    test_start = dates[int(len(dates) * 0.7)]
    reference = []
    purged = []
    test = []
    for day, row in dated:
        if day >= test_start:
            test.append(row)
            continue
        entry = shanghai_day((row.get("settlement") or {}).get("entry_date"))
        exit_day = shanghai_day((row.get("settlement") or {}).get("exit_date"))
        if entry and exit_day and entry < test_start and exit_day < test_start:
            reference.append(row)
        else:
            purged.append(row)
    gate_rows = [row for row in test if _nonzero_rule_row(row)]
    blocks = len(_nonoverlapping_blocks(gate_rows)["blocks"])
    insufficient = blocks < minimum
    reference_eval = _evaluate_universe(reference)
    test_eval = _evaluate_universe(test)
    return {
        **base,
        "status": "证据不足" if insufficient else "回顾性时间外描述；不是前瞻确认，也不选择候选",
        "reason": "测试段的非重叠日期块没有达到生产监测的24块门槛。" if insufficient else "阈值在看结果之前就固定。即使某个候选更高，也不晋升。",
        "distinct_decision_dates": len(dates),
        "test_start": test_start,
        "purge": "reference label entry and fifth-session exit must both be strictly before test_start",
        "reference_rows_after_purge": len(reference),
        "purged_rows": len(purged),
        "test_rows": len(test),
        "test_nonoverlapping_date_blocks": blocks,
        "insufficient_evidence": insufficient,
        "in_sample_reference_not_a_result": reference_eval,
        "retrospective_test_slice": test_eval,
    }


def _group_rows(rows: list[dict[str, Any]], key_fn: Any) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row)), []).append(row)
    report = []
    for key, items in sorted(groups.items()):
        positives = sum(_event(row) for row in items)
        report.append({
            "group": key,
            "rows": len(items),
            "positive_events": positives,
            "positive_event_rate": positives / len(items) if items else None,
            "confirmatory": False,
            "status": "探索性描述；不是验证结论" if len(items) >= 2 and len({_event(row) for row in items}) == 2 else "证据不足",
        })
    return report


def _magnitude_bin(row: dict[str, Any]) -> str:
    score = _safe_float(row.get("rule_score"))
    if score is None:
        return "missing"
    if abs(score) <= SCORE_EPSILON:
        return "near_zero"
    if score < 0:
        return "negative"
    if score < BUY_AT:
        return "below_buy_threshold"
    if score < 0.55:
        return "buy_band_below_0.55"
    return "at_least_0.55"


def _agreement_bin(row: dict[str, Any]) -> str:
    votes = [value for value in _participant_scores(row).values() if value is not None and abs(value) > SCORE_EPSILON]
    if len(votes) < 2:
        return "insufficient_participants"
    if any(value > 0 for value in votes) and any(value < 0 for value in votes):
        return "disagreement"
    return "unanimous"


def _missing_count(row: dict[str, Any]) -> str:
    scores = _participant_scores(row)
    missing = sum(value is None for value in scores.values())
    return str(missing)


def _buy_slice(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        recommendation = row.get("recommendation") if isinstance(row.get("recommendation"), dict) else {}
        action = recommendation.get(field)
        if isinstance(action, str) and action.strip().casefold() == "buy":
            selected.append(row)
    return selected


def _attribution(rows: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    slices = {}
    for field in ("model_action", "account_action"):
        chosen = _buy_slice(rows, field)
        slices[field] = {
            "rows": len(chosen),
            "confirmatory": False,
            "selected_after_looking_at_outcomes": False,
            "regime": _group_rows(chosen, lambda row: contemporaneous_regime(row)["regime"] or contemporaneous_regime(row)["status"]),
            "invalidation_active": _group_rows(chosen, lambda row: "missing" if contemporaneous_regime(row)["invalidation_active"] is None else str(contemporaneous_regime(row)["invalidation_active"]).lower()),
            "available_input_count": _group_rows(chosen, lambda row: "missing" if contemporaneous_regime(row)["available_input_count"] is None else str(contemporaneous_regime(row)["available_input_count"])),
            "signal_magnitude": _group_rows(chosen, _magnitude_bin),
            "submodel_agreement": _group_rows(chosen, _agreement_bin),
            "missing_participant_count": _group_rows(chosen, _missing_count),
            "cohorts": _cohorts(chosen, annotations, {str(row.get("prediction_id")) for row in rows}, {str(row.get("prediction_id")) for row in chosen}),
        }
    per_name = {}
    model_buys = _buy_slice(rows, "model_action")
    for name in PARTICIPANTS:
        missing = sum(_participant_scores(row)[name] is None for row in model_buys)
        per_name[name] = {"buy_rows": len(model_buys), "missing_scores": missing}
    return {
        "confirmatory": False,
        "exploratory": True,
        "uses_future_prices": False,
        "note": "分组使用保存下来的状态、分数和缺失情况。样本少的组只说明数量，不能当成已经验证的买点原因。",
        "slices": slices,
        "model_buy_missing_scores": per_name,
    }


def _next_day(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return (date.fromisoformat(value) + timedelta(days=1)).isoformat()
    except ValueError:
        return None


def select_universe(events: list[dict[str, Any]], *, as_of: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    integrity = chain_integrity(events)
    sliced, cutoff = _slice_events(events, as_of)
    if not integrity["valid"]:
        return [], {"scoring_paused": True, "integrity": integrity, "cutoff": cutoff}
    records, fold_meta = fold_records(sliced)
    multiple_ids = fold_meta.pop("_multiple_ids")
    filtered = _apply_decision_as_of(_dedupe_formal(records)["rows"], cutoff.get("as_of"))
    rows = [
        row for row in filtered["rows"]
        if _classify_label(row, multiple_ids, cutoff.get("as_of")) == "resolvable"
    ]
    return rows, {"scoring_paused": False, "integrity": integrity, "cutoff": cutoff, "fold": fold_meta}


def audit_net_pnl_gap(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Explain why this ledger cannot be turned into executable net PnL."""
    endpoint_rows = 0
    not_applied = 0
    snapshot_bar_rows = 0
    for row in rows:
        settlement = row.get("settlement") or {}
        if settlement.get("execution_simulation") == "not_applied" or settlement.get("execution_simulation") is None:
            not_applied += 1
        if settlement.get("entry_open") is not None and settlement.get("exit_close") is not None:
            endpoint_rows += 1
        snapshot = row.get("input_snapshot") if isinstance(row.get("input_snapshot"), dict) else {}
        bars = snapshot.get("bars") or snapshot.get("daily_bars")
        if isinstance(bars, list) and bars:
            snapshot_bar_rows += 1
    return {
        "status": "unavailable",
        "global_executable": False,
        "total_return": None,
        "max_drawdown": None,
        "uses_label_return_as_net_return": False,
        "endpoint_rows_seen": endpoint_rows,
        "endpoint_rows_used": 0,
        "snapshot_bar_rows_seen": snapshot_bar_rows,
        "snapshot_bar_rows_used": 0,
        "execution_simulation_not_applied_rows": not_applied,
        "reasons": [
            "execution_simulation_not_applied",
            "pre_prediction_or_snapshot_bars_are_not_an_execution_path",
            "endpoint_prices_and_five_extra_rows_are_a_coverage_proxy_not_a_full_path",
            "adjustment_factors_not_verified",
            "suspension_st_and_limit_flags_are_not_verified_tradability",
            "prediction_time_quote_limits_are_not_future_session_limits",
            "fee_history_applicability_not_verified",
        ],
    }


def _fee_values(raw: dict[str, Any]) -> dict[str, float] | None:
    values = {}
    for key in FEE_KEYS:
        value = _safe_float(raw.get(key))
        if value is None or value < 0:
            return None
        values[key] = value
    return values


def _load_fees(spec: dict[str, Any]) -> tuple[dict[str, float] | None, str | None]:
    if spec.get("fees_historically_applicable") is not True:
        return None, "fee_history_applicability_not_verified"
    inline = spec.get("fees") if isinstance(spec.get("fees"), dict) else None
    path = spec.get("fees_path")
    if path:
        try:
            loaded = json.loads(Path(str(path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, "fee_file_unreadable"
        if not isinstance(loaded, dict):
            return None, "fee_file_unreadable"
        inline = {key: loaded.get(key) for key in FEE_KEYS}
    if not isinstance(inline, dict):
        return None, "fees_required"
    values = _fee_values(inline)
    if values is None:
        return None, "fees_required"
    return values, None


def _validate_execution_bars(bars: list[Any]) -> str | None:
    if not isinstance(bars, list) or len(bars) < 2:
        return "execution_bars_missing"
    for item in bars:
        if not isinstance(item, dict):
            return "execution_bar_not_an_object"
        for field in ("trading_date", "open", "high", "low", "close", "previous_close", "suspended", "is_st", "limit_rate"):
            if field not in item:
                return f"missing_{field}"
        if _safe_float(item.get("previous_close")) is None or float(item["previous_close"]) <= 0:
            return "previous_close_not_positive"
        if _safe_float(item.get("limit_rate")) is None:
            return "limit_rate_required_per_future_session"
        if not isinstance(item.get("suspended"), bool) or not isinstance(item.get("is_st"), bool):
            return "tradability_flags_must_be_booleans"
    return None


def _prepare_symbol_path(symbol: str, payload: dict[str, Any], fees: dict[str, float], spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("path_proof") != "future_execution_bars" or spec.get("not_a_coverage_proxy") is not True:
        return {"symbol": symbol, "status": "unavailable", "reason": "execution_path_not_verified", "total_return": None, "max_drawdown": None}
    if spec.get("adjustment_factors_verified") is not True:
        return {"symbol": symbol, "status": "unavailable", "reason": "adjustment_factors_not_verified", "total_return": None, "max_drawdown": None}
    if spec.get("tradability_verified") is not True:
        return {"symbol": symbol, "status": "unavailable", "reason": "tradability_not_verified", "total_return": None, "max_drawdown": None}
    if not isinstance(spec.get("price_basis"), str) or not spec["price_basis"].strip():
        return {"symbol": symbol, "status": "unavailable", "reason": "price_basis_required", "total_return": None, "max_drawdown": None}
    bars = payload.get("bars")
    problem = _validate_execution_bars(bars)
    if problem:
        return {"symbol": symbol, "status": "unavailable", "reason": problem, "total_return": None, "max_drawdown": None}
    signals = payload.get("signals")
    if not isinstance(signals, list) or not signals:
        return {"symbol": symbol, "status": "unavailable", "reason": "signals_missing", "total_return": None, "max_drawdown": None}
    parsed_bars: list[DailyBar] = []
    for item in bars:
        parsed_bars.append(DailyBar(
            trading_date=date.fromisoformat(str(item["trading_date"])),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            previous_close=float(item["previous_close"]),
            suspended=_json_bool(item["suspended"], "suspended"),
            is_st=_json_bool(item["is_st"], "is_st"),
            limit_rate=float(item["limit_rate"]),
        ))
    if any(parsed_bars[index].trading_date >= parsed_bars[index + 1].trading_date for index in range(len(parsed_bars) - 1)):
        return {"symbol": symbol, "status": "unavailable", "reason": "bars_not_strictly_increasing", "total_return": None, "max_drawdown": None}
    slippage = _safe_float(spec.get("slippage_bps"))
    cash = _safe_float(spec.get("initial_cash"))
    target = _safe_float(spec.get("target_position_pct"))
    if slippage is None or slippage < 0 or cash is None or cash <= 0 or target is None or not 0 < target <= 1:
        return {"symbol": symbol, "status": "unavailable", "reason": "cash_slippage_or_position_missing", "total_return": None, "max_drawdown": None}
    schedule: dict[date, SignalAction] = {}
    hold_day: date | None = None
    for signal in signals:
        if not isinstance(signal, dict):
            return {"symbol": symbol, "status": "unavailable", "reason": "signal_not_an_object", "total_return": None, "max_drawdown": None}
        try:
            decision_day = date.fromisoformat(str(signal["decision_date"]))
            action = SignalAction(str(signal["action"]))
        except (KeyError, ValueError):
            return {"symbol": symbol, "status": "unavailable", "reason": "signal_decision_or_action_invalid", "total_return": None, "max_drawdown": None}
        anchor = [bar for bar in parsed_bars if bar.trading_date == decision_day]
        future = [bar for bar in parsed_bars if bar.trading_date > decision_day]
        if not anchor:
            return {"symbol": symbol, "status": "unavailable", "reason": "decision_session_bar_missing", "total_return": None, "max_drawdown": None}
        if not future:
            return {"symbol": symbol, "status": "unavailable", "reason": "entry_bar_missing", "total_return": None, "max_drawdown": None}
        if decision_day in schedule and schedule[decision_day] != action:
            return {"symbol": symbol, "status": "unavailable", "reason": "ambiguous_same_day_signals", "total_return": None, "max_drawdown": None}
        schedule[decision_day] = action
        if action == SignalAction.BUY and hold_day is None:
            hold_day = decision_day
    fees = FeeSchedule(**fees)
    result = AShareBacktester(initial_cash=cash, fees=fees, slippage_bps=slippage).run(parsed_bars, schedule, target_position_pct=target)
    buy_hold = None
    if hold_day is not None:
        held = AShareBacktester(initial_cash=cash, fees=fees, slippage_bps=slippage).run(
            parsed_bars, {hold_day: SignalAction.BUY}, target_position_pct=target,
        )
        buy_hold = {
            "total_return": held.total_return,
            "max_drawdown": held.max_drawdown,
            "ending_cash": held.ending_cash,
            "ending_quantity": held.ending_quantity,
            "trade_count": len(held.trades),
            "rejected_orders": list(held.rejected_orders),
        }
    return {
        "symbol": symbol,
        "status": "simulated_per_symbol_not_global_pnl",
        "not_global_pnl": True,
        "not_a_substitute_for_the_audit_gap": True,
        "uses_label_return_as_net_return": False,
        "opens_short": False,
        "total_return": result.total_return,
        "max_drawdown": result.max_drawdown,
        "ending_cash": result.ending_cash,
        "ending_quantity": result.ending_quantity,
        "trade_count": len(result.trades),
        "trades": [{"date": trade.trading_date.isoformat(), "side": trade.side.value, "quantity": trade.quantity, "price": trade.price, "fees": trade.fees} for trade in result.trades],
        "rejected_orders": list(result.rejected_orders),
        "matched_buy_hold": buy_hold,
        "fill_rule": "signal_known_on_decision_session_fills_next_session_open",
    }


def evaluate_execution_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(spec, dict):
        return {"status": "not_supplied", "global_net_pnl": None, "total_return": None, "max_drawdown": None}
    if "snapshots" in spec or spec.get("coverage_proxy") is True:
        return {"status": "unavailable", "reason": "snapshot_stitch_refused", "total_return": None, "max_drawdown": None, "global_net_pnl": None}
    if spec.get("path_proof") != "future_execution_bars" or spec.get("not_a_coverage_proxy") is not True:
        return {"status": "unavailable", "reason": "execution_path_not_verified", "total_return": None, "max_drawdown": None, "global_net_pnl": None}
    if spec.get("adjustment_factors_verified") is not True:
        return {"status": "unavailable", "reason": "adjustment_factors_not_verified", "total_return": None, "max_drawdown": None, "global_net_pnl": None}
    if spec.get("tradability_verified") is not True:
        return {"status": "unavailable", "reason": "tradability_not_verified", "total_return": None, "max_drawdown": None, "global_net_pnl": None}
    fees, fee_problem = _load_fees(spec)
    if fee_problem or fees is None:
        return {"status": "unavailable", "reason": fee_problem or "fees_required", "total_return": None, "max_drawdown": None, "global_net_pnl": None}
    symbols = spec.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        return {"status": "unavailable", "reason": "symbols_missing", "total_return": None, "max_drawdown": None, "global_net_pnl": None}
    prepared = [_prepare_symbol_path(str(symbol), payload if isinstance(payload, dict) else {}, fees, spec) for symbol, payload in symbols.items()]
    return {
        "status": "per_symbol_only",
        "global_net_pnl": None,
        "global_executable": False,
        "reason": "shared_cash_across_symbols_not_simulated",
        "total_return": None,
        "max_drawdown": None,
        "uses_label_return_as_net_return": False,
        "fees_source": "user_supplied_local_input" if spec.get("fees_path") else "explicit_spec",
        "symbols": prepared,
    }


def build_candidate_report(
    events: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    annotations: list[dict[str, Any]] | None = None,
    execution_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows, context = select_universe(events, as_of=as_of)
    fingerprint = config_fingerprint()
    if context.get("scoring_paused"):
        return {
            "schema": SCHEMA,
            "scoring_paused": True,
            "config_fingerprint": fingerprint,
            "promoted": False,
            "production_weights_unchanged": True,
            "calibrated_probability_available_for_production": False,
            "predictive_improvement_claim_allowed": False,
            "audit_rewritten": False,
            "integrity": context["integrity"],
            "net_pnl": {"status": "unavailable", "total_return": None, "reason": "hash_chain_invalid"},
        }
    evaluation = _evaluate_universe(rows)
    decision_days = sorted({day for row in rows if (day := _decision_day(row))})
    shadow_basis = as_of or (decision_days[-1] if decision_days else None)
    annotations = annotations or []
    report = {
        "schema": SCHEMA,
        "read_only": True,
        "scoring_paused": False,
        "audit_rewritten": False,
        "promoted": False,
        "selected_candidate": None,
        "winner_chosen_from_outcomes": False,
        "production_weights_unchanged": True,
        "calibrated_probability_available_for_production": False,
        "predictive_improvement_claim_allowed": False,
        "retrospective_confirmation_only": True,
        "config_fingerprint": fingerprint,
        "spec": FROZEN_SPEC,
        "evaluation_cutoff": as_of,
        "prospective_shadow_start": _next_day(shadow_basis),
        "prospective_shadow_start_is_not_part_of_config_fingerprint": True,
        "universe": {
            "rows": len(rows),
            "symbols": len({str(row.get("symbol")) for row in rows}),
            "decision_dates": len(decision_days),
            "same_universe_for_every_candidate_and_benchmark": True,
        },
        "comparison": evaluation,
        "out_of_sample": _split_rows(rows),
        "buy_error_attribution": _attribution(rows, annotations),
        "net_pnl": audit_net_pnl_gap(rows),
        "execution_adapter": evaluate_execution_spec(execution_spec),
        "integrity": context["integrity"],
        "cutoff": context["cutoff"],
    }
    return report


def diagnose_candidates_file(
    path: Path,
    *,
    as_of: str | None = None,
    cohort_annotations: Path | None = None,
    execution_spec: Path | None = None,
) -> dict[str, Any]:
    events = read_audit_events(path)
    annotations = read_annotations(cohort_annotations) if cohort_annotations else []
    spec = None
    if execution_spec is not None:
        if execution_spec.resolve() == path.resolve():
            raise ValueError("执行规格不能就是审计文件")
        try:
            loaded = json.loads(execution_spec.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("执行规格不是有效 JSON") from exc
        if not isinstance(loaded, dict):
            raise ValueError("执行规格必须是 JSON 对象")
        spec = loaded
    report = build_candidate_report(events, as_of=as_of, annotations=annotations, execution_spec=spec)
    report["audit_basename"] = path.name
    return report
