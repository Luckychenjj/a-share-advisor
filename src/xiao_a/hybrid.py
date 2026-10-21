"""Local-first transport shared by the desktop UI and future mobile clients."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


READ_PATHS = {"/api/quote", "/api/chart", "/api/search"}
ANALYSIS_PATHS = {"/api/evaluate", "/api/auto-evaluate", "/api/screener"}


def user_data_directory() -> Path:
    override = os.environ.get("XIAO_A_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "XiaoAAdvisor") if base else Path.home() / ".xiao-a-advisor"


def stale_copy(value):
    """Never present an offline cached decision as permission to trade."""
    if isinstance(value, list):
        return [stale_copy(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: stale_copy(item) for key, item in value.items()}
    if "status" in result:
        result["status"] = "stale"
    if "data_status" in result:
        result["data_status"] = "stale"
    if "executable" in result:
        result["executable"] = False
    return result


class HybridGateway:
    def __init__(self, directory: Path | None = None, *, cloud_url: str | None = None, token: str | None = None):
        self.directory = directory or user_data_directory() / "cache"
        self.cloud_url = (cloud_url if cloud_url is not None else os.environ.get("XIAO_A_CLOUD_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("XIAO_A_CLOUD_TOKEN", "")
        if self.cloud_url:
            parsed = urlparse(self.cloud_url)
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("云端地址必须使用 HTTPS；本机调试可用 HTTP")
            if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("云端地址格式不正确")
        self._lock = threading.Lock()

    @staticmethod
    def supported(method: str, path: str) -> bool:
        return (method == "GET" and path in READ_PATHS) or (method == "POST" and path in ANALYSIS_PATHS)

    def _file(self, method: str, path: str, payload=None) -> Path:
        raw = json.dumps(["market-20261002-v4", method, path, payload], sort_keys=True, ensure_ascii=False).encode("utf-8")
        return self.directory / (hashlib.sha256(raw).hexdigest() + ".json")

    def save(self, method: str, path: str, payload, response) -> None:
        record = {"saved_at": datetime.now(timezone.utc).isoformat(), "response": response}
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            destination = self._file(method, path, payload)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            temporary.replace(destination)

    def fallback(self, method: str, path: str, payload=None):
        try:
            record = json.loads(self._file(method, path, payload).read_text(encoding="utf-8"))
            response = stale_copy(record["response"])
            response["offline"] = True
            response["cached_at"] = record["saved_at"]
            response.setdefault("warnings", []).append("离线缓存，仅供查看；不能据此执行交易")
            return response
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def remote(self, method: str, path: str, payload=None):
        if not self.cloud_url:
            raise RuntimeError("尚未配置云端服务")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.cloud_url + path, data=body, headers=headers, method=method)
        with urlopen(request, timeout=30) as result:
            response = json.loads(result.read().decode("utf-8"))
        if not isinstance(response, dict) or response.get("error") or response.get("status") == "unavailable":
            raise RuntimeError("云端没有返回可用结果")
        return response
