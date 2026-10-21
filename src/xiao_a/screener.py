"""Manual two-stage screening for the A-share dashboard.

The batch snapshot is intentionally used only as a fast first pass.  A small
liquid shortlist is then sent through the project's existing multi-model
analysis, so a full-market click does not turn into thousands of slow,
fragile per-stock requests.
"""

from __future__ import annotations

import json
import os
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request

from xiao_a.data.http import open_market_url
from xiao_a.data.symbols import normalize_symbol
from xiao_a.risk import RiskContext


_MARKET_URLS = (
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
)
# Eastmoney's public web "ut" parameter (embedded in its public quote pages; not a
# personal credential). Override with XIAO_A_EASTMONEY_UT if Eastmoney changes it.
_EASTMONEY_TOKEN = os.environ.get("XIAO_A_EASTMONEY_UT", "fa5fd1943c7b386f172d6893dbbd1d0c")  # gitleaks:allow
_A_SHARE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_FIELDS = "f2,f3,f5,f6,f7,f8,f9,f12,f14,f18,f20,f21,f22,f23"
_SINA_MARKET_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"


def _number(value: object, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _bounded(value: object, default: float, low: float, high: float) -> float:
    number = _number(value, default)
    return max(low, min(high, number if number is not None else default))


def _as_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    diff = (payload.get("data") or {}).get("diff") or []
    return list(diff.values()) if isinstance(diff, dict) else list(diff)


def _read_json(request: Request, *, encoding: str = "utf-8", retries: int = 1) -> Any:
    """Read a public endpoint with one short retry for transient disconnects."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with open_market_url(request, timeout=9.0) as response:
                payload = response.read().decode(encoding, errors="replace").strip()
            return json.loads(payload)
        except Exception as exc:  # public market endpoints occasionally close idle sockets
            last_error = exc
            if attempt < retries:
                time.sleep(0.35 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _fetch_eastmoney_snapshot() -> list[dict[str, Any]]:
    """Fetch Eastmoney's paginated universe, retrying transient page failures."""
    page_size = 100

    def fetch_page(page: int) -> tuple[list[dict[str, Any]], int]:
        params = {
            "pn": str(page), "pz": str(page_size), "po": "1", "np": "1", "fltt": "2", "invt": "2",
            "ut": _EASTMONEY_TOKEN, "fid": "f3", "fs": _A_SHARE_FILTER, "fields": _FIELDS,
        }
        last_error: Exception | None = None
        for base_url in _MARKET_URLS:
            try:
                request = Request(
                    f"{base_url}?{urlencode(params)}",
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://quote.eastmoney.com/",
                        "Accept": "application/json, text/plain, */*",
                        "Connection": "close",
                    },
                )
                raw = _read_json(request, retries=1)
                data = raw.get("data") or {}
                rows = _as_rows(raw)
                if rows:
                    return rows, int(data.get("total") or 0)
            except Exception as exc:  # public endpoint fallback is deliberate
                last_error = exc
        detail = f"{type(last_error).__name__}: {last_error}" if last_error else "返回为空"
        raise RuntimeError(f"第{page}页全市场行情暂不可用：{detail}")

    first_page, total = fetch_page(1)
    page_count = max(1, min(70, math.ceil(total / page_size)))
    if page_count == 1:
        return first_page
    pages: dict[int, list[dict[str, Any]]] = {1: first_page}
    with ThreadPoolExecutor(max_workers=min(4, page_count - 1), thread_name_prefix="xiao-a-snapshot") as executor:
        futures = {page: executor.submit(fetch_page, page) for page in range(2, page_count + 1)}
        for page, future in futures.items():
            pages[page] = future.result()[0]
    unique: dict[str, dict[str, Any]] = {}
    for page in sorted(pages):
        for row in pages[page]:
            code = str(row.get("f12") or "")
            if code:
                unique[code] = row
    return list(unique.values())


def _sina_snapshot_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map Sina's full-market schema to the established Eastmoney-like row."""
    code = str(raw.get("code") or "").strip()
    if len(code) != 6:
        return None
    previous = _number(raw.get("settlement"))
    high = _number(raw.get("high"))
    low = _number(raw.get("low"))
    amplitude = ((high - low) / previous * 100) if high and low is not None and previous > 0 else None
    return {
        "f12": code,
        "f14": str(raw.get("name") or "").strip(),
        "f2": raw.get("trade"),
        "f3": raw.get("changepercent"),
        # Sina publishes shares while the established snapshot contract uses lots.
        "f5": (_number(raw.get("volume")) or 0.0) / 100,
        "f6": raw.get("amount"),
        "f7": amplitude,
        "f8": raw.get("turnoverratio"),
        "f9": raw.get("per"),
        "f18": raw.get("settlement"),
        # Sina reports market caps in 万元; the dashboard contract is yuan.
        "f20": (_number(raw.get("mktcap")) or 0.0) * 10_000,
        "f21": (_number(raw.get("nmc")) or 0.0) * 10_000,
        "f22": 0.0,
        "f23": raw.get("pb"),
    }


def _fetch_sina_snapshot() -> list[dict[str, Any]]:
    """Fetch a complete A-share snapshot from an independent public source."""
    page_size = 100

    def fetch_page(page: int) -> list[dict[str, Any]]:
        params = {"page": str(page), "num": str(page_size), "sort": "symbol", "asc": "1", "node": "hs_a", "_s_r_a": "page"}
        request = Request(
            f"{_SINA_MARKET_URL}?{urlencode(params)}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/", "Connection": "close"},
        )
        data = _read_json(request, encoding="gbk", retries=1)
        if not isinstance(data, list):
            raise ValueError("Sina 全市场接口返回格式异常")
        return [item for item in (_sina_snapshot_row(raw) for raw in data if isinstance(raw, dict)) if item]

    pages: list[dict[str, Any]] = []
    # The endpoint does not publish a total. Request four pages per batch and
    # stop at its first short page; bounded concurrency keeps it responsive.
    for start in range(1, 81, 4):
        page_numbers = range(start, start + 4)
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="xiao-a-sina") as executor:
            batch = list(executor.map(fetch_page, page_numbers))
        for rows in batch:
            pages.extend(rows)
        if any(len(rows) < page_size for rows in batch):
            break
    if not pages:
        raise RuntimeError("Sina 全市场接口返回为空")
    unique = {str(row["f12"]): row for row in pages}
    return list(unique.values())


def fetch_a_share_snapshot() -> list[dict[str, Any]]:
    """Fetch a full-market snapshot, switching sources when one is unavailable."""
    try:
        return _fetch_eastmoney_snapshot()
    except Exception as eastmoney_error:
        try:
            return _fetch_sina_snapshot()
        except Exception as sina_error:
            raise RuntimeError(
                "全市场行情暂不可用："
                f"东方财富 {type(eastmoney_error).__name__}: {eastmoney_error}；"
                f"新浪备用源 {type(sina_error).__name__}: {sina_error}"
            ) from sina_error


def normalize_snapshot_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert Eastmoney's abbreviated fields into one stable UI row."""
    code = str(raw.get("f12") or "").strip()
    name = str(raw.get("f14") or "").strip()
    price = _number(raw.get("f2"))
    if len(code) != 6 or not name or price is None or price <= 0:
        return None
    try:
        symbol = normalize_symbol(code).canonical
    except ValueError:
        return None
    return {
        "symbol": symbol,
        "code": code,
        "name": name,
        "price": price,
        "change_pct": _number(raw.get("f3"), 0.0) or 0.0,
        "volume": _number(raw.get("f5")),
        "amount": _number(raw.get("f6"), 0.0) or 0.0,
        "amplitude_pct": _number(raw.get("f7")),
        "turnover_pct": _number(raw.get("f8")),
        "pe_dynamic": _number(raw.get("f9")),
        "previous_close": _number(raw.get("f18")),
        "market_cap": _number(raw.get("f20")),
        "float_market_cap": _number(raw.get("f21")),
        "speed_pct": _number(raw.get("f22")),
        "pb": _number(raw.get("f23")),
    }


def cheap_score(row: dict[str, Any]) -> float:
    """Rank the batch pass; this is not an investment decision by itself."""
    amount = max(float(row.get("amount") or 0.0), 1.0)
    change = float(row.get("change_pct") or 0.0)
    turnover = float(row.get("turnover_pct") or 0.0)
    speed = float(row.get("speed_pct") or 0.0)
    liquidity = min(1.0, max(0.0, math.log10(amount / 10_000_000) / 2.0))
    turnover_quality = max(0.0, 1.0 - min(abs(math.log10(max(turnover, 0.15) / 3.0)) / 1.3, 1.0))
    momentum = max(-1.0, min(1.0, change / 5.0))
    score = liquidity * 36 + turnover_quality * 24 + (momentum + 1) * 17 + min(abs(speed), 3.0) * 2
    # Avoid letting one-day limit-up style moves dominate a trend shortlist.
    if change >= 9.5:
        score -= 18
    elif change <= -6.0:
        score -= 12
    return round(score, 2)


def eligible_rows(rows: list[dict[str, Any]], *, minimum_amount: float) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = normalize_snapshot_row(raw)
        if row is None:
            continue
        upper_name = row["name"].upper()
        turnover = row.get("turnover_pct")
        if "ST" in upper_name or "退" in row["name"]:
            continue
        if row["amount"] < minimum_amount or row["price"] < 2.0:
            continue
        if turnover is not None and (turnover < 0.15 or turnover > 35):
            continue
        row["pre_score"] = cheap_score(row)
        result.append(row)
    return sorted(result, key=lambda item: item["pre_score"], reverse=True)


def _analysis_row(analyzer: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        analysis = analyzer.analyze(
            row["symbol"],
            RiskContext(daily_amount=row["amount"]),
            include_supplemental=False,
        )
    except Exception:
        return None
    decision = analysis.decision
    signals = sorted(analysis.decision.strategy_signals, key=lambda item: item.confidence, reverse=True)
    basis = [item.thesis for item in signals[:2] if item.thesis]
    warnings = [*decision.warnings, *decision.vetoes, *analysis.warnings]
    return {
        **row,
        "final_action": decision.final_action.value,
        "score": round(float(decision.score), 4),
        "confidence": round(float(decision.confidence), 4),
        "executable": bool(decision.executable),
        "basis": basis,
        "warnings": warnings[:2],
        "data_status": analysis.quote.status.value if analysis.quote else "partial",
    }


def run_market_screener(
    analyzer: Any,
    *,
    minimum_amount: float = 50_000_000,
    result_limit: int = 20,
    snapshot_fetcher: Callable[[], list[dict[str, Any]]] = fetch_a_share_snapshot,
) -> dict[str, Any]:
    """Run a manual scan, returning only the strongest model-compatible rows."""
    minimum_amount = _bounded(minimum_amount, 50_000_000, 10_000_000, 2_000_000_000)
    result_limit = int(_bounded(result_limit, 20, 5, 30))
    snapshot = snapshot_fetcher()
    candidates = eligible_rows(snapshot, minimum_amount=minimum_amount)
    deep_limit = min(36, max(24, result_limit * 2))
    shortlist = candidates[:deep_limit]
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="xiao-a-screener") as executor:
        analyzed = [item for item in executor.map(lambda row: _analysis_row(analyzer, row), shortlist) if item]

    action_order = {"buy": 0, "hold": 1, "reduce": 2, "sell": 3}
    analyzed.sort(key=lambda item: (
        action_order.get(item["final_action"], 9),
        -item["score"], -item["confidence"], -item["pre_score"],
    ))
    displayed = [item for item in analyzed if item["final_action"] in {"buy", "hold"}][:result_limit]
    for rank, item in enumerate(displayed, 1):
        item["rank"] = rank
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "minimum_amount": minimum_amount,
        "universe_count": len(snapshot),
        "eligible_count": len(candidates),
        "analyzed_count": len(analyzed),
        "failed_count": len(shortlist) - len(analyzed),
        "results": displayed,
        "warnings": [
            "先按流动性、价格与非ST规则初筛，再对有限候选运行现有模型。",
            "为控制时延，本轮未逐股拉取公告、资金流和涨停池；这些信息会在单股详细分析时补齐。",
        ],
    }
