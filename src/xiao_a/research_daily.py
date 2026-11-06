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
OFFLINE_HYPOTHESIS = {
    "id": "single-factor-offline-v1",
    "status": "not_applied",
    "automatic_production_update": False,
    "note": "单因子假设只保留版本号，供线下回顾。这里不挑选、不校准、不改生产。",
}


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


def append_sample_link(study_dir: Path, session_date: str, response: dict[str, Any], *, timeout_seconds: float = 5) -> dict[str, Any]:
    manifest = load_frozen_manifest(study_dir, session_date)
    root = _session_dir(study_dir, session_date)
    identity = _extract_identity(response)
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
        "reason": None if identity["confirmed"] and in_plan else ("not_in_manifest" if identity["confirmed"] else "identity_incomplete"),
        "missing_fields": identity["missing_fields"],
        "linked_at": _now(),
    }
    path = root / "links.jsonl"
    with study_lock(root, timeout_seconds):
        existing = _read_jsonl(path)
        if record["confirmed"] and any(item.get("prediction_id") == record["prediction_id"] and item.get("confirmed") for item in existing):
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
            if freeze.get("manifest_sha256") != manifest["identity_sha256"] or freeze.get("config_fingerprint") != config_fingerprint():
                raise StudyRefusal("影子配置已冻结，不能改用另一套清单或规则指纹")
        else:
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
            }
            _atomic_write(freeze_path, json.dumps(freeze, ensure_ascii=False, indent=2) + "\n")
        done = {item.get("prediction_id") for item in _read_jsonl(scores_path)}
        appended = 0
        ambiguous = 0
        with scores_path.open("a", encoding="utf-8") as handle:
            for link in links:
                prediction_id = link.get("prediction_id")
                if prediction_id in done:
                    continue
                if not link.get("confirmed"):
                    ambiguous += 1
                    handle.write(json.dumps({"prediction_id": prediction_id, "status": "ambiguous", "reason": link.get("reason")}, ensure_ascii=False) + "\n")
                    done.add(prediction_id)
                    appended += 1
                    continue
                record = by_id.get(prediction_id)
                if record is None or record.get("fingerprint") != link.get("fingerprint"):
                    ambiguous += 1
                    reason = "missing_prediction" if record is None else "fingerprint_mismatch"
                    handle.write(json.dumps({"prediction_id": prediction_id, "status": "ambiguous", "reason": reason}, ensure_ascii=False) + "\n")
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
                    "calls": {name: evaluate_candidate(name, saved) for name in ("ensemble_baseline", "multi_factor_alone", "consensus_only")},
                }
                handle.write(json.dumps(score, ensure_ascii=False) + "\n")
                done.add(prediction_id)
                appended += 1
    return {"status": "frozen", "appended": appended, "ambiguous": ambiguous, "config_fingerprint": freeze["config_fingerprint"], "prospective_shadow_start": freeze["prospective_shadow_start"]}


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


def _call_fetcher(fetcher: Any, symbol: str, adjustment: str, retries: int) -> dict[str, Any]:
    last_error = "not_called"
    for _ in range(max(1, retries)):
        try:
            result = fetcher(symbol, adjustment)
            if isinstance(result, dict):
                return result
            last_error = "fetcher_returned_non_object"
        except Exception as exc:  # adapter failures stay explicit
            last_error = f"{type(exc).__name__}: {exc}"
    return {"status": "unavailable", "error": last_error}


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
    cache = root / "evidence-cache"
    cache.mkdir(parents=True, exist_ok=True)
    deadline = clock() + max(0.0, budget_seconds)
    symbols = [item["symbol"] for item in manifest["membership"]]
    stopped = False
    written = 0
    reused = 0
    fees = {"path_supplied": fees_path is not None, "historically_applicable": False, "rates_copied_into_study": False, "sha256": None}
    if fees_path is not None and fees_path.is_file():
        blob = fees_path.read_bytes()
        fees["sha256"] = _sha256_bytes(blob)
        try:
            payload = json.loads(blob.decode("utf-8"))
            fees["historically_applicable"] = payload.get("historically_applicable") is True
        except (UnicodeDecodeError, json.JSONDecodeError):
            fees["historically_applicable"] = False
    for symbol in symbols:
        if clock() >= deadline:
            stopped = True
            break
        target = cache / f"{symbol.replace('.', '_')}.json"
        with study_lock(root, timeout_seconds):
            if target.exists():
                reused += 1
                continue
        evidence = _evidence_shell(symbol, session_date)
        evidence["fees"] = fees
        if fetcher is None:
            evidence["raw_ohlc"] = {"status": "unavailable", "reason": "public_adapter_not_invoked"}
            evidence["adjusted_ohlc"] = {"status": "unavailable", "reason": "public_adapter_not_invoked"}
            evidence["blockers"].append("public_adapter_not_invoked")
        else:
            for label, adjustment in (("raw_ohlc", "none"), ("adjusted_ohlc", "qfq")):
                fetched = _call_fetcher(fetcher, symbol, adjustment, retries)
                evidence[label] = {
                    "status": fetched.get("status") or "unavailable",
                    "source": fetched.get("source"),
                    "bar_count": len(fetched.get("bars") or []),
                    "error": fetched.get("error"),
                    "adapter_limit_rate_ignored": True,
                }
            evidence["blockers"].append("adapter_prices_are_not_verified_execution_evidence")
        evidence["usable_for_executable_backtest"] = False
        with study_lock(root, timeout_seconds):
            if target.exists():
                reused += 1
                continue
            _atomic_write(target, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            written += 1
    status = {
        "schema": SCHEMA_EVIDENCE,
        "session_date": session_date,
        "manifest_sha256": manifest["identity_sha256"],
        "stopped_budget": stopped,
        "budget_seconds": budget_seconds,
        "symbols_total": len(symbols),
        "symbols_written": written,
        "symbols_reused": reused,
        "usable_for_executable_backtest": 0,
        "probability_available": False,
        "fees": fees,
    }
    _atomic_write(root / "evidence-status.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return status


def daily_status(study_dir: Path, session_date: str) -> dict[str, Any]:
    manifest = load_frozen_manifest(study_dir, session_date)
    root = _session_dir(study_dir, session_date)
    links = _read_jsonl(root / "links.jsonl")
    scores = _read_jsonl(root / "shadow-scores.jsonl")
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
        "shadow_scored": sum(item.get("status") == "scored" for item in scores),
        "shadow_ambiguous": sum(item.get("status") == "ambiguous" for item in scores),
        "evidence": evidence,
        "usable_for_executable_backtest": 0,
        "probability_available": False,
        "promoted": False,
    }


def _blocks(rows: list[dict[str, Any]]) -> int:
    from xiao_a.research_diagnostics import _nonoverlapping_blocks
    return len(_nonoverlapping_blocks(rows)["blocks"])


def weekly_review(study_dir: Path, review_date: str, predictions: Path | None = None) -> dict[str, Any]:
    date.fromisoformat(review_date)
    matured = []
    sessions = []
    by_id = _read_predictions(predictions) if predictions else {}
    if study_dir.exists():
        for child in sorted(path for path in study_dir.iterdir() if path.is_dir()):
            freeze_path = child / "shadow-freeze.json"
            if not freeze_path.exists():
                continue
            freeze = _read_json(freeze_path)
            sessions.append(freeze.get("session_date"))
            for score in _read_jsonl(child / "shadow-scores.jsonl"):
                if score.get("status") != "scored":
                    continue
                record = by_id.get(score.get("prediction_id"))
                settlement = (record or {}).get("settlement") if isinstance(record, dict) else None
                exit_day = shanghai_day((settlement or {}).get("exit_date")) if isinstance(settlement, dict) else None
                decision_day = shanghai_day((record or {}).get("as_of")) if isinstance(record, dict) else None
                if exit_day and decision_day and decision_day >= freeze.get("prospective_shadow_start") and exit_day < review_date:
                    matured.append({**record, "shadow": score})
    minimum = monitoring_block_minimum()
    blocks = _blocks(matured) if matured else 0
    return {
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
        "hypotheses": [OFFLINE_HYPOTHESIS],
        "note": "成熟标签少于生产监测门槛时不报告方向命中率。影子分数文件本身不含未来标签。",
    }


def run_daily_command(args: Any) -> int:
    try:
        command = args.research_command
        if command == "sample-manifest":
            from datetime import datetime
            from xiao_a.research_study_extensions import assert_prospective_session, classify_market_bucket
            pool, rejected = _read_symbol_lines(args.pool)
            universe = _load_universe(args.universe)
            holdings = _holding_symbols(args.holdings)
            extension = None
            definition = getattr(args, "study_definition", "research-study-v1")
            if definition == "research-study-v2":
                if args.index_history is None:
                    raise StudyRefusal("research-study-v2 需要沪深300价格指数快照")
                try:
                    assert_prospective_session(args.session_date, datetime.now(_SHANGHAI).date().isoformat())
                    extension = classify_market_bucket(_read_json(args.index_history), args.session_date)
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
            result = append_sample_link(args.study_dir, args.session_date, response)
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
            from xiao_a.research_study_extensions import report_frozen_relative
            manifest = load_frozen_manifest(args.study_dir, args.session_date)
            labels = _read_json(args.labels)
            if isinstance(labels, dict):
                labels = labels.get("labels")
            if not isinstance(labels, list):
                raise StudyRefusal("相对标签文件必须是数组")
            try:
                result = report_frozen_relative(manifest, labels)
            except ValueError as exc:
                raise StudyRefusal(str(exc)) from exc
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


def _public_fetcher():
    from xiao_a.data.kline import KlineRouter, EastmoneyKlineProvider, TencentKlineProvider

    router = KlineRouter([TencentKlineProvider(priority=10), EastmoneyKlineProvider(priority=20)])

    def fetch(symbol: str, adjustment: str) -> dict[str, Any]:
        result = router.fetch_daily(symbol, limit=30, adjustment=adjustment)
        return {
            "status": result.status.value,
            "source": result.provenance.source if result.provenance else None,
            "bars": [{"trading_date": bar.trading_date.isoformat()} for bar in result.bars],
            "error": "; ".join(result.errors) or None,
        }

    return fetch
