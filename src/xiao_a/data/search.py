from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlencode
from urllib.request import Request

from xiao_a.data.http import open_market_url


_SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"
_MARKET_TO_EXCHANGE = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}


@dataclass(frozen=True)
class StockSearchMatch:
    symbol: str
    code: str
    name: str
    pinyin: str
    security_type: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def parse_tencent_smartbox_payload(payload: str) -> list[StockSearchMatch]:
    """Parse Tencent Smartbox matches into A-share/ETF search results.

    Smartbox uses a JavaScript assignment containing caret-separated records:
    exchange~code~name~pinyin~type. Chinese names are sometimes represented as
    JSON-style unicode escapes, so decode only that quoted value through JSON.
    """

    match = re.search(r'v_hint="(.*)"\s*;?\s*$', payload.strip(), flags=re.S)
    if not match:
        return []
    try:
        contents = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        contents = match.group(1)

    results: list[StockSearchMatch] = []
    seen: set[str] = set()
    for record in contents.split("^"):
        fields = record.split("~")
        if len(fields) < 5:
            continue
        market, code, name, pinyin, security_type = (field.strip() for field in fields[:5])
        exchange = _MARKET_TO_EXCHANGE.get(market.lower())
        if not exchange or not re.fullmatch(r"\d{6}", code) or not name:
            continue
        # Keep stocks and exchange-traded funds; filter out board/index entries
        # returned by the broad Smartbox endpoint.
        security_type = security_type.upper()
        if not (security_type.startswith("GP") or security_type == "ETF"):
            continue
        symbol = f"{exchange}.{code}"
        if symbol in seen:
            continue
        seen.add(symbol)
        results.append(StockSearchMatch(symbol, code, name, pinyin.lower(), security_type))
    return results


class TencentStockSearchProvider:
    """Search A shares by code, name, or pinyin initials via Tencent Smartbox."""

    def __init__(self, *, timeout: float = 5.0, cache_ttl: float = 10.0):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, list[StockSearchMatch]]] = {}
        self._lock = threading.RLock()

    def search(self, keyword: str, *, limit: int = 8) -> list[StockSearchMatch]:
        query = str(keyword or "").strip()
        if not query:
            return []
        key = query.lower()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_ttl:
                return cached[1][:limit]

        params = {"v": 2, "t": "all", "c": 1, "q": query}
        request = Request(
            f"{_SMARTBOX_URL}?{urlencode(params)}",
            headers=_HEADERS,
        )
        with open_market_url(request, timeout=self.timeout) as response:
            payload = response.read().decode("gbk", errors="replace")
        matches = parse_tencent_smartbox_payload(payload)
        with self._lock:
            self._cache[key] = (time.monotonic(), matches)
        return matches[:limit]
