from __future__ import annotations

import os
from urllib.request import ProxyHandler, Request, build_opener


def open_market_url(request: Request, *, timeout: float):
    """Open market-data URLs without inheriting unrelated process proxies.

    Codex and some corporate shells inject HTTP_PROXY/HTTPS_PROXY values that
    are not usable by a local dashboard. Set XIAOA_PROXY explicitly when this
    application really should use a proxy.
    """

    configured_proxy = os.environ.get("XIAOA_PROXY", "").strip()
    proxies = {"http": configured_proxy, "https": configured_proxy} if configured_proxy else {}
    opener = build_opener(ProxyHandler(proxies))
    return opener.open(request, timeout=timeout)
