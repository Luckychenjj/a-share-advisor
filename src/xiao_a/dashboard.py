from __future__ import annotations

import json
import os
import re
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from math import isfinite
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request
from xiao_a.data.http import open_market_url
from xiao_a.data.symbols import is_market_index, normalize_symbol
from xiao_a.data.volume import SHANGHAI, average_prior_volume, projected_volume_ratio, trading_progress
from xiao_a.models import Quote, Provenance
from xiao_a.data.router import ProviderResult

from xiao_a.auto import AutoAnalyzer
from xiao_a.backtest import AShareBacktester, moving_average_crossover_signals
from xiao_a.data.kline import EastmoneyKlineProvider, KlineResult, KlineRouter, SinaKlineProvider, TencentKlineProvider
from xiao_a.data.fallback import BaostockKlineProvider, PytdxKlineProvider, PytdxQuoteProvider, SinaQuoteProvider
from xiao_a.data.router import QuoteRouter
from xiao_a.data.search import TencentStockSearchProvider
from xiao_a.data.tencent import TencentQuoteProvider
from xiao_a.service import evaluate_payload, jsonable
from xiao_a.risk import RiskContext
from xiao_a.portfolio import HoldingInput, PortfolioAnalyzer
from xiao_a.portfolio_store import PortfolioStore
from xiao_a.performance import summarize_equity_history
from xiao_a.screener import run_market_screener
from xiao_a.hybrid import HybridGateway
from xiao_a.hybrid import user_data_directory
from xiao_a.models import DataStatus
from xiao_a.prediction_audit import MODEL_CATALOG, TARGET_DEFINITION, PredictionLedger, chronological_calibration, select_future_bar_window
from xiao_a.market_prediction import independent_market_prediction


_AUTO_ANALYZER = AutoAnalyzer()
_PORTFOLIO_ANALYZER = PortfolioAnalyzer(_AUTO_ANALYZER)
_PORTFOLIO_STORE = PortfolioStore()
_HYBRID_GATEWAY = HybridGateway()
_PREDICTION_LEDGER = PredictionLedger(
    os.environ.get("XIAO_A_AUDIT_FILE", str(user_data_directory() / "prediction_audit.jsonl"))
)
_QUOTE_ROUTER = QuoteRouter([TencentQuoteProvider(), SinaQuoteProvider(), PytdxQuoteProvider()])
_SEARCH_PROVIDER = TencentStockSearchProvider()
_QUOTE_CACHE: dict[str, tuple[float, object]] = {}
_QUOTE_CACHE_LOCK = threading.RLock()
_QUOTE_CACHE_TTL = 0.35
_VOLUME_BASELINE_PROVIDER = KlineRouter([
    TencentKlineProvider(priority=10),
    EastmoneyKlineProvider(priority=20),
    SinaKlineProvider(priority=25),
    PytdxKlineProvider(priority=30),
    BaostockKlineProvider(priority=40),
])
_BACKTEST_KLINE_PROVIDER = KlineRouter([
    TencentKlineProvider(priority=10),
    EastmoneyKlineProvider(priority=20),
    SinaKlineProvider(priority=25),  # indices / unadjusted only; never stock qfq
])
_VOLUME_BASELINE_CACHE: dict[tuple[str, date], tuple[float, float | None]] = {}
_VOLUME_BASELINE_LOCK = threading.RLock()
_VOLUME_BASELINE_TTL = 300.0
_INTRADAY_URLS = (
    "https://push2.eastmoney.com/api/qt/stock/trends2/get",
    "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
)
_TENCENT_INTRADAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
_FUNDAMENTALS_URLS = (
    "https://82.push2.eastmoney.com/api/qt/stock/get",
    "https://91.push2.eastmoney.com/api/qt/stock/get",
    "https://push2.eastmoney.com/api/qt/stock/get",
    "https://push2delay.eastmoney.com/api/qt/stock/get",
    "https://push2his.eastmoney.com/api/qt/stock/get",
)
_FUNDAMENTALS_CACHE: dict[str, tuple[float, dict]] = {}
_FUNDAMENTALS_LOCK = threading.RLock()
_FUNDAMENTALS_TTL = 300.0
_FINANCIAL_CACHE: dict[str, tuple[float, dict]] = {}
_FINANCIAL_LOCK = threading.RLock()
_FINANCIAL_TTL = 86400.0
_COMPANY_BRIEF_CACHE: dict[str, tuple[float, dict]] = {}
_COMPANY_BRIEF_LOCK = threading.RLock()
_COMPANY_BRIEF_TTL = 3600.0
_PUBLIC_SHARES_CACHE: dict[str, tuple[float, float | None]] = {}
_PUBLIC_SHARES_LOCK = threading.RLock()
_PUBLIC_SHARES_TTL = 86400.0


def _public_share_capital(symbol: str) -> float | None:
    """Read the latest publicly displayed total share capital from Tonghuashun F10."""
    key = str(symbol).strip().upper()
    now = time.monotonic()
    with _PUBLIC_SHARES_LOCK:
        cached = _PUBLIC_SHARES_CACHE.get(key)
        if cached and now - cached[0] < _PUBLIC_SHARES_TTL:
            return cached[1]
    shares = None
    try:
        normalized = normalize_symbol(symbol)
        url = f"https://basic.10jqka.com.cn/{normalized.code}/equity.html"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://basic.10jqka.com.cn/"})
        with open_market_url(request, timeout=8.0) as response:
            html = response.read().decode("gbk", errors="ignore")
        section = html[html.find('id="stockcapit"'):]
        section = section[:section.find('id="astockconstr"')] if 'id="astockconstr"' in section else section
        match = re.search(r"<td>\s*([\d.]+)\s*([亿万])\s*</td>", section)
        if match:
            multiplier = 100_000_000 if match.group(2) == "亿" else 10_000
            shares = float(match.group(1)) * multiplier
    except Exception:
        shares = None
    with _PUBLIC_SHARES_LOCK:
        _PUBLIC_SHARES_CACHE[key] = (time.monotonic(), shares)
    return shares


def _fundamentals(symbol: str) -> dict:
    key = str(symbol).strip().upper()
    now = time.monotonic()
    with _FUNDAMENTALS_LOCK:
        cached = _FUNDAMENTALS_CACHE.get(key)
        if cached and now - cached[0] < _FUNDAMENTALS_TTL:
            return cached[1]
    result: dict = {}
    try:
        normalized = normalize_symbol(symbol)
        secid = f"{1 if normalized.exchange == 'SH' else 0}.{normalized.code}"
        for base_url in _FUNDAMENTALS_URLS:
            try:
                url = f"{base_url}?ut=fa5fd1943c7b386f172d6893dbfba10b&fltt=2&invt=2&secid={secid}&fields=f84,f85,f116,f117,f127,f162,f163,f164,f167"
                request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
                with open_market_url(request, timeout=4.0) as response:
                    data = (json.loads(response.read().decode("utf-8", errors="replace")).get("data") or {})
                if data:
                    result = {
                        "total_shares": data.get("f84"),
                        "float_shares": data.get("f85"),
                        "market_cap": data.get("f116"),
                        "float_market_cap": data.get("f117"),
                        "industry": data.get("f127"),
                        "pe_dynamic": data.get("f162"),
                        "pe_static": data.get("f163"),
                        "pe_ttm": data.get("f164"),
                        "pb": data.get("f167"),
                    }
                    break
            except Exception:
                continue
    except Exception:
        result = {}
    with _FUNDAMENTALS_LOCK:
        _FUNDAMENTALS_CACHE[key] = (time.monotonic(), result)
    return result


def _financial_metrics(symbol: str, market_cap: float | None) -> dict:
    """Compute PE values from Eastmoney financial statements when quote PE is absent."""
    key = str(symbol).strip().upper()
    now = time.monotonic()
    with _FINANCIAL_LOCK:
        cached = _FINANCIAL_CACHE.get(key)
        if cached and now - cached[0] < _FINANCIAL_TTL:
            rows = cached[1]
        else:
            rows = {}
    if not rows:
        normalized = normalize_symbol(symbol)
        secucode = f"{normalized.code}.{normalized.exchange}"
        params = {
            "type": "RPT_F10_FINANCE_MAINFINADATA",
            "sty": "APP_F10_MAINFINADATA",
            "st": "REPORT_DATE",
            "sr": "-1",
            "filter": f'(SECUCODE="{secucode}")',
            "p": "1", "ps": "10", "source": "HSF10",
        }
        try:
            url = "https://datacenter.eastmoney.com/securities/api/data/get?" + urlencode(params)
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            with open_market_url(request, timeout=8.0) as response:
                raw = json.loads(response.read().decode("utf-8", errors="replace"))
            rows = (raw.get("result") or {}).get("data") or []
        except Exception:
            rows = []
        with _FINANCIAL_LOCK:
            _FINANCIAL_CACHE[key] = (time.monotonic(), rows)
    if not rows:
        return {}

    def number(row: dict, *names: str) -> float | None:
        for name in names:
            value = row.get(name)
            try:
                if value is not None and value != "":
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def report_date(row: dict) -> datetime | None:
        try:
            return datetime.fromisoformat(str(row.get("REPORT_DATE", "")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    dated = sorted(((report_date(row), row) for row in rows), key=lambda item: item[0] or datetime.min, reverse=True)
    dated = [(date, row) for date, row in dated if date is not None]
    if not dated:
        return {}
    latest_date, latest = dated[0]
    total_shares = number(
        latest,
        "TOTAL_SHARES",
        "TOTAL_SHARE_CAPITAL",
        "TOTAL_SHARES_CAPITAL",
        "ISSUED_SHARES",
        "SHARE_CAPITAL",
        "TOTAL_CAPITAL_STOCK",
    )
    result = {}
    if total_shares and total_shares > 0:
        result["total_shares"] = total_shares
    if not market_cap or market_cap <= 0:
        return result
    latest_profit = number(latest, "PARENTNETPROFIT", "NET_PROFIT")
    annual_rows = [(date, row) for date, row in dated if date.month == 12]
    annual_profit = number(annual_rows[0][1], "PARENTNETPROFIT", "NET_PROFIT") if annual_rows else latest_profit
    ttm_profit = annual_profit
    if latest_date.month != 12 and annual_rows:
        prior_year_date = latest_date.replace(year=latest_date.year - 1)
        prior_period = next((number(row, "PARENTNETPROFIT", "NET_PROFIT") for date, row in dated if date.date() == prior_year_date.date()), None)
        if prior_period is not None and latest_profit is not None:
            ttm_profit = annual_profit + latest_profit - prior_period
    if annual_profit and annual_profit > 0:
        result["pe_static"] = market_cap / annual_profit
    # Financial-report-based dynamic PE: annualize the latest cumulative
    # attributable profit, rather than misusing year-over-year growth as a
    # forecast. This is an accounting estimate, not an analyst consensus.
    if latest_profit and latest_profit > 0:
        months = 12 if latest_date.month == 12 else latest_date.month
        if months in (3, 6, 9, 12):
            annualized_profit = latest_profit * 12 / months
            if annualized_profit > 0:
                result["pe_dynamic"] = market_cap / annualized_profit
    if ttm_profit and ttm_profit > 0:
        result["pe_ttm"] = market_cap / ttm_profit
    return result


def _company_brief(symbol: str, *, fallback_name: str | None = None, fallback_industry: str | None = None) -> dict:
    """Company profile, latest reported fundamentals, and any recent disclosed forecast."""
    key = str(symbol).strip().upper()
    now = time.monotonic()
    with _COMPANY_BRIEF_LOCK:
        cached = _COMPANY_BRIEF_CACHE.get(key)
        if cached and now - cached[0] < _COMPANY_BRIEF_TTL:
            return cached[1]

    normalized = normalize_symbol(symbol)
    secucode = f"{normalized.code}.{normalized.exchange}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://quote.eastmoney.com/",
    }

    def fetch_report(report_name: str, *, legacy: bool = False) -> list[dict]:
        if legacy:
            params = {
                "type": report_name,
                "sty": "APP_F10_MAINFINADATA",
                "filter": f'(SECUCODE="{secucode}")',
                "p": 1,
                "ps": 8,
                "sr": -1,
                "st": "REPORT_DATE",
                "source": "HSF10",
                "client": "PC",
            }
            url = "https://datacenter.eastmoney.com/securities/api/data/get?" + urlencode(params)
        else:
            params = {
                "reportName": report_name,
                "columns": "ALL",
                "filter": f'(SECUCODE="{secucode}")',
                "pageNumber": 1,
                "pageSize": 8,
                "sortColumns": "NOTICE_DATE" if report_name == "RPT_PUBLIC_OP_NEWPREDICT" else "REPORT_DATE",
                "sortTypes": -1,
                "source": "HSF10",
                "client": "PC",
            }
            url = "https://datacenter.eastmoney.com/securities/api/data/v1/get?" + urlencode(params)
        request = Request(url, headers=headers)
        with open_market_url(request, timeout=5.0) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace"))
        rows = ((raw.get("result") or {}).get("data") or [])
        return [row for row in rows if isinstance(row, dict)]

    def fetch_profile() -> list[dict]:
        params = {
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": 1,
            "pageSize": 1,
            "source": "HSF10",
            "client": "PC",
        }
        for endpoint in (
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
        ):
            try:
                request = Request(endpoint + "?" + urlencode(params), headers=headers)
                with open_market_url(request, timeout=5.0) as response:
                    raw = json.loads(response.read().decode("utf-8", errors="replace"))
                rows = ((raw.get("result") or {}).get("data") or [])
                if rows:
                    return [row for row in rows if isinstance(row, dict)]
            except Exception:
                continue
        return []

    with ThreadPoolExecutor(max_workers=3) as executor:
        profile_future = executor.submit(fetch_profile)
        finance_future = executor.submit(fetch_report, "RPT_F10_FINANCE_MAINFINADATA", legacy=True)
        forecast_future = executor.submit(fetch_report, "RPT_PUBLIC_OP_NEWPREDICT")
        try:
            profile_rows = profile_future.result()
        except Exception:
            profile_rows = []
        try:
            finance_rows = finance_future.result()
        except Exception:
            finance_rows = []
        try:
            forecast_rows = forecast_future.result()
        except Exception:
            forecast_rows = []

    def first_number(row: dict, *names: str) -> float | None:
        for name in names:
            try:
                value = float(row.get(name))
                if isfinite(value):
                    return value
            except (TypeError, ValueError):
                pass
        return None

    def percent_value(value: float | None) -> float | None:
        if value is None:
            return None
        return value * 100 if abs(value) <= 1 else value

    profile = profile_rows[0] if profile_rows else {}
    def clean_profile_text(value: object) -> str:
        text = re.sub(r"<[^>]*>", " ", str(value or ""))
        return re.sub(r"\s+", " ", text).strip()

    raw_description = str(
        profile.get("MAIN_BUSINESS") or profile.get("ORG_PROFILE") or ""
    )
    description = clean_profile_text(raw_description)
    description = re.split(r"(?<=[。；;])", description, maxsplit=1)[0].strip()
    # Only present an industry-position statement when the company profile itself
    # explicitly provides one. Industry classification alone is not evidence of
    # leadership or market rank.
    profile_narrative = clean_profile_text(profile.get("ORG_PROFILE"))
    position_terms = ("行业地位", "市场地位", "龙头", "领先", "领导者", "第一", "首家", "首批", "少数几家", "市场占有率")
    position_sentence = next(
        (
            sentence.strip()
            for sentence in re.split(r"(?<=[。；;])", profile_narrative)
            if any(term in sentence for term in position_terms)
        ),
        "",
    )
    position_basis = "东方财富公司资料中的公司自述，未作独立市场份额排名核验" if position_sentence else None
    position_source_url = None
    # Annual-report disclosures contain industry-position statements that are
    # not included in the short F10 profile feed. Keep these explicit, dated,
    # company-attributed notes separate from any model inference.
    annual_report_positions = {
        "300308": (
            "公司在2025年年报中将自身表述为光模块领域的领先者，并称市场份额持续成长。",
            "公司2025年年度报告（行业地位章节；公司披露）",
            "https://disc.static.szse.cn/disc/disk03/finalpage/2026-03-31/7bb16111-3899-41e7-8a07-abf2c8798dd7.PDF",
        ),
    }
    if not position_sentence and normalized.code in annual_report_positions:
        position_sentence, position_basis, position_source_url = annual_report_positions[normalized.code]
    if len(position_sentence) > 150:
        position_sentence = position_sentence[:147].rstrip("，；, ") + "…"
    latest = finance_rows[0] if finance_rows else {}
    latest_report_text = str(latest.get("REPORT_DATE") or "")[:10]
    try:
        latest_report_date = date.fromisoformat(latest_report_text)
    except ValueError:
        latest_report_date = None
    financial = {
        "report_date": latest_report_text or None,
        "revenue": first_number(latest, "TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "TOTAL_OPERATING_REVENUE"),
        "revenue_yoy_pct": percent_value(first_number(latest, "TOTAL_OPERATE_INCOME_YOY", "YSTZ", "OPERATE_INCOME_YOY")),
        "net_profit": first_number(latest, "PARENTNETPROFIT", "NETPROFIT", "NET_PROFIT"),
        "net_profit_yoy_pct": percent_value(first_number(latest, "PARENTNETPROFIT_YOY", "SJLTZ", "NETPROFIT_YOY")),
        "roe_pct": percent_value(first_number(latest, "ROEJQ", "WEIGHTED_ROE", "ROE")),
        "eps": first_number(latest, "EPSJB", "BASIC_EPS", "EPS"),
    }

    today = date.today()
    forecast = None
    for row in forecast_rows:
        notice_text = str(row.get("NOTICE_DATE") or "")[:10]
        report_text = str(row.get("REPORT_DATE") or "")[:10]
        try:
            notice_date = date.fromisoformat(notice_text)
        except ValueError:
            continue
        if (today - notice_date).days > 400:
            continue
        try:
            forecast_report_date = date.fromisoformat(report_text)
        except ValueError:
            continue
        # A forecast for a period with an already-published actual report is no
        # longer forward-looking, even if the forecast endpoint still returns it.
        if latest_report_date and forecast_report_date <= latest_report_date:
            continue
        forecast = {
            "notice_date": notice_text,
            "report_date": report_text or None,
            "type": str(row.get("PREDICT_FINANCE") or row.get("PREDICT_TYPE") or "公司业绩预告"),
            "amount_lower": first_number(row, "PREDICT_AMT_LOWER"),
            "amount_upper": first_number(row, "PREDICT_AMT_UPPER"),
            "growth_lower_pct": first_number(row, "PREDICT_RATIO_LOWER", "ADD_AMP_LOWER"),
            "growth_upper_pct": first_number(row, "PREDICT_RATIO_UPPER", "ADD_AMP_UPPER"),
            "content": str(row.get("PREDICT_CONTENT") or row.get("CHANGE_REASON_EXPLAIN") or "").strip(),
        }
        break

    result = {
        "available": bool(profile_rows or finance_rows or forecast_rows),
        "name": str(profile.get("ORG_NAME") or profile.get("SECURITY_NAME_ABBR") or fallback_name or ""),
        "industry": str(profile.get("EM2016") or profile.get("INDUSTRYCSRC1") or fallback_industry or "") or None,
        "industry_position": position_sentence or None,
        "industry_position_basis": position_basis,
        "industry_position_source_url": position_source_url,
        "main_business": description[:180] or None,
        "financial": financial if finance_rows else {},
        "forecast": forecast,
        "source_label": "东方财富公司资料、定期报告与已披露业绩预告",
        "source_url": "https://datacenter.eastmoney.com/",
    }
    with _COMPANY_BRIEF_LOCK:
        _COMPANY_BRIEF_CACHE[key] = (now, result)
    return result


def _chart_payload(symbol: str) -> dict:
    """Return chart-ready daily and intraday data using the existing market providers."""
    normalized = normalize_symbol(symbol)
    daily_result = _VOLUME_BASELINE_PROVIDER.fetch_daily(symbol, limit=240)
    secid = f"{1 if normalized.exchange == 'SH' else 0}.{normalized.code}"
    intraday: list[dict] = []
    intraday_error = ""
    intraday_source = None
    for base_url in _INTRADAY_URLS:
        if intraday:
            break
        try:
            url = (
                f"{base_url}?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
                "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1"
            )
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            with open_market_url(request, timeout=8.0) as response:
                raw = json.loads(response.read().decode("utf-8", errors="replace"))
            node = raw.get("data") or {}
            for row in node.get("trends") or []:
                parts = str(row).split(",")
                if len(parts) < 5:
                    continue
                try:
                    intraday.append({
                        "time": parts[0],
                        "price": float(parts[1]),
                        "average": float(parts[2]) if parts[2] not in {"", "-"} else None,
                        "volume": float(parts[3]) if parts[3] not in {"", "-"} else None,
                        "amount": float(parts[4]) if parts[4] not in {"", "-"} else None,
                    })
                except (TypeError, ValueError):
                    continue
            if intraday:
                intraday_source = "eastmoney-intraday"
        except Exception as exc:
            intraday_error = f"{type(exc).__name__}: {exc}"
    if not intraday:
        try:
            url = f"{_TENCENT_INTRADAY_URL}?code={normalized.tencent}"
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            with open_market_url(request, timeout=8.0) as response:
                raw = json.loads(response.read().decode("utf-8", errors="replace"))
            node = ((raw.get("data") or {}).get(normalized.tencent) or {}).get("data") or {}
            rows = node.get("data") or [] if isinstance(node, dict) else node
            for row in rows:
                parts = str(row).replace(" ", ",").split(",")
                if len(parts) < 2:
                    continue
                try:
                    intraday.append({"time": parts[0], "price": float(parts[1]), "average": None, "volume": None, "amount": None})
                except (TypeError, ValueError):
                    continue
            if intraday:
                intraday_source = "tencent-intraday"
        except Exception as exc:
            intraday_error = f"{type(exc).__name__}: {exc}"
    return {
        "symbol": symbol,
        "daily": daily_result.bars,
        "intraday": intraday,
        "status": daily_result.status,
        "warnings": [*daily_result.warnings, *(["分时数据暂不可用，已尝试东方财富和腾讯接口；K线仍可查看"] if intraday_error and not intraday else [])],
        "sources": [item for item in [
            daily_result.provenance.source if daily_result.provenance else None,
            intraday_source,
        ] if item],
    }


def _cached_quote(symbol: str):
    # User-confirmed valuation is date-scoped and never represented as live trading.
    try:
        overrides = json.loads((_PORTFOLIO_STORE.path.parent / "valuation_overrides.json").read_text(encoding="utf-8-sig"))
        reference = overrides.get(normalize_symbol(symbol).code, {})
        now_local = datetime.now(SHANGHAI)
        price = float(reference.get("price", 0))
        if reference.get("valid_date") == now_local.date().isoformat() and price > 0 and isfinite(price):
            quote = Quote(normalize_symbol(symbol).canonical, reference.get("name", "参考估值"), price,
                          float(reference.get("previous_close", price)), status=DataStatus.PARTIAL,
                          provenance=Provenance("user_reference_valuation", "local:valuation_overrides.json", now_local, now_local))
            return ProviderResult(quote, DataStatus.PARTIAL, ["用户确认的非交易参考估值，仅当日有效；次日恢复行情源"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    key = str(symbol).strip().upper()
    now = time.monotonic()
    with _QUOTE_CACHE_LOCK:
        cached = _QUOTE_CACHE.get(key)
        if cached and now - cached[0] < _QUOTE_CACHE_TTL:
            return cached[1]
    result = _QUOTE_ROUTER.fetch_quote(symbol)
    with _QUOTE_CACHE_LOCK:
        _QUOTE_CACHE[key] = (time.monotonic(), result)
    return result


def _average_daily_volume(symbol: str, session: date) -> float | None:
    normalized = normalize_symbol(symbol).canonical
    key = (normalized, session)
    now = time.monotonic()
    with _VOLUME_BASELINE_LOCK:
        cached = _VOLUME_BASELINE_CACHE.get(key)
        if cached and now - cached[0] < _VOLUME_BASELINE_TTL:
            return cached[1]
    average: float | None = None
    try:
        result = _VOLUME_BASELINE_PROVIDER.fetch_daily(normalized, limit=30)
        average = average_prior_volume(result.bars, session)
    except Exception:
        average = None
    with _VOLUME_BASELINE_LOCK:
        _VOLUME_BASELINE_CACHE[key] = (time.monotonic(), average)
    return average


def _volume_ratio(quote: Quote) -> float | None:
    as_of = quote.provenance.as_of if quote.provenance else None
    progress = trading_progress(as_of)
    if progress is None:
        return None
    # Provider ratio and timestamp belong to the same snapshot. Retain it on
    # weekends/holidays rather than extrapolating old volume using today's time.
    if quote.volume_ratio is not None and isfinite(quote.volume_ratio) and quote.volume_ratio > 0:
        return quote.volume_ratio
    if quote.volume is None or not isfinite(quote.volume) or quote.volume <= 0:
        return None
    session = as_of.astimezone(SHANGHAI).date() if as_of.tzinfo else as_of.date()
    average = _average_daily_volume(quote.symbol, session)
    if average is None or average <= 0:
        return None
    return projected_volume_ratio(quote.volume, average, progress)


def _volume_price_relation(price: float | None, previous_close: float | None, ratio: float | None) -> str:
    if price is None or previous_close is None or previous_close <= 0 or ratio is None:
        return "—"
    change = price / previous_close - 1.0
    if change >= 0.002:
        return "放量上涨" if ratio >= 1.5 else "缩量上涨" if ratio <= 0.8 else "量平价升"
    if change <= -0.002:
        return "放量下跌" if ratio >= 1.5 else "缩量下跌" if ratio <= 0.8 else "量平价跌"
    return "量价平稳"


def _holding_daily_profit(symbol: str, quantity: int, quote: Quote, state: dict) -> dict:
    """Today's P&L for one holding.

    Shares carried over from yesterday move from the previous close; shares
    bought today move from their own fill price.  Using the previous close for
    a same-day buy would count yesterday-to-fill movement the account never had.
    """
    empty = {"daily_profit": None, "daily_profit_pct": None, "bought_today_quantity": None}
    previous_close = quote.previous_close
    if not previous_close or previous_close <= 0 or quantity <= 0:
        return {**empty, "bought_today_quantity": 0} if quantity <= 0 else empty
    as_of = quote.provenance.as_of if quote.provenance else None
    session = (as_of.astimezone(SHANGHAI) if as_of and as_of.tzinfo else (as_of or datetime.now(SHANGHAI))).date().isoformat()
    buys = _PORTFOLIO_STORE.buys_on(symbol, session, state=state)
    if buys is None:
        return empty
    remaining = quantity
    profit = 0.0
    basis = 0.0
    bought = 0
    # Shares bought today cannot be sold today (T+1), so they are all still held;
    # cap at the current quantity in case the history and holdings disagree.
    for buy_quantity, buy_price in buys:
        used = min(buy_quantity, remaining)
        if used <= 0:
            break
        profit += (quote.price - buy_price) * used
        basis += buy_price * used
        bought += used
        remaining -= used
    profit += (quote.price - previous_close) * remaining
    basis += previous_close * remaining
    return {
        "daily_profit": profit,
        "daily_profit_pct": profit / basis if basis > 0 else None,
        "bought_today_quantity": bought,
    }


def _live_portfolio(state: dict) -> dict:
    holdings = state.get("holdings", [])

    def fetch_one(item: dict) -> dict:
        symbol = str(item.get("symbol", "")).strip()
        quantity = int(item.get("quantity") or 0)
        raw_cost = item.get("average_cost")
        average_cost = None if raw_cost in {None, ""} else float(raw_cost)
        result = _cached_quote(symbol)
        quote = result.quote
        base = {
            "symbol": symbol,
            "name": "",
            "quantity": quantity,
            "sellable_quantity": _PORTFOLIO_STORE.sellable_quantity(symbol, state=state),
            "average_cost": average_cost,
            "price": None,
            "previous_close": None,
            "daily_amount": None,
            "volume": None,
            "turnover_pct": None,
            "volume_ratio": None,
            "volume_relation": "—",
            "market_value": 0.0,
            "daily_profit": None,
            "daily_profit_pct": None,
            "profit": None,
            "weight": 0.0,
            "status": result.status.value,
            "error": "; ".join(result.errors or result.warnings),
            "as_of": (
                quote.provenance.as_of.isoformat()
                if quote and quote.provenance and quote.provenance.as_of else None
            ),
        }
        if quote is None or quote.price <= 0:
            base["status"] = "unavailable"
            base["error"] = "; ".join(filter(None, [base["error"], "无有效正数报价，不能按零元估值"]))
            return base
        market_value = quote.price * quantity
        volume_ratio = _volume_ratio(quote)
        base.update({
            "name": quote.name,
            "price": quote.price,
            "previous_close": quote.previous_close,
            "daily_amount": quote.amount,
            "volume": quote.volume,
            "turnover_pct": quote.turnover_pct,
            "volume_ratio": volume_ratio,
            "volume_relation": _volume_price_relation(quote.price, quote.previous_close, volume_ratio),
            "market_value": market_value,
            **_holding_daily_profit(symbol, quantity, quote, state),
            "profit": ((quote.price - average_cost) * quantity
                       if average_cost is not None else None),
            "status": result.status.value,
        })
        return base

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(holdings)))) as executor:
        rows = list(executor.map(fetch_one, holdings))
    cash = max(0.0, float(state.get("cash") or 0.0))
    positions_value = sum(row["market_value"] for row in rows)
    total_assets = cash + positions_value
    snapshot_dates = {
        datetime.fromisoformat(str(row["as_of"])).astimezone(SHANGHAI).date().isoformat()
        for row in rows
        if row.get("as_of")
    }


    quotes_complete = all(row.get("price") is not None for row in rows)
    quotes_fresh = all(row.get("status") == "fresh" for row in rows)
    if not rows:
        snapshot_date = datetime.now(SHANGHAI).date().isoformat()
        _PORTFOLIO_STORE.record_equity_snapshot(snapshot_date, total_assets)
    elif quotes_complete and quotes_fresh and len(snapshot_dates) == 1 and all(row.get("as_of") for row in rows):
        _PORTFOLIO_STORE.record_equity_snapshot(next(iter(snapshot_dates)), total_assets)
    stored_state = _PORTFOLIO_STORE.load() or state
    equity_history = stored_state.get("equity_history", [])
    day_open = state.get("day_open") if isinstance(state.get("day_open"), dict) else None
    opening_cash = cash
    opening_positions_value = 0.0
    if day_open:
        opening_cash = max(0.0, float(day_open.get("cash") or 0.0))
        current_rows = {str(row["symbol"]).upper(): row for row in rows}
        for opening in day_open.get("holdings", []):
            symbol = str(opening.get("symbol", "")).strip()
            quantity = int(opening.get("quantity") or 0)
            row = current_rows.get(symbol.upper())
            previous_close = row.get("previous_close") if row else None
            if previous_close is None and symbol:
                try:
                    previous_close = _cached_quote(symbol).quote.previous_close
                except Exception:
                    previous_close = None
            if previous_close is not None:
                opening_positions_value += float(previous_close) * quantity
    else:
        opening_positions_value = sum(
            (row["previous_close"] or 0.0) * row["quantity"]
            for row in rows
            if row["previous_close"] is not None
        )
        opening_cash = cash
    previous_total_assets = opening_cash + opening_positions_value
    baseline_reliable = not day_open or day_open.get("baseline_reliable", True)
    account_daily_profit = total_assets - previous_total_assets if baseline_reliable else None
    for row in rows:
        row["weight"] = row["market_value"] / total_assets if total_assets else 0.0
    return {
        "version": state.get("version"),
        "updated_at": state.get("updated_at"),
        "transactions": state.get("transactions", []),
        "cash": cash,
        "positions_value": positions_value,
        "total_assets": total_assets,
        # Account-level P&L is based on the day-open snapshot, so today's
        # purchases/sales change cash and holdings without being double-counted
        # as price profit.
        "daily_profit": account_daily_profit,
        "daily_profit_pct": (
            account_daily_profit / previous_total_assets
            if baseline_reliable and previous_total_assets > 0 else None
        ),
        "day_open_date": day_open.get("date") if day_open else None,
        "largest_position_pct": max((row["weight"] for row in rows), default=0.0),
        "holdings": rows,
        "equity_history": equity_history,
        "performance": summarize_equity_history(equity_history),
    }


def _record_auto_prediction(analysis: dict, request_payload: dict) -> dict:
    """Store the exact visible live judgment and its available point-in-time inputs."""
    decision = analysis.get("decision") if isinstance(analysis.get("decision"), dict) else {}
    quote = analysis.get("quote") if isinstance(analysis.get("quote"), dict) else {}
    provenance = quote.get("provenance") if isinstance(quote.get("provenance"), dict) else {}
    as_of = decision.get("as_of") or datetime.now().astimezone().isoformat()
    information_cutoff = provenance.get("as_of") or as_of
    input_snapshot = analysis.get("audit_snapshot") if isinstance(analysis.get("audit_snapshot"), dict) else {
        "features": analysis.get("features", {}), "quote": quote,
        "source_urls": analysis.get("sources", []),
        "snapshot_completeness": "远端服务未提供可重现的原始日线快照",
    }
    input_hash = __import__("hashlib").sha256(json.dumps(
        input_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()
    stored_account = _PORTFOLIO_STORE.load() or {}
    symbol_key = str(analysis.get("symbol", "")).upper().replace("SH.", "").replace("SZ.", "").replace("BJ.", "")
    held = next((item for item in stored_account.get("holdings", [])
                 if str(item.get("symbol", "")).upper().replace("SH.", "").replace("SZ.", "").replace("BJ.", "") == symbol_key), None)
    risk_context = request_payload.get("risk") if isinstance(request_payload.get("risk"), dict) else {}
    holding_qty = int(risk_context.get("holding_quantity") if risk_context.get("holding_quantity") is not None else ((held or {}).get("quantity") or 0))
    action = str(decision.get("final_action") or decision.get("raw_action") or "hold")
    has_account = bool(risk_context.get("position_data_available", held is not None))
    sellable_raw = risk_context.get("sellable_quantity", (held or {}).get("sellable_quantity"))
    sellable = int(sellable_raw) if isinstance(sellable_raw, (int, float)) and sellable_raw >= 0 else None
    position_advice = _position_aware_advice(action, holding_qty, sellable, has_account)
    record = {
        "symbol": analysis.get("symbol") or request_payload.get("symbol"), "as_of": as_of,
        "information_cutoff": information_cutoff,
        "model_id": "advisor_rules_ensemble",
        "model_type": "规则与因子模型混合；未调用大模型",
        "model_versions": MODEL_CATALOG, "prompt_version": None,
        "configuration": {"risk_context": request_payload.get("risk") or {}},
        "input_snapshot": input_snapshot, "input_sha256": input_hash,
        "data_sources": analysis.get("sources") or input_snapshot.get("source_urls") or [],
        "data_quality": {
            "status": analysis.get("status") or input_snapshot.get("source_status"),
            "warnings": analysis.get("warnings") or input_snapshot.get("data_warnings") or [],
            "errors": analysis.get("diagnostics") or input_snapshot.get("data_errors") or [],
            "quote_updated_at": information_cutoff,
        },
        "raw_output": decision, "score_type": "rule_conformance_score",
        "rule_score": decision.get("score"),
        "confidence_type": decision.get("confidence_type", "rule_conformance_score"),
        "confidence_score": decision.get("model_confidence"),
        "market_prediction": analysis.get("market_prediction") or independent_market_prediction(decision),
        "submodel_scores": {
            str(item.get("strategy")): item.get("score")
            for item in decision.get("strategy_signals", [])
            if isinstance(item, dict) and item.get("strategy")
        },
        "raw_probability": None, "calibrated_probability": None,
        "target": dict(TARGET_DEFINITION),
        "recommendation": {
            "model_action": decision.get("raw_action"), "account_action": action,
            "position_aware_action": position_advice,
            "conditions": decision.get("reasons", []),
            "invalidation_conditions": [item.get("invalidation", []) for item in decision.get("strategy_signals", []) if isinstance(item, dict)],
        },
        "account_snapshot": {
            "cash": stored_account.get("cash"), "holdings": stored_account.get("holdings", []),
            "matched_holding": held, "sellable_quantity": None,
            "sellable_quantity_status": "unknown_without_lot_level_T_plus_1_data",
            "risk_check": request_payload.get("risk") or {},
        },
        "execution_status": "not_recorded", "execution_records": [], "settlement_status": "pending",
    }
    stored, created = _PREDICTION_LEDGER.create_prediction(record, mode=request_payload.get("record_mode", "formal"))
    return {"prediction_id": stored["prediction_id"], "created": created, "mode": stored["mode"], "target": stored["target"]}


def _position_aware_advice(action: str, holding_qty: int, sellable_qty: int | None, has_account: bool) -> dict:
    if action in {"sell", "reduce"}:
        if has_account and holding_qty <= 0:
            return {"action": "no_buy", "suggested_sell_quantity": 0, "reason": "无持仓时看空观点只表示不买入，不产生卖空交易"}
        if not has_account:
            return {"action": "position_check_required", "suggested_sell_quantity": None, "reason": "尚未取得账户持仓，不能建议卖出数量；先检查持仓"}
        if sellable_qty is None:
            return {"action": "sellable_check_required", "holding_quantity": holding_qty, "suggested_sell_quantity": None, "reason": "T+1可卖股数无法核实，当前不给具体卖出数量"}
        sellable_qty = min(max(0, sellable_qty), max(0, holding_qty))
        if sellable_qty == 0:
            return {"action": "wait_t_plus_1", "holding_quantity": holding_qty, "sellable_quantity": 0, "suggested_sell_quantity": 0, "reason": "当前无可卖股份，遵守T+1"}
        if action == "sell":
            quantity = sellable_qty
        else:
            quantity = (sellable_qty // 200) * 100
            if quantity == 0 and sellable_qty >= 100:
                quantity = 100
        return {"action": "sell" if action == "sell" else "reduce", "holding_quantity": holding_qty, "sellable_quantity": sellable_qty, "suggested_sell_quantity": quantity, "reason": "数量受当前可卖持仓限制；实际下单前仍需核对券商可卖数量及涨跌停/停牌状态"}
    if action == "buy":
        return {"action": "buy", "suggested_buy_quantity": None, "reason": "未设置单笔风险预算和止损距离，不计算买入股数"}
    return {"action": "hold", "suggested_buy_quantity": 0, "reason": "观察/持有，不新增仓位"}


_SETTLEMENT_FETCH_WORKERS = max(1, int(os.environ.get("XIAO_A_SETTLE_WORKERS", "6")))


def _settle_forward_predictions() -> dict:
    """Settle only completed future windows from qfq daily bars after cutoff."""
    records = _PREDICTION_LEDGER.records()
    pending = [item for item in records if not item.get("settlement") and item.get("target", {}).get("target_id") == TARGET_DEFINITION["target_id"]]
    today = datetime.now(SHANGHAI).date()
    benchmark_result = None
    stock_results = {}
    settled: list[str] = []
    pending_reasons: list[dict] = []
    # Prefetch each distinct symbol's history concurrently. Fetching ~200+
    # symbols one after another took minutes and outlived client timeouts;
    # ledger writes below stay strictly sequential.
    # A 5-session window strictly between the cutoff day and today needs at
    # least 6 calendar days, so younger predictions cannot settle yet and their
    # history is not downloaded (they still get the same "not complete" note).
    def _maybe_due(item: dict) -> bool:
        try:
            raw_cutoff = datetime.fromisoformat(str(item["information_cutoff"]).replace("Z", "+00:00"))
            if raw_cutoff.tzinfo is None:
                raw_cutoff = raw_cutoff.replace(tzinfo=SHANGHAI)
            return (today - raw_cutoff.astimezone(SHANGHAI).date()).days >= 6
        except Exception:
            return True  # let the main loop report the bad record
    symbols = sorted({str(item.get("symbol")) for item in pending if item.get("symbol") and _maybe_due(item)})
    if symbols:
        def _fetch(symbol: str):
            try:
                return symbol, _BACKTEST_KLINE_PROVIDER.fetch_daily(symbol, limit=1000, adjustment="qfq")
            except Exception as exc:  # router normally never raises; keep settlement going
                return symbol, KlineResult([], DataStatus.UNAVAILABLE, errors=[f"{type(exc).__name__}: {exc}"])
        with ThreadPoolExecutor(max_workers=_SETTLEMENT_FETCH_WORKERS) as pool:
            for symbol, result in pool.map(_fetch, [*symbols, "SH.000300"]):
                if symbol == "SH.000300" and symbol not in symbols:
                    benchmark_result = result
                else:
                    stock_results[symbol] = result
                    if symbol == "SH.000300":
                        benchmark_result = result
    for record in pending:
        prediction_id = record["prediction_id"]
        # Settlement is an explicit user action. A failed fetch must not block
        # another attempt after connectivity/data is repaired on the same day.
        try:
            cutoff = datetime.fromisoformat(str(record["information_cutoff"]).replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=SHANGHAI)
            cutoff_day = cutoff.astimezone(SHANGHAI).date()
            symbol = str(record["symbol"])
            if int(record["target"].get("horizon_sessions") or 0) == 5 and (today - cutoff_day).days < 6:
                raise RuntimeError("预测期限尚未完整，尚无5个已结束的后续交易日")
            if symbol not in stock_results:
                stock_results[symbol] = _BACKTEST_KLINE_PROVIDER.fetch_daily(symbol, limit=1000, adjustment="qfq")
            stock_result = stock_results[symbol]
            if stock_result.status is not DataStatus.FRESH:
                details = "; ".join(stock_result.errors + stock_result.warnings)
                raise RuntimeError(f"标的历史数据非最新或质量状态不完整：{details or stock_result.status.value}")
            horizon = int(record["target"].get("horizon_sessions") or 0)
            window = select_future_bar_window(
                stock_result.bars, information_cutoff_day=cutoff_day,
                horizon_sessions=horizon, before_day=today,
            ) if horizon == 5 else []
            if not window:
                raise RuntimeError("预测期限尚未完整，尚无5个已结束的后续交易日")
            if not window or window[0].open <= 0 or window[-1].close <= 0:
                raise RuntimeError("目标价格缺失或无效，不能结算")
            if benchmark_result is None:
                benchmark_result = _BACKTEST_KLINE_PROVIDER.fetch_daily("SH.000300", limit=1000, adjustment="qfq")
            if benchmark_result.status is not DataStatus.FRESH:
                details = "; ".join(benchmark_result.errors + benchmark_result.warnings)
                raise RuntimeError(f"沪深300基准日线非最新或质量状态不完整：{details or benchmark_result.status.value}")
            by_day = {bar.trading_date: bar for bar in benchmark_result.bars if bar.trading_date < today}
            benchmark_window = [by_day.get(bar.trading_date) for bar in window]
            if any(bar is None for bar in benchmark_window):
                raise RuntimeError("同期沪深300基准数据缺失，暂不结算")
            target_return = window[-1].close / window[0].open - 1.0
            benchmark_return = benchmark_window[-1].close / benchmark_window[0].open - 1.0
            event_occurred = target_return > 0
            model_direction = float(record.get("rule_score") or 0.0)
            event = _PREDICTION_LEDGER.settle(prediction_id, {
                "status": "settled", "resolved_at": window[-1].trading_date.isoformat(),
                "event_occurred": event_occurred, "target_return": target_return,
                "benchmark_return": benchmark_return,
                "benchmark_event_occurred": benchmark_return > 0,
                "model_direction_hit": (model_direction > 0) == event_occurred if abs(model_direction) > 1e-12 else None,
                "entry_date": window[0].trading_date.isoformat(), "entry_open": window[0].open,
                "exit_date": window[-1].trading_date.isoformat(), "exit_close": window[-1].close,
                "data_source": stock_result.provenance.source if stock_result.provenance else None,
                "benchmark_source": benchmark_result.provenance.source if benchmark_result.provenance else None,
                "price_basis": "指数为原始点位，个股为前复权日线；市场事件标签，不是可执行策略收益",
                "benchmark_price_basis": "沪深300原始指数点位，无股票除权复权",
                "settlement_version": "forward-settlement-v2-index-day",
                "execution_simulation": "not_applied",
            })
            settled.append(event["prediction_id"])
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            _PREDICTION_LEDGER.append("settlement_attempted", {
                "prediction_id": prediction_id, "attempt_at": datetime.now(SHANGHAI).isoformat(), "reason": reason,
            })
            pending_reasons.append({"prediction_id": prediction_id, "symbol": record.get("symbol"), "reason": reason})
    return {"settled_count": len(settled), "settled_ids": settled, "pending_count": len(pending) - len(settled), "pending_reasons": pending_reasons}


def _annotate_offline_fallback(cached: dict, live_payload, live_status: int) -> dict:
    """Make a cached answer impossible to mistake for a fresh one.

    The cached copy keeps its original quote timestamps, so a caller that only
    reads the price would otherwise report an old price as current.
    """
    live_error = None
    if isinstance(live_payload, dict):
        live_error = live_payload.get("error") or (
            "实时数据不可用" if live_payload.get("status") == "unavailable" else None)
    cached_local = None
    try:
        cached_local = datetime.fromisoformat(str(cached.get("cached_at"))).astimezone(SHANGHAI)
    except (TypeError, ValueError):
        pass
    when = cached_local.strftime("%m-%d %H:%M") if cached_local else "更早"
    notice = f"实时分析失败，以下是 {when}（北京时间）的缓存结果，价格和结论都不是最新的"
    cached["status"] = "stale"
    cached["live_error"] = live_error or f"HTTP {live_status}"
    cached["live_status_code"] = live_status
    cached["cached_at_local"] = cached_local.isoformat() if cached_local else None
    cached["stale_notice"] = notice
    warnings = [item for item in cached.get("warnings", []) if item != notice]
    cached["warnings"] = [notice, *warnings]
    return cached


class DashboardHandler(BaseHTTPRequestHandler):
    def _live_only(self) -> bool:
        """Callers that must never get cached answers send X-Xiao-A-Live-Only: 1."""
        try:
            return str(self.headers.get("X-Xiao-A-Live-Only", "")).strip().lower() in {"1", "true", "yes"}
        except AttributeError:
            return False

    def _try_remote(self, method: str, path: str, payload=None) -> bool:
        self._cache_request = (method, path, payload) if _HYBRID_GATEWAY.supported(method, urlparse(path).path) else None
        if not self._cache_request or not _HYBRID_GATEWAY.cloud_url:
            return False
        try:
            remote_payload = _HYBRID_GATEWAY.remote(method, path, payload)
            # A cloud/cache response may predate fields added to the local
            # company profile. Enrich stock quote responses locally so the
            # industry-position line is not silently lost on older cache data.
            parsed = urlparse(path)
            if method == "POST" and parsed.path == "/api/auto-evaluate" and isinstance(remote_payload, dict):
                try:
                    decision = remote_payload.get("decision") or {}
                    remote_payload["market_prediction"] = independent_market_prediction(decision)
                    risk_context = (payload or {}).get("risk") or {}
                    remote_payload["position_advice"] = _position_aware_advice(
                        str(decision.get("final_action") or "hold"),
                        int(risk_context.get("holding_quantity") or 0),
                        risk_context.get("sellable_quantity"),
                        bool(risk_context.get("position_data_available", False)),
                    )
                    remote_payload["prediction_audit"] = _record_auto_prediction(remote_payload, payload or {})
                except (OSError, ValueError, TypeError, KeyError):
                    remote_payload.setdefault("warnings", []).append("预测台账写入失败；本次分析未进入可审计评估样本")
            if method == "GET" and parsed.path == "/api/quote" and isinstance(remote_payload, dict):
                quote = remote_payload.get("quote")
                if isinstance(quote, dict) and quote.get("symbol") and not is_market_index(quote["symbol"]):
                    remote_payload["company_brief"] = _company_brief(
                        str(quote["symbol"]),
                        fallback_name=str(quote.get("name") or ""),
                        fallback_industry=remote_payload.get("industry"),
                    )
            self._send_json(remote_payload)
            return True
        except Exception:
            # The existing local engine remains available when cloud access fails.
            return False

    def _send_json(self, payload, status: int = 200) -> None:
        payload = jsonable(payload)
        cache_request = getattr(self, "_cache_request", None)
        if cache_request:
            if status == 200 and not payload.get("error") and payload.get("status") != "unavailable":
                try:
                    _HYBRID_GATEWAY.save(*cache_request, payload)
                except OSError:
                    pass  # A read-only/full disk must not hide a successful live result.
            elif not self._live_only():
                cached = _HYBRID_GATEWAY.fallback(*cache_request)
                if cached is not None:
                    payload, status = _annotate_offline_fallback(cached, payload, status), 200
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if isinstance(payload, dict) and payload.get("offline"):
            # Machine clients can detect a cached answer without parsing the body.
            self.send_header("X-Xiao-A-Offline", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self._try_remote("GET", self.path):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/assets/interact.min.js":
            body = files("xiao_a.web").joinpath("interact.min.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/runtime":
            self._send_json({"api_version": 1, "market_data_revision": "market-20261002-v4", "mode": "hybrid" if _HYBRID_GATEWAY.cloud_url else "local", "cloud_configured": bool(_HYBRID_GATEWAY.cloud_url), "account_storage": "local", "account_sync": "not_configured"})
            return
        if parsed.path == "/api/prediction-audit":
            records = _PREDICTION_LEDGER.records()
            response = _PREDICTION_LEDGER.report()
            response["calibration"] = (
                {"status": "哈希链异常，时间校准已暂停"}
                if response.get("scoring_paused")
                else chronological_calibration([item for item in records if item.get("mode") == "formal"])
            )
            response["records"] = list(reversed(records[-300:]))
            response["failed_attempts"] = [event for event in reversed(_PREDICTION_LEDGER.events()) if event.get("event_type") == "analysis_attempt_failed"][:100]
            self._send_json(response)
            return
        if parsed.path == "/":
            body = files("xiao_a.web").joinpath("index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/portfolio-state":
            self._send_json({"state": _PORTFOLIO_STORE.load()})
            return
        if parsed.path == "/api/portfolio-live":
            state = _PORTFOLIO_STORE.ensure_day_open()
            self._send_json({"data": _live_portfolio(state) if state else None})
            return
        if parsed.path == "/api/search":
            keyword = parse_qs(parsed.query).get("q", [""])[0].strip()
            if not keyword:
                self._send_json({"matches": []})
                return
            if len(keyword) > 32:
                self._send_json({"error": "搜索内容过长"}, 400)
                return
            try:
                matches = _SEARCH_PROVIDER.search(keyword)
                self._send_json({"matches": [item.as_dict() for item in matches]})
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        if parsed.path == "/api/quote":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol:
                self._send_json({"error": "请输入股票代码"}, 400)
                return
            try:
                result = _cached_quote(symbol)
                extras = {}
                quote_payload = result.quote
                if result.quote:
                    ratio = _volume_ratio(result.quote)
                    extras = {
                        "volume_ratio": ratio,
                        "volume_ratio_source": (result.quote.provenance.source
                                                if ratio is not None and result.quote.volume_ratio is not None
                                                else "calculated_5_sessions" if ratio is not None else None),
                        "volume_ratio_as_of": result.quote.provenance.as_of if result.quote.provenance else None,
                        "volume_relation": _volume_price_relation(result.quote.price, result.quote.previous_close, ratio),
                        "turnover_pct": result.quote.turnover_pct,
                    }
                    if is_market_index(result.quote.symbol):
                        # An index is not an issuer: company market-cap, share
                        # capital, price limits and stock turnover are invalid.
                        # Keep only source-supplied index valuation when positive.
                        quote_payload = replace(
                            result.quote,
                            turnover_pct=None,
                            pb=None,
                            limit_up=None,
                            limit_down=None,
                        )
                        extras.update({
                            "instrument_type": "index",
                            "turnover_pct": None,
                            "pe_ttm": result.quote.pe_ttm if result.quote.pe_ttm and result.quote.pe_ttm > 0 else None,
                            "pb": None,
                            "market_cap": result.quote.market_cap,
                            "float_market_cap": result.quote.float_market_cap,
                            "market_cap_source": "腾讯指数行情",
                        })
                    else:
                        extras.update(_fundamentals(result.quote.symbol))
                        # Tencent supplies these two valuation fields even when the
                        # Eastmoney fundamentals endpoint is temporarily unavailable.
                        if extras.get("pe_ttm") is None and result.quote.pe_ttm is not None:
                            extras["pe_ttm"] = result.quote.pe_ttm
                        if extras.get("pb") is None and result.quote.pb is not None:
                            extras["pb"] = result.quote.pb
                        if extras.get("market_cap") is None and extras.get("total_shares"):
                            # Eastmoney f84 is total share capital in shares.
                            extras["market_cap"] = result.quote.price * float(extras["total_shares"])
                        if extras.get("float_market_cap") is None and extras.get("float_shares"):
                            extras["float_market_cap"] = result.quote.price * float(extras["float_shares"])
                        if extras.get("float_market_cap") is None and result.quote.volume and result.quote.turnover_pct and result.quote.turnover_pct > 0:
                            # Quote volume is normalized to lots; turnover derives an
                            # estimated circulating market value, not total market value.
                            extras["float_market_cap"] = result.quote.price * result.quote.volume * 10000 / result.quote.turnover_pct
                        if extras.get("total_shares") is None:
                            public_shares = _public_share_capital(result.quote.symbol)
                            if public_shares:
                                extras["total_shares"] = public_shares
                                extras["market_cap"] = result.quote.price * public_shares
                        # PE must use total market value. Circulating market value
                        # is not a valid substitute, so leave calculated PE blank
                        # until total shares/total market value is available.
                        computed = _financial_metrics(result.quote.symbol, extras.get("market_cap"))
                        if computed:
                            extras.update(computed)
                        if extras.get("market_cap") is None and extras.get("total_shares"):
                            extras["market_cap"] = result.quote.price * float(extras["total_shares"])
                        if extras.get("market_cap"):
                            computed = _financial_metrics(result.quote.symbol, extras.get("market_cap"))
                            for key, value in computed.items():
                                if extras.get(key) is None:
                                    extras[key] = value
                        extras["company_brief"] = _company_brief(
                            result.quote.symbol,
                            fallback_name=result.quote.name,
                            fallback_industry=extras.get("industry"),
                        )
                self._send_json({"quote": quote_payload, **extras, "status": result.status, "warnings": result.warnings, "errors": result.errors}, 200 if result.quote else 502)
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return
        if parsed.path == "/api/chart":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol:
                self._send_json({"error": "请输入股票代码"}, 400)
                return
            try:
                self._send_json(_chart_payload(symbol))
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return
        self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802
        if self.path != "/api/portfolio-state":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("请求内容为空或过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            holdings = payload.get("holdings", [])
            if not isinstance(holdings, list):
                raise ValueError("持仓数据格式不正确")
            state = _PORTFOLIO_STORE.save(holdings, float(payload.get("cash") or 0.0))
            self._send_json({"state": state})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def do_POST(self) -> None:  # noqa: N802
        self._cache_request = None
        if self.path not in {"/api/evaluate", "/api/auto-evaluate", "/api/portfolio", "/api/trade", "/api/screener", "/api/backtest", "/api/prediction-audit"}:
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("请求内容为空或过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path != "/api/backtest" and self._try_remote("POST", self.path, payload):
                return
            if self.path == "/api/evaluate":
                self._send_json(evaluate_payload(payload))
            elif self.path == "/api/auto-evaluate":
                symbol = str(payload.get("symbol", "")).strip()
                if not symbol:
                    raise ValueError("请输入股票代码")
                if is_market_index(symbol):
                    supplemental = _AUTO_ANALYZER.supplemental_provider.fetch_market_limit_up_pool(symbol)
                    self._send_json({"symbol": symbol, "scope": "market_context", "supplemental": jsonable(supplemental.data)})
                    return
                allowed = {item.name for item in fields(RiskContext)} - {"data_status"}
                risk_raw = payload.get("risk", {})
                risk = RiskContext(**{key: value for key, value in risk_raw.items() if key in allowed})
                try:
                    analysis = jsonable(_AUTO_ANALYZER.analyze(symbol, risk))
                except Exception as analysis_error:
                    _PREDICTION_LEDGER.append("analysis_attempt_failed", {
                        "symbol": symbol, "requested_at": datetime.now(SHANGHAI).isoformat(),
                        "information_cutoff": None, "target": dict(TARGET_DEFINITION),
                        "risk_context": risk_raw,
                        "reason": f"{type(analysis_error).__name__}: {analysis_error}",
                        "status": "no_prediction_no_score",
                    })
                    raise
                analysis["position_advice"] = _position_aware_advice(
                    str(analysis.get("decision", {}).get("final_action") or "hold"),
                    int(risk_raw.get("holding_quantity") or 0),
                    risk_raw.get("sellable_quantity"),
                    bool(risk_raw.get("position_data_available", False)),
                )
                try:
                    analysis["prediction_audit"] = _record_auto_prediction(analysis, payload)
                except Exception as audit_error:
                    analysis["prediction_audit"] = {"created": False, "status": "not_recorded", "reason": f"{type(audit_error).__name__}: {audit_error}"}
                    analysis.setdefault("warnings", []).append("本次分析未能写入预测台账，不会纳入后续评分")
                self._send_json(analysis)
            elif self.path == "/api/prediction-audit":
                operation = payload.get("operation")
                if operation == "settle_pending":
                    result = _settle_forward_predictions()
                    _PREDICTION_LEDGER.check_review_reminders()
                    self._send_json({"settlement": result, "report": _PREDICTION_LEDGER.report()})
                elif operation == "check_review":
                    self._send_json({"review_monitor": _PREDICTION_LEDGER.check_review_reminders()})
                elif operation == "ack_review":
                    event = _PREDICTION_LEDGER.acknowledge_review(
                        str(payload.get("review_id", "")),
                        reviewer=str(payload.get("reviewer", "local_user")),
                        reason=str(payload.get("reason", "用户在桌面端确认已知晓；尚未进行模型修改。")),
                    )
                    self._send_json({"acknowledgement": event, "review_monitor": _PREDICTION_LEDGER.review_monitor()})
                elif operation == "correct":
                    event = _PREDICTION_LEDGER.correct(
                        str(payload.get("prediction_id", "")),
                        payload.get("replacement") if isinstance(payload.get("replacement"), dict) else {},
                        str(payload.get("reason", "")),
                    )
                    self._send_json({"event": event})
                elif operation == "record_execution":
                    prediction_id = str(payload.get("prediction_id", ""))
                    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
                    symbol = str(execution.get("symbol", "")).upper()
                    side = str(execution.get("side", "")).lower()
                    quantity = int(execution.get("quantity") or 0)
                    price = float(execution.get("price") or 0)
                    if side not in {"buy", "sell"} or quantity <= 0 or price <= 0:
                        raise ValueError("成交记录须包含买入/卖出方向、正股数和正价格")
                    if side == "sell":
                        state = _PORTFOLIO_STORE.load() or {}
                        holding = next((item for item in state.get("holdings", []) if str(item.get("symbol", "")).upper() == symbol), None)
                        if not holding or quantity > int(holding.get("quantity") or 0):
                            raise ValueError("卖出股数超过当前持仓；记录要求来自已入账的实际多头持仓")
                        tx = next((item for item in reversed(state.get("transactions", []))
                                   if str(item.get("symbol", "")).upper() == symbol
                                   and str(item.get("side", "")).lower() == side
                                   and int(item.get("quantity") or 0) == quantity
                                   and abs(float(item.get("price") or 0) - price) < 1e-8), None)
                        if not tx:
                            raise ValueError("无法在账户成交账本中核实该卖出记录")
                        execution["transaction"] = tx
                    elif side == "buy":
                        state = _PORTFOLIO_STORE.load() or {}
                        tx = next((item for item in reversed(state.get("transactions", []))
                                   if str(item.get("symbol", "")).upper() == symbol
                                   and str(item.get("side", "")).lower() == side
                                   and int(item.get("quantity") or 0) == quantity
                                   and abs(float(item.get("price") or 0) - price) < 1e-8), None)
                        if not tx:
                            raise ValueError("无法在账户成交账本中核实该买入记录")
                        execution["transaction"] = tx
                    self._send_json({"event": _PREDICTION_LEDGER.record_execution(prediction_id, execution)})
                elif operation == "propose_candidate":
                    event = _PREDICTION_LEDGER.propose_candidate(
                        version=str(payload.get("version", "")),
                        rationale=str(payload.get("rationale", "")),
                        allowed_change=payload.get("allowed_change") if isinstance(payload.get("allowed_change"), dict) else {},
                        evaluation_plan=payload.get("evaluation_plan") if isinstance(payload.get("evaluation_plan"), dict) else {},
                    )
                    self._send_json({"candidate": event})
                elif operation == "review_candidate":
                    event = _PREDICTION_LEDGER.review_candidate(
                        str(payload.get("candidate_id", "")),
                        decision=str(payload.get("decision", "")),
                        reviewer=str(payload.get("reviewer", "")),
                        rationale=str(payload.get("rationale", "")),
                        time_test_status=payload.get("time_test_status"),
                        shadow_status=payload.get("shadow_status"),
                    )
                    self._send_json({"review": event})
                else:
                    raise ValueError("操作类型不受支持")
            elif self.path == "/api/screener":
                self._send_json(run_market_screener(
                    _AUTO_ANALYZER,
                    minimum_amount=float(payload.get("minimum_amount") or 50_000_000),
                    result_limit=int(payload.get("result_limit") or 20),
                ))
            elif self.path == "/api/backtest":
                normalized = normalize_symbol(str(payload.get("symbol", "")).strip())
                if is_market_index(normalized.canonical):
                    raise ValueError("回测仅支持个股，请输入A股股票代码")
                initial_cash = float(payload.get("initial_cash", 100_000))
                history_limit = int(payload.get("history_limit", 500))
                fast_period = int(payload.get("fast_period", 5))
                slow_period = int(payload.get("slow_period", 20))
                target_pct = float(payload.get("target_position_pct", 0.95))
                if not isfinite(initial_cash) or initial_cash <= 0 or initial_cash > 1_000_000_000:
                    raise ValueError("初始资金须大于0且不超过10亿元")
                if not 2 <= fast_period < slow_period <= 250:
                    raise ValueError("均线周期须满足 2 ≤ 快线 < 慢线 ≤ 250")
                if not 60 <= history_limit <= 1000 or history_limit < slow_period + 2:
                    raise ValueError("历史天数须为60至1000，且应大于慢线周期")
                if not 0 < target_pct <= 1:
                    raise ValueError("目标仓位须在0%至100%之间")
                history = _BACKTEST_KLINE_PROVIDER.fetch_daily(
                    normalized.canonical, limit=history_limit, adjustment="none"
                )
                ordered_bars = sorted(history.bars, key=lambda item: item.trading_date)
                if len(ordered_bars) < slow_period + 2:
                    raise ValueError("可用未复权日线不足，无法进行可靠回测")
                result = AShareBacktester(initial_cash=initial_cash).run(
                    ordered_bars,
                    moving_average_crossover_signals(
                        ordered_bars, fast_period=fast_period, slow_period=slow_period
                    ),
                    target_position_pct=target_pct,
                )
                self._send_json({
                    "symbol": normalized.canonical,
                    "strategy": "简单移动平均线交叉",
                    "parameters": {"fast_period": fast_period, "slow_period": slow_period,
                                   "initial_cash": initial_cash, "target_position_pct": target_pct},
                    "source": history.provenance.source if history.provenance else None,
                    "source_url": history.provenance.source_url if history.provenance else None,
                    "status": history.status.value,
                    "warnings": [*history.warnings,
                                 "仅为收盘信号、次日开盘执行的示例策略；不代表收益预测。",
                                 "未模拟分红、送转、退市/ST历史状态变化；手续费按默认示例费率。"],
                    "first_date": ordered_bars[0].trading_date.isoformat(),
                    "last_date": ordered_bars[-1].trading_date.isoformat(),
                    "bar_count": len(ordered_bars),
                    "initial_cash": result.initial_cash,
                    "ending_cash": result.ending_cash,
                    "ending_quantity": result.ending_quantity,
                    "final_equity": result.final_equity,
                    "total_return": result.total_return,
                    "max_drawdown": result.max_drawdown,
                    "trade_count": len(result.trades),
                    "trades": [{"date": trade.trading_date.isoformat(), "side": trade.side.value,
                                "quantity": trade.quantity, "price": trade.price, "fees": trade.fees}
                               for trade in result.trades],
                    "rejected_orders": result.rejected_orders,
                    "equity_curve": [{"date": day.isoformat(), "equity": equity}
                                     for day, equity in result.equity_curve],
                })
            elif self.path == "/api/trade":
                state, transaction = _PORTFOLIO_STORE.apply_trade(
                    payload.get("symbol", ""),
                    payload.get("side", ""),
                    payload.get("quantity", 0),
                    payload.get("price", 0),
                    payload.get("commission_rate", 0),
                    stamp_tax_rate=payload.get("stamp_tax_rate", 0.0005),
                    transfer_fee_rate=payload.get("transfer_fee_rate", 0.00001),
                    minimum_commission=payload.get("minimum_commission", 5.0),
                )
                self._send_json({"state": state, "transaction": transaction})
            else:
                holdings = []
                for item in payload.get("holdings", []):
                    symbol = str(item.get("symbol", "")).strip()
                    quantity = int(item.get("quantity") or 0)
                    raw_cost = item.get("average_cost")
                    average_cost = float(raw_cost) if raw_cost not in {None, ""} else None
                    if symbol and quantity > 0:
                        holdings.append(HoldingInput(symbol, quantity, average_cost))
                if not holdings:
                    raise ValueError("至少需要一条有效持仓")
                self._send_json(_PORTFOLIO_ANALYZER.analyze(
                    holdings,
                    cash=float(payload.get("cash") or 0.0),
                ))
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    def log_message(self, format: str, *args) -> None:
        return


def run_dashboard(*, port: int = 8765, open_browser: bool = True) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"小A顾问可视化已启动：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_dashboard())
