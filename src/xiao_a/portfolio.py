from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from xiao_a.auto import AutoAnalysis, AutoAnalyzer
from xiao_a.models import SignalAction
from xiao_a.risk import RiskContext, RiskEngine


@dataclass(frozen=True)
class HoldingInput:
    symbol: str
    quantity: int
    average_cost: float | None = None


@dataclass
class HoldingAnalysis:
    symbol: str
    name: str = ""
    quantity: int = 0
    average_cost: float | None = None
    price: float | None = None
    previous_close: float | None = None
    daily_amount: float | None = None
    daily_profit: float | None = None
    daily_profit_pct: float | None = None
    market_value: float = 0.0
    weight: float = 0.0
    profit: float | None = None
    profit_pct: float | None = None
    model_action: SignalAction | None = None
    portfolio_action: SignalAction | None = None
    score: float | None = None
    confidence: float | None = None
    portfolio_warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class PortfolioAnalysis:
    cash: float
    positions_value: float
    total_assets: float
    known_cost_basis: float
    known_profit: float
    daily_profit: float
    daily_profit_pct: float | None
    largest_position_pct: float
    holdings: list[HoldingAnalysis]
    warnings: list[str]


class PortfolioAnalyzer:
    def __init__(self, auto_analyzer: AutoAnalyzer | None = None):
        self.auto_analyzer = auto_analyzer or AutoAnalyzer()
        self.risk_engine = RiskEngine()

    def _analyze_one(self, holding: HoldingInput) -> tuple[HoldingInput, AutoAnalysis | None, str | None]:
        try:
            return holding, self.auto_analyzer.analyze(holding.symbol), None
        except Exception as exc:
            return holding, None, f"{type(exc).__name__}: {exc}"

    def analyze(self, holdings: list[HoldingInput], *, cash: float = 0.0) -> PortfolioAnalysis:
        clean = [item for item in holdings if item.symbol.strip() and item.quantity > 0]
        raw_results: list[tuple[HoldingInput, AutoAnalysis | None, str | None]] = []
        with ThreadPoolExecutor(max_workers=min(3, max(1, len(clean)))) as executor:
            futures = [executor.submit(self._analyze_one, item) for item in clean]
            for future in as_completed(futures):
                raw_results.append(future.result())

        order = {item.symbol: index for index, item in enumerate(clean)}
        raw_results.sort(key=lambda item: order.get(item[0].symbol, 9999))
        rows: list[HoldingAnalysis] = []
        for holding, analysis, error in raw_results:
            if analysis is None or analysis.quote is None:
                rows.append(HoldingAnalysis(
                    symbol=holding.symbol,
                    quantity=holding.quantity,
                    average_cost=holding.average_cost,
                    error=error or "实时行情不可用",
                ))
                continue
            price = analysis.quote.price
            market_value = price * holding.quantity
            cost = holding.average_cost if holding.average_cost and holding.average_cost > 0 else None
            profit = market_value - cost * holding.quantity if cost is not None else None
            rows.append(HoldingAnalysis(
                symbol=analysis.symbol,
                name=analysis.quote.name,
                quantity=holding.quantity,
                average_cost=cost,
                price=price,
                previous_close=analysis.quote.previous_close,
                daily_amount=analysis.quote.amount,
                daily_profit=(price - analysis.quote.previous_close) * holding.quantity
                if analysis.quote.previous_close and analysis.quote.previous_close > 0 else None,
                daily_profit_pct=(price / analysis.quote.previous_close - 1.0)
                if analysis.quote.previous_close and analysis.quote.previous_close > 0 else None,
                market_value=market_value,
                profit=profit,
                profit_pct=(price / cost - 1.0) if cost else None,
                model_action=analysis.decision.final_action,
                portfolio_action=analysis.decision.final_action,
                score=analysis.decision.score,
                confidence=analysis.decision.confidence,
            ))

        positions_value = sum(item.market_value for item in rows)
        total_assets = max(0.0, cash) + positions_value
        warnings: list[str] = []
        for item in rows:
            item.weight = item.market_value / total_assets if total_assets > 0 else 0.0
            if item.model_action is not None:
                risk = self.risk_engine.apply(
                    item.model_action,
                    item.confidence or 0.0,
                    RiskContext(position_pct=item.weight),
                )
                item.portfolio_action = risk.action
                item.portfolio_warnings = [*risk.warnings, *risk.vetoes]
            if item.weight >= 0.40:
                warnings.append(f"{item.name or item.symbol}占总资产{item.weight:.1%}，集中度超过硬阈值")
            elif item.weight >= 0.25:
                warnings.append(f"{item.name or item.symbol}占总资产{item.weight:.1%}，禁止继续加仓")
            if item.error:
                warnings.append(f"{item.symbol}：{item.error}")

        known_rows = [item for item in rows if item.average_cost is not None and item.profit is not None]
        known_cost = sum((item.average_cost or 0.0) * item.quantity for item in known_rows)
        known_profit = sum(item.profit or 0.0 for item in known_rows)
        daily_profit = sum(item.daily_profit or 0.0 for item in rows if item.daily_profit is not None)
        previous_positions_value = sum(
            (item.previous_close or 0.0) * item.quantity
            for item in rows
            if item.previous_close is not None
        )
        previous_total_assets = max(0.0, cash) + previous_positions_value
        largest = max((item.weight for item in rows), default=0.0)
        return PortfolioAnalysis(
            cash=max(0.0, cash),
            positions_value=positions_value,
            total_assets=total_assets,
            known_cost_basis=known_cost,
            known_profit=known_profit,
            daily_profit=daily_profit,
            largest_position_pct=largest,
            holdings=rows,
            warnings=warnings,
            daily_profit_pct=daily_profit / previous_total_assets if previous_total_assets > 0 else None,
        )
