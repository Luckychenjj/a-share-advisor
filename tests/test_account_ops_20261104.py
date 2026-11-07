"""2026-11-04: cash-adjust, account fee config, explicit fee / fill time, 昨收估值."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from xiao_a import dashboard
from xiao_a.data.router import ProviderResult
from xiao_a.models import DataStatus, Provenance, Quote
from xiao_a.portfolio_store import PortfolioStore

from test_maintenance_20261024 import _Server

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _post(base, path, payload):
    request = Request(base + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


class CashAdjust(unittest.TestCase):
    def test_set_delta_audit_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            first = store.save([{"symbol": "300613", "quantity": 500, "average_cost": 64.2}], 19176.48)
            state, entry = store.adjust_cash(cash=22.79, note="按券商10:45截图对齐", source="test")
            self.assertEqual(state["version"], first["version"] + 1)
            self.assertAlmostEqual(state["cash"], 22.79)
            self.assertEqual(entry["mode"], "set")
            self.assertAlmostEqual(entry["before"], 19176.48)
            self.assertEqual(state["holdings"], first["holdings"])
            state, entry = store.adjust_cash(amount=-2.79, note="delta")
            self.assertAlmostEqual(state["cash"], 20.0)
            lines = (Path(directory) / "cash_audit.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(len(store.load()["cash_adjustments"]), 2)
            # audit list survives later writes
            store.apply_trade("300613", "buy", 100, 0.1, 0.0, minimum_commission=0)
            self.assertEqual(len(store.load()["cash_adjustments"]), 2)

    def test_rejects_bad_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            store.save([], 100)
            for kwargs in ({"cash": 1, "note": ""}, {"note": "x"}, {"cash": 1, "amount": 1, "note": "x"},
                           {"amount": -200, "note": "x"}):
                with self.assertRaises(ValueError):
                    store.adjust_cash(**kwargs)
            self.assertEqual(store.load()["version"], 1)

    def test_http_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            store.save([], 100)
            with _Server() as base, patch("xiao_a.dashboard._PORTFOLIO_STORE", store):
                status, body = _post(base, "/api/cash-adjust", {"cash": 22.79, "note": "对齐"})
                self.assertEqual(status, 200)
                self.assertAlmostEqual(body["state"]["cash"], 22.79)
                status, body = _post(base, "/api/cash-adjust", {"cash": 1})
                self.assertEqual(status, 400)
                with urlopen(base + "/api/cash-adjust", timeout=10) as response:
                    self.assertEqual(len(json.loads(response.read())["adjustments"]), 1)


class FeesAndTime(unittest.TestCase):
    def test_legacy_defaults_unchanged_without_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            store.save([], 100000)
            _, tx = store.apply_trade("300613", "buy", 100, 64.0, 0.0003)
            self.assertAlmostEqual(tx["commission"], 5.0)
            self.assertAlmostEqual(tx["transfer_fee"], 6400 * 0.00001)
            self.assertEqual(tx["fee_source"], "calculated")

    def test_config_free_five_and_fund_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            store.save([], 100000)
            store.save_fee_config({"commission_rate": 0.0002, "free_five": True, "fund_no_stamp_or_transfer": True})
            _, tx = store.apply_trade("300613", "buy", 300, 64.01, None)
            self.assertAlmostEqual(tx["commission"], 19203 * 0.0002)
            _, tx = store.apply_trade("159513", "buy", 1000, 1.78, None)
            self.assertEqual(tx["transfer_fee"], 0.0)
            # explicit request fields still win over the config
            _, tx = store.apply_trade("300613", "buy", 100, 64.0, 0.0003, minimum_commission=5)
            self.assertAlmostEqual(tx["commission"], 5.0)
            with self.assertRaises(ValueError):
                store.save_fee_config({"bogus": 1})

    def test_explicit_fee_and_trade_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            store.save([], 100000)
            today = datetime.now(SHANGHAI).date().isoformat()
            _, tx = store.apply_trade("300613", "buy", 300, 64.01, None, fee=4.03, traded_at=f"{today} 09:31:05")
            self.assertAlmostEqual(tx["total_fee"], 4.03)
            self.assertAlmostEqual(tx["cash_delta"], -19207.03)
            self.assertEqual(tx["fee_source"], "explicit")
            self.assertEqual(tx["time_source"], "traded_at")
            when = datetime.fromisoformat(tx["time"]).astimezone(SHANGHAI)
            self.assertEqual((when.hour, when.minute, when.second), (9, 31, 5))
            self.assertAlmostEqual(store.load()["holdings"][0]["average_cost"], (19203 + 4.03) / 300)
            future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            with self.assertRaises(ValueError):
                store.apply_trade("300613", "buy", 100, 64, None, traded_at=future)
            with self.assertRaises(ValueError):
                store.apply_trade("300613", "buy", 100, 64, None, traded_at="明天")


class PreviousCloseValuation(unittest.TestCase):
    def test_zero_price_falls_back_to_previous_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            state = store.save([{"symbol": "159513", "quantity": 12300, "average_cost": 1.779}], 22.79)
            now = datetime.now(SHANGHAI)
            quote = Quote("SZ.159513", "ETF", 0.0, 1.80, status=DataStatus.FRESH,
                          provenance=Provenance("test", "local:test", now, now))
            with patch("xiao_a.dashboard._PORTFOLIO_STORE", store), \
                    patch("xiao_a.dashboard._cached_quote", return_value=ProviderResult(quote, DataStatus.FRESH)):
                live = dashboard._live_portfolio(state)
            row = live["holdings"][0]
            self.assertEqual(row["valuation_source"], "昨收估值")
            self.assertAlmostEqual(row["market_value"], 12300 * 1.80)
            self.assertEqual(row["status"], "estimated")
            self.assertEqual(live["estimated_symbols"], ["159513"])
            self.assertAlmostEqual(live["total_assets"], 12300 * 1.80 + 22.79)


if __name__ == "__main__":
    unittest.main()
