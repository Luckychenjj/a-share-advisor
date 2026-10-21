from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from xiao_a.data.kline import EastmoneyKlineProvider, KlineResult, KlineRouter, TencentKlineProvider
from xiao_a.data.volume import SHANGHAI, trading_progress
from xiao_a.data.fallback import BaostockKlineProvider, PytdxKlineProvider, PytdxQuoteProvider, SinaQuoteProvider
from xiao_a.data.market import (
    CailianMarketEmotionProvider,
    EastmoneyMarketBreadthProvider,
    MarketBreadthResult,
    MarketBreadthRouter,
)
from xiao_a.data.router import ProviderResult, QuoteRouter
from xiao_a.data.supplemental import SupplementalDataProvider, SupplementalResult
from xiao_a.data.symbols import is_market_index
from xiao_a.data.tencent import TencentQuoteProvider
from xiao_a.decision import AdvisorEngine
from xiao_a.features import build_features
from xiao_a.models import AdvisorDecision, DailyBar, DataStatus, MarketSnapshot, Quote
from xiao_a.market_prediction import independent_market_prediction
from xiao_a.risk import RiskContext
from xiao_a.strategies.chan import ChanStructureStrategy
from xiao_a.strategies.core import CycleTrendStrategy, LeaderSentimentStrategy, MarketRegimeStrategy, MultiFactorStrategy


@dataclass
class AutoAnalysis:
    symbol: str
    quote: Quote | None
    bars_count: int
    benchmark_bars_count: int
    features: dict[str, float]
    omitted_features: list[str]
    decision: AdvisorDecision
    warnings: list[str]
    diagnostics: list[str]
    sources: list[str]
    supplemental: dict[str, object] = field(default_factory=dict)
    audit_snapshot: dict[str, object] = field(default_factory=dict)
    market_prediction: dict[str, object] = field(default_factory=dict)


class AutoAnalyzer:
    def __init__(
        self,
        *,
        quote_router: QuoteRouter | None = None,
        kline_provider: object | None = None,
        market_provider: object | None = None,
        benchmark_symbol: str = "SH.000300",
    ):
        self.quote_router = quote_router or QuoteRouter([TencentQuoteProvider(), SinaQuoteProvider(), PytdxQuoteProvider()])
        self.kline_provider = kline_provider or KlineRouter([
            EastmoneyKlineProvider(priority=10),
            TencentKlineProvider(priority=20),
            PytdxKlineProvider(priority=30),
            BaostockKlineProvider(priority=40),
        ])
        self.market_provider = market_provider or MarketBreadthRouter([
            CailianMarketEmotionProvider(),
            EastmoneyMarketBreadthProvider(),
        ])
        self.benchmark_symbol = benchmark_symbol
        self.engine = AdvisorEngine([
            CycleTrendStrategy(),
            LeaderSentimentStrategy(),
            ChanStructureStrategy(),
            MultiFactorStrategy(),
            MarketRegimeStrategy(),
        ])
        self._benchmark_cache: tuple[float, KlineResult] | None = None
        self._benchmark_lock = Lock()
        self._stock_cache: dict[str, tuple[float, KlineResult]] = {}
        self._stock_lock = Lock()
        self._stock_cache_ttl = 30.0
        self._market_cache: tuple[float, MarketBreadthResult] | None = None
        self._market_lock = Lock()
        self._market_cache_ttl = 60.0
        self.supplemental_provider = SupplementalDataProvider()
        self._supplemental_cache: dict[str, tuple[float, SupplementalResult]] = {}
        self._supplemental_lock = Lock()
        self._supplemental_cache_ttl = 60.0

    def _safe_kline(self, symbol: str) -> KlineResult:
        key = str(symbol).strip().upper()
        now = monotonic()
        with self._stock_lock:
            cached = self._stock_cache.get(key)
            if cached and now - cached[0] < self._stock_cache_ttl:
                return cached[1]
        try:
            result = self.kline_provider.fetch_daily(symbol, limit=360)
        except Exception as exc:
            result = KlineResult([], DataStatus.UNAVAILABLE, errors=[f"{type(exc).__name__}: {exc}"])
        if result.bars:
            with self._stock_lock:
                self._stock_cache[key] = (monotonic(), result)
        return result

    def _benchmark(self) -> KlineResult:
        with self._benchmark_lock:
            if self._benchmark_cache and monotonic() - self._benchmark_cache[0] < 300:
                return self._benchmark_cache[1]
            result = self._safe_kline(self.benchmark_symbol)
            if result.bars:
                self._benchmark_cache = (monotonic(), result)
            return result

    def _market_breadth(self) -> MarketBreadthResult:
        with self._market_lock:
            if self._market_cache and monotonic() - self._market_cache[0] < self._market_cache_ttl:
                return self._market_cache[1]
            try:
                result = self.market_provider.fetch()
            except Exception as exc:
                result = MarketBreadthResult(
                    {}, DataStatus.UNAVAILABLE,
                    warnings=["板块广度与市场情绪数据暂不可用，相关输入已弃权"],
                    errors=[f"{type(exc).__name__}: {exc}"],
                )
            # Cache both successful and unavailable results.  A failing remote
            # endpoint should not delay every subsequent stock analysis.
            self._market_cache = (monotonic(), result)
            return result

    def _supplemental(self, symbol: str) -> SupplementalResult:
        key = str(symbol).strip().upper()
        now = monotonic()
        with self._supplemental_lock:
            cached = self._supplemental_cache.get(key)
            if cached:
                # Keep good snapshots stable, but retry partial snapshots
                # quickly so a transient public-endpoint disconnect does not
                # suppress a source for the whole minute.
                cache_ttl = self._supplemental_cache_ttl if cached[1].status is DataStatus.FRESH else 5.0
                if now - cached[0] < cache_ttl:
                    return cached[1]
        try:
            result = self.supplemental_provider.fetch(symbol)
        except Exception as exc:
            result = SupplementalResult(
                {
                    "announcements": {"available": False, "count": 0, "items": [], "risk_flags": []},
                    "fund_flow": {"available": False},
                    "limit_up_pool": {"available": False, "count": None, "max_continuous": None, "selected": None, "top": []},
                },
                DataStatus.UNAVAILABLE,
                warnings=["公告、资金流和涨停池数据暂不可用，相关信息不参与判断"],
                errors=[f"{type(exc).__name__}: {exc}"],
            )
        with self._supplemental_lock:
            self._supplemental_cache[key] = (monotonic(), result)
        return result

    @staticmethod
    def _apply_live_quote(bars: list[DailyBar], quote: Quote | None) -> list[DailyBar]:
        """Make the current intraday quote visible to daily-based strategies."""
        if not bars or quote is None or quote.price <= 0:
            return bars
        as_of = quote.provenance.as_of if quote.provenance else None
        if as_of is None or trading_progress(as_of) is None:
            return bars
        session = as_of.astimezone(SHANGHAI).date() if as_of.tzinfo else as_of.date()
        last = bars[-1]
        if session < last.trading_date:
            return bars
        high = max(quote.price, quote.high or quote.price)
        low = min(quote.price, quote.low or quote.price)
        if last.trading_date == session:
            return [
                *bars[:-1],
                replace(
                    last,
                    close=quote.price,
                    high=max(last.high, high),
                    low=min(last.low, low),
                    volume=quote.volume if quote.volume and quote.volume > 0 else last.volume,
                    previous_close=quote.previous_close or last.previous_close,
                ),
            ]
        return [
            *bars,
            DailyBar(
                trading_date=session,
                open=quote.open or quote.price,
                high=high,
                low=low,
                close=quote.price,
                volume=quote.volume or 0.0,
                previous_close=quote.previous_close or last.close,
                is_st=last.is_st,
                limit_rate=last.limit_rate,
            ),
        ]

    def analyze(
        self,
        symbol: str,
        risk_context: RiskContext | None = None,
        *,
        include_supplemental: bool = True,
    ) -> AutoAnalysis:
        quote_result: ProviderResult = self.quote_router.fetch_quote(symbol)
        stock_result = self._safe_kline(symbol)
        benchmark_result = self._benchmark()
        market_result = self._market_breadth()
        if include_supplemental and is_market_index(symbol):
            supplemental_result = self.supplemental_provider.fetch_market_limit_up_pool(symbol)
        elif include_supplemental:
            supplemental_result = self._supplemental(symbol)
        else:
            supplemental_result = SupplementalResult(
                {
                    "announcements": {"available": False, "count": 0, "items": [], "risk_flags": []},
                    "fund_flow": {"available": False},
                    "limit_up_pool": {"available": False, "count": None, "max_continuous": None, "selected": None, "top": []},
                },
                DataStatus.UNAVAILABLE,
            )
        if not stock_result.bars:
            details = "; ".join([*stock_result.errors, *stock_result.warnings]) or "unknown provider failure"
            raise RuntimeError(f"无法取得历史日线：{details}")

        quote = quote_result.quote
        analysis_bars = self._apply_live_quote(stock_result.bars, quote)
        feature_result = build_features(
            analysis_bars,
            benchmark_bars=benchmark_result.bars,
            quote=quote,
        )
        features = {**feature_result.values, **market_result.values}
        quote_day = (quote.provenance.as_of.astimezone(SHANGHAI).date().isoformat()
                     if quote and quote.provenance and quote.provenance.as_of else None)
        supplemental_date_warnings = []
        for label, key in (("资金流", "fund_flow"), ("涨停池", "limit_up_pool")):
            observation = supplemental_result.data.get(key) or {}
            if observation.get("available") and (not quote_day or observation.get("trade_date") != quote_day):
                supplemental_date_warnings.append(f"{label}与行情交易日不一致或日期未确认，仅展示，不参与判断")
        fund_flow = supplemental_result.data.get("fund_flow") or {}
        if fund_flow.get("available") and quote_day and fund_flow.get("trade_date") == quote_day and fund_flow.get("signal") is not None:
            features["fund_flow_signal"] = float(fund_flow["signal"])
        limit_up_pool = supplemental_result.data.get("limit_up_pool") or {}
        if limit_up_pool.get("available") and quote_day and limit_up_pool.get("trade_date") == quote_day:
            features["limit_up_signal"] = 1.0 if limit_up_pool.get("selected") else 0.0
        warnings = [
            *quote_result.warnings,
            *stock_result.warnings,
            *benchmark_result.warnings,
            *market_result.warnings,
            *supplemental_result.warnings,
            *supplemental_date_warnings,
            *feature_result.warnings,
        ]
        diagnostics = [
            *quote_result.errors,
            *stock_result.errors,
            *benchmark_result.errors,
            *market_result.errors,
            *supplemental_result.errors,
        ]
        status = DataStatus.FRESH
        if (
            quote is None
            or quote_result.status != DataStatus.FRESH
            or not benchmark_result.bars
            or benchmark_result.status != DataStatus.FRESH
            or stock_result.status != DataStatus.FRESH
        ):
            status = DataStatus.PARTIAL
        snapshot = MarketSnapshot(
            symbol=quote.symbol if quote else symbol,
            as_of=datetime.now(timezone.utc),
            quote=quote,
            bars=analysis_bars,
            features=features,
            data_status=status,
            data_warnings=warnings,
        )
        risk = risk_context or RiskContext()
        if quote:
            upper_name = quote.name.upper()
            risk = replace(
                risk,
                daily_amount=quote.amount if quote.amount is not None else risk.daily_amount,
                is_st=risk.is_st or upper_name.startswith("ST") or upper_name.startswith("*ST"),
                limit_up_locked=bool(quote.limit_up and quote.price >= quote.limit_up),
                limit_down_locked=bool(quote.limit_down and quote.price <= quote.limit_down),
            )
        announcement_flags = (supplemental_result.data.get("announcements") or {}).get("risk_flags") or []
        if announcement_flags:
            warnings.append(f"近期公告需关注：{'；'.join(announcement_flags[:2])}")
            high_severity = [f"公告高风险：{title}" for title in announcement_flags[:2]]
            risk = replace(risk, high_severity_flags=[*risk.high_severity_flags, *high_severity])
        decision = self.engine.decide(snapshot, risk)
        expected = {
            "monthly_trend", "weekly_trend", "daily_trend", "relative_strength", "cycle_position",
            "leader_strength", "sector_heat", "limit_up_breadth", "emotion_cycle", "broken_board_risk",
            "liquidity_quality", "fund_flow_signal", "limit_up_signal",
            "momentum_20", "momentum_60", "volatility_score", "volume_price_signal", "valuation_signal",
        }
        sources = []
        if quote and quote.provenance:
            sources.append(quote.provenance.source_url)
        if stock_result.provenance:
            sources.append(stock_result.provenance.source_url)
        if benchmark_result.provenance:
            sources.append(benchmark_result.provenance.source_url)
        if market_result.provenance:
            sources.append(market_result.provenance.source_url)
        sources.extend(provenance.source_url for provenance in supplemental_result.provenances)
        return AutoAnalysis(
            symbol=snapshot.symbol,
            quote=quote,
            bars_count=len(stock_result.bars),
            benchmark_bars_count=len(benchmark_result.bars),
            features=features,
            omitted_features=sorted(expected - features.keys()),
            decision=decision,
            market_prediction=independent_market_prediction(decision),
            warnings=warnings,
            diagnostics=diagnostics,
            sources=sources,
            supplemental={**supplemental_result.data, "status": supplemental_result.status.value},
            audit_snapshot={
                "features": features,
                "quote": quote,
                "daily_bars": analysis_bars,
                "benchmark_bars": benchmark_result.bars,
                "source_status": {
                    "quote": quote_result.status.value,
                    "daily_bars": stock_result.status.value,
                    "benchmark": benchmark_result.status.value,
                    "market": market_result.status.value,
                    "supplemental": supplemental_result.status.value,
                },
                "source_urls": sources,
                "data_warnings": warnings,
                "data_errors": diagnostics,
            },
        )
