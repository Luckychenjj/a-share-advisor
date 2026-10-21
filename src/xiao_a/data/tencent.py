from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from urllib.request import Request

from xiao_a.data.http import open_market_url
from xiao_a.data.router import ProviderResult
from xiao_a.data.symbols import normalize_symbol
from xiao_a.data.volume import SHANGHAI
from xiao_a.models import DataStatus, Provenance, Quote


_URL = "https://qt.gtimg.cn/q="  # box patch: plain http times out from overseas network


def _number(fields: list[str], index: int) -> float | None:
    if index >= len(fields) or fields[index] in {"", "-", "--"}:
        return None
    try:
        return float(fields[index])
    except ValueError:
        return None


def parse_tencent_quote_payload(payload: str, requested_symbol: str) -> Quote:
    normalized = normalize_symbol(requested_symbol)
    expected = normalized.tencent
    matching_line = next(
        (line for line in payload.split(";") if f"v_{expected}=" in line),
        "",
    )
    if '"' not in matching_line:
        raise ValueError(f"Tencent returned no quote for {normalized.canonical}")
    fields = matching_line.split('"', 2)[1].split("~")
    if len(fields) < 53:
        raise ValueError(f"Tencent quote schema changed: expected >=53 fields, got {len(fields)}")

    price = _number(fields, 3) or 0.0
    previous_close = _number(fields, 4) or 0.0
    amount_wan = _number(fields, 37)
    amount = amount_wan * 10_000 if amount_wan is not None else None
    stale = price > 0 and amount == 0 and price == previous_close
    status = DataStatus.STALE if stale else DataStatus.FRESH
    fetched_at = datetime.now(timezone.utc)
    try:
        as_of = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
    except (ValueError, TypeError):
        as_of = None
    reported_ratio = _number(fields, 49)
    if reported_ratio is not None and (not isfinite(reported_ratio) or reported_ratio <= 0):
        reported_ratio = None
    return Quote(
        symbol=normalized.canonical,
        name=fields[1],
        price=price,
        previous_close=previous_close,
        open=_number(fields, 5),
        # Tencent's A-share quote volume is reported in lots (手).
        volume=_number(fields, 6),
        high=_number(fields, 33),
        low=_number(fields, 34),
        amount=amount,
        turnover_pct=_number(fields, 38),
        pe_ttm=_number(fields, 39),
        pb=_number(fields, 46),
        limit_up=_number(fields, 47),
        limit_down=_number(fields, 48),
        status=status,
        volume_ratio=reported_ratio,
        # Tencent fields 44/45 are circulating/total cap, each in 亿元.
        float_market_cap=(value * 100_000_000 if (value := _number(fields, 44)) is not None else None),
        market_cap=(value * 100_000_000 if (value := _number(fields, 45)) is not None else None),
        provenance=Provenance(
            source="tencent",
            source_url=_URL + expected,
            fetched_at=fetched_at,
            as_of=as_of,
        ),
    )


class TencentQuoteProvider:
    name = "tencent"

    def __init__(self, *, priority: int = 10, timeout: float = 8.0):
        self.priority = priority
        self.timeout = timeout

    def fetch_quote(self, symbol: str) -> ProviderResult:
        normalized = normalize_symbol(symbol)
        url = _URL + normalized.tencent
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with open_market_url(request, timeout=self.timeout) as response:
            payload = response.read().decode("gbk", errors="replace")
        quote = parse_tencent_quote_payload(payload, symbol)
        warnings = []
        if quote.status == DataStatus.STALE:
            warnings.append("zero turnover and unchanged price; quote may be suspended, pre-open, or stale")
        return ProviderResult(quote=quote, status=quote.status, warnings=warnings)
