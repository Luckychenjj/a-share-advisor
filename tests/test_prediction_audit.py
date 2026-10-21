from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from xiao_a.models import DailyBar
from xiao_a.prediction_audit import PredictionLedger, chronological_calibration, select_future_bar_window
from xiao_a.dashboard import _position_aware_advice


def make_record(day: date, *, score: float = 0.4, price: float = 10.0) -> dict:
    stamp = f"{day.isoformat()}T15:00:00+08:00"
    return {
        "symbol": "SZ.300750",
        "as_of": stamp,
        "information_cutoff": stamp,
        "model_id": "advisor_rules_ensemble",
        "model_versions": {"advisor": "test-v1"},
        "input_snapshot": {"features": {"trend": score}, "quote": {"price": price, "fetched_at": stamp}},
        "configuration": {"risk_context": {"holding_quantity": 100}},
        "account_snapshot": {"holdings": [{"symbol": "SZ.300750", "quantity": 100}]},
        "recommendation": {"model_action": "buy"},
        "rule_score": score,
        "score_type": "rule_conformance_score",
        "target": {"target_id": "fixture-forward-5d", "horizon_sessions": 5},
    }


class PredictionAuditTests(unittest.TestCase):
    def test_future_window_excludes_cutoff_and_incomplete_current_session(self):
        bars = [
            DailyBar(date(2026, 1, 2), 10, 10, 10, 10),
            DailyBar(date(2026, 1, 5), 11, 11, 11, 11),
            DailyBar(date(2026, 1, 6), 12, 12, 12, 12),
            DailyBar(date(2026, 1, 7), 13, 13, 13, 13),
            DailyBar(date(2026, 1, 8), 14, 14, 14, 14),
            DailyBar(date(2026, 1, 9), 15, 15, 15, 15),
            DailyBar(date(2026, 1, 12), 16, 16, 16, 16),
        ]
        selected = select_future_bar_window(
            bars, information_cutoff_day=date(2026, 1, 2), horizon_sessions=5,
            before_day=date(2026, 1, 12),
        )
        self.assertEqual([bar.trading_date for bar in selected], [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)])

    def test_position_aware_sell_is_no_buy_without_holdings_and_bounded_by_sellable(self):
        no_position = _position_aware_advice("sell", 0, 0, True)
        self.assertEqual(no_position["action"], "no_buy")
        self.assertEqual(no_position["suggested_sell_quantity"], 0)
        unknown = _position_aware_advice("sell", 300, None, True)
        self.assertIsNone(unknown["suggested_sell_quantity"])
        bounded = _position_aware_advice("sell", 300, 100, True)
        self.assertEqual(bounded["suggested_sell_quantity"], 100)
        self.assertLessEqual(bounded["suggested_sell_quantity"], bounded["sellable_quantity"])

    def test_append_only_hash_chain_deduplicates_requeries_and_keeps_corrections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            ledger = PredictionLedger(path)
            record = make_record(date(2026, 1, 2))
            first, created = ledger.create_prediction(record)
            self.assertTrue(created)
            duplicate_record = make_record(date(2026, 1, 2))
            duplicate_record["input_snapshot"]["quote"]["fetched_at"] = "later-fetch-time"
            duplicate_record["as_of"] = "2026-01-02T15:01:00+08:00"
            duplicate, created_again = ledger.create_prediction(duplicate_record)
            self.assertFalse(created_again)
            self.assertEqual(first["prediction_id"], duplicate["prediction_id"])
            ledger.correct(first["prediction_id"], {"data_quality": {"status": "corrected"}}, "修正行情来源时间戳")
            self.assertTrue(ledger.integrity()["valid"])
            materialized = ledger.records()[0]
            self.assertNotIn("data_quality", materialized)
            self.assertEqual(materialized["corrections"][0]["replacement"]["data_quality"]["status"], "corrected")
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["event_type"] for item in events], ["prediction_created", "prediction_repeated", "prediction_correction"])

    def test_tampered_hash_chain_pauses_scoring_and_rejects_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            ledger = PredictionLedger(path)
            ledger.create_prediction(make_record(date(2026, 1, 2)))
            event = json.loads(path.read_text(encoding="utf-8"))
            event["rule_score"] = 0.99
            path.write_text(json.dumps(event), encoding="utf-8")
            self.assertFalse(ledger.integrity()["valid"])
            self.assertTrue(ledger.report()["scoring_paused"])
            with self.assertRaisesRegex(RuntimeError, "哈希链异常"):
                ledger.create_prediction(make_record(date(2026, 1, 3)))

    def test_rule_score_is_not_counted_as_probability(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            record, _ = ledger.create_prediction(make_record(date(2026, 1, 2)))
            with self.assertRaises(ValueError):
                ledger.settle(record["prediction_id"], {
                    "status": "settled", "resolved_at": "2026-01-02", "event_occurred": True,
                    "target_return": 0.02, "benchmark_return": 0.01,
                    "entry_date": "2026-01-02", "exit_date": "2026-01-02",
                })
            ledger.settle(record["prediction_id"], {
                "status": "settled", "resolved_at": "2026-01-12", "event_occurred": True,
                "target_return": 0.02, "benchmark_return": 0.01,
                "entry_date": "2026-01-05", "exit_date": "2026-01-12",
            })
            report = ledger.report()
            self.assertEqual(report["market_event"]["direction_hit_rate"], 1.0)
            self.assertIsNone(report["probability"]["brier"])
            self.assertEqual(report["probability"]["samples"], 0)
            self.assertEqual(report["strategy_value"]["status"], "尚未验证")

    def test_neutral_score_is_not_misclassified_as_a_bearish_direction_call(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            record, _ = ledger.create_prediction(make_record(date(2026, 1, 2), score=0.0))
            ledger.settle(record["prediction_id"], {
                "status": "settled", "resolved_at": "2026-01-12", "event_occurred": True,
                "target_return": 0.02, "benchmark_return": 0.01,
                "entry_date": "2026-01-05", "exit_date": "2026-01-12",
            })
            report = ledger.report()
            self.assertEqual(report["market_event"]["directional_samples"], 0)
            self.assertIsNone(report["market_event"]["direction_hit_rate"])
            self.assertIsNone(ledger.records()[0]["settlement"].get("model_direction_hit"))

    def test_settlement_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            record, _ = ledger.create_prediction(make_record(date(2026, 1, 2)))
            result = {"status": "settled", "event_occurred": False, "target_return": -0.01, "resolved_at": "2026-01-12", "entry_date": "2026-01-05", "exit_date": "2026-01-12"}
            ledger.settle(record["prediction_id"], result)
            with self.assertRaises(ValueError):
                ledger.settle(record["prediction_id"], {**result, "event_occurred": True})
            self.assertEqual(len(ledger.records()[0]["settlement_attempts"] if "settlement_attempts" in ledger.records()[0] else []), 0)

    def test_time_calibration_has_disjoint_chronological_ranges_and_reports_insufficient_data(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            for index in range(20):
                day = date(2026, 1, 1) + timedelta(days=index * 2)
                record, _ = ledger.create_prediction(make_record(day, score=(-0.3 if index % 2 else 0.5)))
                label = index % 2 == 0
                resolved = day + timedelta(days=1)
                ledger.settle(record["prediction_id"], {
                    "status": "settled", "event_occurred": label,
                    "target_return": 0.01 if label else -0.01,
                    "resolved_at": resolved.isoformat(), "entry_date": resolved.isoformat(),
                    "exit_date": resolved.isoformat(),
                })
            calibration = chronological_calibration(ledger.records())
            self.assertIn(calibration["status"], {"时间外描述性对照；不代表校准可靠或有盈利优势", "测试集类别单一，校准证据不足"})
            self.assertEqual(calibration["split"], "chronological_50_20_30_with_label_end_purge")
            self.assertLess(calibration["development_end"], calibration["calibration_start"])
            self.assertLess(calibration["calibration_end"], calibration["test_start"])
            self.assertFalse(calibration["calibrated_probability_available_for_production"])
            self.assertEqual(chronological_calibration([])["status"], "证据不足")

    def test_candidate_review_cannot_change_hard_risk_or_activate_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            with self.assertRaisesRegex(ValueError, "不得修改"):
                ledger.propose_candidate(version="candidate-1", rationale="test", allowed_change={"strategy": {"risk_policy": {"max_drawdown": 0.5}}}, evaluation_plan={})
            candidate = ledger.propose_candidate(
                version="candidate-2", rationale="预先记录待验证规则变更",
                allowed_change={"strategy": {"factor_weights": {"trend": 0.2}}},
                evaluation_plan={"metric": "out_of_time_brier", "shadow_period": "5 sessions"},
            )
            with self.assertRaisesRegex(ValueError, "时间外验证"):
                ledger.review_candidate(candidate["candidate_id"], decision="approve", reviewer="user", rationale="review")
            approved = ledger.review_candidate(
                candidate["candidate_id"], decision="approve", reviewer="user", rationale="审核记录",
                time_test_status="passed", shadow_status="passed",
            )
            self.assertEqual(approved["status"], "approved_pending_manual_release")
            self.assertFalse(approved["active"])


if __name__ == "__main__":
    unittest.main()
