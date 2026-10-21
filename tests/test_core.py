from __future__ import annotations

import unittest
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from xiao_a.backtest import AShareBacktester, FeeSchedule, moving_average_crossover_signals
from xiao_a.auto import AutoAnalysis, AutoAnalyzer
from xiao_a.data.router import ProviderResult, QuoteRouter
from xiao_a.data.kline import KlineResult, KlineRouter, parse_eastmoney_kline_payload, parse_tencent_kline_payload
from xiao_a.data.market import MarketBreadthResult, parse_cailian_market_emotion_payload
from xiao_a.data.fallback import parse_sina_quote_payload
from xiao_a.data.supplemental import (
    parse_announcements_payload,
    parse_fund_flow_payload,
    parse_limit_up_payload,
)
from xiao_a.data.search import parse_tencent_smartbox_payload
from xiao_a.data.symbols import is_market_index, normalize_symbol
from xiao_a.data.tencent import parse_tencent_quote_payload
from xiao_a.decision import AdvisorEngine
from xiao_a.ensemble import AdaptiveEnsemble, StrategyPerformance
from xiao_a.journal import DecisionJournal
from xiao_a.features import build_features
from xiao_a.models import AdvisorDecision, DailyBar, DataStatus, MarketSnapshot, Quote, SignalAction
from xiao_a.portfolio import HoldingInput, PortfolioAnalyzer
from xiao_a.risk import RiskContext, RiskEngine, RiskPolicy
from xiao_a.strategies.chan import ChanStructureStrategy
from xiao_a.strategies.core import CycleTrendStrategy, LeaderSentimentStrategy, MarketRegimeStrategy, MultiFactorStrategy


class SymbolTests(unittest.TestCase):
    def test_routes_common_symbols(self):
        self.assertEqual(normalize_symbol("600519").canonical, "SH.600519")
        self.assertEqual(normalize_symbol("000001.SZ").canonical, "SZ.000001")
        self.assertEqual(normalize_symbol("sh000001").canonical, "SH.000001")
        self.assertEqual(normalize_symbol("920982").canonical, "BJ.920982")
        self.assertEqual(normalize_symbol("510300").canonical, "SH.510300")

    def test_identifies_supported_market_indexes(self):
        self.assertTrue(is_market_index("SH.000001"))
        self.assertTrue(is_market_index("399006"))
        self.assertTrue(is_market_index("SH.000688"))
        self.assertFalse(is_market_index("SZ.300750"))

    def test_sma_crossover_signals_are_recorded_at_signal_bar_close(self):
        closes = [5, 4, 3, 2, 1, 1, 1, 1, 6, 7, 8, 6, 3, 1]
        bars = [
            DailyBar(date(2026, 1, 1) + timedelta(days=index), value, value, value, value)
            for index, value in enumerate(closes)
        ]
        signals = moving_average_crossover_signals(bars, fast_period=2, slow_period=3)
        self.assertIn(SignalAction.BUY, signals.values())
        self.assertIn(SignalAction.SELL, signals.values())
        self.assertTrue(all(day in {bar.trading_date for bar in bars[:-1]} for day in signals))

    def test_parses_tencent_search_for_name_code_and_pinyin(self):
        payload = (
            'v_hint="sz~300750~\\u5b81\\u5fb7\\u65f6\\u4ee3~ndsd~GP-A'
            '^sh~510300~\\u6caa\\u6df1300ETF~hs300etf~ETF'
            '^sz~399001~\\u6df1\\u8bc1\\u6210\\u6307~szcz~ZS"'
        )
        matches = parse_tencent_smartbox_payload(payload)
        self.assertEqual([(item.symbol, item.name, item.pinyin) for item in matches], [
            ("SZ.300750", "宁德时代", "ndsd"),
            ("SH.510300", "沪深300ETF", "hs300etf"),
        ])

    def test_parses_tencent_fields_without_pb_amplitude_mixup(self):
        fields = [""] * 53
        fields[1], fields[3], fields[4], fields[5], fields[6] = "测试股", "10.50", "10.00", "10.10", "12000"
        fields[33], fields[34], fields[37] = "10.80", "9.90", "1234"
        fields[38], fields[39], fields[43], fields[46] = "2.5", "18.0", "8.0", "1.7"
        fields[47], fields[48] = "11.00", "9.00"
        payload = f'v_sh600519="{"~".join(fields)}";'
        quote = parse_tencent_quote_payload(payload, "600519")
        self.assertEqual(quote.pb, 1.7)
        self.assertEqual(quote.amount, 12_340_000)
        self.assertEqual(quote.volume, 12_000)
        self.assertEqual(quote.status, DataStatus.FRESH)

    def test_parses_cailian_market_emotion(self):
        payload = json.dumps({
            "code": 200,
            "data": {
                "market_degree": "65",
                "up_ratio": "75.00%",
                "up_open_num": 40,
                "up_down_dis": {
                    "rise_num": 3200,
                    "fall_num": 1800,
                    "flat_num": 120,
                    "up_num": 90,
                    "down_num": 20,
                },
            },
        })
        result = parse_cailian_market_emotion_payload(payload)
        self.assertEqual(result.status, DataStatus.FRESH)
        self.assertEqual(set(result.values), {
            "sector_heat", "limit_up_breadth", "emotion_cycle", "broken_board_risk",
        })
        self.assertGreater(result.values["limit_up_breadth"], 0)
        self.assertAlmostEqual(result.values["broken_board_risk"], 0.25)

    def test_parses_supplemental_market_data(self):
        announcements, flags, _ = parse_announcements_payload(json.dumps({
            "data": {"list": [
                {"title": "关于公司业绩预亏的公告", "notice_date": "2026-09-29", "art_code": "A1"},
            ]},
        }))
        self.assertEqual(len(announcements), 1)
        self.assertEqual(flags, ["关于公司业绩预亏的公告"])

        flow, _ = parse_fund_flow_payload(json.dumps({
            "data": {"klines": [
                "2026-09-28,100,-20,30,40,50",
                "2026-09-29,200,-10,20,80,110",
            ]},
        }))
        self.assertEqual(flow["trade_date"], "2026-09-29")
        self.assertIsNone(flow["five_day_net_inflow"])
        self.assertGreater(flow["signal"], 0)

        pool, _ = parse_limit_up_payload(json.dumps({
            "code": 200,
            "data": [
                {"secu_code": "sz300750", "secu_name": "测试股", "last_px": 10, "change": 20, "up_reason": "算力", "continuous": 2},
                {"secu_code": "sh600000", "secu_name": "其他", "last_px": 10, "change": 10, "up_reason": "银行", "continuous": 1},
            ],
        }), symbol="300750")
        self.assertEqual(pool["count"], 2)
        self.assertTrue(pool["selected"]["name"] == "测试股")

    def test_parses_kline_and_assigns_board_limit(self):
        payload = json.dumps({
            "code": 0,
            "data": {
                "sz300750": {
                    "qfqday": [
                        ["2026-01-06", "10.5", "10.8", "11.0", "10.3", "1200"],
                        ["2026-01-05", "10.0", "10.5", "10.6", "9.9", "1000"],
                    ]
                }
            },
        })
        result = parse_tencent_kline_payload(payload, "300750")
        self.assertEqual(result.bars[0].trading_date, date(2026, 1, 5))
        self.assertEqual(result.bars[1].previous_close, 10.5)
        self.assertEqual(result.bars[0].limit_rate, 0.20)

    def test_parses_eastmoney_kline(self):
        payload = json.dumps({
            "data": {
                "klines": [
                    "2026-01-05,10.0,10.5,10.6,9.9,1000,10500000,7,5,0.5,2",
                    "2026-01-06,10.5,10.8,11.0,10.3,1200,1280000,6,3,0.3,2.2",
                ]
            }
        })
        result = parse_eastmoney_kline_payload(payload, "300750")
        self.assertEqual(len(result.bars), 2)
        self.assertEqual(result.bars[-1].close, 10.8)
        self.assertEqual(result.bars[-1].volume, 1200)

    def test_normalizes_sina_quote_volume_to_lots(self):
        quote = parse_sina_quote_payload(
            'var hq_str_sz300750="宁德时代,10.1,10.0,10.2,10.3,9.9,0,0,1200000,12345678";',
            "300750",
        )
        self.assertEqual(quote.volume, 12_000)


class _KlineSource:
    def __init__(self, name, priority, result=None, error=None):
        self.name, self.priority, self.result, self.error = name, priority, result, error

    def fetch_daily(self, symbol, *, limit=360):
        if self.error:
            raise self.error
        return self.result


class KlineRouterTests(unittest.TestCase):
    def test_fallback_preserves_first_provider_error(self):
        bars = [DailyBar(date(2026, 1, 5), 10, 11, 9, 10, volume=100)]
        router = KlineRouter([
            _KlineSource("first", 1, error=RuntimeError("boom")),
            _KlineSource("second", 2, result=KlineResult(bars, DataStatus.FRESH)),
        ])
        result = router.fetch_daily("300750")
        self.assertEqual(len(result.bars), 1)
        self.assertTrue(any("first" in item for item in result.errors))


class FeatureTests(unittest.TestCase):
    @staticmethod
    def _bars(growth: float) -> list[DailyBar]:
        result = []
        price = 10.0
        for index in range(400):
            price *= 1.0 + growth
            result.append(DailyBar(
                date(2025, 1, 1) + timedelta(days=index),
                price * 0.995,
                price * 1.01,
                price * 0.99,
                price,
                volume=1_000_000,
            ))
        return result

    def test_builds_multitimeframe_and_relative_strength_features(self):
        quote = Quote("SZ.300750", "测试股", 20, 19.5, amount=800_000_000, pe_ttm=20.0)
        result = build_features(self._bars(0.004), benchmark_bars=self._bars(0.001), quote=quote)
        for key in (
            "daily_trend", "weekly_trend", "monthly_trend", "relative_strength",
            "cycle_position", "leader_strength", "liquidity_quality",
            "momentum_20", "momentum_60", "volatility_score", "volume_price_signal", "valuation_signal",
        ):
            self.assertIn(key, result.values)
            self.assertGreaterEqual(result.values[key], -1.0)
            self.assertLessEqual(result.values[key], 1.0)
        self.assertGreater(result.values["relative_strength"], 0)
        self.assertNotIn("sector_heat", result.values)


class _QuoteRouter:
    def fetch_quote(self, symbol):
        return ProviderResult(
            Quote("SZ.300750", "测试股", 20, 19.5, amount=800_000_000),
            DataStatus.FRESH,
        )


class _KlineProvider:
    def fetch_daily(self, symbol, *, limit=360):
        growth = 0.004 if "300750" in symbol else 0.001
        return KlineResult(FeatureTests._bars(growth), DataStatus.FRESH)


class _MarketProvider:
    def fetch(self):
        return MarketBreadthResult(
            {"sector_heat": 0.20, "limit_up_breadth": 0.15, "emotion_cycle": 0.10},
            DataStatus.FRESH,
        )


class AutoAnalysisTests(unittest.TestCase):
    def test_auto_pipeline_builds_features_and_runs_chan(self):
        result = AutoAnalyzer(
            quote_router=_QuoteRouter(),
            kline_provider=_KlineProvider(),
            market_provider=_MarketProvider(),
        ).analyze("300750", RiskContext(position_pct=0.10))
        self.assertEqual(result.bars_count, 400)
        self.assertIn("relative_strength", result.features)
        self.assertIn("sector_heat", result.features)
        self.assertIn("emotion_cycle", result.features)
        self.assertIn("broken_board_risk", result.omitted_features)
        chan = next(item for item in result.decision.strategy_signals if item.strategy == "chan_structure")
        self.assertGreater(chan.confidence, 0)
        self.assertIn("multi_factor", {item.strategy for item in result.decision.strategy_signals})
        self.assertIn("market_regime", {item.strategy for item in result.decision.strategy_signals})


class _PortfolioAutoAnalyzer:
    def analyze(self, symbol):
        quote = Quote("SZ.300750", "测试股", 12.0, 11.5, amount=500_000_000)
        decision = AdvisorDecision(
            symbol=quote.symbol,
            as_of=datetime.now(timezone.utc),
            raw_action=SignalAction.HOLD,
            final_action=SignalAction.HOLD,
            score=0.1,
            confidence=0.7,
            executable=True,
            reasons=[],
            warnings=[],
            vetoes=[],
            strategy_signals=[],
            strategy_weights={},
        )
        return AutoAnalysis(
            symbol=quote.symbol,
            quote=quote,
            bars_count=360,
            benchmark_bars_count=360,
            features={},
            omitted_features=[],
            decision=decision,
            warnings=[],
            diagnostics=[],
            sources=[],
        )


class PortfolioTests(unittest.TestCase):
    def test_concentration_overrides_hold_and_calculates_profit(self):
        result = PortfolioAnalyzer(_PortfolioAutoAnalyzer()).analyze(
            [HoldingInput("300750", 100, 10.0)],
            cash=0,
        )
        self.assertEqual(result.total_assets, 1200)
        self.assertEqual(result.known_profit, 200)
        self.assertEqual(result.holdings[0].portfolio_action, SignalAction.REDUCE)
        self.assertTrue(result.warnings)


class _Provider:
    def __init__(self, name, priority, result=None, error=None):
        self.name, self.priority, self.result, self.error = name, priority, result, error

    def fetch_quote(self, symbol):
        if self.error:
            raise self.error
        return self.result


class RouterTests(unittest.TestCase):
    def test_fallback_keeps_failure_trace(self):
        fresh = ProviderResult(None, DataStatus.UNAVAILABLE)
        router = QuoteRouter([
            _Provider("broken", 1, error=RuntimeError("boom")),
            _Provider("empty", 2, result=fresh),
        ])
        result = router.fetch_quote("600519")
        self.assertEqual(result.status, DataStatus.UNAVAILABLE)
        self.assertTrue(any("broken" in error for error in result.errors))


class DecisionTests(unittest.TestCase):
    def _snapshot(self):
        return MarketSnapshot(
            symbol="SH.600519",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            features={
                "monthly_trend": 0.8,
                "weekly_trend": 0.7,
                "daily_trend": 0.6,
                "relative_strength": 0.7,
                "cycle_position": 0.3,
                "leader_strength": 0.8,
                "sector_heat": 0.6,
                "limit_up_breadth": 0.5,
                "emotion_cycle": 0.4,
                "broken_board_risk": 0.1,
                "liquidity_quality": 0.9,
            },
        )

    def test_high_risk_veto_beats_bullish_strategies(self):
        engine = AdvisorEngine([CycleTrendStrategy(), LeaderSentimentStrategy()])
        decision = engine.decide(
            self._snapshot(),
            RiskContext(high_severity_flags=["regulatory investigation"]),
        )
        self.assertEqual(decision.raw_action, SignalAction.BUY)
        self.assertEqual(decision.final_action, SignalAction.HOLD)
        self.assertIn("regulatory investigation", decision.vetoes)

    def test_losing_strategy_is_shrunk_not_blindly_obeyed(self):
        ensemble = AdaptiveEnsemble()
        snapshot = self._snapshot()
        signals = [CycleTrendStrategy().evaluate(snapshot), LeaderSentimentStrategy().evaluate(snapshot)]
        result = ensemble.combine(signals, {
            "cycle_trend": StrategyPerformance(samples=200, hit_rate=0.30, mean_excess_return=-0.03),
            "leader_sentiment": StrategyPerformance(samples=200, hit_rate=0.70, mean_excess_return=0.03),
        })
        self.assertGreater(result.weights["leader_sentiment"], result.weights["cycle_trend"])

    def test_stale_data_caps_confidence(self):
        result = RiskEngine(RiskPolicy(stale_confidence_cap=0.40)).apply(
            SignalAction.BUY, 0.90, RiskContext(data_status=DataStatus.STALE)
        )
        self.assertEqual(result.confidence, 0.40)

    def test_strategy_with_no_inputs_abstains(self):
        snapshot = MarketSnapshot("SH.600519", datetime.now(timezone.utc))
        signal = CycleTrendStrategy().evaluate(snapshot)
        self.assertEqual(signal.confidence, 0.0)

    def test_new_models_keep_factor_evidence_and_regime_label(self):
        snapshot = self._snapshot()
        snapshot.features.update({
            "momentum_20": 0.5,
            "momentum_60": 0.4,
            "volatility_score": 0.2,
            "volume_price_signal": 0.3,
            "valuation_signal": 0.1,
        })
        factor = MultiFactorStrategy().evaluate(snapshot)
        regime = MarketRegimeStrategy().evaluate(snapshot)
        self.assertGreater(factor.confidence, 0.5)
        self.assertIn("trend", factor.metadata["available_factors"])
        self.assertIn("状态=", regime.evidence[0])
        self.assertIn("regime", regime.metadata)

    def test_decision_does_not_mutate_caller_risk_context(self):
        context = RiskContext(data_status=DataStatus.STALE)
        AdvisorEngine([CycleTrendStrategy()]).decide(self._snapshot(), context)
        self.assertEqual(context.data_status, DataStatus.STALE)


class ChanTests(unittest.TestCase):
    def test_insufficient_data_is_explicitly_neutral(self):
        bars = [DailyBar(date(2026, 1, i + 1), 10, 11, 9, 10) for i in range(10)]
        signal = ChanStructureStrategy().evaluate(
            MarketSnapshot("SZ.000001", datetime.now(timezone.utc), bars=bars)
        )
        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertLess(signal.confidence, 0.2)


class JournalTests(unittest.TestCase):
    def test_point_in_time_excludes_future_outcomes(self):
        engine = AdvisorEngine([CycleTrendStrategy()])
        snapshot = MarketSnapshot(
            "SH.600519",
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            features={"monthly_trend": 1, "weekly_trend": 1, "daily_trend": 1, "relative_strength": 1, "cycle_position": 1},
        )
        decision = engine.decide(snapshot, RiskContext())
        directory = Path(__file__).parent / ".test-output"
        directory.mkdir(exist_ok=True)
        journal_path = directory / "journal.jsonl"
        journal_path.unlink(missing_ok=True)
        try:
            journal = DecisionJournal(journal_path)
            journal.append(decision)
            journal.settle(
                symbol=decision.symbol,
                as_of=decision.as_of,
                resolved_at=date(2026, 1, 12),
                realized_return=0.05,
                benchmark_return=0.01,
            )
            self.assertEqual(journal.settled_as_of(date(2026, 1, 10)), [])
            self.assertEqual(len(journal.settled_as_of(date(2026, 1, 12))), 1)
        finally:
            journal_path.unlink(missing_ok=True)
            directory.rmdir()


class BacktestTests(unittest.TestCase):
    def test_signal_fills_next_day_and_charges_fees(self):
        bars = [
            DailyBar(date(2026, 1, 2), 10.0, 10.2, 9.8, 10.0, previous_close=9.9),
            DailyBar(date(2026, 1, 5), 10.0, 10.5, 9.9, 10.4, previous_close=10.0),
            DailyBar(date(2026, 1, 6), 10.5, 10.8, 10.3, 10.7, previous_close=10.4),
        ]
        result = AShareBacktester(
            initial_cash=100_000,
            fees=FeeSchedule(0.0003, 5.0, 0.0005, 0.00001),
            slippage_bps=0,
        ).run(bars, {date(2026, 1, 2): SignalAction.BUY, date(2026, 1, 5): SignalAction.SELL})
        self.assertEqual(result.trades[0].trading_date, date(2026, 1, 5))
        self.assertEqual(result.trades[1].trading_date, date(2026, 1, 6))
        self.assertGreater(sum(trade.fees for trade in result.trades), 0)
        self.assertGreater(result.final_equity, result.initial_cash)
        self.assertEqual(result.ending_quantity, 0)
        self.assertGreaterEqual(result.ending_cash, 0)

    def test_bearish_signal_without_position_does_not_open_short(self):
        bars = [
            DailyBar(date(2026, 1, 2), 10.0, 10.2, 9.8, 10.0, previous_close=9.9),
            DailyBar(date(2026, 1, 5), 9.5, 9.8, 9.2, 9.4, previous_close=10.0),
        ]
        result = AShareBacktester(initial_cash=100_000).run(
            bars, {date(2026, 1, 2): SignalAction.SELL}
        )
        self.assertEqual(result.trades, [])
        self.assertEqual(result.ending_quantity, 0)
        self.assertIn("short sale prohibited", result.rejected_orders[0])

    def test_board_specific_limit_rate_is_respected(self):
        bar = DailyBar(
            date(2026, 1, 5),
            12.0,
            12.0,
            12.0,
            12.0,
            previous_close=10.0,
            limit_rate=0.20,
        )
        self.assertTrue(AShareBacktester._locked_limit(bar, side="buy"))


if __name__ == "__main__":
    unittest.main()
