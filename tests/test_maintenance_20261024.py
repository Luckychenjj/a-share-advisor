from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from xiao_a import auto as auto_module
from xiao_a import dashboard
from xiao_a.data.kline import KlineResult
from xiao_a.data.market import MarketBreadthResult
from xiao_a.data.router import ProviderResult
from xiao_a.data.supplemental import SupplementalResult
from xiao_a.data.symbols import normalize_symbol
from xiao_a.hybrid import HybridGateway
from xiao_a.models import DailyBar, DataStatus, Provenance, Quote
from xiao_a.portfolio_store import PortfolioStore

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Server:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return "http://127.0.0.1:" + str(self.server.server_port)

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class OfflineFallbackIsExplicit(unittest.TestCase):
    def test_cached_auto_evaluate_says_it_is_stale_and_why(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = HybridGateway(Path(directory), cloud_url="")
            payload = {"symbol": "300502"}
            cache.save("POST", "/api/auto-evaluate", payload, {"decision": {"executable": True}, "status": "fresh"})
            with _Server() as base, patch("xiao_a.dashboard._HYBRID_GATEWAY", cache), \
                    patch("xiao_a.dashboard._AUTO_ANALYZER.analyze", side_effect=RuntimeError("kline down")):
                request = Request(base + "/api/auto-evaluate", data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as result:
                    self.assertEqual(result.headers.get("X-Xiao-A-Offline"), "1")
                    response = json.load(result)
            self.assertTrue(response["offline"])
            self.assertEqual(response["status"], "stale")
            self.assertIn("kline down", response["live_error"])
            self.assertIn("缓存结果", response["warnings"][0])
            self.assertIsNotNone(response["cached_at_local"])

    def test_live_only_header_returns_the_real_error(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = HybridGateway(Path(directory), cloud_url="")
            payload = {"symbol": "300502"}
            cache.save("POST", "/api/auto-evaluate", payload, {"decision": {}, "status": "fresh"})
            with _Server() as base, patch("xiao_a.dashboard._HYBRID_GATEWAY", cache), \
                    patch("xiao_a.dashboard._AUTO_ANALYZER.analyze", side_effect=RuntimeError("kline down")):
                request = Request(base + "/api/auto-evaluate", data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json", "X-Xiao-A-Live-Only": "1"})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 400)
                self.assertIn("kline down", json.load(caught.exception)["error"])


class SameDayBuyDailyProfit(unittest.TestCase):
    def _quote(self, price, previous_close):
        now = datetime.now(SHANGHAI)
        return Quote("SZ.300502", "测试", price, previous_close, status=DataStatus.FRESH,
                     provenance=Provenance("test", "local:test", now, now))

    def test_today_buy_counts_from_fill_price_not_previous_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            store.save([{"symbol": "300502", "quantity": 100, "average_cost": 9.0}], 100000)
            store.apply_trade("300502", "buy", 200, 10.5, 0.0003)
            state = store.load()
            with patch("xiao_a.dashboard._PORTFOLIO_STORE", store):
                row = dashboard._holding_daily_profit("300502", 300, self._quote(11.0, 10.0), state)
            # 100 carried shares: +1.0 each; 200 bought today at 10.5: +0.5 each.
            self.assertAlmostEqual(row["daily_profit"], 100 * 1.0 + 200 * 0.5)
            self.assertEqual(row["bought_today_quantity"], 200)
            self.assertAlmostEqual(row["daily_profit_pct"], 200 / (100 * 10.0 + 200 * 10.5))

    def test_carried_position_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            state = store.save([{"symbol": "300502", "quantity": 100, "average_cost": 9.0}], 0)
            with patch("xiao_a.dashboard._PORTFOLIO_STORE", store):
                row = dashboard._holding_daily_profit("300502", 100, self._quote(11.0, 10.0), state)
            self.assertAlmostEqual(row["daily_profit"], 100.0)
            self.assertAlmostEqual(row["daily_profit_pct"], 0.1)
            self.assertEqual(row["bought_today_quantity"], 0)

    def test_buys_from_other_days_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "p.json")
            state = store.save([{"symbol": "300502", "quantity": 100, "average_cost": 9.0}], 0)
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            state["transactions"] = [{"time": yesterday, "symbol": "300502", "side": "buy", "quantity": 100, "price": 9.0}]
            self.assertEqual(store.buys_on("300502", datetime.now(SHANGHAI).date().isoformat(), state=state), [])


class _SlowAnalyzerParts:
    """Every data stage sleeps; with stages in parallel the run costs one sleep."""

    delay = 0.6

    def __init__(self):
        today = datetime.now(SHANGHAI).date()
        self.bars = [DailyBar(trading_date=today - timedelta(days=200 - i), open=10, high=10.5, low=9.5,
                              close=10 + i * 0.01, volume=1000, previous_close=10) for i in range(200)]

    def fetch_quote(self, symbol):
        time.sleep(self.delay)
        return ProviderResult(None, DataStatus.UNAVAILABLE, ["no quote"])

    def fetch_daily(self, symbol, *, limit=360):
        time.sleep(self.delay)
        return KlineResult(list(self.bars), DataStatus.FRESH)

    def fetch(self, *args):
        time.sleep(self.delay)
        return MarketBreadthResult({}, DataStatus.UNAVAILABLE)


class AnalysisStagesRunInParallel(unittest.TestCase):
    def test_stages_overlap_and_slow_supplemental_is_skipped(self):
        parts = _SlowAnalyzerParts()
        analyzer = auto_module.AutoAnalyzer(quote_router=parts, kline_provider=parts, market_provider=parts)

        def slow_supplemental(symbol):
            time.sleep(3)
            return SupplementalResult({}, DataStatus.FRESH)

        analyzer.supplemental_provider.fetch = slow_supplemental
        with patch.object(auto_module, "_SUPPLEMENTAL_BUDGET", 0.5):
            started = time.monotonic()
            result = analyzer.analyze("300502")
            elapsed = time.monotonic() - started
        # Serial would be ~0.6 * 3 (+ benchmark) + 3s supplemental.
        self.assertLess(elapsed, 2.0)
        self.assertIn("supplemental: timed out", result.diagnostics)
        self.assertEqual(result.bars_count, 200)


class BeijingExchangeSymbols(unittest.TestCase):
    def test_new_920_codes_route_to_beijing_with_tencent_prefix(self):
        for code in ("920060", "920346", "920183"):
            normalized = normalize_symbol(code)
            self.assertEqual(normalized.exchange, "BJ")
            self.assertEqual(normalized.tencent, "bj" + code)


if __name__ == "__main__":
    unittest.main()
