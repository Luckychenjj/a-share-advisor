from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path

from xiao_a.cli import main
from xiao_a.research_candidates import (
    build_candidate_report,
    config_fingerprint,
    evaluate_candidate,
    evaluate_execution_spec,
    production_action,
)
from xiao_a.strategies.core import _action
from tests.test_research_diagnostics import prediction, seal


def _signals(regime: str = "中性震荡", available: list[str] | None = None, invalid: bool = False) -> list[dict]:
    return [{
        "strategy": "market_regime",
        "score": 0.9,
        "metadata": {
            "regime": regime,
            "available_inputs": ["weekly_trend"] if available is None else available,
            "invalidation_active": invalid,
        },
    }]


def _row(index: int, **kwargs) -> list[dict]:
    day = kwargs.pop("day", date(2026, 1, 5) + timedelta(days=40 * index))
    scores = kwargs.pop("submodel_scores", {
        "cycle_trend": 0.4, "leader_sentiment": 0.4, "chan_structure": 0.4,
        "multi_factor": 0.4, "market_regime": 0.3,
    })
    events = prediction(
        prediction_id=kwargs.pop("prediction_id", f"c{index}"),
        day=day,
        symbol=kwargs.pop("symbol", f"TEST.C{index:02d}"),
        score=kwargs.pop("score", 0.4),
        event=kwargs.pop("event", True),
        model_action=kwargs.pop("model_action", "buy"),
        account_action=kwargs.pop("account_action", "hold"),
        submodel_scores=scores,
        entry_date=kwargs.pop("entry_date", None),
        exit_date=kwargs.pop("exit_date", None),
        recorded_at=kwargs.pop("recorded_at", None),
        settlement_recorded_at=kwargs.pop("settlement_recorded_at", None),
    )
    events[0]["strategy_signals"] = kwargs.pop("strategy_signals", _signals())
    events[0]["input_snapshot"] = {"bars": [{"trading_date": "2026-01-02", "close": 10}]}
    if kwargs:
        raise AssertionError(kwargs)
    return events


def _report(*groups: list[dict], as_of: str | None = None, execution_spec: dict | None = None) -> dict:
    events = []
    for group in groups:
        events.extend(group)
    return build_candidate_report(seal(events), as_of=as_of, execution_spec=execution_spec)


class FrozenCandidateTests(unittest.TestCase):
    def test_thresholds_match_production_and_fingerprint_ignores_outcomes(self):
        for score in (0.25, 0.249, 0.0, -0.2, -0.55, -0.549):
            self.assertEqual(production_action(score), _action(score).value)
        first = config_fingerprint()
        bullish = _report(_row(0, event=True))
        bearish = _report(_row(0, event=False))
        self.assertEqual(first, bullish["config_fingerprint"])
        self.assertEqual(bullish["config_fingerprint"], bearish["config_fingerprint"])
        self.assertIsNone(bullish["selected_candidate"])
        self.assertFalse(bullish["winner_chosen_from_outcomes"])
        self.assertTrue(bullish["retrospective_confirmation_only"])
        self.assertFalse(bullish["promoted"])
        self.assertFalse(bullish["predictive_improvement_claim_allowed"])

    def test_coverage_abstention_disagreement_and_separate_precision(self):
        agreed = _row(0, score=0.10, event=True, submodel_scores={
            "cycle_trend": 0.4, "leader_sentiment": 0.3, "chan_structure": 0.0,
            "multi_factor": 0.4, "market_regime": 0.2,
        })
        disagreed = _row(1, score=0.8, event=False, model_action="hold", submodel_scores={
            "cycle_trend": 0.4, "leader_sentiment": -0.4, "chan_structure": 0.0,
            "multi_factor": None, "market_regime": 0.2,
        })
        report = _report(agreed, disagreed)
        self.assertEqual(report["universe"]["rows"], 2)
        self.assertTrue(report["universe"]["same_universe_for_every_candidate_and_benchmark"])
        ensemble = report["comparison"]["candidates"]["ensemble_baseline"]
        factor = report["comparison"]["candidates"]["multi_factor_alone"]
        consensus = report["comparison"]["candidates"]["consensus_only"]
        self.assertEqual(ensemble["universe_rows"], factor["universe_rows"])
        self.assertEqual(ensemble["directional_rows"], 2)
        self.assertEqual(ensemble["buy_rows"], 1)
        self.assertNotEqual(ensemble["direction_accuracy"], ensemble["positive_event_precision"])
        self.assertEqual(factor["abstentions"], 1)
        self.assertEqual(factor["buy_rows"], 1)
        self.assertEqual(consensus["abstentions"], 1)
        self.assertEqual(consensus["directional_rows"], 1)
        self.assertEqual(consensus["buy_rows"], 0)
        self.assertEqual(consensus["direction_accuracy"], 1)
        self.assertEqual(report["comparison"]["benchmarks"]["no_signal"]["coverage"], 0)
        self.assertEqual(report["comparison"]["benchmarks"]["no_signal"]["missed_positive_events"], 1)
        self.assertEqual(report["comparison"]["benchmarks"]["always_positive"]["positive_event_precision"], 0.5)
        self.assertFalse(report["comparison"]["benchmarks"]["always_negative"]["opens_short"])
        self.assertEqual(evaluate_candidate("consensus_only", agreed[0])["direction"], "up")
        self.assertEqual(evaluate_candidate("consensus_only", agreed[0])["action"], "hold")
        self.assertEqual(evaluate_candidate("consensus_only", disagreed[0])["reason"], "disagreement")
        self.assertEqual(evaluate_candidate("multi_factor_alone", disagreed[0])["reason"], "missing_multi_factor")
        poisoned = dict(agreed[0])
        poisoned["submodel_scores"] = dict(agreed[0]["submodel_scores"])
        poisoned["submodel_scores"]["target_return"] = -1
        self.assertEqual(evaluate_candidate("consensus_only", poisoned)["direction"], "up")
        self.assertEqual(evaluate_candidate("ensemble_baseline", poisoned)["action"], "hold")

    def test_regime_comes_from_saved_metadata_and_groups_stay_exploratory(self):
        first = _row(0, event=True, strategy_signals=_signals("中性震荡"))
        second = _row(1, event=False, strategy_signals=_signals("中性震荡", invalid=True))
        second[0]["strategy_signals"][0]["score"] = -0.9
        report = _report(first, second)
        groups = report["buy_error_attribution"]["slices"]["model_action"]["regime"]
        self.assertEqual([item["group"] for item in groups], ["中性震荡"])
        self.assertEqual(groups[0]["rows"], 2)
        self.assertFalse(groups[0]["confirmatory"])
        self.assertFalse(report["buy_error_attribution"]["confirmatory"])
        self.assertFalse(report["buy_error_attribution"]["uses_future_prices"])
        self.assertEqual(report["buy_error_attribution"]["slices"]["model_action"]["cohorts"]["legacy_unspecified_rows"], 2)
        missing = _row(2, event=True, strategy_signals=[])
        missing_report = _report(missing)
        self.assertEqual(missing_report["buy_error_attribution"]["slices"]["model_action"]["regime"][0]["group"], "missing")

    def test_as_of_leakage_purges_later_labels_and_keeps_gate(self):
        visible = _row(
            0, day=date(2026, 5, 4), event=False, score=-0.4,
            entry_date=date(2026, 5, 6), exit_date=date(2026, 5, 12),
            recorded_at="2026-05-04T15:00:00+08:00",
            settlement_recorded_at="2026-05-12T16:00:00+08:00",
        )
        later_label = _row(
            1, day=date(2026, 5, 20), event=True, score=0.4,
            entry_date=date(2026, 5, 21), exit_date=date(2026, 5, 29),
            recorded_at="2026-05-20T15:00:00+08:00",
            settlement_recorded_at="2026-05-22T15:00:00+08:00",
        )
        future_decision = _row(
            2, day=date(2026, 7, 1), event=True,
            recorded_at="2026-05-21T15:00:00+08:00",
            settlement_recorded_at="2026-05-21T16:00:00+08:00",
        )
        report = _report(visible, later_label, future_decision, as_of="2026-05-25")
        self.assertEqual(report["universe"]["rows"], 1)
        self.assertEqual(report["comparison"]["candidates"]["ensemble_baseline"]["direction_hits"], 1)
        self.assertTrue(report["out_of_sample"]["insufficient_evidence"])
        self.assertEqual(report["out_of_sample"]["minimum_monitoring_blocks"], 24)
        self.assertLess(report["out_of_sample"].get("test_nonoverlapping_date_blocks", 0), 24)
        self.assertIsNone(report["selected_candidate"])

    def test_audit_paths_never_become_net_pnl(self):
        events = _row(0, event=True)
        events[0]["settlement_hint"] = {"target_return": 0.4}
        report = _report(events)
        pnl = report["net_pnl"]
        self.assertEqual(pnl["status"], "unavailable")
        self.assertIsNone(pnl["total_return"])
        self.assertIsNone(pnl["max_drawdown"])
        self.assertFalse(pnl["uses_label_return_as_net_return"])
        self.assertEqual(pnl["snapshot_bar_rows_used"], 0)
        self.assertGreaterEqual(pnl["snapshot_bar_rows_seen"], 1)
        self.assertIn("adjustment_factors_not_verified", pnl["reasons"])
        self.assertIn("suspension_st_and_limit_flags_are_not_verified_tradability", pnl["reasons"])
        refused = evaluate_execution_spec({
            "target_return": 0.4,
            "event_occurred": True,
            "quote_limit_up": 11,
            "coverage_proxy": True,
            "fees": {"commission_rate": 0, "minimum_commission": 0, "stamp_duty_sell": 0, "transfer_fee_rate": 0},
        })
        self.assertEqual(refused["status"], "unavailable")
        self.assertIsNone(refused["total_return"])
        self.assertNotEqual(refused.get("total_return"), 0.4)

    def test_execution_adapter_refuses_incomplete_evidence_and_fills_next_open_when_proven(self):
        fees = {"commission_rate": 0, "minimum_commission": 0, "stamp_duty_sell": 0, "transfer_fee_rate": 0}
        bars = [
            {"trading_date": "2026-01-05", "open": 10, "high": 10.4, "low": 9.8, "close": 10.2, "previous_close": 9.8, "suspended": False, "is_st": False, "limit_rate": 0.1},
            {"trading_date": "2026-01-06", "open": 12, "high": 12.4, "low": 11.6, "close": 12.2, "previous_close": 10.2, "suspended": False, "is_st": False, "limit_rate": 0.1},
            {"trading_date": "2026-01-07", "open": 12.2, "high": 12.5, "low": 11.8, "close": 12.1, "previous_close": 12.2, "suspended": False, "is_st": False, "limit_rate": 0.1},
        ]
        base = {
            "path_proof": "future_execution_bars",
            "not_a_coverage_proxy": True,
            "adjustment_factors_verified": True,
            "tradability_verified": True,
            "fees_historically_applicable": True,
            "price_basis": "qfq",
            "slippage_bps": 0,
            "initial_cash": 100000,
            "target_position_pct": 1,
            "fees": fees,
            "symbols": {"TEST.FAKE01": {"bars": bars, "signals": [
                {"decision_date": "2026-01-05", "action": "buy"},
                {"decision_date": "2026-01-06", "action": "sell"},
            ]}},
        }
        for broken in (
            {**base, "tradability_verified": False},
            {**base, "adjustment_factors_verified": False},
            {**base, "fees_historically_applicable": False},
            {**base, "path_proof": "snapshot_coverage"},
        ):
            refused = evaluate_execution_spec(broken)
            self.assertEqual(refused["status"], "unavailable")
            self.assertIsNone(refused["total_return"])
        pre_only = json.loads(json.dumps(base))
        pre_only["symbols"]["TEST.FAKE01"]["bars"] = [bars[0], {**bars[0], "trading_date": "2026-01-04"}]
        pre_only["symbols"]["TEST.FAKE01"]["bars"].sort(key=lambda item: item["trading_date"])
        pre_only["symbols"]["TEST.FAKE01"]["signals"] = [{"decision_date": "2026-01-05", "action": "buy"}]
        missing_entry = evaluate_execution_spec(pre_only)
        self.assertEqual(missing_entry["symbols"][0]["reason"], "entry_bar_missing")
        self.assertIsNone(missing_entry["symbols"][0]["total_return"])
        no_limit = json.loads(json.dumps(base))
        del no_limit["symbols"]["TEST.FAKE01"]["bars"][1]["limit_rate"]
        no_limit["quote_limit_up"] = 11
        self.assertEqual(evaluate_execution_spec(no_limit)["symbols"][0]["reason"], "missing_limit_rate")
        ran = evaluate_execution_spec(base)
        self.assertIsNone(ran["global_net_pnl"])
        self.assertIsNone(ran["total_return"])
        symbol = ran["symbols"][0]
        self.assertEqual(symbol["trades"][0]["date"], "2026-01-06")
        self.assertEqual(symbol["trades"][0]["price"], 12)
        self.assertEqual(symbol["trades"][1]["date"], "2026-01-07")
        self.assertNotEqual(symbol["total_return"], 0.4)
        self.assertIsNotNone(symbol["matched_buy_hold"])
        self.assertGreaterEqual(symbol["matched_buy_hold"]["ending_quantity"], 0)
        short = json.loads(json.dumps(base))
        short["symbols"]["TEST.FAKE01"]["signals"] = [{"decision_date": "2026-01-05", "action": "sell"}]
        short_result = evaluate_execution_spec(short)["symbols"][0]
        self.assertEqual(short_result["ending_quantity"], 0)
        self.assertTrue(any("short" in item for item in short_result["rejected_orders"]))

    def test_cli_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            events = seal(_row(0))
            path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8")
            before = path.read_bytes()
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(["research", "candidates", "--audit", str(path), "--as-of", "2026-06-01"])
            self.assertEqual(code, 0)
            self.assertEqual(path.read_bytes(), before)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema"], "xiao_a_research_candidates_v1")
            self.assertEqual(payload["net_pnl"]["status"], "unavailable")
            self.assertFalse(payload["audit_rewritten"])


if __name__ == "__main__":
    unittest.main()
