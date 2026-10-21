from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from xiao_a.auto import AutoAnalysis
from xiao_a.models import AdvisorDecision, DataStatus, Quote, SignalAction
from xiao_a.screener import _sina_snapshot_row, eligible_rows, fetch_a_share_snapshot, run_market_screener


def _raw(code: str, name: str, *, amount: float = 200_000_000, change: float = 2.0) -> dict:
    return {
        "f12": code, "f14": name, "f2": 12.5, "f3": change, "f5": 20_000,
        "f6": amount, "f7": 3.0, "f8": 3.0, "f9": 20, "f18": 12.2,
        "f20": 10_000_000_000, "f21": 7_000_000_000, "f22": 0.4, "f23": 2.0,
    }


class _Analyzer:
    def analyze(self, symbol, risk_context=None, *, include_supplemental=True):
        action = SignalAction.BUY if symbol.endswith("000001") else SignalAction.HOLD
        decision = AdvisorDecision(
            symbol=symbol, as_of=datetime.now(timezone.utc), raw_action=action, final_action=action,
            score=0.6 if action is SignalAction.BUY else 0.2, confidence=0.72,
            executable=True, reasons=[], warnings=[], vetoes=[], strategy_signals=[], strategy_weights={},
        )
        return AutoAnalysis(symbol, Quote(symbol, "测试", 12.5, 12.2), 360, 360, {}, [], decision, [], [], [])


class ScreenerTests(unittest.TestCase):
    def test_sina_row_uses_dashboard_units(self):
        row = _sina_snapshot_row({
            "code": "300001", "name": "测试科技", "trade": "12.50", "changepercent": "2.4",
            "settlement": "12.20", "high": "12.80", "low": "12.10", "volume": "20000",
            "amount": "250000", "turnoverratio": "1.8", "per": "23", "pb": "2.1",
            "mktcap": "123456", "nmc": "100000",
        })
        self.assertIsNotNone(row)
        self.assertEqual(row["f12"], "300001")
        self.assertEqual(row["f5"], 200.0)
        self.assertEqual(row["f20"], 1_234_560_000.0)
        self.assertAlmostEqual(row["f7"], (12.8 - 12.1) / 12.2 * 100)

    @patch("xiao_a.screener._fetch_sina_snapshot", return_value=[_raw("000001", "平安银行")])
    @patch("xiao_a.screener._fetch_eastmoney_snapshot", side_effect=RuntimeError("connection closed"))
    def test_snapshot_switches_to_sina_when_eastmoney_disconnects(self, _eastmoney, sina):
        rows = fetch_a_share_snapshot()
        self.assertEqual(rows[0]["f12"], "000001")
        sina.assert_called_once()

    def test_eligible_rows_excludes_st_delisted_and_illiquid(self):
        rows = eligible_rows([
            _raw("000001", "平安银行"), _raw("000002", "*ST测试"),
            _raw("000003", "退市测试"), _raw("000004", "低流动", amount=2_000_000),
        ], minimum_amount=50_000_000)
        self.assertEqual([item["code"] for item in rows], ["000001"])

    def test_manual_scan_runs_second_stage_and_ranks_buy_first(self):
        result = run_market_screener(
            _Analyzer(), minimum_amount=50_000_000, result_limit=10,
            snapshot_fetcher=lambda: [_raw("000002", "万科A"), _raw("000001", "平安银行")],
        )
        self.assertEqual(result["universe_count"], 2)
        self.assertEqual(result["results"][0]["final_action"], "buy")
        self.assertEqual(result["results"][0]["rank"], 1)

