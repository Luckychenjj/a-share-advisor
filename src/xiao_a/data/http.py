from __future__ import annotations

import os
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

# Hard ceilings so no market request can hang a settlement or analysis call.
# urllib applies `timeout` to the TCP connect and to every blocking read.
MAX_MARKET_TIMEOUT = float(os.environ.get("XIAOA_MARKET_TIMEOUT_MAX", "15"))
# After a connect-level failure (timeout, refused, reset, TLS EOF) the host is
# skipped for this many seconds so routers fall through to the next provider
# immediately instead of paying the full timeout again for every symbol.
HOST_COOLDOWN_SECONDS = float(os.environ.get("XIAOA_HOST_COOLDOWN", "60"))

_DOWN_HOSTS: dict[str, tuple[float, str]] = {}
_DOWN_LOCK = threading.Lock()


class HostUnavailableError(ConnectionError):
    """Raised without touching the network while a host is cooling down."""


def _host_key(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}"


def host_is_down(key: str) -> str | None:
    with _DOWN_LOCK:
        entry = _DOWN_HOSTS.get(key)
        if entry is None:
            return None
        until, reason = entry
        if until <= time.monotonic():
            _DOWN_HOSTS.pop(key, None)
            return None
        return reason


def mark_host_down(key: str, reason: str) -> None:
    if HOST_COOLDOWN_SECONDS <= 0:
        return
    with _DOWN_LOCK:
        _DOWN_HOSTS[key] = (time.monotonic() + HOST_COOLDOWN_SECONDS, reason[:200])


def reset_host_health() -> None:
    with _DOWN_LOCK:
        _DOWN_HOSTS.clear()


def open_market_url(request: Request, *, timeout: float):
    """Open market-data URLs without inheriting unrelated process proxies.

    Codex and some corporate shells inject HTTP_PROXY/HTTPS_PROXY values that
    are not usable by a local dashboard. Set XIAOA_PROXY explicitly when this
    application really should use a proxy.

    The timeout is capped and hosts that fail at the connection level are put
    on a short cooldown, so an unreachable source fails fast and callers fall
    back to the next provider instead of hanging.
    """

    url = request.full_url if isinstance(request, Request) else str(request)
    key = _host_key(url)
    reason = host_is_down(key)
    if reason is not None:
        raise HostUnavailableError(f"{key} skipped (recently unreachable: {reason})")
    configured_proxy = os.environ.get("XIAOA_PROXY", "").strip()
    proxies = {"http": configured_proxy, "https": configured_proxy} if configured_proxy else {}
    opener = build_opener(ProxyHandler(proxies))
    effective = max(0.5, min(float(timeout), MAX_MARKET_TIMEOUT))
    try:
        return opener.open(request, timeout=effective)
    except HTTPError:
        # The host answered; an HTTP status is not a connectivity failure.
        raise
    except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        mark_host_down(key, f"{type(exc).__name__}: {exc}")
        raise


def tcp_reachable(host: str, port: int, *, timeout: float = 3.0) -> bool:
    """Quick TCP probe (with cooldown) for raw-socket sources such as pytdx/baostock."""

    key = f"tcp://{host.lower()}:{int(port)}"
    if host_is_down(key) is not None:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError as exc:
        mark_host_down(key, f"{type(exc).__name__}: {exc}")
        return False
