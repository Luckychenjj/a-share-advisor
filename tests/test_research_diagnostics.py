from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from io import StringIO
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from xiao_a.cli import main
from xiao_a.prediction_audit import TARGET_DEFINITION, PredictionLedger, _canonical
from xiao_a.research_diagnostics import (
    build_research_report,
    chain_integrity,
    diagnose_audit_file,
    fold_records,
    monitoring_block_minimum,
    read_audit_events,
    shanghai_day,
)
from xiao_a.review_monitor import REVIEW_POLICY


TARGET = {"target_id": TARGET_DEFINITION["target_id"], "horizon_sessions": 5}


def seal(events: list[dict]) -> list[dict]:
    previous = "0" * 64
    sealed = []
    for index, payload in enumerate(events, start=1):
        event = dict(payload)
        event["sequence"] = index
        event["previous_hash"] = previous
        event["event_id"] = event.get("event_id") or f"evt-{index}"
        body = {key: value for key, value in event.items() if key != "event_hash"}
        event["event_hash"] = hashlib.sha256(previous.encode("ascii") + _canonical(body)).hexdigest()
        previous = event["event_hash"]
        sealed.append(event)
    return sealed


def prediction(
    *,
    prediction_id: str,
    day: date,
    symbol: str = "TEST.AAA001",
    score: float = 0.4,
    event: bool | None = True,
    mode: str = "formal",
    model_action: str = "hold",
    account_action: str = "hold",
    submodel_scores: dict | None = None,
    score_type: str | None = "rule_conformance_score",
    target: dict | None = None,
    recorded_at: str | None = None,
    settlement_recorded_at: str | None = None,
    target_return: float | None = None,
    include_settlement: bool = True,
    research_cohort: dict | None = None,
    as_of: str | None = None,
    information_cutoff: str | None = None,
    entry_date: date | None = None,
    exit_date: date | None = None,
) -> list[dict]:
    stamp = f"{day.isoformat()}T15:00:00+08:00"
    created = {
        "event_type": "prediction_created",
        "recorded_at": recorded_at or stamp,
        "prediction_id": prediction_id,
        "mode": mode,
        "symbol": symbol,
        "as_of": as_of or stamp,
        "information_cutoff": information_cutoff or stamp,
        "rule_score": score,
        "model_id": "advisor_rules_ensemble",
        "recommendation": {
            "model_action": model_action,
            "account_action": account_action,
            "position_aware_action": {"action": account_action},
        },
        "submodel_scores": submodel_scores or {},
        "target": dict(TARGET if target is None else target),
    }
    if score_type is not None:
        created["score_type"] = score_type
    if mode is None:
        created.pop("mode")
    if research_cohort is not None:
        created["research_cohort"] = research_cohort
    events = [created]
    if event is None or not include_settlement:
        return events
    entry = entry_date or (day + timedelta(days=1))
    exit_day = exit_date or (day + timedelta(days=7))
    resolved_return = (0.02 if event else -0.02) if target_return is None else target_return
    events.append({
        "event_type": "prediction_settled",
        "recorded_at": settlement_recorded_at or f"{exit_day.isoformat()}T16:00:00+08:00",
        "prediction_id": prediction_id,
        "status": "settled",
        "event_occurred": event,
        "target_return": resolved_return,
        "entry_date": entry.isoformat(),
        "exit_date": exit_day.isoformat(),
        "resolved_at": exit_day.isoformat(),
        "execution_simulation": "not_applied",
    })
    return events


def report_for(*groups: list[dict], annotations: list[dict] | None = None, as_of: str | None = None) -> dict:
    events = []
    for group in groups:
        events.extend(group)
    return build_research_report(seal(events), as_of=as_of, annotations=annotations, engine="python")


class ResearchDiagnosticTests(unittest.TestCase):
    def test_cutoff_calendar_and_hash_chain_match_the_ledger(self):
        self.assertEqual(shanghai_day("2026-11-06T16:30:00+00:00"), "2026-11-07")
        self.assertEqual(shanghai_day("2026-11-06T23:30:00"), "2026-11-06")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            ledger = PredictionLedger(path)
            record, created = ledger.create_prediction({
                "symbol": "TEST.AAA001",
                "as_of": "2026-01-05T15:00:00+08:00",
                "information_cutoff": "2026-01-05T15:00:00+08:00",
                "model_versions": {"advisor": "test"},
                "input_snapshot": {"features": {"trend": 1}, "quote": {"price": 10}},
                "configuration": {},
                "account_snapshot": {},
                "recommendation": {"model_action": "hold"},
                "rule_score": 0.2,
                "score_type": "rule_conformance_score",
                "target": dict(TARGET),
            })
            self.assertTrue(created)
            ledger.settle(record["prediction_id"], {
                "status": "settled",
                "event_occurred": True,
                "target_return": 0.01,
                "resolved_at": "2026-01-12",
                "entry_date": "2026-01-06",
                "exit_date": "2026-01-12",
                "execution_simulation": "not_applied",
            })
            before = path.read_bytes()
            events = read_audit_events(path)
            self.assertEqual(chain_integrity(events), {
                "valid": ledger.integrity()["valid"],
                "events": ledger.integrity()["events"],
                "broken_event_id": None,
                "head": ledger.integrity()["head"],
            })
            folded, _meta = fold_records(events)
            self.assertEqual(
                [item["prediction_id"] for item in folded],
                [item["prediction_id"] for item in ledger.records()],
            )
            self.assertEqual(path.read_bytes(), before)

    def test_denominators_separate_duplicates_from_same_day_cutoffs(self):
        day = date(2026, 3, 2)
        report = report_for(
            prediction(prediction_id="keep", day=day, symbol="TEST.AAA001", score=0.4, event=True),
            prediction(prediction_id="drop", day=day, symbol="TEST.AAA001", score=-0.2, event=False),
            prediction(prediction_id="other", day=day, symbol="TEST.BBB002", score=0.3, event=False),
            prediction(prediction_id="legacy", day=day, symbol="TEST.CCC003", score=0.2, event=True, mode=None),
        )
        ladder = {item["name"]: item["count"] for item in report["denominator_ladder"]}
        self.assertEqual(ladder["slice_prediction_created"], 4)
        self.assertEqual(ladder["formal_mode"], 3)
        self.assertEqual(ladder["legacy_mode_unspecified"], 1)
        self.assertEqual(ladder["excluded_duplicate_symbol_as_of_date"], 1)
        self.assertEqual(ladder["formal_research_rows"], 2)
        self.assertEqual(report["same_calendar_day_as_information_cutoff"]["rows"], 2)
        self.assertFalse(report["same_calendar_day_as_information_cutoff"]["excluded"])
        self.assertEqual(report["direction"]["nonzero_production_definition"]["rows"], 2)
        self.assertNotIn("account_snapshot", json.dumps(report))
        self.assertNotIn("holdings", json.dumps(report))

    def test_direction_baseline_action_fields_and_chan_sample_gap(self):
        rows = []
        for index in range(10):
            rows.append(prediction(
                prediction_id=f"p{index}",
                day=date(2026, 4, 1) + timedelta(days=index),
                symbol="TEST.AAA001" if index < 7 else f"TEST.S{index}",
                score=0.5,
                event=index < 3,
                model_action="buy" if index < 4 else "hold",
                account_action="buy" if index < 2 else "no_buy",
                submodel_scores={
                    "multi_factor": 0.8 if index % 2 == 0 else -0.4,
                    "chan_structure": 0.0 if index == 0 else (0.6 if index % 2 == 0 else -0.3),
                },
            ))
        report = report_for(*rows)
        direction = report["direction"]["nonzero_production_definition"]
        self.assertEqual(direction["rows"], 10)
        self.assertEqual(direction["hits"], 3)
        self.assertEqual(direction["constant_negative_hits"], 7)
        self.assertAlmostEqual(direction["hit_rate"], 0.3)
        self.assertAlmostEqual(direction["constant_negative_hit_rate"], 0.7)
        self.assertFalse(direction["beats_constant_negative"])
        self.assertFalse(report["predictive_improvement_claim_allowed"])
        self.assertIsNone(report["net_pnl"])
        self.assertEqual(report["execution_simulation"], "not_applied")
        self.assertFalse(report["calibrated_probability_available_for_production"])
        self.assertFalse(report["candidate_promoted"])
        self.assertTrue(report["production_weights_unchanged"])
        buy = report["actions"]["buy_event_rates"]
        self.assertEqual(buy["model_action"]["rows"], 4)
        self.assertEqual(buy["model_action"]["positive_events"], 3)
        self.assertEqual(buy["account_action"]["rows"], 2)
        self.assertNotEqual(buy["model_action"]["positive_event_rate"], direction["hit_rate"])
        chan = report["auc"]["submodels"]["chan_structure"]
        self.assertEqual(chan["float_scores"], 10)
        self.assertEqual(chan["direction_samples"], 9)
        self.assertEqual(chan["near_zero_excluded_from_direction_sample"], 1)
        self.assertIsNotNone(report["auc"]["ensemble_rule_score_direction_sample"])
        self.assertGreater(report["overlap"]["overlapping_same_symbol_window_pairs"], 0)
        self.assertLess(report["overlap"]["nonoverlapping_date_blocks"], 8)
        self.assertTrue(report["overlap"]["insufficient_evidence"])
        self.assertIsNone(report["overlap"]["block_bootstrap"]["percentile_2_5"])
        self.assertEqual(report["sampling"]["design"], "not_a_probability_sample")

    def test_ambiguous_labels_cohorts_and_later_events_are_not_called_corruption(self):
        good = prediction(prediction_id="good", day=date(2026, 5, 4), symbol="TEST.AAA001", event=True)
        bad = prediction(
            prediction_id="bad",
            day=date(2026, 5, 6),
            symbol="TEST.BBB002",
            event=True,
            target_return=-0.01,
        )
        partial = prediction(
            prediction_id="partial",
            day=date(2026, 5, 8),
            symbol="EX.FAKE1",
            event=False,
            research_cohort={"cohort_bucket": "pool"},
        )
        bare = prediction(prediction_id="bare", day=date(2026, 5, 11), symbol="FAKE1", event=False)
        report = report_for(
            good,
            bad,
            partial,
            bare,
            annotations=[
                {"prediction_id": "good", "cohort_bucket": "watch", "cohort_provenance": "manual_annotation_v1"},
                {"prediction_id": "good", "cohort_bucket": "pool", "cohort_provenance": "manual_annotation_v1"},
                {"prediction_id": "missing", "cohort_bucket": "watch", "cohort_provenance": "manual_annotation_v1"},
                {"cohort_bucket": "watch"},
            ],
        )
        self.assertEqual(report["labels"]["counts"].get("ambiguous_label"), 1)
        self.assertEqual(report["direction"]["nonzero_production_definition"]["rows"], 3)
        self.assertEqual(report["cohorts"]["unmatched_annotation_count"], 1)
        self.assertEqual(report["cohorts"]["invalid_annotation_objects"], 1)
        self.assertGreaterEqual(report["cohorts"]["ambiguous_rows"], 2)
        self.assertFalse(report["cohorts"]["backfilled_legacy_records"])
        self.assertGreaterEqual(report["cohorts"]["legacy_unspecified_rows"], 1)
        self.assertTrue(report["symbol_namespace_ambiguous"])
        self.assertIn("legacy_unspecified", report["cohorts"]["rows_by_provenance"])
        later = report_for(
            prediction(
                prediction_id="early",
                day=date(2026, 6, 1),
                event=True,
                recorded_at="2026-06-01T15:00:00+08:00",
                settlement_recorded_at="2026-06-08T15:00:00+08:00",
            ),
            prediction(
                prediction_id="after",
                day=date(2026, 6, 20),
                event=False,
                recorded_at="2026-06-20T15:00:00+08:00",
                settlement_recorded_at="2026-06-27T15:00:00+08:00",
            ),
            as_of="2026-06-10",
        )
        ladder = {item["name"]: item["count"] for item in later["denominator_ladder"]}
        self.assertEqual(later["full_file_events"], 4)
        self.assertEqual(ladder["slice_prediction_created"], 1)
        self.assertEqual(later["cutoff"]["excluded_after_cutoff"], 2)
        self.assertTrue(later["cutoff"]["chain_prefix_aligned"])
        self.assertTrue(later["event_count_difference_is_not_automatically_corruption"])
        self.assertTrue(later["integrity"]["valid"])

    def test_out_of_sample_purges_overlapping_labels_and_does_not_select_a_rule(self):
        groups = []
        for index in range(8):
            day = date(2026, 1, 1) + timedelta(days=3 * index)
            groups.append(prediction(
                prediction_id=f"d{index}",
                day=day,
                symbol=f"TEST.U{index:02d}",
                score=0.4 if index % 2 == 0 else -0.4,
                event=index % 2 == 0,
                submodel_scores={"multi_factor": 0.2 if index % 2 == 0 else -0.2},
            ))
        report = report_for(*groups)
        comparison = report["out_of_sample_rule_comparison"]
        dates = sorted(day.isoformat() for day in (date(2026, 1, 1) + timedelta(days=3 * index) for index in range(8)))
        test_start = dates[int(len(dates) * 0.7)]
        expected_purged = 0
        expected_reference = 0
        expected_test = 0
        for index, day in enumerate(dates):
            if day >= test_start:
                expected_test += 1
            elif (date.fromisoformat(day) + timedelta(days=7)).isoformat() < test_start:
                expected_reference += 1
            else:
                expected_purged += 1
        self.assertEqual(comparison["test_start"], test_start)
        self.assertEqual(comparison["purged_rows"], expected_purged)
        self.assertGreater(expected_purged, 0)
        self.assertEqual(comparison["reference_rows_after_purge"], expected_reference)
        self.assertEqual(comparison["test_rows"], expected_test)
        self.assertEqual(comparison["in_sample_threshold_search"], "not_performed")
        self.assertFalse(comparison["selection_performed"])
        self.assertIsNone(comparison["selected_rule"])
        self.assertFalse(comparison["predictive_improvement_claim_allowed"])
        self.assertFalse(comparison["calibrated_probability_available_for_production"])
        self.assertTrue(comparison["insufficient_evidence"])
        self.assertEqual(comparison["minimum_monitoring_blocks"], monitoring_block_minimum())
        self.assertEqual(comparison["status"], "证据不足")
        self.assertLess(comparison["test_nonoverlapping_date_blocks"], monitoring_block_minimum())
        self.assertTrue(all(rule["promoted"] is False for rule in comparison["out_of_sample_descriptive"]["rules"]))
        self.assertTrue(all(rule["status"] == "证据不足" for rule in comparison["out_of_sample_descriptive"]["rules"]))
        self.assertIn("样本内参照", comparison["in_sample_reference_not_a_result"]["label"])

    def test_monitoring_gate_is_twenty_four_blocks_and_eight_is_not_enough(self):
        minimum = monitoring_block_minimum()
        self.assertEqual(
            minimum,
            REVIEW_POLICY["reference_blocks"] + 2 * REVIEW_POLICY["recent_window_blocks"],
        )
        self.assertEqual(minimum, 24)
        self.assertGreater(minimum, REVIEW_POLICY["reference_blocks"])
        self.assertGreater(minimum, REVIEW_POLICY["recent_window_blocks"])
        short = []
        for index in range(8):
            day = date(2026, 1, 5) + timedelta(days=40 * index)
            short.append(prediction(
                prediction_id=f"s{index}",
                day=day,
                symbol=f"TEST.S{index}",
                score=0.3 if index % 2 == 0 else -0.3,
                event=index % 2 == 0,
            ))
        short_report = report_for(*short)
        self.assertEqual(short_report["overlap"]["nonoverlapping_date_blocks"], 8)
        self.assertEqual(short_report["overlap"]["minimum_monitoring_blocks"], 24)
        self.assertTrue(short_report["overlap"]["insufficient_evidence"])
        self.assertTrue(short_report["direction"]["insufficient_evidence"])
        self.assertIsNone(short_report["overlap"]["block_bootstrap"]["percentile_2_5"])
        self.assertIn("24", short_report["overlap"]["block_bootstrap"]["reason"])
        groups = []
        for index in range(minimum):
            day = date(2026, 1, 5) + timedelta(days=40 * index)
            groups.append(prediction(
                prediction_id=f"b{index}",
                day=day,
                symbol=f"TEST.B{index}",
                score=0.3 if index % 2 == 0 else -0.3,
                event=index % 2 == 0,
            ))
        first = report_for(*groups)
        second = report_for(*groups)
        overlap = first["overlap"]
        self.assertEqual(overlap["nonoverlapping_date_blocks"], 24)
        self.assertFalse(overlap["insufficient_evidence"])
        bootstrap = overlap["block_bootstrap"]
        self.assertIn("描述性", bootstrap["status"])
        self.assertGreaterEqual(bootstrap["percentile_2_5"], 0)
        self.assertLessEqual(bootstrap["percentile_97_5"], 1)
        self.assertLessEqual(bootstrap["percentile_2_5"], bootstrap["percentile_97_5"])
        self.assertEqual(first["overlap"]["block_bootstrap"], second["overlap"]["block_bootstrap"])
        self.assertFalse(first["predictive_improvement_claim_allowed"])
        self.assertFalse(first["candidate_promoted"])

    def test_as_of_uses_prediction_dates_and_five_session_exit_not_event_time(self):
        future_decision = prediction(
            prediction_id="future",
            day=date(2026, 7, 1),
            symbol="TEST.FUTURE",
            event=True,
            recorded_at="2026-06-01T10:00:00+08:00",
            settlement_recorded_at="2026-06-02T10:00:00+08:00",
        )
        early_label = prediction(
            prediction_id="early-label",
            day=date(2026, 6, 1),
            symbol="TEST.EARLY",
            event=True,
            recorded_at="2026-06-01T11:00:00+08:00",
            settlement_recorded_at="2026-06-03T11:00:00+08:00",
            entry_date=date(2026, 6, 2),
            exit_date=date(2026, 6, 8),
        )
        visible = prediction(
            prediction_id="visible",
            day=date(2026, 5, 20),
            symbol="TEST.VISIBLE",
            score=-0.2,
            event=False,
            recorded_at="2026-05-20T15:00:00+08:00",
            settlement_recorded_at="2026-05-29T15:00:00+08:00",
            entry_date=date(2026, 5, 21),
            exit_date=date(2026, 5, 27),
        )
        original = json.dumps([*future_decision, *early_label, *visible], sort_keys=True)
        report = report_for(future_decision, early_label, visible, as_of="2026-06-04")
        self.assertEqual(json.dumps([*future_decision, *early_label, *visible], sort_keys=True), original)
        self.assertEqual(report["cutoff"]["excluded_decision_after_report_as_of"], 1)
        self.assertEqual(report["labels"]["counts"].get("label_not_knowable_at_as_of"), 1)
        self.assertEqual(report["direction"]["nonzero_production_definition"]["rows"], 1)
        self.assertEqual(report["direction"]["nonzero_production_definition"]["hits"], 1)
        self.assertFalse(report["audit_rewritten"])

    def test_zero_scores_leave_the_direction_denominator_and_do_not_create_blocks(self):
        zeros = [
            prediction(prediction_id=f"z{index}", day=date(2026, 4, 1) + timedelta(days=index), score=0.0, event=False)
            for index in range(3)
        ]
        nonzero = prediction(prediction_id="nz", day=date(2026, 4, 10), score=0.4, event=True)
        report = report_for(*zeros, nonzero)
        direction = report["direction"]["nonzero_production_definition"]
        inclusive = report["direction"]["all_finite_scores_nonpositive_means_not_up"]
        self.assertEqual(direction["rows"], 1)
        self.assertEqual(inclusive["rows"], 4)
        self.assertIsNone(direction["beats_constant_negative"])
        self.assertIsNone(report["direction"]["beats_constant_negative_direction_baseline"])
        self.assertIsNone(report["auc"]["ensemble_rule_score_direction_sample"])
        spaced = [
            prediction(
                prediction_id="left",
                day=date(2026, 1, 5),
                symbol="TEST.LEFT",
                score=0.4,
                event=True,
                entry_date=date(2026, 1, 6),
                exit_date=date(2026, 1, 12),
            ),
            prediction(
                prediction_id="zero-gap",
                day=date(2026, 2, 2),
                symbol="TEST.GAP",
                score=0.0,
                event=False,
                entry_date=date(2026, 2, 3),
                exit_date=date(2026, 2, 9),
            ),
            prediction(
                prediction_id="right",
                day=date(2026, 3, 2),
                symbol="TEST.RIGHT",
                score=-0.4,
                event=False,
                entry_date=date(2026, 3, 3),
                exit_date=date(2026, 3, 9),
            ),
        ]
        blocked = report_for(*spaced)
        self.assertEqual(blocked["overlap"]["nonoverlapping_date_blocks"], 2)
        self.assertGreater(blocked["overlap"]["resolvable_blocks_including_near_zero_scores"], 2)
        self.assertTrue(blocked["overlap"]["near_zero_scores_do_not_add_monitoring_blocks"])
        touching = [
            prediction(
                prediction_id="window",
                day=date(2026, 1, 2),
                symbol="TEST.WINDOW",
                score=0.5,
                event=True,
                entry_date=date(2026, 1, 5),
                exit_date=date(2026, 1, 13),
            ),
            prediction(
                prediction_id="still-inside",
                day=date(2026, 1, 6),
                symbol="TEST.WINDOW",
                score=-0.5,
                event=False,
                entry_date=date(2026, 1, 7),
                exit_date=date(2026, 1, 13),
            ),
            prediction(
                prediction_id="after-exit",
                day=date(2026, 1, 14),
                symbol="TEST.NEXT",
                score=0.5,
                event=False,
                entry_date=date(2026, 1, 15),
                exit_date=date(2026, 1, 21),
            ),
        ]
        windows = report_for(*touching)
        self.assertEqual(windows["overlap"]["nonoverlapping_date_blocks"], 2)
        self.assertGreater(windows["overlap"]["representative_rows_skipped_because_cutoff_falls_inside_prior_window"], 0)
        self.assertGreater(windows["overlap"]["overlapping_same_symbol_window_pairs"], 0)

    def test_multiple_settlements_and_broken_chain_stop_confident_scoring(self):
        created, settled = prediction(prediction_id="twice", day=date(2026, 2, 2), event=True)
        extra = dict(settled)
        extra["target_return"] = 0.03
        report = report_for([created, settled, extra])
        self.assertEqual(report["labels"]["counts"].get("ambiguous_multiple_settlements"), 1)
        self.assertEqual(report["direction"]["nonzero_production_definition"]["rows"], 0)
        self.assertEqual(report["structural_warnings"]["multiple_settlement_predictions"], 1)
        tampered = seal(prediction(prediction_id="one", day=date(2026, 2, 3), event=True))
        tampered[0]["rule_score"] = 0.99
        paused = build_research_report(tampered, engine="python")
        self.assertTrue(paused["scoring_paused"])
        self.assertIsNone(paused["direction"]["direction_hit_rate"])
        self.assertFalse(paused["predictive_improvement_claim_allowed"])

    def test_cli_is_read_only_and_research_imports_stay_lazy(self):
        cli_tree = ast.parse(Path("src/xiao_a/cli.py").read_text(encoding="utf-8"))
        for node in cli_tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                text = ast.dump(node)
                self.assertNotIn("research_diagnostics", text)
                self.assertNotIn("duckdb", text)
        dashboard = Path("src/xiao_a/dashboard.py").read_text(encoding="utf-8")
        self.assertNotIn("research_diagnostics", dashboard)
        research_tree = ast.parse(Path("src/xiao_a/research_diagnostics.py").read_text(encoding="utf-8"))
        for node in research_tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                text = ast.dump(node)
                self.assertNotIn("decision", text)
                self.assertNotIn("ensemble", text)
                self.assertNotIn("duckdb", text)
                self.assertNotIn("vectorbt", text)
                self.assertNotIn("quantstats", text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prediction_audit.jsonl"
            ledger = PredictionLedger(path)
            record, _created = ledger.create_prediction({
                "symbol": "TEST.AAA001",
                "as_of": "2026-01-05T15:00:00+08:00",
                "information_cutoff": "2026-01-05T15:00:00+08:00",
                "model_versions": {"advisor": "test"},
                "input_snapshot": {"features": {}, "quote": {"price": 1}},
                "configuration": {},
                "account_snapshot": {},
                "recommendation": {"model_action": "buy", "account_action": "no_buy"},
                "rule_score": 0.2,
                "score_type": "rule_conformance_score",
                "target": dict(TARGET),
            })
            ledger.settle(record["prediction_id"], {
                "status": "settled", "event_occurred": False, "target_return": -0.01,
                "resolved_at": "2026-01-12", "entry_date": "2026-01-06", "exit_date": "2026-01-12",
                "execution_simulation": "not_applied",
            })
            before = path.read_bytes()
            real_open = Path.open

            def guarded_open(self, mode="r", *args, **kwargs):
                if self == path and any(flag in mode for flag in ("w", "a", "x", "+")):
                    raise AssertionError(f"audit opened for write: {mode}")
                return real_open(self, mode, *args, **kwargs)

            stdout = StringIO()
            with redirect_stdout(stdout):
                Path.open = guarded_open
                try:
                    code = main(["research", "audit", "--audit", str(path), "--engine", "duckdb"])
                finally:
                    Path.open = real_open
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["audit_basename"], "prediction_audit.jsonl")
            duckdb_report = payload["engine"]["duckdb"]
            self.assertFalse(duckdb_report["persistent_sidecar_written"])
            self.assertEqual(payload["engine"]["authoritative"], "python")
            if duckdb_report["status"] == "ok":
                self.assertTrue(duckdb_report["matches_python"])
            else:
                self.assertIn(duckdb_report["status"], {"unavailable", "error"})
            stderr = StringIO()
            with redirect_stderr(stderr), redirect_stdout(StringIO()):
                refused = main(["research", "audit", "--audit", str(path), "--output", str(path)])
            self.assertEqual(refused, 2)
            self.assertIn("拒绝", stderr.getvalue())
            self.assertEqual(path.read_bytes(), before)
            stdout = StringIO()
            spec = Path(__file__).parent / "fixtures" / "research_backtest_spec.json"
            with redirect_stdout(stdout):
                backtest_code = main(["research", "backtest", "--spec", str(spec)])
            self.assertEqual(backtest_code, 0)
            backtest = json.loads(stdout.getvalue())
            self.assertEqual(backtest["authority"], "AShareBacktester")
            self.assertFalse(backtest["promoted_to_production"])


if __name__ == "__main__":
    unittest.main()
