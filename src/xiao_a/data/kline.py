from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import quote as urlquote
from urllib.request import Request
from typing import Protocol

from xiao_a.data.symbols import NormalizedSymbol, normalize_symbol, is_market_index
from xiao_a.data.http import open_market_url
from xiao_a.models import DailyBar, DataStatus, Provenance


_BASE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# Same Tencent qfq daily-kline data behind a different front end. The
# web.ifzq host intermittently answers with a WAF 501 page (notably when a
# Referer is sent and for index symbols), so it is tried second.
_TENCENT_KLINE_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    _BASE_URL,
)
# Plain HTTP (port 80) is unreachable from some networks and used to hang
# settlement; always use HTTPS.
_EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


@dataclass
class KlineResult:
    bars: list[DailyBar]
    status: DataStatus
    provenance: Provenance | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class KlineProvider(Protocol):
    name: str
    priority: int

    def fetch_daily(self, symbol: str, *, limit: int = 360) -> KlineResult: ...


def _default_limit_rate(symbol: NormalizedSymbol, trading_date: date) -> float:
    if symbol.exchange == "BJ":
        return 0.30
    if symbol.code.startswith(("688", "689")):
        return 0.20
    if symbol.code.startswith(("300", "301")) and trading_date >= date(2020, 8, 24):
        return 0.20
    return 0.10


def parse_tencent_kline_payload(payload: str, requested_symbol: str, *, adjustment: str = "qfq") -> KlineResult:
    normalized = normalize_symbol(requested_symbol)
    raw = json.loads(payload)
    if raw.get("code") not in {0, None}:
        raise ValueError(f"Tencent kline error: {raw.get('msg') or raw.get('code')}")
    node = (raw.get("data") or {}).get(normalized.tencent) or {}
    rows = (node.get("qfqday") if adjustment == "qfq" else node.get("day")) or []
    # Indices have no share ex-rights adjustment. Tencent returns their level
    # history under day even for a qfq request. Never apply this to stocks:
    # silently mixing unadjusted stock prices would corrupt return labels.
    if not rows and adjustment == "qfq" and is_market_index(requested_symbol):
        rows = node.get("day") or []
    if not rows:
        return KlineResult([], DataStatus.UNAVAILABLE, warnings=["Tencent returned no daily bars"])

    clean_rows: list[tuple[date, float, float, float, float, float]] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            trading_date = date.fromisoformat(row[0])
            open_price = float(row[1])
            close = float(row[2])
            high = float(row[3])
            low = float(row[4])
            volume = float(row[5])
        except (TypeError, ValueError):
            continue
        if min(open_price, close, high, low) <= 0 or high < low:
            continue
        clean_rows.append((trading_date, open_price, high, low, close, volume))

    clean_rows.sort(key=lambda item: item[0])
    parsed: list[DailyBar] = []
    previous_close: float | None = None
    for trading_date, open_price, high, low, close, volume in clean_rows:
        parsed.append(DailyBar(
            trading_date=trading_date,
            open=open_price,
            high=high,
            low=low,
            close=close,
            # Tencent daily K-lines report volume in lots (手).
            volume=volume,
            previous_close=previous_close,
            suspended=volume <= 0,
            limit_rate=_default_limit_rate(normalized, trading_date),
        ))
        previous_close = close
    fetched_at = datetime.now(timezone.utc)
    source_url = f"{_BASE_URL}?param={normalized.tencent},day,,,{len(rows)},{adjustment}"
    status = DataStatus.FRESH if parsed else DataStatus.INVALID
    return KlineResult(
        parsed,
        status,
        Provenance(
            "tencent-kline",
            source_url,
            fetched_at,
            datetime.combine(parsed[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc) if parsed else None,
        ),
        warnings=[] if len(parsed) >= 120 else [f"only {len(parsed)} valid daily bars"],
    )


class TencentKlineProvider:
    name = "tencent-kline"

    def __init__(self, *, priority: int = 10, timeout: float = 10.0):
        self.priority = priority
        self.timeout = timeout

    def fetch_daily(self, symbol: str, *, limit: int = 360, adjustment: str = "qfq") -> KlineResult:
        normalized = normalize_symbol(symbol)
        limit = max(30, min(1000, int(limit)))
        if adjustment not in {"qfq", "none"}:
            raise ValueError("unsupported adjustment")
        param = f"{normalized.tencent},day,,,{limit},{'qfq' if adjustment == 'qfq' else ''}"
        last_error: Exception | None = None
        for base_url in _TENCENT_KLINE_URLS:
            url = f"{base_url}?param={urlquote(param, safe=',')}"
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with open_market_url(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8", errors="replace")
                result = parse_tencent_kline_payload(payload, symbol, adjustment=adjustment)
            except Exception as exc:  # WAF page / timeout: try the next Tencent front end
                last_error = exc
                continue
            if result.bars:
                return result
            last_error = ValueError("; ".join(result.warnings) or "Tencent returned no daily bars")
        raise last_error or RuntimeError("Tencent kline unavailable")


def parse_eastmoney_kline_payload(payload: str, requested_symbol: str, *, adjustment: str = "qfq") -> KlineResult:
    normalized = normalize_symbol(requested_symbol)
    raw = json.loads(payload)
    data = raw.get("data") or {}
    rows = data.get("klines") or []
    if not rows:
        return KlineResult([], DataStatus.UNAVAILABLE, warnings=["Eastmoney returned no daily bars"])

    clean_rows: list[tuple[date, float, float, float, float, float]] = []
    for raw_row in rows:
        row = raw_row.split(",") if isinstance(raw_row, str) else raw_row
        if len(row) < 6:
            continue
        try:
            trading_date = date.fromisoformat(row[0])
            open_price, close, high, low, volume = map(float, row[1:6])
        except (TypeError, ValueError):
            continue
        if min(open_price, close, high, low) <= 0 or high < low:
            continue
        clean_rows.append((trading_date, open_price, high, low, close, volume))

    clean_rows.sort(key=lambda item: item[0])
    bars: list[DailyBar] = []
    previous_close: float | None = None
    for trading_date, open_price, high, low, close, volume in clean_rows:
        bars.append(DailyBar(
            trading_date=trading_date,
            open=open_price,
            high=high,
            low=low,
            close=close,
            # f56 is lots, verified against Tencent and amount / average price.
            volume=volume,
            previous_close=previous_close,
            suspended=volume <= 0,
            limit_rate=_default_limit_rate(normalized, trading_date),
        ))
        previous_close = close

    fetched_at = datetime.now(timezone.utc)
    secid = f"{1 if normalized.exchange == 'SH' else 0}.{normalized.code}"
    source_url = f"{_EASTMONEY_URL}?secid={secid}&klt=101&fqt={1 if adjustment == 'qfq' else 0}"
    return KlineResult(
        bars,
        DataStatus.FRESH if bars else DataStatus.INVALID,
        Provenance(
            "eastmoney-kline",
            source_url,
            fetched_at,
            datetime.combine(bars[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc) if bars else None,
        ),
        warnings=[] if len(bars) >= 120 else [f"only {len(bars)} valid daily bars"],
    )


class EastmoneyKlineProvider:
    name = "eastmoney-kline"

    def __init__(self, *, priority: int = 20, timeout: float = 10.0):
        self.priority = priority
        self.timeout = timeout

    def fetch_daily(self, symbol: str, *, limit: int = 360, adjustment: str = "qfq") -> KlineResult:
        normalized = normalize_symbol(symbol)
        limit = max(30, min(1000, int(limit)))
        secid = f"{1 if normalized.exchange == 'SH' else 0}.{normalized.code}"
        fields1 = "f1,f2,f3,f4,f5,f6"
        fields2 = "f51,f52,f53,f54,f55,f56,f57,f61"
        if adjustment not in {"qfq", "none"}:
            raise ValueError("unsupported adjustment")
        url = (
            f"{_EASTMONEY_URL}?secid={secid}&klt=101&fqt={1 if adjustment == 'qfq' else 0}&lmt={limit}&end=20500101"
            f"&fields1={fields1}&fields2={fields2}"
        )
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        with open_market_url(request, timeout=self.timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
        return parse_eastmoney_kline_payload(payload, symbol, adjustment=adjustment)


def parse_sina_kline_payload(payload: str, requested_symbol: str) -> KlineResult:
    normalized = normalize_symbol(requested_symbol)
    text = payload.strip()
    rows = json.loads(text) if text and text != "null" else []
    clean_rows: list[tuple[date, float, float, float, float, float]] = []
    for row in rows or []:
        try:
            trading_date = date.fromisoformat(str(row["day"])[:10])
            open_price, close = float(row["open"]), float(row["close"])
            high, low = float(row["high"]), float(row["low"])
            # Sina reports shares; the app uses lots (手).
            volume = float(row.get("volume") or 0) / 100
        except (KeyError, TypeError, ValueError):
            continue
        if min(open_price, close, high, low) <= 0 or high < low:
            continue
        clean_rows.append((trading_date, open_price, high, low, close, volume))
    if not clean_rows:
        return KlineResult([], DataStatus.UNAVAILABLE, warnings=["Sina returned no daily bars"])
    clean_rows.sort(key=lambda item: item[0])
    bars: list[DailyBar] = []
    previous_close: float | None = None
    for trading_date, open_price, high, low, close, volume in clean_rows:
        bars.append(DailyBar(
            trading_date=trading_date, open=open_price, high=high, low=low, close=close,
            volume=volume, previous_close=previous_close, suspended=volume <= 0,
            limit_rate=_default_limit_rate(normalized, trading_date),
        ))
        previous_close = close
    fetched_at = datetime.now(timezone.utc)
    return KlineResult(
        bars, DataStatus.FRESH,
        Provenance("sina-kline", f"{_SINA_KLINE_URL}?symbol={normalized.tencent}&scale=240", fetched_at,
                   datetime.combine(bars[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc)),
        warnings=[] if len(bars) >= 120 else [f"only {len(bars)} valid daily bars"],
    )


class SinaKlineProvider:
    """HTTPS daily-bar fallback. Sina only serves unadjusted prices, so stocks
    are served for adjustment="none" only; indices (no ex-rights) for both."""

    name = "sina-kline"

    def __init__(self, *, priority: int = 25, timeout: float = 8.0):
        self.priority = priority
        self.timeout = timeout

    def fetch_daily(self, symbol: str, *, limit: int = 360, adjustment: str = "qfq") -> KlineResult:
        normalized = normalize_symbol(symbol)
        if adjustment not in {"qfq", "none"}:
            raise ValueError("unsupported adjustment")
        if adjustment == "qfq" and not is_market_index(symbol):
            raise ValueError("Sina kline has no qfq prices for stocks")
        limit = max(30, min(1000, int(limit)))
        url = f"{_SINA_KLINE_URL}?symbol={normalized.tencent}&scale=240&ma=no&datalen={limit}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        with open_market_url(request, timeout=self.timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
        return parse_sina_kline_payload(payload, symbol)


class KlineRouter:
    def __init__(self, providers: list[KlineProvider]):
        self.providers = sorted(providers, key=lambda item: item.priority)

    def fetch_daily(self, symbol: str, *, limit: int = 360, adjustment: str = "qfq") -> KlineResult:
        errors: list[str] = []
        degraded: KlineResult | None = None
        for provider in self.providers:
            try:
                try:
                    result = provider.fetch_daily(symbol, limit=limit, adjustment=adjustment)
                except TypeError:
                    if adjustment != "qfq":
                        raise RuntimeError(f"{provider.name} does not support {adjustment} prices")
                    result = provider.fetch_daily(symbol, limit=limit)
            except Exception as exc:
                errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                continue
            result.errors = [*errors, *result.errors]
            if result.bars and result.status == DataStatus.FRESH:
                return result
            if result.bars:
                degraded = result
            errors = result.errors
        if degraded is not None:
            degraded.errors = errors
            degraded.warnings.append("all fresh kline providers failed; using degraded history")
            return degraded
        return KlineResult([], DataStatus.UNAVAILABLE, warnings=["no provider returned usable daily bars"], errors=errors)
