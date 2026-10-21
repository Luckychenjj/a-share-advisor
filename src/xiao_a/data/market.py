from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import tanh
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request

from xiao_a.data.http import open_market_url
from xiao_a.models import DataStatus, Provenance


_URLS = (
    "http://82.push2.eastmoney.com/api/qt/clist/get",
    "http://push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
# Eastmoney's public web "ut" parameter (embedded in its public quote pages; not a
# personal credential). Override with XIAO_A_EASTMONEY_UT if Eastmoney changes it.
_TOKEN = os.environ.get("XIAO_A_EASTMONEY_UT", "fa5fd1943c7b386f172d6893dbbd1d0c")  # gitleaks:allow
# Eastmoney is more tolerant of one larger board snapshot than many small
# paginated requests.  The current industry-board universe is below this cap.
_PAGE_SIZE = 500
_CLS_EMOTION_URL = "https://x-quote.cls.cn/v2/quote/a/stock/emotion"
_CLS_PARAMS = {
    "app": "CailianpressWeb",
    "os": "web",
    "sv": "8.4.6",
    "sign": "9f8797a1f4de66c2370f7a03990d2737",
}


@dataclass
class MarketBreadthResult:
    values: dict[str, float]
    status: DataStatus
    provenance: Provenance | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MarketBreadthProvider(Protocol):
    name: str

    def fetch(self) -> MarketBreadthResult: ...


def _number(value: object) -> float | None:
    """Parse numeric API values, including percentage strings."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "None", "null"}:
        return None
    try:
        return float(text.rstrip("%"))
    except ValueError:
        return None


def _bounded(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


class CailianMarketEmotionProvider:
    """Use CLS's public market-emotion snapshot for breadth and sentiment.

    This is intentionally dependency-free and follows the source layout used
    by the MIT-licensed levistock project, while keeping our own normalized
    feature contract and explicit provenance.
    """

    name = "cailian-market-emotion"

    def __init__(self, *, timeout: float = 8.0):
        self.timeout = timeout

    def fetch(self) -> MarketBreadthResult:
        source_url = f"{_CLS_EMOTION_URL}?{urlencode(_CLS_PARAMS)}"
        try:
            request = Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.cls.cn/",
                },
            )
            with open_market_url(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
            return parse_cailian_market_emotion_payload(payload, source_url=source_url)
        except Exception as exc:
            return MarketBreadthResult(
                {}, DataStatus.UNAVAILABLE,
                warnings=["财联社市场情绪数据暂不可用，继续尝试备用数据源"],
                errors=[f"{type(exc).__name__}: {exc}"],
            )


def parse_cailian_market_emotion_payload(payload: str, *, source_url: str = _CLS_EMOTION_URL) -> MarketBreadthResult:
    """Normalize the public CLS response into the advisor's four features."""
    raw = json.loads(payload)
    if raw.get("code") != 200:
        raise ValueError(f"CLS returned code={raw.get('code')!r}")
    body = raw.get("data") or {}
    distribution = body.get("up_down_dis") or {}
    rising = _number(distribution.get("rise_num"))
    falling = _number(distribution.get("fall_num"))
    flat = _number(distribution.get("flat_num", distribution.get("flat")))
    limit_up = _number(distribution.get("up_num"))
    limit_down = _number(distribution.get("down_num"))
    market_degree = _number(body.get("market_degree"))
    seal_rate = _number(body.get("up_ratio"))
    touched_limit_up = _number(body.get("up_open_num"))
    required = (rising, falling, flat, limit_up, limit_down, market_degree)
    if any(value is None for value in required):
        raise ValueError("CLS emotion response is missing breadth fields")

    total = rising + falling + flat
    breadth = (rising - falling) / total if total > 0 else 0.0
    limit_balance = tanh((limit_up - limit_down) / 30.0)
    degree_signal = (market_degree - 50.0) / 50.0
    sector_heat = _bounded(0.55 * degree_signal + 0.45 * breadth)
    emotion = _bounded(0.45 * degree_signal + 0.35 * breadth + 0.20 * limit_balance)

    # CLS exposes the sealed-board rate and the number of stocks that touched
    # the limit. Prefer the rate; derive a conservative fallback only when the
    # rate is absent.
    if seal_rate is not None:
        if seal_rate > 1.0:
            seal_rate /= 100.0
        broken_board_risk = 1.0 - max(0.0, min(1.0, seal_rate))
    elif touched_limit_up and touched_limit_up > 0:
        broken_board_risk = max(0.0, min(1.0, 1.0 - limit_up / touched_limit_up))
    else:
        broken_board_risk = 0.0

    fetched_at = datetime.now(timezone.utc)
    return MarketBreadthResult(
        {
            "sector_heat": sector_heat,
            "limit_up_breadth": _bounded(breadth),
            "emotion_cycle": emotion,
            "broken_board_risk": _bounded(broken_board_risk),
        },
        DataStatus.FRESH,
        Provenance("cailian-market-emotion", source_url, fetched_at, fetched_at),
        warnings=["市场情绪来自财联社快照，按公开字段归一化为模型输入"],
    )


class MarketBreadthRouter:
    """Try independent breadth sources in priority order."""

    def __init__(self, providers: list[MarketBreadthProvider]):
        self.providers = providers

    def fetch(self) -> MarketBreadthResult:
        errors: list[str] = []
        warnings: list[str] = []
        for provider in self.providers:
            try:
                result = provider.fetch()
            except Exception as exc:
                errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                continue
            errors.extend(result.errors)
            warnings.extend(result.warnings)
            if result.values and result.status == DataStatus.FRESH:
                result.errors = errors
                result.warnings = warnings
                return result
        return MarketBreadthResult(
            {}, DataStatus.UNAVAILABLE,
            warnings=warnings or ["板块广度与市场情绪数据暂不可用，相关输入已弃权"],
            errors=errors,
        )


class EastmoneyMarketBreadthProvider:
    """Fetch industry-board breadth and derive transparent market-emotion scores."""

    name = "eastmoney-market-breadth"

    def __init__(self, *, timeout: float = 8.0):
        self.timeout = timeout

    def _fetch_page(self, page: int) -> tuple[int, list[dict], int]:
        last_error: Exception | None = None
        for base_url in _URLS:
            for attempt in range(2):
                try:
                    url = (
                        f"{base_url}?cb=j&pn={page}&pz={_PAGE_SIZE}&po=1&np=1&ut={_TOKEN}"
                        f"&fltt=2&invt=2&fid=f3&fs=m:90+t:2+f:!50"
                        "&fields=f12,f14,f3,f104,f105,f106"
                    )
                    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
                    with open_market_url(request, timeout=self.timeout) as response:
                        payload = response.read().decode("utf-8", errors="replace").strip()
                    if payload.startswith("j(") and payload.endswith(")"):
                        payload = payload[2:-1]
                    raw = json.loads(payload)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 1:
                        time.sleep(0.25)
            else:
                continue
            break
        else:
            raise last_error or RuntimeError("market breadth request failed")
        data = raw.get("data") or {}
        diff = data.get("diff") or []
        rows = list(diff.values()) if isinstance(diff, dict) else list(diff)
        return page, [row for row in rows if isinstance(row, dict)], int(data.get("total") or 0)

    def fetch(self) -> MarketBreadthResult:
        try:
            _, first_page, total = self._fetch_page(1)
            page_count = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
            pages: dict[int, list[dict]] = {1: first_page}
            if page_count > 1:
                with ThreadPoolExecutor(max_workers=min(3, page_count - 1)) as executor:
                    futures = [executor.submit(self._fetch_page, page) for page in range(2, page_count + 1)]
                    for future in as_completed(futures):
                        page, rows, _ = future.result()
                        pages[page] = rows
            rows = [row for page in sorted(pages) for row in pages[page]]
            if not rows:
                raise ValueError("Eastmoney returned no industry-board breadth rows")
        except Exception as exc:
            return MarketBreadthResult(
                {}, DataStatus.UNAVAILABLE,
                warnings=["板块广度与市场情绪数据暂不可用，相关输入已弃权"],
                errors=[f"{type(exc).__name__}: {exc}"],
            )

        changes = [float(row["f3"]) for row in rows if isinstance(row.get("f3"), (int, float))]
        up = sum(float(row.get("f104") or 0) for row in rows)
        down = sum(float(row.get("f105") or 0) for row in rows)
        flat = sum(float(row.get("f106") or 0) for row in rows)
        total_constituents = up + down + flat
        breadth = (up - down) / total_constituents if total_constituents else 0.0
        average_change = sum(changes) / len(changes) if changes else 0.0
        sector_heat = tanh(average_change / 3.0)
        emotion = 0.65 * breadth + 0.35 * tanh(average_change / 2.5)
        fetched_at = datetime.now(timezone.utc)
        source_url = f"{_URLS[0]}?pn=1&pz={_PAGE_SIZE}&fs=m:90+t:2+f:!50&fields=f12,f14,f3,f104,f105,f106"
        return MarketBreadthResult(
            {
                "sector_heat": max(-1.0, min(1.0, sector_heat)),
                "limit_up_breadth": max(-1.0, min(1.0, breadth)),
                "emotion_cycle": max(-1.0, min(1.0, emotion)),
            },
            DataStatus.FRESH,
            Provenance(self.name, source_url, fetched_at, fetched_at),
        )
