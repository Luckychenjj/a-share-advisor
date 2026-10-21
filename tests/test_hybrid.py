import json
import hashlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from xiao_a.hybrid import HybridGateway
from xiao_a.cloud import CloudHandler
from xiao_a.dashboard import DashboardHandler


class HybridTests(unittest.TestCase):
    def test_previous_volume_revision_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = "/api/quote?symbol=300750"
            old_key = hashlib.sha256(json.dumps(["GET", path, None], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            (Path(directory) / (old_key + ".json")).write_text(json.dumps({
                "saved_at": "2026-10-02T04:00:00Z", "response": {"volume_ratio": 83},
            }))
            cache = HybridGateway(Path(directory), cloud_url="")
            self.assertIsNone(cache.fallback("GET", path))

    def test_cache_is_persistent_and_risk_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = HybridGateway(Path(directory), cloud_url="")
            request = {"symbol": "300750", "risk": {"position_pct": 0.9}}
            value = {"decision": {"executable": True, "final_action": "buy"}, "quote": {"status": "fresh"}}
            cache.save("POST", "/api/auto-evaluate", request, value)
            restored = HybridGateway(Path(directory), cloud_url="").fallback("POST", "/api/auto-evaluate", request)
            self.assertTrue(restored["offline"])
            self.assertFalse(restored["decision"]["executable"])
            self.assertEqual(restored["quote"]["status"], "stale")
            self.assertTrue(value["decision"]["executable"])
            self.assertIsNone(cache.fallback("POST", "/api/auto-evaluate", {"symbol": "300750"}))

    def test_corrupt_cache_is_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = HybridGateway(Path(directory), cloud_url="")
            cache._file("GET", "/api/quote").write_text("broken")
            self.assertIsNone(cache.fallback("GET", "/api/quote"))

    def test_plain_http_cloud_is_rejected(self):
        with self.assertRaises(ValueError):
            HybridGateway(cloud_url="http://example.com")
        HybridGateway(cloud_url="http://127.0.0.1:8766")

    def test_account_operations_stay_local(self):
        for method, path in [("GET", "/api/portfolio-state"), ("POST", "/api/trade"), ("PUT", "/api/portfolio-state")]:
            self.assertFalse(HybridGateway.supported(method, path))


class CloudTests(unittest.TestCase):
    def test_authentication_and_account_isolation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), CloudHandler)
        server.api_token = "test-" + "a" * 32
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = "http://127.0.0.1:" + str(server.server_port)
        try:
            for path, headers, expected in [
                ("/api/quote?symbol=300750", {}, 401),
                ("/api/portfolio-state", {"Authorization": "Bearer " + server.api_token}, 404),
            ]:
                with self.assertRaises(HTTPError) as context:
                    urlopen(Request(address + path, headers=headers), timeout=3)
                self.assertEqual(context.exception.code, expected)
            payload = json.dumps({"symbol": "300750", "features": {}}).encode()
            with urlopen(Request(address + "/api/evaluate", data=payload, headers={"Authorization": "Bearer " + server.api_token, "Content-Type": "application/json"}), timeout=3) as result:
                self.assertEqual(result.status, 200)
                self.assertIn("final_action", json.load(result))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_handler_returns_safe_cache_when_local_analysis_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = HybridGateway(Path(directory), cloud_url="")
            payload = {"symbol": "300750"}
            cache.save("POST", "/api/auto-evaluate", payload, {"decision": {"executable": True}, "features": {}})
            server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("xiao_a.dashboard._HYBRID_GATEWAY", cache), patch("xiao_a.dashboard._AUTO_ANALYZER.analyze", side_effect=RuntimeError("offline")):
                    request = Request("http://127.0.0.1:" + str(server.server_port) + "/api/auto-evaluate", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                    with urlopen(request, timeout=3) as result:
                        response = json.load(result)
                    self.assertTrue(response["offline"])
                    self.assertFalse(response["decision"]["executable"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
