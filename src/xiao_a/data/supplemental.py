from __future__ import annotations

import json
import math
from threading import Lock
from .limit_pool import parse_cls, parse_eastmoney, enrich_pool, summarize
from .volume import SHANGHAI
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request

from xiao_a.data.http import open_market_url
from xiao_a.data.symbols import is_market_index, normalize_symbol
from xiao_a.models import DataStatus, Provenance


_ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_FUND_FLOW_URLS = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
)
_FUND_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_POOL_DETAIL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
_CLS_FUND_URL = "https://x-quote.cls.cn/quote/stock/fundflow"
_FUND_FLOW_URL = _FUND_FLOW_URLS[0]
_LIMIT_UP_URL = "https://x-quote.cls.cn/quote/index/up_down_analysis"
_LIMIT_UP_PARAMS = {
    "app": "CailianpressWeb",
    "os": "web",
    "rever": 1,
    "sv": "8.4.6",
    "type": "up_pool",
    "way": "last_px",
    "sign": "a6ab28604a6dbe891cdbd7764799eda1",
}
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
_NEGATIVE_KEYWORDS = (
    "退市", "立案", "重大诉讼", "重大违法", "债务违约", "业绩预亏", "被处罚", "ST",
)


@dataclass
class SupplementalResult:
    data: dict[str, object]
    status: DataStatus
    provenances: list[Provenance] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        result = None if text in {"", "-", "--", "None", "null"} else float(text.rstrip("%"))
        return result if result is not None and math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _market_code(symbol: str) -> int:
    exchange = normalize_symbol(symbol).exchange
    return 1 if exchange == "SH" else 0


def _source_time() -> datetime:
    return datetime.now(timezone.utc)


def parse_announcements_payload(payload: str, *, source_url: str = _ANNOUNCEMENT_URL) -> tuple[list[dict], list[str], Provenance]:
    raw = json.loads(payload)
    rows = ((raw.get("data") or {}).get("list") or [])
    items: list[dict] = []
    risk_flags: list[str] = []
    for row in rows:
        title = str(row.get("title") or row.get("notice_title") or "").strip()
        if not title:
            continue
        notice_date = str(row.get("notice_date") or row.get("date") or row.get("ctime") or "")
        item = {
            "title": title,
            "date": notice_date,
            "type": str(row.get("column_name") or row.get("ann_type") or "公告"),
            "art_code": str(row.get("art_code") or ""),
        }
        items.append(item)
        if any(keyword in title.upper() for keyword in _NEGATIVE_KEYWORDS):
            risk_flags.append(title)
    fetched_at = _source_time()
    return items[:30], risk_flags[:3], Provenance("eastmoney-announcements", source_url, fetched_at, fetched_at)


def parse_fund_flow_payload(payload: str, *, source_url: str = _FUND_FLOW_URL) -> tuple[dict, Provenance]:
    raw = json.loads(payload)
    rows = ((raw.get("data") or {}).get("klines") or [])
    parsed: list[dict] = []
    for line in rows:
        parts = line.split(",") if isinstance(line, str) else list(line)
        if len(parts) < 6:
            continue
        try:
            datetime.strptime(str(parts[0]), "%Y-%m-%d")
            if not all(math.isfinite(float(v)) for v in parts[1:6]):
                continue
            parsed.append({
                "date": str(parts[0]),
                "main_net_inflow": float(parts[1]),
                "small_inflow": float(parts[2]),
                "medium_inflow": float(parts[3]),
                "large_inflow": float(parts[4]),
                "super_large_inflow": float(parts[5]),
            })
        except (TypeError, ValueError):
            continue
    parsed = sorted({item["date"]: item for item in parsed}.values(), key=lambda item: item["date"])
    if not parsed:
        raise ValueError("Eastmoney returned no fund-flow rows")
    latest = parsed[-1]
    five_day = parsed[-5:]
    latest_main = latest["main_net_inflow"]
    five_day_main = sum(item["main_net_inflow"] for item in five_day) if len(five_day) == 5 else None
    # Normalize by a fixed scale so the feature remains comparable across
    # stocks; the raw amounts are retained for display.
    signal = max(-1.0, min(1.0, latest_main / 300_000_000.0))
    return {
        "available": True,
        "source_label": "东方财富 · 按订单大小统计，不代表机构真实账户流向",
        "source_url": source_url,
        "trade_date": latest["date"],
        "main_net_inflow": latest_main,
        "five_day_net_inflow": five_day_main,
        "super_large_inflow": latest["super_large_inflow"],
        "large_inflow": latest["large_inflow"],
        "medium_inflow": latest["medium_inflow"],
        "small_inflow": latest["small_inflow"],
        "signal": signal,
        "trend": "主力净流入" if latest_main > 0 else "主力净流出" if latest_main < 0 else "主力资金接近平衡",
        "history": parsed[-5:],
    }, Provenance("eastmoney-fund-flow", source_url, _source_time(), datetime.fromisoformat(latest["date"]).replace(tzinfo=SHANGHAI))


def parse_cls_fund_flow(payload: str, *, source_url: str = _CLS_FUND_URL) -> tuple[dict, Provenance]:
    raw = json.loads(payload)
    data = raw.get("data") or {}
    if raw.get("code") != 200 or not data:
        raise ValueError("CLS fund-flow unavailable")
    as_of = datetime.strptime(str(data.get("date")), "%Y%m%d").replace(tzinfo=SHANGHAI)
    main = _float(data.get("main_fund_diff"))
    if main is None:
        raise ValueError("CLS main fund-flow missing")
    result = {"available": True, "trade_date": as_of.date().isoformat(),
              "source_label": "财联社 · 资金流为供应商统计口径，不代表机构真实账户流向",
              "source_url": source_url,
              "main_net_inflow": main, "five_day_net_inflow": _float(data.get("main_fund_5")),
              "super_large_inflow": _float(data.get("super_fund_diff")),
              "large_inflow": _float(data.get("large_fund_diff")),
              "medium_inflow": _float(data.get("medium_fund_diff")),
              "small_inflow": _float(data.get("little_fund_diff")),
              "signal": max(-1.0, min(1.0, main / 300_000_000.0)),
              "trend": "主力净流入" if main > 0 else "主力净流出" if main < 0 else "主力资金接近平衡"}
    return result, Provenance("cailian-fund-flow", source_url, _source_time(), as_of)


def parse_limit_up_payload(payload: str, *, symbol: str, source_url: str = _LIMIT_UP_URL) -> tuple[dict, Provenance]:
    result = parse_cls(payload, symbol)
    return result, Provenance("cailian-limit-up-pool", source_url, _source_time(), None)


class SupplementalDataProvider:
    """Fetch stock notices, capital flow, and today's limit-up pool."""

    name = "supplemental-market-data"

    def __init__(self, *, timeout: float = 8.0):
        self.timeout = timeout
        self._pool_cache = None
        self._pool_lock = Lock()

    def _get_json(self, url: str, params: dict, *, referer: str | None = None, attempts: int = 2, timeout: float | None = None) -> str:
        full_url = f"{url}?{urlencode(params)}"
        headers = {
            **_HEADERS,
            "Referer": referer or _HEADERS["Referer"],
            "Accept": "application/json,text/plain,*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                request = Request(full_url, headers=headers)
                with open_market_url(request, timeout=timeout or self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.2)
        raise last_error or RuntimeError("market-data request failed")

    def _announcements(self, symbol: str) -> tuple[dict, Provenance]:
        if is_market_index(symbol):
            raise ValueError("指数不适用个股公告查询")
        normalized = normalize_symbol(symbol)
        end = datetime.now().date()
        begin = end - timedelta(days=90)
        params = {
            "sr": -1, "page_size": 50, "page_index": 1, "ann_type": "SHA,SZA",
            "client_source": "web", "f_node": 0, "s_node": 0,
            "begin_time": begin.isoformat(), "end_time": end.isoformat(),
            "stock_list": normalized.code,
        }
        payload = self._get_json(_ANNOUNCEMENT_URL, params, referer="https://data.eastmoney.com/")
        items, risk_flags, provenance = parse_announcements_payload(payload, source_url=f"{_ANNOUNCEMENT_URL}?{urlencode(params)}")
        return {"available": True, "count": len(items), "items": items, "risk_flags": risk_flags}, provenance

    def _fund_flow(self, symbol: str) -> tuple[dict, Provenance]:
        if is_market_index(symbol):
            raise ValueError("指数不适用个股资金流查询")
        params = {"secu_code": normalize_symbol(symbol).tencent, "app": "CailianpressWeb", "os": "web", "sv": "8.4.6"}
        try:
            return parse_cls_fund_flow(self._get_json(_CLS_FUND_URL, params,
                referer="https://www.cls.cn/", timeout=min(self.timeout, 3)),
                source_url=f"{_CLS_FUND_URL}?{urlencode(params)}")
        except Exception:
            # Use the complete EM observation, never mix providers' daily or
            # multi-day amounts because order-classification definitions differ.
            return self._eastmoney_fund_flow(symbol)

    def _eastmoney_fund_flow(self, symbol: str) -> tuple[dict, Provenance]:
        normalized = normalize_symbol(symbol)
        params = {"lmt": 10, "klt": 101, "secid": f"{_market_code(symbol)}.{normalized.code}",
                  "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56",
                  "ut": "b2884a393a59ad64002292a3e90d46a5"}
        last_error = None
        for url in _FUND_FLOW_URLS:
            try:
                payload = self._get_json(url, params, timeout=min(self.timeout, 3))
                code = (json.loads(payload).get("data") or {}).get("code")
                if code != normalized.code:
                    raise ValueError("fund-flow symbol mismatch")
                current, provenance = parse_fund_flow_payload(payload, source_url=url)
                break
            except Exception as exc:
                last_error = exc
        else:
            raise last_error or ValueError("fund-flow endpoints unavailable")
        # History is optional: its failure must not discard a valid current observation.
        try:
            history, _ = parse_fund_flow_payload(self._get_json(
                _FUND_HISTORY_URL, params, attempts=1, timeout=min(self.timeout, 2)))
            if history["trade_date"] == current["trade_date"]:
                rows = {x["date"]: x for x in history["history"]}
                rows[current["trade_date"]] = current["history"][-1]
                current["history"] = sorted(rows.values(), key=lambda x: x["date"])[-5:]
                if len(current["history"]) == 5:
                    current["five_day_net_inflow"] = sum(x["main_net_inflow"] for x in current["history"])
        except Exception:
            pass
        return current, provenance

    def _limit_up_pool(self, symbol: str) -> tuple[dict, Provenance]:
        with self._pool_lock:
            if self._pool_cache and time.monotonic() - self._pool_cache[0] < 45:
                cached, provenance = self._pool_cache[1:]
                return summarize(cached["items"], symbol, cached["trade_date"], cached["source_label"]), provenance
            primary = None
            try:
                primary = parse_cls(self._get_json(_LIMIT_UP_URL, _LIMIT_UP_PARAMS,
                    referer="https://www.cls.cn/", timeout=min(self.timeout, 3)), symbol)
            except Exception:
                pass
            trade_date = primary.get("trade_date") if primary else None
            if not trade_date:
                from .tencent import TencentQuoteProvider
                quote = TencentQuoteProvider(timeout=3).fetch_quote(symbol).quote
                if quote and quote.provenance and quote.provenance.as_of:
                    trade_date = quote.provenance.as_of.astimezone(SHANGHAI).date().isoformat()
            try:
                if not trade_date:
                    raise ValueError("pool trading date unknown")
                extra = parse_eastmoney(self._get_json(_POOL_DETAIL_URL, {
                    "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": 0,
                    "pagesize": 10000, "sort": "fbt:asc", "date": trade_date.replace("-", "")},
                    timeout=min(self.timeout, 3)), symbol)
                if extra["trade_date"] != trade_date:
                    raise ValueError("pool date mismatch")
                primary = enrich_pool(primary, extra, symbol) if primary else extra
            except Exception:
                if primary is None:
                    raise
            if not primary["trade_date"]:
                raise ValueError("pool trading date unknown")
            # CLS covers some stocks not present in the EM pool. One quote batch
            # fills their actual turnover/amount; never blend different sessions.
            missing = [x for x in primary["items"] if x.get("turnover_pct") is None]
            if missing:
                try:
                    from .tencent import parse_tencent_quote_payload
                    symbols = ",".join(normalize_symbol(x["symbol"]).tencent for x in missing)
                    request = Request("https://qt.gtimg.cn/q=" + symbols, headers=_HEADERS)
                    with open_market_url(request, timeout=min(self.timeout, 3)) as response:
                        payload = response.read().decode("gbk", errors="replace")
                    for item in missing:
                        quote = parse_tencent_quote_payload(payload, item["symbol"])
                        if (quote.provenance and quote.provenance.as_of and
                            quote.provenance.as_of.astimezone(SHANGHAI).date().isoformat() == primary["trade_date"]):
                            item.update(turnover_pct=quote.turnover_pct, amount=quote.amount)
                    primary["source_label"] += " · 腾讯同日行情补充换手率与成交额"
                except Exception:
                    pass
            provenance = Provenance("limit-up-pool", _LIMIT_UP_URL + " ; " + _POOL_DETAIL_URL,
                _source_time(), datetime.fromisoformat(primary["trade_date"]).replace(tzinfo=SHANGHAI))
            self._pool_cache = (time.monotonic(), primary, provenance)
            return primary, provenance

    def fetch(self, symbol: str) -> SupplementalResult:
        data = {
            "announcements": {"available": False, "count": 0, "items": [], "risk_flags": []},
            "fund_flow": {"available": False},
            "limit_up_pool": {"available": False, "count": None, "max_continuous": None, "selected": None, "top": []},
        }
        provenances: list[Provenance] = []
        warnings: list[str] = []
        errors: list[str] = []
        tasks = {
            "公告": self._announcements,
            "资金流": self._fund_flow,
            "涨停池": self._limit_up_pool,
        }
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fetcher, symbol): label for label, fetcher in tasks.items()}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    value, provenance = future.result()
                    key = {"公告": "announcements", "资金流": "fund_flow", "涨停池": "limit_up_pool"}[label]
                    data[key] = value
                    provenances.append(provenance)
                except Exception as exc:
                    warnings.append(f"{label}数据暂不可用，相关信息不参与判断")
                    errors.append(f"{label}: {type(exc).__name__}: {exc}")
        available_count = sum(bool(value.get("available")) for value in data.values())
        status = DataStatus.FRESH if available_count == 3 else DataStatus.PARTIAL if available_count else DataStatus.UNAVAILABLE
        return SupplementalResult(data, status, provenances, warnings, errors)

    def fetch_market_limit_up_pool(self, symbol: str) -> SupplementalResult:
        """Fetch the all-market pool without querying stock-only notices or flows."""
        data = {
            "announcements": {"available": False, "count": 0, "items": [], "risk_flags": []},
            "fund_flow": {"available": False},
            "limit_up_pool": {"available": False, "count": None, "max_continuous": None, "selected": None, "top": []},
        }
        try:
            data["limit_up_pool"], provenance = self._limit_up_pool(symbol)
            return SupplementalResult(data, DataStatus.FRESH, [provenance])
        except Exception as exc:
            return SupplementalResult(
                data,
                DataStatus.UNAVAILABLE,
                warnings=["全A涨停池暂不可用"],
                errors=[f"limit-up-pool: {type(exc).__name__}: {exc}"],
            )
