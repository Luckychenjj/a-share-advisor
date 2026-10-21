"""Synthetic fixtures test reminder correctness, not real model performance."""
from __future__ import annotations

import tempfile
import copy
import unittest
from datetime import date, timedelta
from pathlib import Path

from xiao_a.prediction_audit import PredictionLedger
from xiao_a.review_monitor import evaluate_review_signals


def forecast(index: int, *, direction_up: bool = True, version: str = "v1",
             day: date | None = None, symbol: str = "SZ.300750") -> dict:
    day = day or date(2025, 1, 1) + timedelta(days=index * 7)
    end = day + timedelta(days=5)
    return {
        "prediction_id": f"fixture-{index}-{symbol}", "mode": "formal",
        "symbol": symbol, "as_of": f"{day}T15:00:00+08:00",
        "information_cutoff": f"{day}T15:00:00+08:00",
        "model_id": "fixture-rules", "model_versions": {"rule": version},
        "score_type": "rule_conformance_score", "rule_score": 0.5 if direction_up else -0.5,
        "target": {"target_id": "synthetic-positive-5d", "horizon_sessions": 5},
        "data_quality": {"status": "fresh"},
        "settlement": {"status": "settled", "event_occurred": True,
                       "exit_date": str(end), "resolved_at": str(end)},
    }


class ReviewMonitorTests(unittest.TestCase):
    def monitor(self, rows, events=None, today=date(2030, 1, 1)):
        return evaluate_review_signals(rows, events or [], {"valid": True}, today=today)

    def test_one_loss_and_small_sample_do_not_trigger_model_review(self):
        result = self.monitor([forecast(0, direction_up=False)])
        self.assertEqual(result["status"], "证据不足")
        self.assertEqual(result["signals"], [])
        self.assertIn("规则分", result["probability_status"])

    def test_two_separate_persistent_windows_trigger_only_review(self):
        rows = [forecast(i, direction_up=i < 8) for i in range(24)]
        result = self.monitor(rows)
        self.assertEqual(len(result["signals"]), 1)
        signal = result["signals"][0]
        self.assertEqual(signal["category"], "model_performance")
        self.assertEqual(signal["evidence"]["reference_event_rate"], 1)
        self.assertEqual(signal["evidence"]["nonoverlapping_date_blocks"], 24)
        self.assertEqual(result["policy"]["action"], "manual_review_only")
        self.assertTrue(all(x["bootstrap_95_upper"] == -1 for x in signal["evidence"]["windows"]))
        # Pure monitoring leaves every original model output untouched.
        self.assertEqual(rows[-1]["rule_score"], -0.5)

    def test_one_bad_window_is_not_a_sustained_signal(self):
        rows = [forecast(i, direction_up=i < 16) for i in range(24)]
        self.assertEqual(self.monitor(rows)["signals"], [])

    def test_account_and_risk_adjusted_action_do_not_change_model_monitoring(self):
        rows = [forecast(i, direction_up=i < 8) for i in range(24)]
        independent = self.monitor(rows)
        changed_account = copy.deepcopy(rows)
        for item in changed_account:
            item["account_snapshot"] = {"cash": 0, "holdings": [{"quantity": 1000}], "drawdown": .5}
            item["recommendation"] = {"model_action": "hold", "position_aware_action": {"action": "no_buy"}}
            item["confidence_score"] = 0.01
        self.assertEqual(self.monitor(changed_account), independent)

    def test_undefined_target_cannot_trigger_performance_review(self):
        rows = [forecast(i, direction_up=False) for i in range(24)]
        for item in rows:
            item.pop("target")
        self.assertEqual(self.monitor(rows)["groups"], [])

    def test_versions_and_targets_are_not_pooled(self):
        rows = [forecast(i, direction_up=False, version="v1" if i < 12 else "v2") for i in range(24)]
        result = self.monitor(rows)
        self.assertEqual(result["signals"], [])
        self.assertEqual(len(result["groups"]), 2)
        rows = [forecast(i, direction_up=False) for i in range(24)]
        for item in rows[12:]:
            item["target"]["target_id"] = "different-target"
        self.assertEqual(self.monitor(rows)["signals"], [])

    def test_same_day_and_overlapping_windows_do_not_inflate_sample_count(self):
        same_day = [forecast(i, direction_up=False, day=date(2025, 1, 1), symbol=f"SZ.{i:06d}") for i in range(24)]
        result = self.monitor(same_day)
        self.assertEqual(result["groups"][0]["nonoverlapping_date_blocks"], 1)
        self.assertEqual(result["signals"], [])
        overlapping = [forecast(i, direction_up=False, day=date(2025, 1, 1) + timedelta(days=i)) for i in range(100)]
        self.assertLess(self.monitor(overlapping)["groups"][0]["nonoverlapping_date_blocks"], 24)
        self.assertEqual(self.monitor(overlapping)["signals"], [])

    def test_future_and_experiment_labels_are_excluded(self):
        rows = [forecast(i, direction_up=False) for i in range(24)]
        self.assertEqual(self.monitor(rows, today=date(2024, 12, 31))["groups"], [])
        for item in rows:
            item["mode"] = "experiment"
        self.assertEqual(self.monitor(rows)["groups"], [])

    def test_data_problem_requires_multiple_dates_and_deduplicates_retries(self):
        today = date(2026, 10, 7)
        events = [{"event_type": "analysis_attempt_failed", "symbol": "SZ.300750",
                   "requested_at": f"{today - timedelta(days=i)}T16:00:00+08:00"}
                  for i in range(3) for _ in range(30)]
        self.assertEqual(self.monitor([], events, today)["signals"], [])
        events += [{**x, "symbol": "SH.600000"} for x in events[:]]
        result = self.monitor([], events, today)
        self.assertEqual(result["signals"][0]["category"], "data_quality")
        self.assertEqual(result["signals"][0]["evidence"]["sample_units"], 6)
        self.assertEqual(self.monitor([], events, today + timedelta(days=40))["signals"], [])

    def test_normal_not_yet_due_is_not_a_data_problem(self):
        today = date(2026, 10, 7)
        item = forecast(0)
        item["settlement"] = None
        item["settlement_attempts"] = [
            {"attempt_at": str(today - timedelta(days=i)), "reason": "预测期限尚未完整，尚无5个已结束的后续交易日"}
            for i in range(3)
        ]
        self.assertEqual(self.monitor([item], today=today)["signals"], [])
        for attempt in item["settlement_attempts"]:
            attempt["reason"] = "同期沪深300基准数据缺失，暂不结算"
        self.assertEqual(self.monitor([item], today=today)["signals"][0]["category"], "settlement_quality")

    def test_episode_is_recorded_once_acknowledgement_is_not_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            today = date.today()
            for i in range(3):
                for symbol in ("SZ.300750", "SH.600000"):
                    ledger.append("analysis_attempt_failed", {
                        "symbol": symbol, "requested_at": f"{today - timedelta(days=i)}T10:00:00+08:00",
                    })
            monitor = ledger.check_review_reminders()
            review_id = monitor["alerts"][0]["review_id"]
            count = len(ledger.events())
            ledger.check_review_reminders()
            self.assertEqual(len(ledger.events()), count)
            ledger.acknowledge_review(review_id, reviewer="test-user", reason="synthetic acknowledgement")
            self.assertTrue(ledger.check_review_reminders()["alerts"][0]["acknowledged"])
            self.assertEqual(ledger.review_monitor()["active_count"], 1)
            self.assertEqual(ledger.review_monitor()["unacknowledged_count"], 0)
            ledger.acknowledge_review(review_id, reviewer="test-user", reason="duplicate click")
            self.assertEqual(len(ledger.events()), count + 1)
            self.assertTrue(ledger.integrity()["valid"])

    def test_integrity_problem_pauses_monitor_and_cannot_be_hidden_by_other_results(self):
        result = evaluate_review_signals([forecast(i) for i in range(30)], [], {"valid": False})
        self.assertEqual(len(result["signals"]), 1)
        self.assertEqual(result["signals"][0]["severity"], "error")
        self.assertEqual(result["groups"], [])


if __name__ == "__main__":
    unittest.main()
