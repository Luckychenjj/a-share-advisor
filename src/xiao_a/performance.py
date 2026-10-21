"""Small, dependency-free portfolio performance statistics."""

from __future__ import annotations

from math import isfinite, sqrt
from statistics import stdev
from typing import Any, Iterable


def summarize_equity_history(snapshots: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize observed marked-to-market account equity snapshots.

    Daily observations may have gaps because snapshots are recorded only when
    the user opens the account with fresh quotes. Returns are therefore per
    observed interval; annualized figures are estimates, not broker statements.
    """
    by_date: dict[str, float] = {}
    for item in snapshots:
        try:
            day = str(item["date"])
            equity = float(item["equity"])
            if len(day) == 10 and isfinite(equity) and equity > 0:
                by_date[day] = equity
        except (KeyError, TypeError, ValueError):
            continue
    ordered = sorted(by_date.items())
    base: dict[str, Any] = {
        "available": len(ordered) >= 2,
        "observations": len(ordered),
        "first_date": ordered[0][0] if ordered else None,
        "last_date": ordered[-1][0] if ordered else None,
        "total_return": None,
        "max_drawdown": None,
        "annualized_volatility": None,
        "sharpe_ratio": None,
        "positive_interval_pct": None,
        "note": "基于本机记录的有效账户权益快照；若日期有缺口，波动率与夏普比率为估算值。",
    }
    if len(ordered) < 2:
        base["note"] = "有效记录不足：在至少两个不同交易日读取账户且行情数据有效后，开始计算收益与回撤。"
        return base

    values = [value for _, value in ordered]
    returns = [current / previous - 1.0 for previous, current in zip(values, values[1:])]
    base["total_return"] = values[-1] / values[0] - 1.0
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = max(drawdown, 1.0 - value / peak)
    base["max_drawdown"] = drawdown
    base["positive_interval_pct"] = sum(value > 0 for value in returns) / len(returns)
    if len(returns) >= 20:
        volatility = stdev(returns) * sqrt(252)
        mean = sum(returns) / len(returns)
        base["annualized_volatility"] = volatility
        base["sharpe_ratio"] = mean / stdev(returns) * sqrt(252) if stdev(returns) else 0.0
    return base
