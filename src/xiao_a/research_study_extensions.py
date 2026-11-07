"""Frozen research-only relative target and market-state buckets.

These definitions are not production targets. They do not change
``TARGET_DEFINITION`` or the audit hash chain. A missing verified price is
unavailable. It is not filled from a regime score, an ETF, or a later download.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from xiao_a.prediction_audit import _canonical, _safe_float
from xiao_a.research_diagnostics import monitoring_block_minimum, shanghai_day


STUDY_DEFINITION_ID = "research-study-v2"
RELATIVE_TARGET_ID = "forward_excess_csi300_5_sessions_v1"
CSI300_INDEX = "SH.000300"
ETF_PROXIES = frozenset({"SH.510300", "SZ.159919", "SH.510310"})
STUDY_DEFINITION: dict[str, Any] = {
    "study_definition_id": STUDY_DEFINITION_ID,
    "relative_target_id": RELATIVE_TARGET_ID,
    "entry_price": "next trading session open after the information cutoff",
    "exit_price": "fifth synchronized trading session close",
    "event": "stock return minus CSI300 price-index return is greater than zero",
    "stock_basis": "corporate-action-verified comparable return; a bare qfq label is not verification",
    "index_instrument": "CSI300 price index SH.000300",
    "index_is_not_an_etf": True,
    "market_bucket": {
        "ma_sessions": 20,
        "return_sessions": 5,
        "ma_includes_cutoff_close": True,
        "strong": "close > ma20 AND five_session_close_return > 0",
        "weak": "close < ma20 AND five_session_close_return < 0",
        "sideways": "both inputs are present and the row is not strong or weak, including equality",
        "unavailable": "missing, unverified, not the cutoff session, or any bar after the cutoff",
    },
    "majority_out_of_sample": "training reference labels only",
    "hindsight_majority": "named descriptive ceiling, not a tradable baseline",
    "excess_return_is_not_stock_alpha": True,
    "past_baseline_does_not_prove_every_regime": True,
    "production_target_unchanged": "forward_positive_return_5_sessions_v1",
}


def study_definition_fingerprint() -> str:
    return hashlib.sha256(_canonical(STUDY_DEFINITION)).hexdigest()


def _bars_on_or_before(payload: dict[str, Any], cutoff: str) -> list[dict[str, Any]]:
    symbol = str(payload.get("symbol") or "")
    if symbol in ETF_PROXIES:
        raise ValueError("etf_proxy_refused")
    if symbol != CSI300_INDEX or payload.get("instrument") != "price_index" or payload.get("price_basis") != "price_index":
        raise ValueError("csi300_price_index_provenance_incomplete")
    if payload.get("provenance_verified") is not True:
        raise ValueError("index_provenance_not_verified")
    for field in ("source", "source_url", "fetched_at"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError("index_provenance_incomplete")
    if shanghai_day(payload.get("as_of")) != cutoff:
        raise ValueError("index_as_of_does_not_match_cutoff")
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("index_history_missing")
    parsed = []
    seen: set[str] = set()
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError("index_bar_invalid")
        day = shanghai_day(bar.get("trading_date"))
        close = _safe_float(bar.get("close"))
        if day is None or close is None or close <= 0:
            raise ValueError("index_bar_invalid")
        if day in seen:
            raise ValueError("index_duplicate_date")
        seen.add(day)
        if day > cutoff:
            raise ValueError("index_history_contains_bars_after_cutoff")
        parsed.append({"trading_date": day, "close": close, "open": _safe_float(bar.get("open"))})
    parsed.sort(key=lambda item: item["trading_date"])
    if parsed[-1]["trading_date"] != cutoff:
        raise ValueError("index_cutoff_session_missing")
    return parsed


def classify_market_bucket(payload: dict[str, Any], cutoff: str) -> dict[str, Any]:
    """Bucket CSI300 with the frozen rule. Incomplete provenance raises."""
    cutoff = date.fromisoformat(cutoff).isoformat()
    bars = _bars_on_or_before(payload, cutoff)
    history_hash = hashlib.sha256(_canonical({
        "symbol": CSI300_INDEX,
        "source": payload["source"],
        "source_url": payload["source_url"],
        "fetched_at": payload["fetched_at"],
        "as_of": cutoff,
        "bars": bars,
    })).hexdigest()
    base = {
        "study_definition_id": STUDY_DEFINITION_ID,
        "study_definition_fingerprint": study_definition_fingerprint(),
        "symbol": CSI300_INDEX,
        "source": payload["source"],
        "source_url": payload["source_url"],
        "fetched_at": payload["fetched_at"],
        "as_of": cutoff,
        "input_dates": [bar["trading_date"] for bar in bars],
        "history_sha256": history_hash,
        "retrospective": False,
        "not_fetched_after_cutoff": True,
    }
    if len(bars) < 20:
        return {**base, "bucket": "unavailable", "reason": "index_history_shorter_than_20_sessions", "close": None, "ma20": None, "five_session_close_return": None}
    closes = [bar["close"] for bar in bars]
    close = closes[-1]
    ma20 = sum(closes[-20:]) / 20
    five = close / closes[-6] - 1
    if close > ma20 and five > 0:
        bucket = "strong"
    elif close < ma20 and five < 0:
        bucket = "weak"
    else:
        bucket = "sideways"
    return {**base, "bucket": bucket, "reason": "frozen_rule", "close": close, "ma20": ma20, "five_session_close_return": five}


def _unavailable_bucket(reason: str, *, source_valid: bool) -> dict[str, Any]:
    return {
        "study_definition_id": STUDY_DEFINITION_ID,
        "study_definition_fingerprint": study_definition_fingerprint(),
        "symbol": CSI300_INDEX,
        "bucket": "unavailable",
        "reason": reason,
        "source_valid": source_valid,
        "provenance_verified": False,
        "verified_price_index": False,
        "etf_substitute": False,
        "close": None,
        "ma20": None,
        "five_session_close_return": None,
        "retrospective": False,
        "not_a_v1_fallback": True,
    }


def _malformed_index_reason(payload: dict[str, Any], cutoff: str) -> str | None:
    bars = payload.get("bars")
    if bars is None:
        return None
    if not isinstance(bars, list):
        return "index_bar_invalid"
    seen: set[str] = set()
    for bar in bars:
        if not isinstance(bar, dict):
            return "index_bar_invalid"
        day = shanghai_day(bar.get("trading_date"))
        close = _safe_float(bar.get("close"))
        if day is None or close is None or close <= 0:
            return "index_bar_invalid"
        if day in seen:
            return "index_duplicate_date"
        seen.add(day)
        if day > cutoff:
            return "index_history_contains_bars_after_cutoff"
    return None


def enroll_market_bucket(payload: dict[str, Any] | None, cutoff: str) -> dict[str, Any]:
    """Freeze v2 even when the index is missing. Malformed bars still fail."""
    cutoff = date.fromisoformat(cutoff).isoformat()
    if payload is None:
        return _unavailable_bucket("index_history_missing", source_valid=False)
    if not isinstance(payload, dict):
        raise ValueError("malformed_index_history")
    malformed = _malformed_index_reason(payload, cutoff)
    if malformed:
        raise ValueError(malformed)
    try:
        classified = classify_market_bucket(payload, cutoff)
    except ValueError as exc:
        return _unavailable_bucket(str(exc), source_valid=False)
    source_valid = classified.get("bucket") != "unavailable" or classified.get("reason") == "index_history_shorter_than_20_sessions"
    return {**classified, "source_valid": source_valid, "provenance_verified": True, "not_a_v1_fallback": True}


def public_index_observation(fetched: dict[str, Any] | None, *, error: str | None = None) -> dict[str, Any]:
    """Keep a public CSI300 read. Do not call it a verified price index or an ETF."""
    if error or not isinstance(fetched, dict):
        return {"payload": None, "reason": "index_fetch_failed", "error": error, "provenance_verified": False, "etf_substitute": False}
    symbol = str(fetched.get("symbol") or CSI300_INDEX)
    if symbol in ETF_PROXIES:
        raise ValueError("etf_proxy_refused")
    payload = {
        "symbol": CSI300_INDEX,
        "instrument": "public_kline",
        "price_basis": fetched.get("price_basis") if isinstance(fetched.get("price_basis"), str) and fetched.get("price_basis") else "unverified",
        "provenance_verified": False,
        "certified_price_index": False,
        "source": fetched.get("source"),
        "source_url": fetched.get("source_url"),
        "fetched_at": fetched.get("fetched_at"),
        "as_of": fetched.get("as_of"),
        "bars": fetched.get("bars") if isinstance(fetched.get("bars"), list) else [],
        "etf_substitute": False,
    }
    return {"payload": payload, "reason": "public_adapter_not_certified_price_index", "provenance_verified": False, "etf_substitute": False}


def assert_prospective_session(session_date: str, today: str) -> None:
    if date.fromisoformat(session_date) < date.fromisoformat(today):
        raise ValueError("past_session_cannot_become_prospective_study")


def relative_outcome(stock: dict[str, Any], index_points: dict[str, Any]) -> dict[str, Any]:
    """Same entry open and fifth close as the production horizon, minus CSI300."""
    unavailable = {
        "available": False,
        "event": None,
        "excess_return": None,
        "target_id": RELATIVE_TARGET_ID,
        "excess_return_is_not_stock_alpha": True,
        "inferred_from_market_regime": False,
    }
    if stock.get("corporate_actions_verified") is not True or stock.get("price_basis") not in {"qfq", "comparable_total_return"}:
        return {**unavailable, "reason": "stock_corporate_actions_not_verified"}
    for field in ("source", "fetched_at"):
        if not isinstance(stock.get(field), str) or not stock[field].strip():
            return {**unavailable, "reason": "stock_provenance_incomplete"}
    symbol = str(index_points.get("symbol") or "")
    if symbol in ETF_PROXIES:
        return {**unavailable, "reason": "etf_proxy_refused"}
    if symbol != CSI300_INDEX or index_points.get("price_basis") != "price_index" or index_points.get("provenance_verified") is not True:
        return {**unavailable, "reason": "csi300_endpoint_not_verified"}
    entry = shanghai_day(stock.get("entry_date"))
    exit_day = shanghai_day(stock.get("exit_date"))
    if entry is None or exit_day is None or entry != shanghai_day(index_points.get("entry_date")) or exit_day != shanghai_day(index_points.get("exit_date")):
        return {**unavailable, "reason": "dates_not_synchronized"}
    stock_entry = _safe_float(stock.get("entry_open"))
    stock_exit = _safe_float(stock.get("exit_close"))
    index_entry = _safe_float(index_points.get("entry_open"))
    index_exit = _safe_float(index_points.get("exit_close"))
    if None in {stock_entry, stock_exit, index_entry, index_exit} or min(stock_entry, stock_exit, index_entry, index_exit) <= 0:
        return {**unavailable, "reason": "endpoint_missing"}
    stock_return = stock_exit / stock_entry - 1
    index_return = index_exit / index_entry - 1
    excess = stock_return - index_return
    return {
        "available": True,
        "event": excess > 0,
        "excess_return": excess,
        "stock_return": stock_return,
        "index_return": index_return,
        "target_id": RELATIVE_TARGET_ID,
        "entry_date": entry,
        "exit_date": exit_day,
        "reason": "verified_synchronized_endpoints",
        "excess_return_is_not_stock_alpha": True,
        "inferred_from_market_regime": False,
    }


def _rate(hits: int, total: int) -> float | None:
    return hits / total if total else None


def _majority_label(events: list[bool]) -> bool | None:
    if not events:
        return None
    positives = sum(events)
    return positives >= (len(events) - positives)


def _baseline_block(events: list[bool], *, training_choice: bool | None, hindsight: bool | None) -> dict[str, Any]:
    positives = sum(events)
    total = len(events)
    negatives = total - positives
    return {
        "rows": total,
        "positive_events": positives,
        "all_positive_accuracy": _rate(positives, total),
        "all_negative_accuracy": _rate(negatives, total),
        "hindsight_majority_choice": hindsight,
        "hindsight_majority_accuracy": None if hindsight is None or not total else _rate(sum((hindsight) == event for event in events), total),
        "hindsight_majority_role": "descriptive_ceiling_not_tradable",
        "training_majority_choice": training_choice,
        "training_majority_accuracy": None if training_choice is None or not total else _rate(sum((training_choice) == event for event in events), total),
        "training_majority_uses_future_test_labels": False,
        "claim_allowed": False,
        "excess_return_is_not_stock_alpha": True,
    }


def baseline_report(events: list[bool], reference_events: list[bool] | None = None) -> dict[str, Any]:
    """Same-universe baselines. Out-of-sample majority sees only the reference list."""
    hindsight = _majority_label(events)
    training = _majority_label(reference_events if reference_events is not None else [])
    if reference_events is None:
        training = None
    return _baseline_block(events, training_choice=training, hindsight=hindsight)


def _split_reference(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dates = sorted({row["decision_date"] for row in rows if row.get("decision_date")})
    if len(dates) < 2:
        return rows, []
    test_start = dates[int(len(dates) * 0.7)]
    reference = [row for row in rows if row["decision_date"] < test_start and row["exit_date"] < test_start]
    test = [row for row in rows if row["decision_date"] >= test_start]
    return reference, test


def report_frozen_relative(manifest: dict[str, Any], labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the bucket frozen in the manifest. Do not recompute it from a later index file."""
    extension = manifest.get("study_extension")
    if not isinstance(extension, dict) or extension.get("study_definition_id") != STUDY_DEFINITION_ID:
        raise ValueError("study_definition_upgrade_refused")
    bucket = extension.get("bucket")
    prepared = []
    unavailable = 0
    for label in labels:
        if not isinstance(label, dict):
            unavailable += 1
            continue
        outcome = relative_outcome(label.get("stock") or {}, label.get("index") or {})
        if not outcome["available"]:
            unavailable += 1
            continue
        prepared.append({
            "prediction_id": label.get("prediction_id"),
            "decision_date": shanghai_day(label.get("information_cutoff")) or shanghai_day(label.get("as_of")),
            "exit_date": outcome["exit_date"],
            "entry_date": outcome["entry_date"],
            "relative_event": outcome["event"],
            "direction_event": label.get("direction_event") if isinstance(label.get("direction_event"), bool) else None,
            "information_cutoff": shanghai_day(label.get("information_cutoff")),
            "symbol": label.get("symbol"),
            "settlement": {"exit_date": outcome["exit_date"], "event_occurred": outcome["event"]},
            "rule_score": label.get("rule_score"),
            "submodel_scores": label.get("submodel_scores") if isinstance(label.get("submodel_scores"), dict) else {},
        })
    reference, test = _split_reference(prepared)
    reference_events = [row["relative_event"] for row in reference]
    from xiao_a.research_candidates import evaluate_candidate

    def action_rows(name: str, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in pool if evaluate_candidate(name, row)["action"] == "buy"]

    def one_bucket(pool: list[dict[str, Any]], *, use_training: bool) -> dict[str, Any]:
        evidence = bucket_evidence(pool)
        relative_events = [row["relative_event"] for row in pool]
        direction_known = [row for row in pool if row["direction_event"] is not None]
        training_direction = [row["direction_event"] for row in reference if row["direction_event"] is not None]
        report = {
            "eligible_relative_rows": len(pool),
            "evidence": evidence,
            "relative_baselines": baseline_report(relative_events, reference_events if use_training else None),
            "direction_baselines": baseline_report(
                [row["direction_event"] for row in direction_known],
                training_direction if use_training else None,
            ) if direction_known else {"rows": 0, "claim_allowed": False},
            "action_subsets": {},
        }
        for name in ("ensemble_baseline", "multi_factor_alone", "consensus_only"):
            selected = action_rows(name, pool)
            selected_reference = action_rows(name, reference)
            report["action_subsets"][name] = {
                "buy_rows": len(selected),
                "buy_coverage": (len(selected) / len(pool)) if pool else None,
                "relative_baselines": baseline_report(
                    [row["relative_event"] for row in selected],
                    [row["relative_event"] for row in selected_reference] if use_training else None,
                ),
            }
        if evidence["insufficient_evidence"]:
            report["relative_hit_rate"] = None
            report["direction_hit_rate"] = None
        return report

    return {
        "study_definition_id": STUDY_DEFINITION_ID,
        "study_definition_fingerprint": extension.get("study_definition_fingerprint"),
        "frozen_market_bucket": bucket,
        "bucket_recomputed_from_later_index": False,
        "relative_target_id": RELATIVE_TARGET_ID,
        "production_target_modified": False,
        "unavailable_labels": unavailable,
        "eligible_labels": len(prepared),
        "bucket_reports": {
            str(bucket): {
                "full_sample_descriptive": one_bucket(prepared, use_training=False),
                "out_of_sample_test": one_bucket(test, use_training=True),
            },
        },
        "limitations": [
            "超额收益没有剥离行业、风格或贝塔，不是个股阿尔法证明。",
            "样本内多数类只是命名过的描述上限。样本外多数类只使用更早的参照段。",
            "每个市场状态单独计算24个非重叠块。过去某一状态的基准率不能外推到所有时期。",
        ],
    }


def bucket_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from xiao_a.research_diagnostics import _nonoverlapping_blocks

    blocks = len(_nonoverlapping_blocks(rows)["blocks"]) if rows else 0
    minimum = monitoring_block_minimum()
    return {
        "rows": len(rows),
        "nonoverlapping_date_blocks": blocks,
        "minimum_monitoring_blocks": minimum,
        "insufficient_evidence": blocks < minimum,
        "claim_allowed": False,
    }
