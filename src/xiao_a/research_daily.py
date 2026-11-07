"""Daily prospective-study sidecars. They do not evaluate, settle, or schedule.

The sample manifest is written before any outcome. Prediction links are an
append-only sidecar and must carry the id, fingerprint, and snapshot reference
from the same evaluation. Shadow scores read those saved fields only. Evidence
collection fail-closes: existing quote and chart adapters do not prove future
tradability, official adjustment factors, or exchange limit prices.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from xiao_a.data.symbols import normalize_symbol
from xiao_a.prediction_audit import _canonical
from xiao_a.research_candidates import PARTICIPANTS, config_fingerprint, evaluate_candidate
from xiao_a.research_diagnostics import monitoring_block_minimum, shanghai_day


_SHANGHAI = timezone(timedelta(hours=8))
SCHEMA_MANIFEST = "xiao_a_sample_manifest_v1"
SCHEMA_LINK = "xiao_a_sample_link_v1"
SCHEMA_SHADOW = "xiao_a_shadow_freeze_v1"
SCHEMA_EVIDENCE = "xiao_a_evidence_v1"
SCHEMA_DAILY = "xiao_a_daily_status_v1"
SCHEMA_WEEKLY = "xiao_a_weekly_review_v1"
SCHEMA_RELATIVE = "xiao_a_relative_settlement_v1"
OUTPUT_FIELD_PATHS = {
    "enrollment_bucket": "<study>/<date>/manifest.json -> study_extension.bucket",
    "enrollment_index_bucket": "<study>/enrollment-index.json -> sessions[].frozen_market_bucket",
    "shadow_bucket": "<study>/<date>/shadow-freeze.json -> frozen_market_bucket",
    "shadow_score_bucket": "<study>/<date>/shadow-scores.jsonl -> frozen_market_bucket",
    "relative_outcome": "<study>/<date>/relative-settlements.jsonl -> outcome.excess_return",
    "relative_refusal": "<study>/<date>/relative-status.json -> reason",
    "weekly_bucket_gate": "<study>/weekly-reviews/<review-date>.json -> bucket_reports.<bucket>.evidence.minimum_monitoring_blocks",
    "evidence_bars": "<study>/evidence-snapshots/<SYMBOL>/<stamp>.json -> series[].bars",
    "index_snapshot": "<study>/index-snapshots/SH_000300/<stamp>.json -> price_basis, provenance_verified",
}
OFFLINE_HYPOTHESIS = {
    "id": "single-factor-offline-v1",
    "status": "not_applied",
    "automatic_production_update": False,
    "note": "单因子假设只保留版本号，供线下回顾。这里不挑选、不校准、不改生产。",
}
HORIZON_SESSIONS = 5
DEFAULT_RESEARCH_ROOT = Path(os.environ.get("XIAO_A_RESEARCH_DIR", "/workspace/xiao-a-research"))
REUSE_POLICY = (
    "刷新尚未覆盖决策日之后五个交易日的窗口；"
    "已存满五个后续交易日的价格快照可以复用，但未核实成交条件时不能标成永久可信；"
    "只有 verified_immutable 的快照才永久跳过。"
)


class StudyRefusal(ValueError):
    """The sidecar refused to overwrite frozen study state."""


def _now() -> str:
    return datetime.now(_SHANGHAI).isoformat(timespec="seconds")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _session_dir(study_dir: Path, session_date: str) -> Path:
    date.fromisoformat(session_date)
    path = study_dir / session_date
    if path.name == "prediction_audit" or path.suffix == ".jsonl":
        raise StudyRefusal("研究侧车不能写到审计台账路径")
    return path


def _manifest_paths(study_dir: Path, session_date: str) -> tuple[Path, Path]:
    root = _session_dir(study_dir, session_date)
    return root / "manifest.json", root / "manifest.lock"


@contextmanager
def study_lock(root: Path, timeout_seconds: float) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".lock"
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise StudyRefusal("研究侧车锁超时") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _canonical_symbol(value: str) -> str:
    return normalize_symbol(value).canonical


def _read_symbol_lines(path: Path) -> tuple[list[str], int]:
    symbols: list[str] = []
    rejected = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            symbol = _canonical_symbol(line.split()[0])
        except ValueError:
            rejected += 1
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols, rejected


def _load_universe(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    file_hash = _sha256_bytes(text.encode("utf-8"))
    stripped = text.lstrip()
    records: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {"as_of": None, "fetched_at": None, "source": "local_snapshot_file"}
    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(text)
        if isinstance(payload, dict):
            metadata["as_of"] = payload.get("as_of")
            metadata["fetched_at"] = payload.get("fetched_at")
            metadata["source"] = payload.get("source") or metadata["source"]
            rows = payload.get("securities") or payload.get("symbols") or []
        else:
            rows = payload
        if not isinstance(rows, list):
            raise StudyRefusal("股票池快照必须包含证券数组")
        for row in rows:
            if isinstance(row, str):
                records.append({"symbol": row})
            elif isinstance(row, dict) and row.get("symbol"):
                records.append(row)
    else:
        for line in text.splitlines():
            symbol = line.strip()
            if symbol and not symbol.startswith("#"):
                records.append({"symbol": symbol.split()[0]})
    normalized: list[dict[str, Any]] = []
    rejected = 0
    seen: set[str] = set()
    for row in records:
        try:
            symbol = _canonical_symbol(str(row.get("symbol")))
        except ValueError:
            rejected += 1
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        item = dict(row)
        item["symbol"] = symbol
        normalized.append(item)
    metadata.update(file_sha256=file_hash, rejected_rows=rejected, records=normalized)
    return metadata


def _known_exclusion(row: dict[str, Any]) -> list[str]:
    reasons = []
    name = str(row.get("name") or "")
    if row.get("is_st") is True or "ST" in name.upper():
        reasons.append("st")
    if row.get("delisted") is True or str(row.get("status") or "").lower() == "delisted":
        reasons.append("delisted")
    if row.get("suspended") is True:
        reasons.append("suspended")
    if "last_price" in row and row.get("last_price") in {0, 0.0}:
        reasons.append("zero")
    if "volume" in row and row.get("volume") in {0, 0.0}:
        reasons.append("zero")
    return reasons


def _holding_symbols(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not_supplied", "symbols": []}
    if not path.is_file():
        return {"status": "unavailable", "symbols": []}
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"status": "unreadable", "symbols": []}
    rows = payload.get("holdings") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and rows is None:
        rows = payload.get("positions")
    if not isinstance(rows, list):
        return {"status": "schema_unrecognized", "symbols": []}
    symbols: list[str] = []
    for item in rows:
        raw = item.get("symbol") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            continue
        try:
            symbol = _canonical_symbol(raw)
        except ValueError:
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return {"status": "read", "symbols": symbols}


def build_sample_manifest(
    *,
    session_date: str,
    pool_symbols: list[str],
    pool_sha256: str,
    pool_rejected: int,
    holdings: dict[str, Any],
    universe: dict[str, Any],
    random_count: int,
    seed: str,
    study_extension: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if random_count < 0:
        raise StudyRefusal("随机数量不能为负")
    session = date.fromisoformat(session_date)
    as_of = shanghai_day(universe.get("as_of"))
    stale = as_of is None or date.fromisoformat(as_of) < session
    unknown = ["st", "delisted", "zero", "suspended"]
    known_fields = set()
    for row in universe["records"]:
        if "is_st" in row or row.get("name"):
            known_fields.add("st")
        if "delisted" in row or "status" in row:
            known_fields.add("delisted")
        if "suspended" in row:
            known_fields.add("suspended")
        if "last_price" in row or "volume" in row:
            known_fields.add("zero")
    blocked = {symbol for symbol in pool_symbols}
    blocked.update(holdings["symbols"])
    eligible = []
    excluded_known = {"st": 0, "delisted": 0, "zero": 0, "suspended": 0}
    for row in universe["records"]:
        symbol = row["symbol"]
        if symbol in blocked:
            continue
        reasons = [reason for reason in _known_exclusion(row) if reason in known_fields or reason in {"st", "delisted", "zero", "suspended"}]
        if reasons:
            for reason in reasons:
                excluded_known[reason] = excluded_known.get(reason, 0) + 1
            continue
        eligible.append(symbol)
    eligible = sorted(set(eligible))
    rng = random.Random(seed)
    rng.shuffle(eligible)
    drawn = eligible[:random_count]
    membership: list[dict[str, Any]] = []
    for symbol in pool_symbols:
        buckets = ["fixed_pool"]
        if symbol in holdings["symbols"]:
            buckets.append("holding")
        membership.append({"symbol": symbol, "buckets": buckets})
    for symbol in holdings["symbols"]:
        if symbol not in pool_symbols:
            membership.append({"symbol": symbol, "buckets": ["holding"]})
    for symbol in drawn:
        membership.append({"symbol": symbol, "buckets": ["random"]})
    identity = {
        "schema": SCHEMA_MANIFEST,
        "session_date": session_date,
        "seed": seed,
        "random_algorithm": "random.Random.shuffle_of_sorted_eligible_v1",
        "random_count_requested": random_count,
        "random_count_drawn": len(drawn),
        "random_shortfall": max(0, random_count - len(drawn)),
        "pool": {"sha256": pool_sha256, "count": len(pool_symbols), "rejected_lines": pool_rejected, "symbols": pool_symbols},
        "holdings": {"status": holdings["status"], "count": len(holdings["symbols"]), "symbols": holdings["symbols"]},
        "universe": {
            "mode": "snapshot_file",
            "live_full_a_share_list": "unsupported",
            "file_sha256": universe["file_sha256"],
            "as_of": as_of,
            "fetched_at": universe.get("fetched_at"),
            "source": universe.get("source"),
            "count": len(universe["records"]),
            "rejected_rows": universe["rejected_rows"],
            "stale_list_reused": stale,
            "completeness": "not_claimed",
            "random_unbiasedness": "not_claimed",
        },
        "exclusions": {
            "random_draw_blocked_buckets": ["fixed_pool", "holding"],
            "applied_when_known": sorted(known_fields),
            "not_applied_because_unknown": sorted(set(unknown) - known_fields),
            "known_exclusion_counts": excluded_known,
            "suspended_false_is_not_future_tradability": True,
        },
        "membership": membership,
    }
    if study_extension is not None:
        identity["study_extension"] = study_extension
    identity["identity_sha256"] = _sha256_json(identity)
    return identity


def write_sample_manifest(study_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    path, lock = _manifest_paths(study_dir, manifest["session_date"])
    with study_lock(path.parent, 5):
        return _write_sample_manifest_locked(path, lock, manifest)


def _write_sample_manifest_locked(path: Path, lock: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if path.exists() or lock.exists():
        if not path.exists():
            raise StudyRefusal("样本清单锁存在但清单丢失，拒绝另写一套成员")
        current = _read_json(path)
        if current.get("identity_sha256") != manifest["identity_sha256"]:
            raise StudyRefusal("样本清单已冻结，不能在出结果后更换随机成员")
        if lock.exists() and lock.read_text(encoding="utf-8").strip() != manifest["identity_sha256"]:
            raise StudyRefusal("样本清单与锁定哈希不一致，拒绝相信后改的清单")
        if not lock.exists():
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, (manifest["identity_sha256"] + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        return {"status": "unchanged", "identity_sha256": manifest["identity_sha256"], "path": str(path.name)}
    _atomic_write(path, payload)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise StudyRefusal("样本清单锁已存在") from exc
    try:
        os.write(fd, (manifest["identity_sha256"] + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return {"status": "created", "identity_sha256": manifest["identity_sha256"], "path": str(path.name)}


def frozen_study_context(manifest: dict[str, Any]) -> dict[str, Any]:
    """Copy the bucket frozen at enrollment. Do not recompute it."""
    extension = manifest.get("study_extension") if isinstance(manifest.get("study_extension"), dict) else None
    if extension and extension.get("study_definition_id") == "research-study-v2":
        return {
            "study_definition_id": "research-study-v2",
            "study_definition_fingerprint": extension.get("study_definition_fingerprint"),
            "frozen_market_bucket": extension.get("bucket"),
            "frozen_market_bucket_reason": extension.get("reason"),
            "relative_target_id": "forward_excess_csi300_5_sessions_v1",
            "market_bucket_rule": "close_vs_ma20_including_cutoff_and_five_session_close_return",
            "bucket_recomputed_from_later_index": False,
            "production_target_modified": False,
        }
    return {
        "study_definition_id": manifest.get("study_definition_id") or "research-study-v1",
        "study_definition_fingerprint": None,
        "frozen_market_bucket": None,
        "frozen_market_bucket_reason": None,
        "relative_target_id": None,
        "market_bucket_rule": None,
        "bucket_recomputed_from_later_index": False,
        "production_target_modified": False,
    }


def load_frozen_manifest(study_dir: Path, session_date: str) -> dict[str, Any]:
    path, lock = _manifest_paths(study_dir, session_date)
    if not path.is_file() or not lock.is_file():
        raise StudyRefusal("缺少已冻结的样本清单")
    manifest = _read_json(path)
    identity = manifest["identity_sha256"]
    if lock.read_text(encoding="utf-8").strip() != identity:
        raise StudyRefusal("样本清单与锁定哈希不一致，拒绝相信后改的清单")
    check = dict(manifest)
    check.pop("identity_sha256", None)
    if _sha256_json(check) != identity:
        raise StudyRefusal("样本清单内容与自身哈希不一致")
    return manifest


def _extract_identity(response: dict[str, Any]) -> dict[str, Any]:
    audit = response.get("prediction_audit") if isinstance(response.get("prediction_audit"), dict) else {}
    prediction_id = response.get("prediction_id") or audit.get("prediction_id")
    fingerprint = response.get("fingerprint") or audit.get("fingerprint")
    snapshot_ref = response.get("snapshot_ref") or response.get("input_sha256") or audit.get("snapshot_ref") or audit.get("input_sha256")
    symbol = response.get("symbol") or audit.get("symbol")
    missing = [name for name, value in (("prediction_id", prediction_id), ("fingerprint", fingerprint), ("snapshot_ref", snapshot_ref)) if not isinstance(value, str) or not value.strip()]
    return {
        "prediction_id": prediction_id if isinstance(prediction_id, str) else None,
        "fingerprint": fingerprint if isinstance(fingerprint, str) else None,
        "snapshot_ref": snapshot_ref if isinstance(snapshot_ref, str) else None,
        "symbol": symbol if isinstance(symbol, str) else None,
        "confirmed": not missing,
        "missing_fields": missing,
    }


def snapshot_digest(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return the canonical snapshot hash, or a reason it cannot be verified."""
    snapshot = record.get("input_snapshot")
    declared = record.get("input_sha256")
    if not isinstance(declared, str) or not declared.strip():
        declared = None
    digest = hashlib.sha256(_canonical(snapshot)).hexdigest() if snapshot is not None else None
    if digest is not None and declared is not None and digest != declared:
        return None, "snapshot_hash_conflict"
    if digest is None:
        return None, "snapshot_body_missing"
    return digest, None


def _audit_created_events(path: Path, prediction_id: str) -> list[dict[str, Any]]:
    from xiao_a.research_diagnostics import read_audit_events
    return [
        event for event in read_audit_events(path)
        if event.get("event_type") == "prediction_created" and event.get("prediction_id") == prediction_id
    ]


def join_response_identity(response: dict[str, Any], audit_path: Path | None = None) -> dict[str, Any]:
    """Fill fingerprint and snapshot hash from one exact prediction id. Never guess by symbol or date."""
    identity = _extract_identity(response)
    if identity["confirmed"] or not identity["prediction_id"] or audit_path is None:
        return identity
    matches = _audit_created_events(audit_path, identity["prediction_id"])
    if len(matches) != 1:
        identity["confirmed"] = False
        identity["join_reason"] = "duplicate_prediction_id" if len(matches) > 1 else "prediction_id_not_in_audit"
        return identity
    event = matches[0]
    digest, problem = snapshot_digest(event)
    if problem or not isinstance(event.get("fingerprint"), str):
        identity["confirmed"] = False
        identity["join_reason"] = problem or "audit_fingerprint_missing"
        return identity
    try:
        audit_symbol = _canonical_symbol(str(event.get("symbol")))
    except ValueError:
        identity["confirmed"] = False
        identity["join_reason"] = "audit_symbol_invalid"
        return identity
    if identity["symbol"]:
        try:
            response_symbol = _canonical_symbol(identity["symbol"])
        except ValueError:
            identity["confirmed"] = False
            identity["join_reason"] = "response_symbol_invalid"
            return identity
        if response_symbol != audit_symbol:
            identity["confirmed"] = False
            identity["join_reason"] = "symbol_mismatch"
            identity["symbol"] = response_symbol
            return identity
    response_day = shanghai_day(response.get("as_of") or (response.get("decision") or {}).get("as_of") or (response.get("prediction_audit") or {}).get("as_of"))
    audit_day = shanghai_day(event.get("as_of") or event.get("information_cutoff"))
    if response_day and audit_day and response_day != audit_day:
        identity["confirmed"] = False
        identity["join_reason"] = "as_of_mismatch"
        return identity
    identity.update(
        fingerprint=event["fingerprint"],
        snapshot_ref=digest,
        symbol=audit_symbol,
        confirmed=True,
        missing_fields=[],
        joined_from_audit=True,
        join_reason=None,
    )
    return identity


def append_sample_link(
    study_dir: Path,
    session_date: str,
    response: dict[str, Any],
    *,
    timeout_seconds: float = 5,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    manifest = load_frozen_manifest(study_dir, session_date)
    root = _session_dir(study_dir, session_date)
    before = audit_path.read_bytes() if audit_path is not None and audit_path.is_file() else None
    identity = join_response_identity(response, audit_path)
    if before is not None and audit_path.read_bytes() != before:
        raise StudyRefusal("只读连接审计时审计文件被改写")
    try:
        symbol = _canonical_symbol(identity["symbol"]) if identity["symbol"] else None
    except ValueError:
        symbol = None
    planned = {item["symbol"] for item in manifest["membership"]}
    in_plan = symbol in planned if symbol else False
    record = {
        "schema": SCHEMA_LINK,
        "session_date": session_date,
        "manifest_sha256": manifest["identity_sha256"],
        "symbol": symbol,
        "prediction_id": identity["prediction_id"],
        "fingerprint": identity["fingerprint"],
        "snapshot_ref": identity["snapshot_ref"],
        "confirmed": bool(identity["confirmed"] and in_plan),
        "status": "confirmed" if identity["confirmed"] and in_plan else "ambiguous",
        "reason": None if identity["confirmed"] and in_plan else (identity.get("join_reason") or ("not_in_manifest" if identity["confirmed"] else "identity_incomplete")),
        "missing_fields": identity["missing_fields"],
        "linked_at": _now(),
        **frozen_study_context(manifest),
    }
    path = root / "links.jsonl"
    with study_lock(root, timeout_seconds):
        existing = _read_jsonl(path)
        same_id = [item for item in existing if item.get("prediction_id") and item.get("prediction_id") == record["prediction_id"]]
        if same_id:
            conflicts = [
                item for item in same_id
                if item.get("fingerprint") != record["fingerprint"] or item.get("snapshot_ref") != record["snapshot_ref"] or item.get("symbol") != record["symbol"]
            ]
            if conflicts or not record["confirmed"]:
                record["confirmed"] = False
                record["status"] = "ambiguous"
                record["reason"] = record["reason"] or "identity_conflict"
            elif any(item.get("confirmed") for item in same_id):
                return {"status": "unchanged", "confirmed": True, "prediction_id": record["prediction_id"]}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"status": "appended", "confirmed": record["confirmed"], "reason": record["reason"], "prediction_id": record["prediction_id"]}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _read_predictions(path: Path) -> dict[str, dict[str, Any]]:
    rows = _read_jsonl(path) if path.suffix == ".jsonl" else _read_json(path)
    if isinstance(rows, dict):
        rows = rows.get("predictions") or []
    found = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("prediction_id"), str):
            found[row["prediction_id"]] = row
    return found


def _code_ref() -> dict[str, Any]:
    try:
        import subprocess
        output = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], timeout=2, stderr=subprocess.DEVNULL)
        return {"status": "git", "rev": output.decode().strip()}
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable", "rev": None}


def linkage_exclusions(links: list[dict[str, Any]]) -> dict[str, str]:
    """Ids whose links conflict, plus every id in that symbol-day group."""
    excluded: dict[str, str] = {}
    grouped: dict[tuple[str, str], set[str]] = {}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        prediction_id = link.get("prediction_id")
        if not isinstance(prediction_id, str) or not prediction_id:
            continue
        by_id.setdefault(prediction_id, []).append(link)
        symbol = link.get("symbol")
        session = link.get("session_date")
        if isinstance(symbol, str) and symbol and isinstance(session, str) and session:
            grouped.setdefault((symbol, session), set()).add(prediction_id)
    for prediction_id, rows in by_id.items():
        identities = {(row.get("fingerprint"), row.get("snapshot_ref"), row.get("symbol")) for row in rows}
        if any(row.get("reason") == "identity_conflict" for row in rows) or len(identities) > 1:
            excluded[prediction_id] = "identity_conflict"
    for prediction_ids in grouped.values():
        if len(prediction_ids) > 1:
            for prediction_id in prediction_ids:
                excluded.setdefault(prediction_id, "duplicate_symbol_day")
    return excluded


def effective_shadow_scores(scores: list[dict[str, Any]], links: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Scored rows that later conflicts have not removed from the performance view."""
    excluded = linkage_exclusions(links)
    for row in scores:
        if row.get("status") == "invalidated" and isinstance(row.get("prediction_id"), str):
            excluded.setdefault(row["prediction_id"], str(row.get("reason") or "invalidated"))
    effective = []
    seen: set[str] = set()
    for row in scores:
        prediction_id = row.get("prediction_id")
        if row.get("status") != "scored" or not isinstance(prediction_id, str) or prediction_id in excluded or prediction_id in seen:
            continue
        seen.add(prediction_id)
        effective.append(row)
    return effective, excluded


def _invalidation_record(prediction_id: str, reason: str, links: list[dict[str, Any]], scores: list[dict[str, Any]]) -> dict[str, Any]:
    symbol = next((item.get("symbol") for item in links if item.get("prediction_id") == prediction_id and item.get("symbol")), None)
    if symbol is None:
        symbol = next((item.get("symbol") for item in scores if item.get("prediction_id") == prediction_id and item.get("symbol")), None)
    session = next((item.get("session_date") for item in links if item.get("prediction_id") == prediction_id), None)
    return {
        "prediction_id": prediction_id,
        "symbol": symbol,
        "session_date": session,
        "status": "invalidated",
        "reason": reason,
        "group": "symbol_day" if reason == "duplicate_symbol_day" else "prediction_id",
        "invalidates_prior_scores": True,
        "append_only": True,
    }


def _append_invalidations(handle: Any, links: list[dict[str, Any]], scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded = linkage_exclusions(links)
    already = {(row.get("prediction_id"), row.get("reason")) for row in scores if row.get("status") == "invalidated"}
    written = []
    for prediction_id, reason in sorted(excluded.items()):
        if (prediction_id, reason) in already:
            continue
        record = _invalidation_record(prediction_id, reason, links, scores)
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        written.append({"prediction_id": prediction_id, "reason": reason, "symbol": record.get("symbol")})
    return written


def freeze_shadow(study_dir: Path, session_date: str, predictions: Path, *, timeout_seconds: float = 5) -> dict[str, Any]:
    manifest = load_frozen_manifest(study_dir, session_date)
    root = _session_dir(study_dir, session_date)
    by_id = _read_predictions(predictions)
    links = [item for item in _read_jsonl(root / "links.jsonl") if item.get("manifest_sha256") == manifest["identity_sha256"]]
    freeze_path = root / "shadow-freeze.json"
    scores_path = root / "shadow-scores.jsonl"
    with study_lock(root, timeout_seconds):
        if freeze_path.exists():
            freeze = _read_json(freeze_path)
            context = frozen_study_context(manifest)
            if freeze.get("manifest_sha256") != manifest["identity_sha256"] or freeze.get("config_fingerprint") != config_fingerprint() or freeze.get("frozen_market_bucket") != context["frozen_market_bucket"]:
                raise StudyRefusal("影子配置已冻结，不能改用另一套清单或规则指纹")
        else:
            context = frozen_study_context(manifest)
            freeze = {
                "schema": SCHEMA_SHADOW,
                "session_date": session_date,
                "manifest_sha256": manifest["identity_sha256"],
                "config_fingerprint": config_fingerprint(),
                "code_ref": _code_ref(),
                "frozen_at": _now(),
                "prospective_shadow_start": session_date,
                "label_use": "forbidden_in_shadow_scores",
                "promoted": False,
                "calibrated_probability_available_for_production": False,
                **context,
            }
            _atomic_write(freeze_path, json.dumps(freeze, ensure_ascii=False, indent=2) + "\n")
        done = {item.get("prediction_id") for item in _read_jsonl(scores_path)}
        confirmed = [item for item in links if item.get("confirmed") and item.get("prediction_id")]
        counts: dict[tuple[str, str], set[str]] = {}
        for item in confirmed:
            counts.setdefault((str(item.get("symbol")), str(item.get("session_date"))), set()).add(str(item.get("prediction_id")))
        duplicate_ids = {prediction_id for ids in counts.values() if len(ids) > 1 for prediction_id in ids}
        conflict_ids = {str(item.get("prediction_id")) for item in links if item.get("reason") == "identity_conflict"}
        appended = 0
        ambiguous = 0
        with scores_path.open("a", encoding="utf-8") as handle:
            for link in links:
                prediction_id = link.get("prediction_id")
                if prediction_id in done:
                    continue
                if not link.get("confirmed") or prediction_id in duplicate_ids or prediction_id in conflict_ids:
                    ambiguous += 1
                    reason = link.get("reason") or ("duplicate_symbol_day" if prediction_id in duplicate_ids else "identity_conflict")
                    handle.write(json.dumps({"prediction_id": prediction_id, "status": "ambiguous", "reason": reason}, ensure_ascii=False) + "\n")
                    done.add(prediction_id)
                    appended += 1
                    continue
                record = by_id.get(prediction_id)
                digest, snapshot_problem = snapshot_digest(record or {})
                if record is None or record.get("fingerprint") != link.get("fingerprint") or snapshot_problem or digest != link.get("snapshot_ref"):
                    ambiguous += 1
                    if record is None:
                        reason = "missing_prediction"
                    elif record.get("fingerprint") != link.get("fingerprint"):
                        reason = "fingerprint_mismatch"
                    else:
                        reason = snapshot_problem or "snapshot_mismatch"
                    handle.write(json.dumps({"prediction_id": prediction_id, "status": "ambiguous", "reason": reason}, ensure_ascii=False) + "\n")
                    done.add(prediction_id)
                    appended += 1
                    continue
                try:
                    record_symbol = _canonical_symbol(str(record.get("symbol")))
                except ValueError:
                    record_symbol = None
                if record_symbol != link.get("symbol"):
                    ambiguous += 1
                    handle.write(json.dumps({"prediction_id": prediction_id, "status": "ambiguous", "reason": "symbol_mismatch"}, ensure_ascii=False) + "\n")
                    done.add(prediction_id)
                    appended += 1
                    continue
                decision_day = shanghai_day(record.get("as_of")) or shanghai_day(record.get("information_cutoff"))
                if decision_day is None or decision_day < freeze["prospective_shadow_start"]:
                    ambiguous += 1
                    handle.write(json.dumps({"prediction_id": prediction_id, "status": "ambiguous", "reason": "before_shadow_start"}, ensure_ascii=False) + "\n")
                    done.add(prediction_id)
                    appended += 1
                    continue
                saved = {
                    "rule_score": record.get("rule_score"),
                    "submodel_scores": {name: (record.get("submodel_scores") or {}).get(name) if isinstance(record.get("submodel_scores"), dict) else None for name in PARTICIPANTS},
                }
                score = {
                    "prediction_id": prediction_id,
                    "fingerprint": link.get("fingerprint"),
                    "snapshot_ref": link.get("snapshot_ref"),
                    "symbol": link.get("symbol"),
                    "status": "scored",
                    "config_fingerprint": freeze["config_fingerprint"],
                    "frozen_market_bucket": freeze.get("frozen_market_bucket"),
                    "relative_target_id": freeze.get("relative_target_id"),
                    "study_definition_id": freeze.get("study_definition_id"),
                    "calls": {name: evaluate_candidate(name, saved) for name in ("ensemble_baseline", "multi_factor_alone", "consensus_only")},
                }
                handle.write(json.dumps(score, ensure_ascii=False) + "\n")
                done.add(prediction_id)
                appended += 1
            handle.flush()
            invalidations = _append_invalidations(handle, links, _read_jsonl(scores_path))
            appended += len(invalidations)
    effective, excluded = effective_shadow_scores(_read_jsonl(scores_path), links)
    return {
        "status": "frozen",
        "appended": appended,
        "ambiguous": ambiguous,
        "config_fingerprint": freeze["config_fingerprint"],
        "prospective_shadow_start": freeze["prospective_shadow_start"],
        "frozen_market_bucket": freeze.get("frozen_market_bucket"),
        "relative_target_id": freeze.get("relative_target_id"),
        "study_definition_id": freeze.get("study_definition_id"),
        "effective_scored": len(effective),
        "invalidations": [{"prediction_id": prediction_id, "reason": reason} for prediction_id, reason in sorted(excluded.items())],
        "field_paths": OUTPUT_FIELD_PATHS,
    }


def _evidence_shell(symbol: str, session_date: str) -> dict[str, Any]:
    blockers = [
        "official_adjustment_factor_source_not_available",
        "historical_security_type_and_board_rules_unavailable",
        "historical_st_status_unavailable",
        "exchange_limit_prices_unavailable",
        "zero_volume_is_not_verified_suspension",
        "prediction_time_quote_is_not_future_tradability",
        "pre_prediction_bars_are_not_an_execution_path",
    ]
    return {
        "schema": SCHEMA_EVIDENCE,
        "symbol": symbol,
        "session_date": session_date,
        "raw_ohlc": {"status": "unavailable"},
        "adjusted_ohlc": {"status": "unavailable"},
        "adjustment_factor": {"status": "unavailable", "reason": blockers[0]},
        "historical_security_type": {"status": "unavailable", "reason": blockers[1]},
        "historical_st": {"status": "unavailable", "reason": blockers[2]},
        "exchange_limit_prices": {"status": "unavailable", "reason": blockers[3]},
        "suspension": {"status": "unverified", "reason": blockers[4]},
        "usable_for_executable_backtest": False,
        "blockers": blockers,
    }


def bounded_fetch(fetcher: Any, symbol: str, adjustment: str, timeout_seconds: float) -> dict[str, Any]:
    """Run one provider call in a forked child that the parent can terminate."""
    if timeout_seconds <= 0:
        return {"status": "unavailable", "error": "budget_exhausted", "timed_out": True}
    import tempfile
    handle = tempfile.NamedTemporaryFile(prefix="xiao-a-fetch-", delete=False)
    result_path = handle.name
    handle.close()
    pid = os.fork()
    if pid == 0:
        try:
            try:
                result = fetcher(symbol, adjustment)
                payload = {"ok": True, "result": result if isinstance(result, dict) else {"status": "unavailable", "error": "fetcher_returned_non_object"}}
            except Exception as exc:
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            Path(result_path).write_text(json.dumps(payload), encoding="utf-8")
        finally:
            os._exit(0)
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while True:
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if finished == pid:
            break
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            timed_out = True
            break
        time.sleep(0.005)
    try:
        if timed_out:
            return {"status": "unavailable", "error": "budget_exhausted", "timed_out": True}
        try:
            message = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "unavailable", "error": "child_result_unreadable", "timed_out": False}
        if not message.get("ok"):
            return {"status": "unavailable", "error": message.get("error") or "fetch_failed", "timed_out": False}
        result = message.get("result")
        return result if isinstance(result, dict) else {"status": "unavailable", "error": "fetcher_returned_non_object", "timed_out": False}
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass


def _stored_bars(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    bars = []
    for series in snapshot.get("series") or []:
        if isinstance(series, dict):
            bars.extend(bar for bar in series.get("bars") or [] if isinstance(bar, dict))
    return bars


def _window_covered(snapshots: list[dict[str, Any]], decision_date: str) -> bool:
    for snapshot in snapshots:
        future = [bar for bar in _stored_bars(snapshot) if str(bar.get("trading_date") or "") > decision_date]
        if snapshot.get("verified_immutable") is True and len(future) >= HORIZON_SESSIONS:
            return True
        if len(future) >= HORIZON_SESSIONS:
            return True
    return False


def _session_manifests(study_dir: Path, through_date: str) -> list[dict[str, Any]]:
    found = []
    if not study_dir.exists():
        return found
    for child in sorted(path for path in study_dir.iterdir() if path.is_dir()):
        try:
            date.fromisoformat(child.name)
        except ValueError:
            continue
        if child.name > through_date:
            continue
        try:
            found.append(load_frozen_manifest(study_dir, child.name))
        except StudyRefusal:
            continue
    return found


def pending_collection_targets(study_dir: Path, session_date: str) -> list[dict[str, Any]]:
    """Current membership plus earlier confirmed links whose five-session window is still open."""
    targets: dict[str, dict[str, Any]] = {}
    for manifest in _session_manifests(study_dir, session_date):
        day = manifest["session_date"]
        root = _session_dir(study_dir, day)
        for item in manifest["membership"]:
            slot = targets.setdefault(item["symbol"], {"symbol": item["symbol"], "sessions": [], "prediction_ids": []})
            if day not in slot["sessions"]:
                slot["sessions"].append(day)
        for link in _read_jsonl(root / "links.jsonl"):
            if not link.get("confirmed") or not link.get("symbol"):
                continue
            slot = targets.setdefault(link["symbol"], {"symbol": link["symbol"], "sessions": [], "prediction_ids": []})
            if day not in slot["sessions"]:
                slot["sessions"].append(day)
            if link.get("prediction_id") not in slot["prediction_ids"]:
                slot["prediction_ids"].append(link["prediction_id"])
    pending = []
    for symbol, slot in sorted(targets.items()):
        snapshots = _symbol_snapshots(study_dir, symbol)
        open_sessions = [day for day in slot["sessions"] if not _window_covered(snapshots, day)]
        if open_sessions:
            pending.append({**slot, "open_sessions": open_sessions})
    return pending


def _symbol_snapshots(study_dir: Path, symbol: str) -> list[dict[str, Any]]:
    folder = study_dir / "evidence-snapshots" / symbol.replace(".", "_")
    if not folder.exists():
        return []
    rows = []
    for path in sorted(folder.glob("*.json")):
        try:
            rows.append(_read_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def _series_from_fetch(fetched: dict[str, Any], requested_adjustment: str, captured_at: str) -> dict[str, Any]:
    reported = fetched.get("price_basis") if isinstance(fetched.get("price_basis"), str) and fetched.get("price_basis") else None
    bars = []
    for bar in fetched.get("bars") or []:
        if not isinstance(bar, dict):
            continue
        bars.append({
            "trading_date": bar.get("trading_date"),
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
        })
    return {
        "requested_adjustment": requested_adjustment,
        "actual_price_basis": reported or "unverified",
        "requested_adjustment_is_not_proof_of_basis": True,
        "status": fetched.get("status") or "unavailable",
        "source": fetched.get("source"),
        "source_url": fetched.get("source_url"),
        "captured_at": captured_at,
        "bars": bars,
        "error": fetched.get("error"),
        "exchange_limit_prices": "unavailable",
        "usable_for_executable_backtest": False,
    }


def collect_evidence(
    study_dir: Path,
    session_date: str,
    *,
    budget_seconds: float,
    fetcher: Any | None = None,
    fees_path: Path | None = None,
    retries: int = 2,
    timeout_seconds: float = 5,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    manifest = load_frozen_manifest(study_dir, session_date)
    root = _session_dir(study_dir, session_date)
    deadline = clock() + max(0.0, budget_seconds)
    targets = pending_collection_targets(study_dir, session_date)
    stopped = False
    written = 0
    reused = 0
    attempts = 0
    fees = {"path_supplied": fees_path is not None, "historically_applicable": False, "rates_copied_into_study": False, "sha256": None}
    if fees_path is not None and fees_path.is_file():
        blob = fees_path.read_bytes()
        fees["sha256"] = _sha256_bytes(blob)
        try:
            payload = json.loads(blob.decode("utf-8"))
            fees["historically_applicable"] = payload.get("historically_applicable") is True
        except (UnicodeDecodeError, json.JSONDecodeError):
            fees["historically_applicable"] = False
    pending_left = [item["symbol"] for item in targets]
    for target in targets:
        remaining = deadline - clock()
        if remaining <= 0:
            stopped = True
            break
        symbol = target["symbol"]
        if fetcher is None:
            snapshot = _evidence_shell(symbol, session_date)
            snapshot["series"] = []
            snapshot["blockers"].append("public_adapter_not_invoked")
            snapshot["fees"] = fees
            snapshot["reuse_policy"] = REUSE_POLICY
            _write_snapshot(study_dir, symbol, snapshot)
            written += 1
            pending_left.remove(symbol)
            continue
        series = []
        timed_out = False
        for adjustment in ("none", "qfq"):
            remaining = deadline - clock()
            if remaining <= 0:
                timed_out = True
                stopped = True
                break
            fetched = None
            for _attempt in range(max(1, retries)):
                remaining = deadline - clock()
                if remaining <= 0:
                    timed_out = True
                    stopped = True
                    break
                attempts += 1
                fetched = bounded_fetch(fetcher, symbol, adjustment, remaining)
                if fetched.get("timed_out"):
                    stopped = True
                    timed_out = True
                    break
                if fetched.get("status") != "unavailable":
                    break
            if fetched is None:
                break
            series.append(_series_from_fetch(fetched, adjustment, _now()))
            if fetched.get("timed_out"):
                break
        snapshot = _evidence_shell(symbol, session_date)
        snapshot["series"] = series
        snapshot["timed_out"] = timed_out
        snapshot["refresh_required"] = timed_out or not _window_covered([snapshot], min(target["open_sessions"]))
        snapshot["verified_immutable"] = False
        snapshot["fees"] = fees
        snapshot["reuse_policy"] = REUSE_POLICY
        snapshot["blockers"].append("adapter_prices_are_not_verified_execution_evidence")
        snapshot["net_pnl"] = {"status": "unavailable", "total_return": None}
        _write_snapshot(study_dir, symbol, snapshot)
        written += 1
        if symbol in pending_left and not snapshot["refresh_required"]:
            pending_left.remove(symbol)
        if stopped:
            break
    covered = [item["symbol"] for item in targets if item["symbol"] not in pending_left]
    status = {
        "schema": SCHEMA_EVIDENCE,
        "session_date": session_date,
        "manifest_sha256": manifest["identity_sha256"],
        "stopped_budget": stopped,
        "budget_seconds": budget_seconds,
        "symbols_total": len(targets),
        "symbols_selected": [item["symbol"] for item in targets],
        "symbols_written": written,
        "symbols_reused": reused,
        "pending_symbols": pending_left,
        "covered_symbols": covered,
        "fetcher_attempts": attempts,
        "usable_for_executable_backtest": 0,
        "probability_available": False,
        "reuse_policy": REUSE_POLICY,
        "fees": fees,
        "net_pnl": {"status": "unavailable", "total_return": None},
        **frozen_study_context(manifest),
        "field_paths": OUTPUT_FIELD_PATHS,
    }
    _atomic_write(root / "evidence-status.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    sessions = []
    for earlier in _session_manifests(study_dir, session_date):
        sessions.append({
            "session_date": earlier["session_date"],
            "manifest_sha256": earlier["identity_sha256"],
            "symbols": [item["symbol"] for item in earlier["membership"]],
            **frozen_study_context(earlier),
        })
    _atomic_write(study_dir / "enrollment-index.json", json.dumps({
        "through_date": session_date,
        "targets": targets,
        "pending_symbols": pending_left,
        "sessions": sessions,
        "derived_from_session_sidecars": True,
        "field_paths": OUTPUT_FIELD_PATHS,
    }, ensure_ascii=False, indent=2) + "\n")
    return status


def _write_snapshot(study_dir: Path, symbol: str, snapshot: dict[str, Any]) -> None:
    folder = study_dir / "evidence-snapshots" / symbol.replace(".", "_")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    _atomic_write(folder / f"{stamp}.json", json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")


def daily_status(study_dir: Path, session_date: str) -> dict[str, Any]:
    manifest = load_frozen_manifest(study_dir, session_date)
    root = _session_dir(study_dir, session_date)
    links = [item for item in _read_jsonl(root / "links.jsonl") if item.get("manifest_sha256") == manifest["identity_sha256"]]
    scores = _read_jsonl(root / "shadow-scores.jsonl")
    effective, excluded = effective_shadow_scores(scores, links)
    evidence = _read_json(root / "evidence-status.json") if (root / "evidence-status.json").exists() else None
    return {
        "schema": SCHEMA_DAILY,
        "session_date": session_date,
        "manifest_sha256": manifest["identity_sha256"],
        "planned": len(manifest["membership"]),
        "pool_count": manifest["pool"]["count"],
        "random_count_drawn": manifest["random_count_drawn"],
        "confirmed_links": sum(bool(item.get("confirmed")) for item in links),
        "ambiguous_links": sum(item.get("status") == "ambiguous" for item in links),
        "shadow_scored": len(effective),
        "shadow_ambiguous": sum(item.get("status") == "ambiguous" for item in scores),
        "invalidations": [{"prediction_id": prediction_id, "reason": reason} for prediction_id, reason in sorted(excluded.items())],
        "evidence": evidence,
        "relative_settlements": _relative_counts(root / "relative-settlements.jsonl"),
        "relative_label_reason": (_read_json(root / "relative-status.json").get("label_reason") if (root / "relative-status.json").exists() else None),
        "field_paths": OUTPUT_FIELD_PATHS,
        **frozen_study_context(manifest),
        "usable_for_executable_backtest": 0,
        "probability_available": False,
        "promoted": False,
    }


def _blocks(rows: list[dict[str, Any]]) -> int:
    from xiao_a.research_diagnostics import _nonoverlapping_blocks
    return len(_nonoverlapping_blocks(rows)["blocks"])


def _relative_counts(path: Path) -> dict[str, int]:
    rows = _read_jsonl(path)
    return {
        "rows": len(rows),
        "settled": sum(item.get("status") == "settled" for item in rows),
        "refused": sum(item.get("status") == "refused" for item in rows),
    }


def _bucket_names() -> tuple[str, ...]:
    return ("strong", "weak", "sideways", "unavailable")


def weekly_review(study_dir: Path, review_date: str, predictions: Path | None = None) -> dict[str, Any]:
    from xiao_a.research_study_extensions import bucket_evidence

    date.fromisoformat(review_date)
    matured = []
    sessions = []
    missing_relative_sessions = []
    bucket_conflicts = 0
    invalidations: list[dict[str, Any]] = []
    relative_label_reasons: list[dict[str, Any]] = []
    relative_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in _bucket_names()}
    by_id = _read_predictions(predictions) if predictions else {}
    if study_dir.exists():
        for child in sorted(path for path in study_dir.iterdir() if path.is_dir()):
            freeze_path = child / "shadow-freeze.json"
            if not freeze_path.exists():
                continue
            freeze = _read_json(freeze_path)
            sessions.append(freeze.get("session_date"))
            frozen_bucket = freeze.get("frozen_market_bucket")
            session_links = [item for item in _read_jsonl(child / "links.jsonl") if item.get("manifest_sha256") == freeze.get("manifest_sha256")]
            session_scores = _read_jsonl(child / "shadow-scores.jsonl")
            _effective, excluded = effective_shadow_scores(session_scores, session_links)
            invalidations.extend({"session_date": freeze.get("session_date"), "prediction_id": prediction_id, "reason": reason} for prediction_id, reason in sorted(excluded.items()))
            settlement_path = child / "relative-settlements.jsonl"
            status_path = child / "relative-status.json"
            if status_path.exists():
                relative_label_reasons.append({
                    "session_date": freeze.get("session_date"),
                    "reason": _read_json(status_path).get("label_reason"),
                })
            elif freeze.get("study_definition_id") == "research-study-v2" and not settlement_path.exists():
                missing_relative_sessions.append(freeze.get("session_date"))
            for row in _read_jsonl(settlement_path):
                if row.get("prediction_id") in excluded:
                    continue
                if row.get("status") != "settled" or not isinstance(row.get("outcome"), dict) or row["outcome"].get("available") is not True:
                    continue
                if row.get("frozen_market_bucket") != frozen_bucket:
                    bucket_conflicts += 1
                    continue
                exit_day = shanghai_day(row["outcome"].get("exit_date"))
                if exit_day is None or exit_day >= review_date:
                    continue
                name = row.get("frozen_market_bucket")
                if name not in relative_rows:
                    bucket_conflicts += 1
                    continue
                relative_rows[name].append({
                    "prediction_id": row.get("prediction_id"),
                    "symbol": row.get("symbol"),
                    "information_cutoff": row.get("session_date"),
                    "as_of": row.get("session_date"),
                    "settlement": {"exit_date": exit_day, "event_occurred": row["outcome"].get("event")},
                })
            for score in _effective:
                if score.get("status") != "scored":
                    continue
                record = by_id.get(score.get("prediction_id"))
                settlement = (record or {}).get("settlement") if isinstance(record, dict) else None
                exit_day = shanghai_day((settlement or {}).get("exit_date")) if isinstance(settlement, dict) else None
                decision_day = shanghai_day((record or {}).get("as_of")) if isinstance(record, dict) else None
                if exit_day and decision_day and decision_day >= freeze.get("prospective_shadow_start") and exit_day < review_date:
                    matured.append({**record, "shadow": score, "frozen_market_bucket": frozen_bucket})
    minimum = monitoring_block_minimum()
    blocks = _blocks(matured) if matured else 0
    bucket_reports = {}
    for name in _bucket_names():
        evidence = bucket_evidence(relative_rows[name])
        events = [row["settlement"]["event_occurred"] for row in relative_rows[name] if isinstance(row["settlement"]["event_occurred"], bool)]
        bucket_reports[name] = {
            "frozen_market_bucket": name,
            "eligible_relative_rows": len(relative_rows[name]),
            "evidence": evidence,
            "relative_hit_rate": None if evidence["insufficient_evidence"] or name == "unavailable" or not events else sum(events) / len(events),
            "claim_allowed": False,
            "direction_hit_rate": None,
        }
    report = {
        "schema": SCHEMA_WEEKLY,
        "review_date": review_date,
        "sessions": sessions,
        "matured_rows": len(matured),
        "nonoverlapping_date_blocks": blocks,
        "minimum_monitoring_blocks": minimum,
        "insufficient_evidence": blocks < minimum,
        "probability_available": False,
        "promoted": False,
        "selected_candidate": None,
        "bucket_reports": bucket_reports,
        "missing_relative_sessions": missing_relative_sessions,
        "bucket_conflicts": bucket_conflicts,
        "invalidations": invalidations,
        "relative_label_reasons": relative_label_reasons,
        "production_target_modified": False,
        "field_paths": OUTPUT_FIELD_PATHS,
        "hypotheses": [OFFLINE_HYPOTHESIS],
        "note": "每个市场状态单独计算24个非重叠块。相对标签缺失时不补超额收益。影子分数文件本身不含未来标签。",
    }
    if study_dir.exists():
        folder = study_dir / "weekly-reviews"
        folder.mkdir(parents=True, exist_ok=True)
        _atomic_write(folder / f"{review_date}.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def settle_relative_labels(
    study_dir: Path,
    session_date: str,
    labels: list[dict[str, Any]],
    *,
    timeout_seconds: float = 5,
    labels_supplied: bool = True,
) -> dict[str, Any]:
    """Append relative settlements for one frozen session. Missing evidence is a refusal row."""
    from xiao_a.research_study_extensions import bucket_evidence, relative_outcome

    manifest = load_frozen_manifest(study_dir, session_date)
    context = frozen_study_context(manifest)
    if context["study_definition_id"] != "research-study-v2":
        raise StudyRefusal("相对沪深300结算只接受已冻结的 research-study-v2 清单")
    root = _session_dir(study_dir, session_date)
    session_links = [item for item in _read_jsonl(root / "links.jsonl") if item.get("manifest_sha256") == manifest["identity_sha256"]]
    excluded = linkage_exclusions(session_links)
    confirmed = {
        item.get("prediction_id"): item
        for item in session_links
        if item.get("confirmed")
    }
    planned = {item["symbol"] for item in manifest["membership"]}
    path = root / "relative-settlements.jsonl"
    appended = 0
    unchanged = 0
    refused = 0
    with study_lock(root, timeout_seconds):
        existing = _read_jsonl(path)
        by_id = {item.get("prediction_id"): item for item in existing if item.get("prediction_id")}
        with path.open("a", encoding="utf-8") as handle:
            for label in labels:
                if not isinstance(label, dict):
                    refused += 1
                    continue
                prediction_id = label.get("prediction_id")
                try:
                    symbol = _canonical_symbol(str(label.get("symbol"))) if label.get("symbol") else None
                except ValueError:
                    symbol = None
                decision_day = shanghai_day(label.get("information_cutoff")) or shanghai_day(label.get("as_of"))
                outcome = relative_outcome(label.get("stock") or {}, label.get("index") or {})
                reason = None
                status = "settled"
                if prediction_id in excluded:
                    status, reason = "refused", excluded[prediction_id]
                    outcome = {**outcome, "available": False, "event": None, "excess_return": None}
                elif context["frozen_market_bucket"] in {None, "unavailable"}:
                    status, reason = "refused", "frozen_bucket_unavailable"
                elif prediction_id not in confirmed:
                    status, reason = "refused", "not_in_confirmed_links"
                elif symbol not in planned or confirmed[prediction_id].get("symbol") != symbol:
                    status, reason = "refused", "not_in_manifest"
                elif decision_day != session_date:
                    status, reason = "refused", "session_date_mismatch"
                elif outcome["available"] is not True:
                    status, reason = "refused", outcome.get("reason") or "relative_evidence_missing"
                record = {
                    "schema": SCHEMA_RELATIVE,
                    "session_date": session_date,
                    "manifest_sha256": manifest["identity_sha256"],
                    "prediction_id": prediction_id,
                    "symbol": symbol,
                    "status": status,
                    "reason": reason,
                    "outcome": outcome,
                    **context,
                }
                identity = _sha256_json({key: record[key] for key in ("prediction_id", "symbol", "status", "reason", "outcome", "frozen_market_bucket")})
                record["settlement_sha256"] = identity
                prior = by_id.get(prediction_id)
                if prior and prediction_id in excluded:
                    unchanged += 1
                    continue
                if prior and prior.get("settlement_sha256") == identity:
                    unchanged += 1
                    continue
                if prior and prior.get("settlement_sha256") != identity:
                    record["status"] = "refused"
                    record["reason"] = "settlement_conflict"
                    record["outcome"] = {**outcome, "available": False, "event": None, "excess_return": None}
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                by_id[prediction_id] = record
                appended += 1
                if record["status"] != "settled":
                    refused += 1
    settled_rows = []
    for item in _read_jsonl(path):
        if item.get("prediction_id") in excluded:
            continue
        if item.get("status") == "settled" and isinstance(item.get("outcome"), dict) and item["outcome"].get("available") is True:
            settled_rows.append({
                "prediction_id": item.get("prediction_id"),
                "symbol": item.get("symbol"),
                "information_cutoff": session_date,
                "as_of": session_date,
                "settlement": {"exit_date": item["outcome"].get("exit_date"), "event_occurred": item["outcome"].get("event")},
            })
    evidence = bucket_evidence(settled_rows)
    events = [row["settlement"]["event_occurred"] for row in settled_rows if isinstance(row["settlement"]["event_occurred"], bool)]
    hit_rate = None
    if context["frozen_market_bucket"] in {"strong", "weak", "sideways"} and not evidence["insufficient_evidence"] and events:
        hit_rate = sum(events) / len(events)
    result = {
        "schema": SCHEMA_RELATIVE,
        "session_date": session_date,
        "manifest_sha256": manifest["identity_sha256"],
        "frozen_market_bucket": context["frozen_market_bucket"],
        "relative_target_id": context["relative_target_id"],
        "production_target_modified": False,
        "bucket_recomputed_from_later_index": False,
        "rows_appended": appended,
        "rows_unchanged": unchanged,
        "rows_refused": refused,
        "eligible_relative_rows": len(settled_rows),
        "invalidations": [{"prediction_id": prediction_id, "reason": reason} for prediction_id, reason in sorted(excluded.items())],
        "evidence": evidence,
        "relative_hit_rate": hit_rate,
        "claim_allowed": False,
        "sidecar": "relative-settlements.jsonl",
        "label_reason": (
            "relative_labels_not_supplied" if not labels_supplied
            else "relative_labels_empty" if not labels
            else "frozen_bucket_unavailable" if context["frozen_market_bucket"] in {None, "unavailable"}
            else None
        ),
        "field_paths": OUTPUT_FIELD_PATHS,
    }
    _atomic_write(root / "relative-status.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def _resolve_study_dir(args: Any) -> Path:
    chosen = getattr(args, "study_dir", None)
    if chosen is not None:
        return chosen
    study_id = getattr(args, "study_id", None)
    if not isinstance(study_id, str) or not study_id.strip() or "/" in study_id or "\\" in study_id:
        raise StudyRefusal("需要 --study-dir，或简单的 --study-id；默认目录是 /workspace/xiao-a-research/<study-id>")
    return DEFAULT_RESEARCH_ROOT / study_id.strip()


def run_daily_command(args: Any) -> int:
    try:
        command = args.research_command
        if command != "retrospective-market-bucket":
            args.study_dir = _resolve_study_dir(args)
        if command == "sample-manifest":
            from datetime import datetime
            from xiao_a.research_study_extensions import assert_prospective_session
            pool, rejected = _read_symbol_lines(args.pool)
            universe = _load_universe(args.universe)
            holdings = _holding_symbols(args.holdings)
            extension = None
            definition = getattr(args, "study_definition", "research-study-v1")
            if definition == "research-study-v2":
                from xiao_a.research_study_extensions import enroll_market_bucket, public_index_observation
                try:
                    assert_prospective_session(args.session_date, datetime.now(_SHANGHAI).date().isoformat())
                except ValueError as exc:
                    raise StudyRefusal(str(exc)) from exc
                payload = None
                if getattr(args, "collect_index", False) and args.index_history is None:
                    try:
                        fetched = bounded_fetch(_public_index_fetch, "SH.000300", "none", 30)
                    except Exception as exc:
                        fetched = {"error": f"{type(exc).__name__}: {exc}"}
                    if fetched.get("timed_out") or (fetched.get("error") and not fetched.get("bars")):
                        observation = public_index_observation(None, error=fetched.get("error") or "budget_exhausted")
                    else:
                        observation = public_index_observation(fetched)
                    _write_index_observation(args.study_dir, observation)
                    payload = observation.get("payload")
                elif args.index_history is not None:
                    if not args.index_history.is_file():
                        payload = None
                    else:
                        try:
                            payload = _read_json(args.index_history)
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise StudyRefusal("malformed_index_history") from exc
                try:
                    extension = enroll_market_bucket(payload, args.session_date)
                except ValueError as exc:
                    raise StudyRefusal(str(exc)) from exc
            elif args.index_history is not None:
                raise StudyRefusal("指数快照只属于 research-study-v2，不会写进已有的 v1 清单")
            manifest = build_sample_manifest(
                session_date=args.session_date,
                pool_symbols=pool,
                pool_sha256=_sha256_bytes(args.pool.read_bytes()),
                pool_rejected=rejected,
                holdings=holdings,
                universe=universe,
                random_count=args.random_count,
                seed=args.seed or args.session_date.replace("-", ""),
                study_extension=extension,
            )
            result = write_sample_manifest(args.study_dir, manifest)
            result["pool_count"] = len(pool)
            result["random_count_drawn"] = manifest["random_count_drawn"]
            result["completeness"] = "not_claimed"
        elif command == "sample-link":
            response = _read_json(args.response)
            if not isinstance(response, dict):
                raise StudyRefusal("评估响应必须是 JSON 对象")
            result = append_sample_link(
                _resolve_study_dir(args), args.session_date, response,
                timeout_seconds=getattr(args, "lock_timeout_seconds", 5),
                audit_path=getattr(args, "audit", None),
            )
        elif command == "shadow-freeze":
            result = freeze_shadow(args.study_dir, args.session_date, args.predictions, timeout_seconds=args.lock_timeout_seconds)
        elif command == "collect-evidence":
            fetcher = _public_fetcher() if args.fetch else None
            result = collect_evidence(
                args.study_dir, args.session_date,
                budget_seconds=args.budget_seconds, fetcher=fetcher, fees_path=args.fees_path,
                timeout_seconds=args.lock_timeout_seconds,
            )
        elif command == "daily-status":
            result = daily_status(args.study_dir, args.session_date)
        elif command == "weekly-review":
            result = weekly_review(args.study_dir, args.review_date, args.predictions)
        elif command == "relative-study":
            labels_path = getattr(args, "labels", None)
            if labels_path is None or not Path(labels_path).is_file():
                result = settle_relative_labels(
                    args.study_dir, args.session_date, [],
                    timeout_seconds=getattr(args, "lock_timeout_seconds", 5),
                    labels_supplied=False,
                )
            else:
                try:
                    labels = _read_json(labels_path)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StudyRefusal("malformed_relative_labels") from exc
                if isinstance(labels, dict):
                    labels = labels.get("labels")
                if not isinstance(labels, list):
                    raise StudyRefusal("相对标签文件必须是数组")
                result = settle_relative_labels(
                    args.study_dir, args.session_date, labels,
                    timeout_seconds=getattr(args, "lock_timeout_seconds", 5),
                    labels_supplied=True,
                )
        elif command == "retrospective-market-bucket":
            from xiao_a.research_study_extensions import classify_market_bucket
            result = classify_market_bucket(_read_json(args.index_history), args.cutoff)
            result["retrospective"] = True
            result["written_to_manifest"] = False
        else:
            raise StudyRefusal("未知的每日研究子命令")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StudyRefusal as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=__import__("sys").stderr)
        return 2


def _write_index_observation(study_dir: Path, observation: dict[str, Any]) -> None:
    folder = study_dir / "index-snapshots" / "SH_000300"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    _atomic_write(folder / f"{stamp}.json", json.dumps(observation, ensure_ascii=False, indent=2) + "\n")


def _public_index_fetch(symbol: str, adjustment: str) -> dict[str, Any]:
    """One short public read of SH.000300. The result is not a certified price index."""
    if symbol != "SH.000300":
        return {"status": "unavailable", "error": "etf_proxy_refused", "symbol": symbol}
    from xiao_a.data.kline import EastmoneyKlineProvider, KlineRouter, TencentKlineProvider

    router = KlineRouter([TencentKlineProvider(priority=10), EastmoneyKlineProvider(priority=20)])
    result = router.fetch_daily(symbol, limit=30, adjustment="none")
    provenance = result.provenance
    last = result.bars[-1].trading_date.isoformat() if result.bars else None
    return {
        "status": result.status.value,
        "symbol": "SH.000300",
        "source": provenance.source if provenance else None,
        "source_url": provenance.source_url if provenance else None,
        "fetched_at": provenance.fetched_at.isoformat() if provenance and provenance.fetched_at else None,
        "as_of": last,
        "price_basis": None,
        "provenance_verified": False,
        "bars": [{"trading_date": bar.trading_date.isoformat(), "open": bar.open, "close": bar.close} for bar in result.bars],
        "error": "; ".join(result.errors) or None,
    }


def _public_fetcher():
    from xiao_a.data.kline import KlineRouter, EastmoneyKlineProvider, TencentKlineProvider

    router = KlineRouter([TencentKlineProvider(priority=10), EastmoneyKlineProvider(priority=20)])

    def fetch(symbol: str, adjustment: str) -> dict[str, Any]:
        result = router.fetch_daily(symbol, limit=30, adjustment=adjustment)
        return {
            "status": result.status.value,
            "source": result.provenance.source if result.provenance else None,
            "source_url": result.provenance.source_url if result.provenance else None,
            "requested_adjustment": adjustment,
            "price_basis": None,
            "bars": [{
                "trading_date": bar.trading_date.isoformat(),
                "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume,
            } for bar in result.bars],
            "error": "; ".join(result.errors) or None,
        }

    return fetch
