import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

from xiao_a.data import http
from xiao_a.data.kline import SinaKlineProvider, parse_sina_kline_payload
from xiao_a.prediction_audit import PredictionLedger


class HostCooldownTest(unittest.TestCase):
    def setUp(self):
        http.reset_host_health()

    def tearDown(self):
        http.reset_host_health()

    def test_connect_failure_puts_host_on_cooldown(self):
        calls = []

        class Opener:
            def open(self, request, timeout):
                calls.append(timeout)
                raise URLError("timed out")

        with patch.object(http, "build_opener", return_value=Opener()):
            with self.assertRaises(URLError):
                http.open_market_url(Request("https://example.invalid/a"), timeout=99)
            started = time.monotonic()
            with self.assertRaises(http.HostUnavailableError):
                http.open_market_url(Request("https://example.invalid/b"), timeout=5)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], http.MAX_MARKET_TIMEOUT)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_tcp_probe_fails_fast_and_caches(self):
        # TEST-NET-1 address: never routable.
        self.assertFalse(http.tcp_reachable("192.0.2.1", 9, timeout=0.3))
        started = time.monotonic()
        self.assertFalse(http.tcp_reachable("192.0.2.1", 9, timeout=0.3))
        self.assertLess(time.monotonic() - started, 0.1)


class SinaKlineTest(unittest.TestCase):
    def test_parse_and_stock_qfq_refused(self):
        payload = json.dumps([
            {"day": "2026-10-20", "open": "10", "high": "11", "low": "9.5", "close": "10.5", "volume": "120000"},
            {"day": "2026-10-21", "open": "10.5", "high": "10.8", "low": "10.1", "close": "10.2", "volume": "80000"},
        ])
        result = parse_sina_kline_payload(payload, "SH.000300")
        self.assertEqual(len(result.bars), 2)
        self.assertEqual(result.bars[0].volume, 1200)
        self.assertEqual(result.bars[1].previous_close, 10.5)
        with self.assertRaises(ValueError):
            SinaKlineProvider().fetch_daily("SZ.000001", adjustment="qfq")


class LedgerAppendCacheTest(unittest.TestCase):
    def test_chain_stays_valid_and_outside_edit_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            ledger = PredictionLedger(path)
            for index in range(5):
                ledger.append("note", {"index": index})
            self.assertEqual(ledger.integrity(), {"valid": True, "events": 5, "head": ledger.events()[-1]["event_hash"]})
            self.assertEqual([event["sequence"] for event in ledger.events()], [1, 2, 3, 4, 5])
            lines = path.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(lines[1])
            tampered["index"] = 99
            lines[1] = json.dumps(tampered)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ledger.append("note", {"index": 6})


if __name__ == "__main__":
    unittest.main()
