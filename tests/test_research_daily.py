from __future__ import annotations

import json
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
            append_sample_link(root / "study", "2026-11-07", {
                "symbol": symbol, "prediction_id": "p1", "fingerprint": "f1", "snapshot_ref": "s1",
            })
            append_sample_link(root / "study", "2026-11-07", {"symbol": symbol})
            predictions = root / "predictions.jsonl"
            predictions.write_text(json.dumps({
                "prediction_id": "p1", "fingerprint": "f1", "symbol": symbol,
                "as_of": "2026-11-07T15:00:00+08:00", "information_cutoff": "2026-11-07T15:00:00+08:00",
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
            calls = {"n": 0}

            def fetcher(symbol: str, adjustment: str) -> dict:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("temporary")
                return {"status": "ok", "source": "fixture", "bars": [{"trading_date": "2026-11-01", "limit_rate": 0.1}], "error": None}

            stopped = collect_evidence(root / "study", "2026-11-07", budget_seconds=0, fetcher=fetcher, clock=lambda: 10)
            self.assertTrue(stopped["stopped_budget"])
            self.assertEqual(stopped["symbols_written"], 0)
            self.assertEqual(calls["n"], 0)
            self.assertEqual(stopped["usable_for_executable_backtest"], 0)
            clock = {"now": 0}

            def forward() -> float:
                clock["now"] += 1
                return clock["now"]

            first = collect_evidence(root / "study", "2026-11-07", budget_seconds=100, fetcher=fetcher, retries=2, clock=forward)
            self.assertGreater(first["symbols_written"], 0)
            self.assertEqual(first["usable_for_executable_backtest"], 0)
            saved = next((root / "study" / "2026-11-07" / "evidence-cache").glob("*.json"))
            body = json.loads(saved.read_text(encoding="utf-8"))
            self.assertFalse(body["usable_for_executable_backtest"])
            self.assertIn("exchange_limit_prices_unavailable", body["blockers"])
            self.assertTrue(body["raw_ohlc"]["adapter_limit_rate_ignored"])
            self.assertNotIn("bars", body["raw_ohlc"])
            used = calls["n"]
            second = collect_evidence(root / "study", "2026-11-07", budget_seconds=100, fetcher=fetcher, clock=forward)
            self.assertEqual(calls["n"], used)
            self.assertGreater(second["symbols_reused"], 0)
            fees = root / "fees.json"
            fees.write_text(json.dumps({"commission_rate": 0.0003, "historically_applicable": False, "holdings": [{"quantity": 12345}]}), encoding="utf-8")
            (root / "study" / "2026-11-07" / "evidence-cache").joinpath(saved.name).unlink()
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
