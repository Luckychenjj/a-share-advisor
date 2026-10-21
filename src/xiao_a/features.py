from __future__ import annotations

from dataclasses import dataclass
from math import log10, sqrt, tanh

from xiao_a.data.volume import SHANGHAI, projected_volume_ratio, trading_progress
from xiao_a.models import DailyBar, Quote


@dataclass(frozen=True)
class FeatureResult:
    values: dict[str, float]
    warnings: tuple[str, ...] = ()


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _return(bars: list[DailyBar], periods: int) -> float | None:
    if len(bars) <= periods or bars[-periods - 1].close <= 0:
        return None
    return bars[-1].close / bars[-periods - 1].close - 1.0


def _daily_volatility(bars: list[DailyBar], periods: int = 20) -> float | None:
    if len(bars) < periods + 1:
        return None
    returns = [bars[index].close / bars[index - 1].close - 1.0 for index in range(len(bars) - periods, len(bars))]
    average = sum(returns) / len(returns)
    return sqrt(sum((item - average) ** 2 for item in returns) / len(returns))


def _aggregate(bars: list[DailyBar], interval: str) -> list[DailyBar]:
    groups: dict[tuple[int, int], list[DailyBar]] = {}
    for bar in bars:
        if interval == "weekly":
            iso = bar.trading_date.isocalendar()
            key = (iso.year, iso.week)
        else:
            key = (bar.trading_date.year, bar.trading_date.month)
        groups.setdefault(key, []).append(bar)

    result: list[DailyBar] = []
    previous_close: float | None = None
    for key in sorted(groups):
        chunk = groups[key]
        result.append(DailyBar(
            trading_date=chunk[-1].trading_date,
            open=chunk[0].open,
            high=max(item.high for item in chunk),
            low=min(item.low for item in chunk),
            close=chunk[-1].close,
            volume=sum(item.volume for item in chunk),
            previous_close=previous_close,
        ))
        previous_close = chunk[-1].close
    return result


def _trend_score(bars: list[DailyBar], *, lookback: int, return_scale: float) -> float | None:
    if len(bars) <= lookback:
        return None
    closes = [item.close for item in bars]
    span = min(20, max(5, lookback))
    average = _ema(closes, span)
    slope_gap = min(5, len(average) - 1)
    distance = closes[-1] / average[-1] - 1.0
    slope = average[-1] / average[-1 - slope_gap] - 1.0
    recent_return = closes[-1] / closes[-1 - lookback] - 1.0
    score = 0.35 * tanh(distance / (return_scale * 0.45))
    score += 0.25 * tanh(slope / (return_scale * 0.30))
    score += 0.40 * tanh(recent_return / return_scale)
    return max(-1.0, min(1.0, score))


def build_features(
    bars: list[DailyBar],
    *,
    benchmark_bars: list[DailyBar] | None = None,
    quote: Quote | None = None,
) -> FeatureResult:
    values: dict[str, float] = {}
    warnings: list[str] = []
    valid = [bar for bar in bars if not bar.suspended and bar.close > 0]
    weekly = _aggregate(valid, "weekly")
    monthly = _aggregate(valid, "monthly")

    for name, series, lookback, scale in (
        ("daily_trend", valid, 20, 0.12),
        ("weekly_trend", weekly, 10, 0.22),
        ("monthly_trend", monthly, 6, 0.30),
    ):
        score = _trend_score(series, lookback=lookback, return_scale=scale)
        if score is not None:
            values[name] = score
        else:
            warnings.append(f"{name} 的历史数据不足")

    if len(valid) >= 30:
        window = valid[-min(120, len(valid)):]
        low, high, current = min(item.low for item in window), max(item.high for item in window), window[-1].close
        values["cycle_position"] = 0.0 if high <= low else max(-1.0, min(1.0, 2.0 * (current - low) / (high - low) - 1.0))

    benchmark = [bar for bar in (benchmark_bars or []) if not bar.suspended and bar.close > 0]
    stock_20, bench_20 = _return(valid, 20), _return(benchmark, 20)
    stock_60, bench_60 = _return(valid, 60), _return(benchmark, 60)
    if stock_20 is not None and bench_20 is not None:
        excess_20 = stock_20 - bench_20
        excess_60 = (stock_60 - bench_60) if stock_60 is not None and bench_60 is not None else excess_20
        relative_strength = 0.65 * tanh(excess_20 / 0.12) + 0.35 * tanh(excess_60 / 0.22)
        values["relative_strength"] = max(-1.0, min(1.0, relative_strength))
        values["leader_strength"] = max(-1.0, min(1.0, 0.65 * relative_strength + 0.35 * tanh(stock_20 / 0.15)))
    else:
        warnings.append("基准指数历史数据不可用，已跳过相对强度")

    if quote and quote.amount and quote.amount > 0:
        values["liquidity_quality"] = max(-1.0, min(1.0, tanh(log10(quote.amount / 100_000_000.0))))
    else:
        warnings.append("成交额不可用，已跳过流动性质量")

    # Inputs used by the transparent multi-factor model. They are deliberately
    # derived from point-in-time bars and the current quote only; no future
    # observations or fitted parameters are introduced here.
    momentum_20, momentum_60 = _return(valid, 20), _return(valid, 60)
    if momentum_20 is not None:
        values["momentum_20"] = max(-1.0, min(1.0, tanh(momentum_20 / 0.12)))
    if momentum_60 is not None:
        values["momentum_60"] = max(-1.0, min(1.0, tanh(momentum_60 / 0.25)))
    volatility = _daily_volatility(valid)
    if volatility is not None:
        values["volatility_score"] = max(-1.0, min(1.0, tanh((0.035 - volatility) / 0.025)))
    if len(valid) >= 21 and valid[-1].volume > 0:
        average_volume = sum(item.volume for item in valid[-21:-1]) / 20.0
        if average_volume > 0:
            # Keep the model's 20-session baseline; partial-day scaling follows
            # the quote timestamp, never the date/time the report is reopened.
            progress = 1.0
            as_of = quote.provenance.as_of if quote and quote.provenance else None
            if as_of and as_of.astimezone(SHANGHAI).date() == valid[-1].trading_date:
                progress = trading_progress(as_of)
            volume_ratio = projected_volume_ratio(valid[-1].volume, average_volume, progress)
            price_change = valid[-1].close / valid[-1].previous_close - 1.0 if valid[-1].previous_close else 0.0
            if volume_ratio is not None:
                values["volume_price_signal"] = max(-1.0, min(1.0, tanh(price_change / 0.04) * min(1.0, volume_ratio / 2.0)))
    if quote and quote.pe_ttm is not None:
        if quote.pe_ttm > 0:
            values["valuation_signal"] = max(-1.0, min(1.0, tanh((28.0 - quote.pe_ttm) / 24.0)))
        else:
            values["valuation_signal"] = -0.35

    return FeatureResult(values, tuple(warnings))
