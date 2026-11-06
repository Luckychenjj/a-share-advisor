from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from xiao_a.prediction_audit import TARGET_DEFINITION
from xiao_a.research_daily import StudyRefusal, build_sample_manifest, write_sample_manifest
from xiao_a.research_study_extensions import (
    CSI300_INDEX,
    RELATIVE_TARGET_ID,
    assert_prospective_session,
    classify_market_bucket,
    relative_outcome,
    report_frozen_relative,
    study_definition_fingerprint,
)


def _index(closes: list[float], *, cutoff: str = "2026-11-07", extra: dict | None = None, symbol: str = CSI300_INDEX) -> dict:
    end = date.fromisoformat(cutoff)
    start = end - timedelta(days=len(closes) - 1)
    bars = [{"trading_date": (start + timedelta(days=index)).isoformat(), "close": close, "open": close} for index, close in enumerate(closes)]
    if extra:
        bars.append(extra)
    return {
        "symbol": symbol,
        "instrument": "price_index",
        "price_basis": "price_index",
        "provenance_verified": True,
        "source": "local_snapshot",
        "source_url": "https://example.test/csi300",
        "fetched_at": cutoff + "T15:10:00+08:00",
        "as_of": cutoff,
        "bars": bars,
    }


def _stock(**overrides) -> dict:
    payload = {
        "entry_date": "2026-11-10",
        "entry_open": 10,
        "exit_date": "2026-11-14",
        "exit_close": 11,
        "price_basis": "qfq",
        "corporate_actions_verified": True,
        "source": "local_snapshot",
        "fetched_at": "2026-11-14T16:00:00+08:00",
    }
    payload.update(overrides)
    return payload


def _index_points(**overrides) -> dict:
    payload = {
        "symbol": CSI300_INDEX,
        "price_basis": "price_index",
        "provenance_verified": True,
        "entry_date": "2026-11-10",
        "entry_open": 100,
        "exit_date": "2026-11-14",
        "exit_close": 100,
    }
    payload.update(overrides)
    return payload


class StudyExtensionTests(unittest.TestCase):
    def test_production_target_is_unchanged_and_fingerprint_is_stable(self):
        self.assertEqual(TARGET_DEFINITION["target_id"], "forward_positive_return_5_sessions_v1")
        self.assertEqual(TARGET_DEFINITION["definition_version"], 1)
        self.assertEqual(study_definition_fingerprint(), study_definition_fingerprint())
        self.assertNotEqual(RELATIVE_TARGET_ID, TARGET_DEFINITION["target_id"])

    def test_market_buckets_and_provenance_refusals(self):
        strong = classify_market_bucket(_index([100] * 19 + [110]), "2026-11-07")
        self.assertEqual(strong["bucket"], "strong")
        weak = classify_market_bucket(_index([100] * 19 + [90]), "2026-11-07")
        self.assertEqual(weak["bucket"], "weak")
        flat = classify_market_bucket(_index([100] * 20), "2026-11-07")
        self.assertEqual(flat["bucket"], "sideways")
        mixed = [50] * 14 + [200] + [50] * 4 + [80]
        self.assertEqual(classify_market_bucket(_index(mixed), "2026-11-07")["bucket"], "sideways")
        short = classify_market_bucket(_index([100] * 19), "2026-11-07")
        self.assertEqual(short["bucket"], "unavailable")
        self.assertNotEqual(short["bucket"], "sideways")
        with self.assertRaisesRegex(ValueError, "after_cutoff"):
            classify_market_bucket(_index([100] * 20, extra={"trading_date": "2026-11-08", "close": 1, "open": 1}), "2026-11-07")
        with self.assertRaisesRegex(ValueError, "etf_proxy_refused"):
            classify_market_bucket(_index([100] * 20, symbol="SH.510300"), "2026-11-07")
        broken = _index([100] * 20)
        broken["provenance_verified"] = False
        with self.assertRaisesRegex(ValueError, "not_verified"):
            classify_market_bucket(broken, "2026-11-07")
        with self.assertRaisesRegex(ValueError, "past_session"):
            assert_prospective_session("2026-11-06", "2026-11-07")

    def test_relative_outcome_requires_synchronized_verified_endpoints(self):
        beat = relative_outcome(_stock(), _index_points())
        self.assertTrue(beat["event"])
        self.assertTrue(beat["excess_return_is_not_stock_alpha"])
        self.assertFalse(relative_outcome(_stock(exit_close=9), _index_points())["event"])
        missing = relative_outcome(_stock(corporate_actions_verified=False), _index_points())
        self.assertFalse(missing["available"])
        self.assertIsNone(missing["event"])
        mismatched = relative_outcome(_stock(), _index_points(exit_date="2026-11-15"))
        self.assertEqual(mismatched["reason"], "dates_not_synchronized")
        etf = relative_outcome(_stock(), _index_points(symbol="SH.510300"))
        self.assertEqual(etf["reason"], "etf_proxy_refused")
        inferred = relative_outcome({"market_regime": "强趋势"}, {})
        self.assertFalse(inferred["available"])
        self.assertFalse(inferred["inferred_from_market_regime"])

    def test_v1_manifest_is_not_upgraded_and_training_majority_ignores_the_test(self):
        manifest = {"study_extension": None, "identity_sha256": "x"}
        with self.assertRaisesRegex(ValueError, "upgrade_refused"):
            report_frozen_relative(manifest, [])
        frozen = classify_market_bucket(_index([100] * 20), "2026-11-07")
        labels = []
        for index, event_close in enumerate((11, 9, 12)):
            labels.append({
                "prediction_id": f"p{index}",
                "information_cutoff": (date(2026, 1, 1) + timedelta(days=30 * index)).isoformat(),
                "symbol": f"TEST.R{index}",
                "direction_event": event_close > 10,
                "rule_score": 0.4,
                "submodel_scores": {"multi_factor": 0.4, "cycle_trend": 0.4},
                "stock": _stock(entry_date=(date(2026, 1, 2) + timedelta(days=30 * index)).isoformat(), exit_date=(date(2026, 1, 8) + timedelta(days=30 * index)).isoformat(), exit_close=event_close),
                "index": _index_points(entry_date=(date(2026, 1, 2) + timedelta(days=30 * index)).isoformat(), exit_date=(date(2026, 1, 8) + timedelta(days=30 * index)).isoformat()),
            })
        report = report_frozen_relative({"study_extension": frozen}, labels)
        self.assertEqual(report["frozen_market_bucket"], "sideways")
        self.assertFalse(report["bucket_recomputed_from_later_index"])
        self.assertFalse(report["production_target_modified"])
        descriptive = report["bucket_reports"]["sideways"]["full_sample_descriptive"]
        tested = report["bucket_reports"]["sideways"]["out_of_sample_test"]
        self.assertTrue(descriptive["evidence"]["insufficient_evidence"])
        self.assertEqual(descriptive["evidence"]["minimum_monitoring_blocks"], 24)
        self.assertIsNone(descriptive["relative_hit_rate"])
        self.assertEqual(descriptive["relative_baselines"]["hindsight_majority_role"], "descriptive_ceiling_not_tradable")
        self.assertIsNone(descriptive["relative_baselines"]["training_majority_choice"])
        self.assertIsNotNone(tested["relative_baselines"]["training_majority_choice"])
        self.assertFalse(tested["relative_baselines"]["training_majority_uses_future_test_labels"])
        self.assertFalse(tested["relative_baselines"]["claim_allowed"])
        self.assertGreater(descriptive["action_subsets"]["ensemble_baseline"]["buy_rows"], 0)

    def test_existing_manifest_refuses_a_different_extension(self):
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = build_sample_manifest(
                session_date="2026-11-07",
                pool_symbols=["SH.600000"],
                pool_sha256="abc",
                pool_rejected=0,
                holdings={"status": "not_supplied", "symbols": []},
                universe={"file_sha256": "u", "as_of": "2026-11-07", "fetched_at": None, "source": "local_snapshot", "rejected_rows": 0, "records": [{"symbol": "SZ.000010"}]},
                random_count=1,
                seed="20261107",
            )
            write_sample_manifest(root, manifest)
            extended = dict(manifest)
            extended["study_extension"] = {"study_definition_id": "research-study-v2", "bucket": "strong"}
            extended.pop("identity_sha256")
            from xiao_a.research_daily import _sha256_json
            extended["identity_sha256"] = _sha256_json(extended)
            with self.assertRaises(StudyRefusal):
                write_sample_manifest(root, extended)
            self.assertNotIn("study_extension", json.loads((root / "2026-11-07" / "manifest.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
