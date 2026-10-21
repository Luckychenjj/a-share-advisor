"""Single-user analysis API. Deploy behind an HTTPS reverse proxy."""
import hmac
import os
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from xiao_a.dashboard import DashboardHandler
from xiao_a.hybrid import HybridGateway


class CloudHandler(DashboardHandler):
    def _try_remote(self, method, path, payload=None):
        self._cache_request = None
        return False

    def _authorized(self, method):
        self._cache_request = None
        expected = "Bearer " + self.server.api_token
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            self._send_json({"error": "unauthorized"}, 401)
            return False
        if not HybridGateway.supported(method, urlparse(self.path).path):
            self._send_json({"error": "not found"}, 404)
            return False
        return True

    def do_GET(self):
        if self._authorized("GET"):
            super().do_GET()

    def do_POST(self):
        if self._authorized("POST"):
            super().do_POST()

    def do_PUT(self):
        self._cache_request = None
        self._send_json({"error": "not found"}, 404)


def run_cloud(*, host="127.0.0.1", port=8766):
    token = os.environ.get("XIAO_A_SERVER_TOKEN", "")
    if len(token) < 32:
        raise ValueError("请设置至少32字符的 XIAO_A_SERVER_TOKEN")
    server = ThreadingHTTPServer((host, port), CloudHandler)
    server.api_token = token
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
