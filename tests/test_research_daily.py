from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from xiao_a.research_candidates import config_fingerprint
from xiao_a.research_daily import (
    StudyRefusal,
    append_sample_link,
    build_sample_manifest,
    collect_evidence,
    daily_status,
    freeze_shadow,
    load_frozen_manifest,
    settle_relative_labels,
    study_lock,
    weekly_review,
    write_sample_manifest,
    _load_universe,
    _read_symbol_lines,
    _sha256_bytes,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _universe(path: Path, as_of: str, rows: list[dict]) -> None:
    _write(path, json.dumps({"as_of": as_of, "fetched_at": as_of + "T16:00:00+08:00", "source": "local_snapshot", "securities": rows}))


def _pool(path: Path, symbols: list[str]) -> None:
    _write(path, "\n".join(symbols) + "\n")


def _manifest(root: Path | str, session: str = "2026-11-07") -> dict:
    root = Path(root)
    pool = root / "pool.txt"
    universe = root / "universe.json"
    holdings = root / "holdings.json"
    _pool(pool, ["SH.600000", "SZ.000001", "SZ.000002", "SH.600004"])
    rows = [{"symbol": f"SZ.{index:06d}", "name": "示例", "is_st": False, "delisted": False, "suspended": False, "last_price": 10, "volume": 100} for index in range(1, 21)]
    rows.append({"symbol": "SH.600001", "name": "*ST示例", "is_st": True, "delisted": False, "suspended": False, "last_price": 1, "volume": 10})
    rows.append({"symbol": "SH.600003", "name": "停牌", "is_st": False, "delisted": False, "suspended": True, "last_price": 1, "volume": 0})
    _universe(universe, "2026-10-01", rows)
    _write(holdings, json.dumps({"cash": 999999, "holdings": [{"symbol": "SH.600000", "quantity": 888, "average_cost": 12.5}]}))
    manifest = build_sample_manifest(
        session_date=session,
        pool_symbols=_read_symbol_lines(pool)[0],
        pool_sha256=_sha256_bytes(pool.read_bytes()),
        pool_rejected=0,
        holdings={"status": "read", "symbols": ["SH.600000"]},
        universe=_load_universe(universe),
        random_count=18,
        seed=session.replace("-", ""),
    )
    write_sample_manifest(root / "study", manifest)
    return manifest


class DailyStudyTests(unittest.TestCase):
    def test_manifest_is_idempotent_stale_and_not_a_fixed_count(self):
        with self._tmp() as root:
            first = _manifest(root)
            self.assertEqual(first["pool"]["count"], 4)
            self.assertNotEqual(first["pool"]["count"], 35)
            self.assertTrue(first["universe"]["stale_list_reused"])
            self.assertEqual(first["universe"]["completeness"], "not_claimed")
            self.assertEqual(first["universe"]["random_unbiasedness"], "not_claimed")
            self.assertEqual(first["universe"]["live_full_a_share_list"], "unsupported")
            random_symbols = {item["symbol"] for item in first["membership"] if "random" in item["buckets"]}
            self.assertNotIn("SH.600001", random_symbols)
            self.assertNotIn("SH.600003", random_symbols)
            pooled = next(item for item in first["membership"] if item["symbol"] == "SH.600000")
            self.assertEqual(pooled["buckets"], ["fixed_pool", "holding"])
            self.assertNotIn("888", json.dumps(first))
            self.assertNotIn("999999", json.dumps(first))
            again = write_sample_manifest(root / "study", first)
            self.assertEqual(again["status"], "unchanged")
            self.assertEqual(load_frozen_manifest(root / "study", "2026-11-07")["identity_sha256"], first["identity_sha256"])

    def test_manifest_rejects_replacement_and_repaired_lock_matches(self):
        with self._tmp() as root:
            manifest = _manifest(root)
            changed = json.loads(json.dumps(manifest))
            changed["membership"] = changed["membership"][:1]
            changed.pop("identity_sha256")
            from xiao_a.research_daily import _sha256_json
            changed["identity_sha256"] = _sha256_json(changed)
            with self.assertRaises(StudyRefusal):
                write_sample_manifest(root / "study", changed)
            lock = root / "study" / "2026-11-07" / "manifest.lock"
            lock.unlink()
            repaired = write_sample_manifest(root / "study", manifest)
            self.assertEqual(repaired["status"], "unchanged")
            self.assertEqual(lock.read_text(encoding="utf-8").strip(), manifest["identity_sha256"])

    def test_links_keep_distinct_predictions_and_reject_rewritten_manifest(self):
        with self._tmp() as root:
            manifest = _manifest(root)
            symbol = next(item["symbol"] for item in manifest["membership"] if "random" in item["buckets"])
            first = append_sample_link(root / "study", "2026-11-07", {
                "symbol": symbol, "prediction_id": "p1", "fingerprint": "f1", "snapshot_ref": "s1",
            })
            same = append_sample_link(root / "study", "2026-11-07", {
                "symbol": symbol, "prediction_id": "p1", "fingerprint": "f1", "snapshot_ref": "s1",
            })
            second = append_sample_link(root / "study", "2026-11-07", {
                "symbol": symbol, "prediction_id": "p2", "fingerprint": "f2", "snapshot_ref": "s2",
            })
            ambiguous = append_sample_link(root / "study", "2026-11-07", {"symbol": symbol, "as_of": "2026-11-07"})
            self.assertEqual(first["status"], "appended")
            self.assertEqual(same["status"], "unchanged")
            self.assertTrue(second["confirmed"])
            self.assertFalse(ambiguous["confirmed"])
            self.assertEqual(ambiguous["reason"], "identity_incomplete")
            path = root / "study" / "2026-11-07" / "manifest.json"
            path.write_text(path.read_text(encoding="utf-8").replace(symbol, "SH.600009"), encoding="utf-8")
            with self.assertRaises(StudyRefusal):
                append_sample_link(root / "study", "2026-11-07", {
                    "symbol": symbol, "prediction_id": "p3", "fingerprint": "f3", "snapshot_ref": "s3",
                })

    def test_shadow_is_frozen_idempotent_and_has_no_future_label(self):
        with self._tmp() as root:
            manifest = _manifest(root)
            symbol = next(item["symbol"] for item in manifest["membership"] if "fixed_pool" in item["buckets"])
            from xiao_a.prediction_audit import _canonical
            import hashlib
            snapshot = {"features": {"trend": 1}}
            digest = hashlib.sha256(_canonical(snapshot)).hexdigest()
            append_sample_link(root / "study", "2026-11-07", {
                "symbol": symbol, "prediction_id": "p1", "fingerprint": "f1", "snapshot_ref": digest,
            })
            append_sample_link(root / "study", "2026-11-07", {"symbol": symbol})
            predictions = root / "predictions.jsonl"
            predictions.write_text(json.dumps({
                "prediction_id": "p1", "fingerprint": "f1", "symbol": symbol,
                "as_of": "2026-11-07T15:00:00+08:00", "information_cutoff": "2026-11-07T15:00:00+08:00",
                "input_snapshot": snapshot, "input_sha256": digest,
                "rule_score": 0.4,
                "submodel_scores": {name: 0.4 for name in ("cycle_trend", "leader_sentiment", "chan_structure", "multi_factor", "market_regime")},
                "settlement": {"event_occurred": True, "target_return": 0.2, "exit_date": "2026-11-14"},
            }) + "\n", encoding="utf-8")
            first = freeze_shadow(root / "study", "2026-11-07", predictions)
            second = freeze_shadow(root / "study", "2026-11-07", predictions)
            self.assertEqual(first["config_fingerprint"], config_fingerprint())
            self.assertEqual(first["prospective_shadow_start"], "2026-11-07")
            self.assertEqual(second["appended"], 0)
            raw = (root / "study" / "2026-11-07" / "shadow-scores.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("event_occurred", raw)
            self.assertNotIn("target_return", raw)
            self.assertIn("\"status\": \"scored\"", raw)
            self.assertIn("ambiguous", raw)

    def test_evidence_budget_cache_and_incomplete_fields(self):
        with self._tmp() as root:
            _manifest(root)
            marker = root / "fail-once"

            def fetcher(symbol: str, adjustment: str) -> dict:
                if not marker.exists():
                    marker.write_text("1", encoding="utf-8")
                    raise TimeoutError("temporary")
                return {"status": "ok", "source": "fixture", "price_basis": None, "bars": [{"trading_date": "2026-11-01", "open": 1, "high": 1, "low": 1, "close": 1, "limit_rate": 0.1}], "error": None}

            stopped = collect_evidence(root / "study", "2026-11-07", budget_seconds=0, fetcher=fetcher, clock=lambda: 10)
            self.assertTrue(stopped["stopped_budget"])
            self.assertEqual(stopped["symbols_written"], 0)
            self.assertEqual(stopped["fetcher_attempts"], 0)
            self.assertEqual(stopped["usable_for_executable_backtest"], 0)
            clock = {"now": 0}

            def forward() -> float:
                clock["now"] += 1
                return clock["now"]

            first = collect_evidence(root / "study", "2026-11-07", budget_seconds=100, fetcher=fetcher, retries=2, clock=forward)
            self.assertGreater(first["symbols_written"], 0)
            self.assertEqual(first["usable_for_executable_backtest"], 0)
            saved = next((root / "study" / "evidence-snapshots").glob("*/*.json"))
            body = json.loads(saved.read_text(encoding="utf-8"))
            self.assertFalse(body["usable_for_executable_backtest"])
            self.assertIn("exchange_limit_prices_unavailable", body["blockers"])
            self.assertTrue(body["series"][0]["requested_adjustment_is_not_proof_of_basis"])
            self.assertEqual(body["series"][0]["actual_price_basis"], "unverified")
            self.assertTrue(body["series"][0]["bars"])
            self.assertNotIn("limit_rate", body["series"][0]["bars"][0])
            self.assertFalse(body["verified_immutable"])
            self.assertGreater(first["fetcher_attempts"], 0)
            second = collect_evidence(root / "study", "2026-11-07", budget_seconds=100, fetcher=fetcher, clock=forward)
            self.assertGreater(second["fetcher_attempts"], 0)
            fees = root / "fees.json"
            fees.write_text(json.dumps({"commission_rate": 0.0003, "historically_applicable": False, "holdings": [{"quantity": 12345}]}), encoding="utf-8")
            collected = collect_evidence(root / "study", "2026-11-07", budget_seconds=100, fetcher=None, fees_path=fees, clock=forward)
            self.assertFalse(collected["fees"]["rates_copied_into_study"])
            self.assertFalse(collected["fees"]["historically_applicable"])
            text = (root / "study" / "2026-11-07" / "evidence-status.json").read_text(encoding="utf-8")
            self.assertNotIn("12345", text)
            self.assertNotIn("0.0003", text)

    def test_lock_timeout_and_weekly_gate(self):
        with self._tmp() as root:
            _manifest(root)
            session = root / "study" / "2026-11-07"
            with study_lock(session, 1):
                with self.assertRaises(StudyRefusal):
                    append_sample_link(root / "study", "2026-11-07", {
                        "symbol": "SH.600000", "prediction_id": "p", "fingerprint": "f", "snapshot_ref": "s",
                    }, timeout_seconds=0.01)
            review = weekly_review(root / "study", "2026-11-20")
            self.assertTrue(review["insufficient_evidence"])
            self.assertEqual(review["minimum_monitoring_blocks"], 24)
            self.assertIsNone(review["selected_candidate"])
            self.assertFalse(review["hypotheses"][0]["automatic_production_update"])
            self.assertFalse(review["probability_available"])
            status = daily_status(root / "study", "2026-11-07")
            self.assertEqual(status["pool_count"], 4)
            self.assertEqual(status["usable_for_executable_backtest"], 0)

    def test_two_day_pending_windows_budget_and_identity(self):
        with self._tmp() as root:
            study = root / "study"

            def tiny(session: str, symbol: str) -> None:
                manifest = build_sample_manifest(
                    session_date=session, pool_symbols=[symbol], pool_sha256=symbol, pool_rejected=0,
                    holdings={"status": "not_supplied", "symbols": []},
                    universe={"file_sha256": "u", "as_of": None, "fetched_at": None, "source": "bare_list", "rejected_rows": 0, "records": [{"symbol": symbol}]},
                    random_count=0, seed=session,
                )
                self.assertTrue(manifest["universe"]["stale_list_reused"])
                write_sample_manifest(study, manifest)

            tiny("2026-11-06", "SH.600000")
            from xiao_a.prediction_audit import _canonical
            import hashlib
            snapshot = {"quote": {"price": 10}}
            digest = hashlib.sha256(_canonical(snapshot)).hexdigest()
            append_sample_link(study, "2026-11-06", {
                "symbol": "SH.600000", "prediction_id": "day1", "fingerprint": "fp", "snapshot_ref": digest,
            })
            def fetcher(symbol: str, adjustment: str) -> dict:
                return {"status": "ok", "source": "fixture", "price_basis": "qfq" if adjustment == "none" else None,
                        "bars": [{"trading_date": "2026-11-01", "open": 1, "high": 1, "low": 1, "close": 1}]}

            collect_evidence(study, "2026-11-06", budget_seconds=30, fetcher=fetcher)
            before = list((study / "evidence-snapshots" / "SH_600000").glob("*.json"))
            self.assertTrue(before)
            tiny("2026-11-07", "SZ.000001")
            second = collect_evidence(study, "2026-11-07", budget_seconds=30, fetcher=fetcher)
            after = list((study / "evidence-snapshots" / "SH_600000").glob("*.json"))
            self.assertGreater(len(after), len(before))
            self.assertIn("SH.600000", second["symbols_selected"])
            self.assertIn("SZ.000001", second["symbols_selected"])
            self.assertEqual(len(second["symbols_selected"]), len(set(second["symbols_selected"])))
            stored = next((study / "evidence-snapshots" / "SH_600000").glob("*.json"))
            body = json.loads(stored.read_text(encoding="utf-8"))
            none_series = next(item for item in body["series"] if item["requested_adjustment"] == "none")
            self.assertEqual(none_series["actual_price_basis"], "qfq")
            self.assertNotEqual(none_series["actual_price_basis"], "raw")
            self.assertFalse(body["usable_for_executable_backtest"])
            self.assertIsNone(body["net_pnl"]["total_return"])

            started = time.monotonic()

            def slow(symbol: str, adjustment: str) -> dict:
                time.sleep(1)
                return {"status": "ok", "bars": []}

            bounded = collect_evidence(study, "2026-11-07", budget_seconds=0.05, fetcher=slow)
            self.assertLess(time.monotonic() - started, 0.6)
            self.assertTrue(bounded["stopped_budget"])
            try:
                waited = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                waited = (0, 0)
            self.assertEqual(waited, (0, 0))

            conflict = append_sample_link(study, "2026-11-06", {
                "symbol": "SH.600000", "prediction_id": "day1", "fingerprint": "other", "snapshot_ref": digest,
            })
            self.assertFalse(conflict["confirmed"])
            self.assertEqual(conflict["reason"], "identity_conflict")
            predictions = root / "predictions.jsonl"
            predictions.write_text(json.dumps({
                "prediction_id": "day1", "fingerprint": "fp", "symbol": "SH.600000",
                "as_of": "2026-11-06T15:00:00+08:00", "input_snapshot": snapshot, "input_sha256": digest,
                "rule_score": 0.4, "submodel_scores": {"multi_factor": 0.4},
            }) + "\n", encoding="utf-8")
            scored = freeze_shadow(study, "2026-11-06", predictions)
            raw_scores = (study / "2026-11-06" / "shadow-scores.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("\"status\": \"scored\"", raw_scores)
            self.assertGreater(scored["ambiguous"], 0)

    def test_audit_join_and_snapshot_must_match(self):
        with self._tmp() as root:
            study = root / "study"
            manifest = build_sample_manifest(
                session_date="2026-11-07", pool_symbols=["SH.600000"], pool_sha256="p", pool_rejected=0,
                holdings={"status": "not_supplied", "symbols": []},
                universe={"file_sha256": "u", "as_of": "2026-11-07", "fetched_at": None, "source": "bare_list", "rejected_rows": 0, "records": [{"symbol": "SH.600000"}]},
                random_count=0, seed="20261107",
            )
            write_sample_manifest(study, manifest)
            from xiao_a.prediction_audit import _canonical
            import hashlib
            snapshot = {"features": {"trend": 1}}
            digest = hashlib.sha256(_canonical(snapshot)).hexdigest()
            audit = root / "audit.jsonl"
            event = {
                "event_type": "prediction_created", "prediction_id": "pid", "fingerprint": "fp",
                "symbol": "SH.600000", "as_of": "2026-11-07T15:00:00+08:00",
                "input_snapshot": snapshot, "input_sha256": digest, "rule_score": 0.4,
            }
            audit.write_text(json.dumps(event) + "\n", encoding="utf-8")
            before = audit.read_bytes()
            linked = append_sample_link(study, "2026-11-07", {
                "symbol": "SH.600000", "prediction_audit": {"prediction_id": "pid"},
            }, audit_path=audit)
            self.assertTrue(linked["confirmed"])
            self.assertEqual(audit.read_bytes(), before)
            mismatch = append_sample_link(study, "2026-11-07", {
                "symbol": "SZ.000001", "prediction_audit": {"prediction_id": "pid"},
            }, audit_path=audit)
            self.assertFalse(mismatch["confirmed"])
            self.assertEqual(mismatch["reason"], "symbol_mismatch")
            other = dict(event)
            other["event_id"] = "second"
            audit.write_text(json.dumps(event) + "\n" + json.dumps(other) + "\n", encoding="utf-8")
            duplicate = append_sample_link(study, "2026-11-07", {"prediction_audit": {"prediction_id": "pid"}}, audit_path=audit)
            self.assertFalse(duplicate["confirmed"])
            self.assertEqual(duplicate["reason"], "duplicate_prediction_id")
            predictions = root / "predictions.jsonl"
            predictions.write_text(json.dumps({
                "prediction_id": "pid", "fingerprint": "fp", "symbol": "SH.600000",
                "as_of": "2026-11-07T15:00:00+08:00", "input_snapshot": snapshot, "input_sha256": digest,
                "rule_score": 0.4, "submodel_scores": {"multi_factor": 0.4},
            }) + "\n", encoding="utf-8")
            append_sample_link(study, "2026-11-07", {
                "symbol": "SH.600000", "prediction_id": "other", "fingerprint": "fp", "snapshot_ref": "not-the-snapshot",
            })
            scored = freeze_shadow(study, "2026-11-07", predictions)
            text = (study / "2026-11-07" / "shadow-scores.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("\"status\": \"scored\"", text)
            self.assertGreater(scored["ambiguous"], 0)

    def test_v2_bucket_is_on_daily_sidecars_and_weekly_gate(self):
        with self._tmp() as root:
            from xiao_a.prediction_audit import TARGET_DEFINITION, _canonical
            from xiao_a.research_study_extensions import classify_market_bucket
            import hashlib
            extension = classify_market_bucket({
                "symbol": "SH.000300", "instrument": "price_index", "price_basis": "price_index",
                "provenance_verified": True, "source": "local", "source_url": "https://example.test/csi300",
                "fetched_at": "2026-11-07T15:10:00+08:00", "as_of": "2026-11-07",
                "bars": [{"trading_date": (date(2026, 10, 19) + timedelta(days=i)).isoformat(), "close": 100 if i < 19 else 110, "open": 100} for i in range(20)],
            }, "2026-11-07")
            self.assertEqual(extension["bucket"], "strong")
            study = root / "study"
            manifest = build_sample_manifest(
                session_date="2026-11-07", pool_symbols=["SH.600000", "SZ.000001"], pool_sha256="p", pool_rejected=0,
                holdings={"status": "not_supplied", "symbols": []},
                universe={"file_sha256": "u", "as_of": "2026-11-07", "fetched_at": None, "source": "bare_list", "rejected_rows": 0, "records": [{"symbol": "SH.600000"}, {"symbol": "SZ.000001"}]},
                random_count=0, seed="20261107", study_extension=extension,
            )
            write_sample_manifest(study, manifest)
            snapshot = {"features": {"trend": 1}}
            digest = hashlib.sha256(_canonical(snapshot)).hexdigest()
            append_sample_link(study, "2026-11-07", {
                "symbol": "SH.600000", "prediction_id": "pid", "fingerprint": "fp", "snapshot_ref": digest,
            })
            append_sample_link(study, "2026-11-07", {
                "symbol": "SZ.000001", "prediction_id": "pid2", "fingerprint": "fp2", "snapshot_ref": digest,
            })
            predictions = root / "predictions.jsonl"
            predictions.write_text("".join(json.dumps(row) + "\n" for row in (
                {"prediction_id": "pid", "fingerprint": "fp", "symbol": "SH.600000", "as_of": "2026-11-07T15:00:00+08:00", "input_snapshot": snapshot, "input_sha256": digest, "rule_score": 0.4, "submodel_scores": {"multi_factor": 0.4}},
                {"prediction_id": "pid2", "fingerprint": "fp2", "symbol": "SZ.000001", "as_of": "2026-11-07T15:00:00+08:00", "input_snapshot": snapshot, "input_sha256": digest, "rule_score": 0.4, "submodel_scores": {"multi_factor": 0.4}},
            )), encoding="utf-8")
            frozen = freeze_shadow(study, "2026-11-07", predictions)
            self.assertEqual(frozen["frozen_market_bucket"], "strong")
            self.assertEqual(frozen["relative_target_id"], "forward_excess_csi300_5_sessions_v1")
            freeze_body = json.loads((study / "2026-11-07" / "shadow-freeze.json").read_text(encoding="utf-8"))
            self.assertEqual(freeze_body["frozen_market_bucket"], "strong")
            self.assertFalse(freeze_body["bucket_recomputed_from_later_index"])
            scores = (study / "2026-11-07" / "shadow-scores.jsonl").read_text(encoding="utf-8")
            self.assertIn("\"frozen_market_bucket\": \"strong\"", scores)
            self.assertNotIn("excess_return", scores)
            collect_evidence(study, "2026-11-07", budget_seconds=5, fetcher=None)
            enrolled = json.loads((study / "enrollment-index.json").read_text(encoding="utf-8"))
            self.assertEqual(enrolled["sessions"][0]["frozen_market_bucket"], "strong")
            from xiao_a.research_daily import settle_relative_labels
            settled = settle_relative_labels(study, "2026-11-07", [
                {
                    "prediction_id": "pid", "symbol": "SH.600000", "information_cutoff": "2026-11-07T15:00:00+08:00",
                    "stock": {"entry_date": "2026-11-10", "entry_open": 10, "exit_date": "2026-11-14", "exit_close": 11, "price_basis": "qfq", "corporate_actions_verified": True, "source": "local", "fetched_at": "2026-11-14T16:00:00+08:00"},
                    "index": {"symbol": "SH.000300", "price_basis": "price_index", "provenance_verified": True, "entry_date": "2026-11-10", "entry_open": 100, "exit_date": "2026-11-14", "exit_close": 100},
                },
                {
                    "prediction_id": "pid2", "symbol": "SZ.000001", "information_cutoff": "2026-11-07",
                    "stock": {"corporate_actions_verified": False}, "index": {},
                },
            ])
            self.assertIsNone(settled["relative_hit_rate"])
            self.assertEqual(settled["evidence"]["minimum_monitoring_blocks"], 24)
            self.assertTrue(settled["evidence"]["insufficient_evidence"])
            self.assertFalse(settled["production_target_modified"])
            rows = (study / "2026-11-07" / "relative-settlements.jsonl").read_text(encoding="utf-8")
            self.assertIn("\"status\": \"settled\"", rows)
            self.assertIn("stock_corporate_actions_not_verified", rows)
            review = weekly_review(study, "2026-11-21", predictions)
            self.assertEqual(review["bucket_reports"]["strong"]["eligible_relative_rows"], 1)
            self.assertEqual(review["bucket_reports"]["weak"]["eligible_relative_rows"], 0)
            self.assertEqual(review["bucket_reports"]["strong"]["evidence"]["minimum_monitoring_blocks"], 24)
            self.assertIsNone(review["bucket_reports"]["strong"]["relative_hit_rate"])
            self.assertTrue((study / "weekly-reviews" / "2026-11-21.json").is_file())
            self.assertEqual(TARGET_DEFINITION["target_id"], "forward_positive_return_5_sessions_v1")
            patterns = [line.strip() for line in Path(".gitignore").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
            self.assertIn("/study/", patterns)
            self.assertNotIn("/workspace/xiao-a-research", patterns)
            self.assertNotIn("/workspace/xiao-a-research/", patterns)
            with self.assertRaises(StudyRefusal):
                plain = root / "plain"
                plain_manifest = build_sample_manifest(
                    session_date="2026-11-07", pool_symbols=["SH.600000"], pool_sha256="p", pool_rejected=0,
                    holdings={"status": "not_supplied", "symbols": []},
                    universe={"file_sha256": "u", "as_of": "2026-11-07", "fetched_at": None, "source": "bare_list", "rejected_rows": 0, "records": [{"symbol": "SH.600000"}]},
                    random_count=0, seed="20261107",
                )
                write_sample_manifest(plain, plain_manifest)
                settle_relative_labels(plain, "2026-11-07", [])

    def test_late_conflict_and_duplicate_drop_out_of_every_view(self):
        with self._tmp() as root:
            from xiao_a.prediction_audit import _canonical
            import hashlib
            study = root / "study"
            manifest = _manifest(root)
            symbol = next(item["symbol"] for item in manifest["membership"] if "fixed_pool" in item["buckets"])
            snapshot = {"features": {"trend": 1}}
            digest = hashlib.sha256(_canonical(snapshot)).hexdigest()
            audit = root / "audit.jsonl"
            audit.write_text('{"event_type":"prediction_created","prediction_id":"SYN-1"}\n', encoding="utf-8")
            before = audit.read_bytes()

            def prediction(prediction_id: str, fingerprint: str) -> dict:
                return {
                    "prediction_id": prediction_id, "fingerprint": fingerprint, "symbol": symbol,
                    "as_of": "2026-11-07T15:00:00+08:00", "information_cutoff": "2026-11-07T15:00:00+08:00",
                    "input_snapshot": snapshot, "input_sha256": digest, "score_type": "rule_conformance_score",
                    "rule_score": 0.4, "submodel_scores": {"multi_factor": 0.4},
                    "settlement": {"event_occurred": True, "exit_date": "2026-11-14"},
                }

            predictions = root / "predictions.jsonl"
            predictions.write_text(json.dumps(prediction("SYN-1", "fp")) + "\n", encoding="utf-8")
            append_sample_link(study, "2026-11-07", {
                "symbol": symbol, "prediction_id": "SYN-1", "fingerprint": "fp", "snapshot_ref": digest,
            }, audit_path=audit)
            first = freeze_shadow(study, "2026-11-07", predictions)
            self.assertEqual(first["effective_scored"], 1)
            counted = weekly_review(study, "2026-11-21", predictions)
            self.assertEqual(counted["matured_rows"], 1)
            self.assertEqual(counted["nonoverlapping_date_blocks"], 1)
            conflict = append_sample_link(study, "2026-11-07", {
                "symbol": symbol, "prediction_id": "SYN-1", "fingerprint": "other", "snapshot_ref": digest,
            }, audit_path=audit)
            self.assertEqual(conflict["reason"], "identity_conflict")
            self.assertEqual(audit.read_bytes(), before)
            hidden = weekly_review(study, "2026-11-21", predictions)
            self.assertEqual(hidden["matured_rows"], 0)
            self.assertEqual(hidden["nonoverlapping_date_blocks"], 0)
            self.assertEqual(hidden["invalidations"], [{"session_date": "2026-11-07", "prediction_id": "SYN-1", "reason": "identity_conflict"}])
            rerun = freeze_shadow(study, "2026-11-07", predictions)
            self.assertGreater(rerun["appended"], 0)
            self.assertEqual(rerun["effective_scored"], 0)
            self.assertEqual(rerun["invalidations"][0]["reason"], "identity_conflict")
            again = freeze_shadow(study, "2026-11-07", predictions)
            self.assertEqual(again["appended"], 0)
            self.assertEqual(again["invalidations"], rerun["invalidations"])
            raw = (study / "2026-11-07" / "shadow-scores.jsonl").read_text(encoding="utf-8")
            self.assertIn("\"status\": \"scored\"", raw)
            self.assertIn("\"status\": \"invalidated\"", raw)
            status = daily_status(study, "2026-11-07")
            self.assertEqual(status["shadow_scored"], 0)
            self.assertEqual(status["invalidations"][0]["prediction_id"], "SYN-1")
            self.assertEqual(weekly_review(study, "2026-11-21", predictions)["matured_rows"], 0)

            other = root / "duplicate"
            duplicate_manifest = build_sample_manifest(
                session_date="2026-11-07", pool_symbols=[symbol], pool_sha256="p", pool_rejected=0,
                holdings={"status": "not_supplied", "symbols": []},
                universe={"file_sha256": "u", "as_of": "2026-11-07", "fetched_at": None, "source": "bare_list", "rejected_rows": 0, "records": [{"symbol": symbol}]},
                random_count=0, seed="dup",
            )
            write_sample_manifest(other, duplicate_manifest)
            other_predictions = root / "duplicate-predictions.jsonl"
            other_predictions.write_text(json.dumps(prediction("SYN-1", "fp")) + "\n" + json.dumps(prediction("SYN-2", "fp2")) + "\n", encoding="utf-8")
            append_sample_link(other, "2026-11-07", {"symbol": symbol, "prediction_id": "SYN-1", "fingerprint": "fp", "snapshot_ref": digest})
            freeze_shadow(other, "2026-11-07", other_predictions)
            append_sample_link(other, "2026-11-07", {"symbol": symbol, "prediction_id": "SYN-2", "fingerprint": "fp2", "snapshot_ref": digest})
            late = freeze_shadow(other, "2026-11-07", other_predictions)
            self.assertEqual(late["effective_scored"], 0)
            self.assertEqual({item["prediction_id"] for item in late["invalidations"]}, {"SYN-1", "SYN-2"})
            self.assertTrue(all(item["reason"] == "duplicate_symbol_day" for item in late["invalidations"]))
            repeated = freeze_shadow(other, "2026-11-07", other_predictions)
            self.assertEqual(repeated["appended"], 0)
            review = weekly_review(other, "2026-11-21", other_predictions)
            self.assertEqual(review["matured_rows"], 0)
            self.assertEqual(review["nonoverlapping_date_blocks"], 0)
            self.assertEqual({item["prediction_id"] for item in review["invalidations"]}, {"SYN-1", "SYN-2"})
            self.assertEqual(daily_status(other, "2026-11-07")["shadow_scored"], 0)

            from xiao_a.research_study_extensions import classify_market_bucket
            extension = classify_market_bucket({
                "symbol": "SH.000300", "instrument": "price_index", "price_basis": "price_index",
                "provenance_verified": True, "source": "local", "source_url": "https://example.test/csi300",
                "fetched_at": "2026-11-07T15:10:00+08:00", "as_of": "2026-11-07",
                "bars": [{"trading_date": (date(2026, 10, 19) + timedelta(days=i)).isoformat(), "close": 100 if i < 19 else 110, "open": 100} for i in range(20)],
            }, "2026-11-07")
            relative = root / "relative"
            relative_manifest = build_sample_manifest(
                session_date="2026-11-07", pool_symbols=[symbol], pool_sha256="p", pool_rejected=0,
                holdings={"status": "not_supplied", "symbols": []},
                universe={"file_sha256": "u", "as_of": "2026-11-07", "fetched_at": None, "source": "bare_list", "rejected_rows": 0, "records": [{"symbol": symbol}]},
                random_count=0, seed="rel", study_extension=extension,
            )
            write_sample_manifest(relative, relative_manifest)
            append_sample_link(relative, "2026-11-07", {"symbol": symbol, "prediction_id": "SYN-1", "fingerprint": "fp", "snapshot_ref": digest})
            label = {
                "prediction_id": "SYN-1", "symbol": symbol, "information_cutoff": "2026-11-07T15:00:00+08:00",
                "stock": {"entry_date": "2026-11-10", "entry_open": 10, "exit_date": "2026-11-14", "exit_close": 11, "price_basis": "qfq", "corporate_actions_verified": True, "source": "local", "fetched_at": "2026-11-14T16:00:00+08:00"},
                "index": {"symbol": "SH.000300", "price_basis": "price_index", "provenance_verified": True, "entry_date": "2026-11-10", "entry_open": 100, "exit_date": "2026-11-14", "exit_close": 100},
            }
            self.assertEqual(settle_relative_labels(relative, "2026-11-07", [label])["eligible_relative_rows"], 1)
            append_sample_link(relative, "2026-11-07", {"symbol": symbol, "prediction_id": "SYN-1", "fingerprint": "other", "snapshot_ref": digest})
            after = settle_relative_labels(relative, "2026-11-07", [label])
            self.assertEqual(after["eligible_relative_rows"], 0)
            self.assertEqual(after["relative_hit_rate"], None)
            self.assertEqual(after["invalidations"][0]["reason"], "identity_conflict")
            self.assertEqual(after["rows_appended"], 0)
            self.assertEqual(settle_relative_labels(relative, "2026-11-07", [label])["rows_appended"], 0)

    def test_missing_index_and_labels_keep_v2_day_moving(self):
        from datetime import datetime
        from xiao_a.cli import main
        from xiao_a.prediction_audit import _canonical
        from xiao_a.research_daily import _SHANGHAI
        from xiao_a.research_study_extensions import enroll_market_bucket, public_index_observation
        import hashlib
        today = datetime.now(_SHANGHAI).date().isoformat()
        with self._tmp() as root:
            study = root / "study"
            pool = root / "pool.txt"
            universe = root / "universe.json"
            pool.write_text("SH.600000\n", encoding="utf-8")
            universe.write_text(json.dumps({
                "as_of": today, "fetched_at": None, "source": "bare_list",
                "securities": [{"symbol": "SH.600000", "name": "示例", "is_st": False, "delisted": False, "suspended": False, "last_price": 10, "volume": 1}],
            }), encoding="utf-8")
            end = date.fromisoformat(today)
            bars = [{"trading_date": (end - timedelta(days=19 - index)).isoformat(), "close": 100 if index < 19 else 110, "open": 100} for index in range(20)]
            tampered = root / "tampered.json"
            tampered.write_text(json.dumps({
                "symbol": "SH.000300", "instrument": "price_index", "price_basis": "price_index",
                "provenance_verified": False, "source": "local", "source_url": "https://example.test/csi300",
                "fetched_at": today + "T15:00:00+08:00", "as_of": today,
                "bars": bars + [{"trading_date": (end + timedelta(days=1)).isoformat(), "close": 1, "open": 1}],
            }), encoding="utf-8")
            self.assertEqual(main(["research", "sample-manifest", "--study-dir", str(study), "--session-date", today, "--pool", str(pool), "--universe", str(universe), "--study-definition", "research-study-v2", "--index-history", str(tampered), "--random-count", "0"]), 2)
            self.assertFalse((study / today / "manifest.json").exists())
            self.assertEqual(main(["research", "sample-manifest", "--study-dir", str(study), "--session-date", today, "--pool", str(pool), "--universe", str(universe), "--study-definition", "research-study-v2", "--random-count", "0"]), 0)
            manifest = json.loads((study / today / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["study_extension"]["study_definition_id"], "research-study-v2")
            self.assertEqual(manifest["study_extension"]["bucket"], "unavailable")
            self.assertEqual(manifest["study_extension"]["reason"], "index_history_missing")
            self.assertFalse(manifest["study_extension"]["source_valid"])
            self.assertTrue(manifest["study_extension"]["not_a_v1_fallback"])
            snapshot = {"features": {"trend": 1}}
            digest = hashlib.sha256(_canonical(snapshot)).hexdigest()
            audit = root / "audit.jsonl"
            audit.write_text(json.dumps({
                "event_type": "prediction_created", "prediction_id": "SYN-1", "fingerprint": "fp",
                "symbol": "SH.600000", "as_of": today + "T15:00:00+08:00",
                "input_snapshot": snapshot, "input_sha256": digest,
            }) + "\n", encoding="utf-8")
            before = audit.read_bytes()
            response = root / "response.json"
            response.write_text(json.dumps({"symbol": "SH.600000", "prediction_audit": {"prediction_id": "SYN-1"}}), encoding="utf-8")
            self.assertEqual(main(["research", "sample-link", "--study-dir", str(study), "--session-date", today, "--response", str(response), "--audit", str(audit)]), 0)
            self.assertEqual(audit.read_bytes(), before)
            predictions = root / "predictions.jsonl"
            predictions.write_text(json.dumps({
                "prediction_id": "SYN-1", "fingerprint": "fp", "symbol": "SH.600000",
                "as_of": today + "T15:00:00+08:00", "input_snapshot": snapshot, "input_sha256": digest,
                "rule_score": 0.4, "submodel_scores": {"multi_factor": 0.4},
            }) + "\n", encoding="utf-8")
            self.assertEqual(main(["research", "shadow-freeze", "--study-dir", str(study), "--session-date", today, "--predictions", str(predictions)]), 0)
            freeze = json.loads((study / today / "shadow-freeze.json").read_text(encoding="utf-8"))
            self.assertEqual(freeze["frozen_market_bucket"], "unavailable")
            self.assertEqual(freeze["study_definition_id"], "research-study-v2")
            self.assertEqual(main(["research", "collect-evidence", "--study-dir", str(study), "--session-date", today, "--budget-seconds", "5"]), 0)
            self.assertEqual(main(["research", "relative-study", "--study-dir", str(study), "--session-date", today]), 0)
            status = json.loads((study / today / "relative-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["label_reason"], "relative_labels_not_supplied")
            self.assertIsNone(status["relative_hit_rate"])
            self.assertEqual(status["frozen_market_bucket"], "unavailable")
            self.assertEqual(main(["research", "daily-status", "--study-dir", str(study), "--session-date", today]), 0)
            self.assertEqual(main(["research", "weekly-review", "--study-dir", str(study), "--review-date", today, "--predictions", str(predictions)]), 0)
            weekly = json.loads((study / "weekly-reviews" / f"{today}.json").read_text(encoding="utf-8"))
            self.assertEqual(weekly["bucket_reports"]["unavailable"]["evidence"]["minimum_monitoring_blocks"], 24)
            self.assertIsNone(weekly["bucket_reports"]["unavailable"]["relative_hit_rate"])
            self.assertEqual(weekly["relative_label_reasons"][0]["reason"], "relative_labels_not_supplied")
            self.assertEqual(weekly["matured_rows"], 0)
            verified = root / "verified.json"
            verified.write_text(json.dumps({
                "symbol": "SH.000300", "instrument": "price_index", "price_basis": "price_index",
                "provenance_verified": True, "source": "local", "source_url": "https://example.test/csi300",
                "fetched_at": today + "T15:00:00+08:00", "as_of": today, "bars": bars,
            }), encoding="utf-8")
            self.assertEqual(main(["research", "sample-manifest", "--study-dir", str(study), "--session-date", today, "--pool", str(pool), "--universe", str(universe), "--study-definition", "research-study-v2", "--index-history", str(verified), "--random-count", "0"]), 2)
            again = json.loads((study / today / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(again["study_extension"]["bucket"], "unavailable")
            self.assertEqual(again["study_extension"]["reason"], "index_history_missing")
            other = root / "unverified"
            unverified = root / "unverified.json"
            unverified.write_text(json.dumps({
                "symbol": "SH.000300", "instrument": "price_index", "price_basis": "price_index",
                "provenance_verified": False, "source": "local", "source_url": "https://example.test/csi300",
                "fetched_at": today + "T15:00:00+08:00", "as_of": today, "bars": bars,
            }), encoding="utf-8")
            self.assertEqual(main(["research", "sample-manifest", "--study-dir", str(other), "--session-date", today, "--pool", str(pool), "--universe", str(universe), "--study-definition", "research-study-v2", "--index-history", str(unverified), "--random-count", "0"]), 0)
            other_manifest = json.loads((other / today / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(other_manifest["study_extension"]["bucket"], "unavailable")
            self.assertEqual(other_manifest["study_extension"]["reason"], "index_provenance_not_verified")
            self.assertFalse(other_manifest["study_extension"]["source_valid"])
            observation = public_index_observation({"symbol": "SH.000300", "price_basis": "qfq", "source": "public", "bars": bars, "as_of": today})
            self.assertFalse(observation["provenance_verified"])
            self.assertFalse(observation["payload"]["certified_price_index"])
            enrolled = enroll_market_bucket(observation["payload"], today)
            self.assertEqual(enrolled["bucket"], "unavailable")
            self.assertFalse(enrolled["source_valid"])
            with self.assertRaisesRegex(ValueError, "etf_proxy_refused"):
                public_index_observation({"symbol": "SH.510300"})

    def _tmp(self):
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def _manager():
            with tempfile.TemporaryDirectory() as raw:
                yield Path(raw)

        return _manager()


if __name__ == "__main__":
    unittest.main()
