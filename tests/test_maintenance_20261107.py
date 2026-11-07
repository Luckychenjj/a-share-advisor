"""Fixes for 2026-11-03 settlement / ETF / BJ sample failures."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from xiao_a import dashboard
from xiao_a.data.kline import KlineResult, KlineRouter, SinaKlineProvider, parse_tencent_kline_payload
from xiao_a.data.symbols import is_etf, normalize_symbol
from xiao_a.models import DailyBar, DataStatus
from xiao_a.prediction_audit import PredictionLedger, TARGET_DEFINITION


SHANGHAI = ZoneInfo("Asia/Shanghai")


class EtfSymbolHelpers(unittest.TestCase):
    def test_etf_prefixes(self):
        self.assertTrue(is_etf("159513"))
        self.assertTrue(is_etf("SZ.159513"))
        self.assertTrue(is_etf("510300"))
        self.assertFalse(is_etf("300502"))
        self.assertFalse(is_etf("920060"))
        self.assertEqual(normalize_symbol("159513").canonical, "SZ.159513")


class TencentEtfDayFallback(unittest.TestCase):
    def test_etf_qfq_uses_day_when_qfqday_missing(self):
        rows = [["2026-10-08", "1.70", "1.71", "1.72", "1.69", "1000"]]
        payload = json.dumps({"data": {"sz159513": {"day": rows}}})
        result = parse_tencent_kline_payload(payload, "SZ.159513")
        self.assertEqual(result.status, DataStatus.FRESH)
        self.assertEqual(result.bars[0].close, 1.71)
        self.assertTrue(any("day series" in w for w in result.warnings))

    def test_ordinary_stock_still_refuses_day_as_qfq(self):
        rows = [["2026-10-08", "100", "101", "102", "99", "1000"]]
        payload = json.dumps({"data": {"sz000001": {"day": rows}}})
        result = parse_tencent_kline_payload(payload, "SZ.000001")
        self.assertEqual(result.status, DataStatus.UNAVAILABLE)


class KlineRouterUnadjustedFallback(unittest.TestCase):
    def test_router_falls_back_to_unadjusted_when_qfq_empty(self):
        class QfqBlind:
            name = "blind"
            priority = 1

            def fetch_daily(self, symbol, *, limit=360, adjustment="qfq"):
                if adjustment == "qfq":
                    raise RuntimeError("no qfq")
                bars = [DailyBar(date(2026, 10, 8), 10, 11, 9, 10.5)]
                return KlineResult(bars, DataStatus.FRESH)

        result = KlineRouter([QfqBlind()]).fetch_daily("SH.688197", limit=30)
        self.assertEqual(result.status, DataStatus.FRESH)
        self.assertEqual(len(result.bars), 1)
        self.assertTrue(any("unadjusted" in w for w in result.warnings))


class SinaAllowsEtfQfqRequest(unittest.TestCase):
    def test_sina_accepts_etf_qfq_without_raising(self):
        provider = SinaKlineProvider()
        # Should not raise the stock-only ValueError before the HTTP call.
        with patch("xiao_a.data.kline.open_market_url") as opener:
            payload = json.dumps([
                {"day": "2026-10-08", "open": "1.70", "high": "1.72", "low": "1.69", "close": "1.71", "volume": "100000"}
            ])
            response = Mock()
            response.read.return_value = payload.encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            opener.return_value = response
            result = provider.fetch_daily("159513", limit=30, adjustment="qfq")
        self.assertEqual(result.status, DataStatus.FRESH)
        self.assertEqual(result.bars[0].close, 1.71)


class SettlePendingAsyncAndSkipNotDue(unittest.TestCase):
    def test_not_due_predictions_are_not_written_to_ledger(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 15, 10, tzinfo=tz)

        pending = [{
            "prediction_id": "fixture-young",
            "symbol": "SZ.300502",
            "information_cutoff": "2026-10-12T15:00:00+08:00",
            "rule_score": 0.5,
            "target": dict(TARGET_DEFINITION),
        }]
        ledger = Mock()
        ledger.records.return_value = pending
        provider = Mock()
        with patch.object(dashboard, "datetime", Clock), \
                patch.object(dashboard, "_PREDICTION_LEDGER", ledger), \
                patch.object(dashboard, "_BACKTEST_KLINE_PROVIDER", provider):
            result = dashboard._settle_forward_predictions()
        provider.fetch_daily.assert_not_called()
        ledger.append.assert_not_called()
        ledger.settle.assert_not_called()
        self.assertEqual(result["skipped_not_due"], 1)
        self.assertFalse(result["pending_reasons"][0]["recorded"])
        self.assertIn("尚未完整", result["pending_reasons"][0]["reason"])

    def test_async_settle_returns_immediately_and_completes_in_background(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PredictionLedger(Path(directory) / "audit.jsonl")
            # Replace module ledger and make settle work cheap.
            barrier = {"done": False}

            def fake_settle():
                time.sleep(0.2)
                barrier["done"] = True
                return {"settled_count": 0, "settled_ids": [], "pending_count": 0, "pending_reasons": [], "skipped_not_due": 0}

            with patch.object(dashboard, "_PREDICTION_LEDGER", ledger), \
                    patch.object(dashboard, "_settle_forward_predictions", side_effect=fake_settle), \
                    patch.object(ledger, "check_review_reminders", return_value={"status": "ok", "alerts": []}), \
                    patch.object(ledger, "report", return_value={"counts": {}}):
                # Reset job state between tests
                with dashboard._SETTLE_JOB_LOCK:
                    dashboard._SETTLE_JOB.update({
                        "status": "idle", "job_id": None, "started_at": None,
                        "finished_at": None, "result": None, "error": None,
                    })
                started = time.monotonic()
                job = dashboard._start_settle_job()
                self.assertLess(time.monotonic() - started, 0.15)
                self.assertEqual(job["status"], "running")
                self.assertTrue(job["job_id"])
                for _ in range(50):
                    snap = dashboard._settle_job_snapshot()
                    if snap["status"] in {"done", "error"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(snap["status"], "done")
                self.assertTrue(barrier["done"])
                self.assertIn("settlement", snap["result"])


if __name__ == "__main__":
    unittest.main()
