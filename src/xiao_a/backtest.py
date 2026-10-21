from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import math

from xiao_a.models import DailyBar, SignalAction


def moving_average_crossover_signals(
    bars: list[DailyBar], *, fast_period: int = 5, slow_period: int = 20
) -> dict[date, SignalAction]:
    """Create close-of-day SMA crossover signals, executed on the next open."""
    if fast_period < 2 or slow_period <= fast_period:
        raise ValueError("slow_period must be greater than fast_period >= 2")
    closes = [bar.close for bar in bars]
    signals: dict[date, SignalAction] = {}
    previous_spread: float | None = None
    for index, bar in enumerate(bars):
        if index + 1 < slow_period:
            continue
        fast = sum(closes[index - fast_period + 1 : index + 1]) / fast_period
        slow = sum(closes[index - slow_period + 1 : index + 1]) / slow_period
        spread = fast - slow
        if previous_spread is not None:
            if previous_spread <= 0 < spread:
                signals[bar.trading_date] = SignalAction.BUY
            elif previous_spread >= 0 > spread:
                signals[bar.trading_date] = SignalAction.SELL
        previous_spread = spread
    return signals


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class FeeSchedule:
    """Configurable example values; set them for the broker and test period."""

    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_duty_sell: float = 0.0005
    transfer_fee_rate: float = 0.00001

    def calculate(self, side: TradeSide, notional: float) -> float:
        commission = max(self.minimum_commission, notional * self.commission_rate)
        stamp = notional * self.stamp_duty_sell if side == TradeSide.SELL else 0.0
        return commission + stamp + notional * self.transfer_fee_rate


@dataclass
class Position:
    quantity: int = 0
    available_quantity: int = 0
    average_cost: float = 0.0


@dataclass(frozen=True)
class Trade:
    trading_date: date
    side: TradeSide
    quantity: int
    price: float
    fees: float
    reason: str


@dataclass
class BacktestResult:
    initial_cash: float
    final_equity: float
    total_return: float
    max_drawdown: float
    trades: list[Trade] = field(default_factory=list)
    rejected_orders: list[str] = field(default_factory=list)
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    ending_cash: float = 0.0
    ending_quantity: int = 0


class AShareBacktester:
    """Small event-driven core for one symbol.

    Signals generated on bar T are filled at bar T+1 open. Purchases become
    sellable on the next trading day. Locked one-price limit bars reject fills.
    """

    def __init__(
        self,
        *,
        initial_cash: float,
        fees: FeeSchedule | None = None,
        slippage_bps: float = 5.0,
        lot_size: int = 100,
    ):
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        if not math.isfinite(slippage_bps) or slippage_bps < 0:
            raise ValueError("slippage_bps must be finite and non-negative")
        if lot_size <= 0:
            raise ValueError("lot_size must be positive")
        self.initial_cash = initial_cash
        self.fees = fees or FeeSchedule()
        self.slippage_bps = slippage_bps
        self.lot_size = lot_size

    @staticmethod
    def _locked_limit(bar: DailyBar, side: TradeSide) -> bool:
        if bar.previous_close is None or bar.previous_close <= 0:
            return False
        rate = bar.limit_rate if bar.limit_rate is not None else (0.05 if bar.is_st else 0.10)
        if rate <= 0:
            return False
        limit = round(bar.previous_close * (1.0 + rate if side == TradeSide.BUY else 1.0 - rate), 2)
        return bar.open == bar.high == bar.low and (
            bar.open >= limit if side == TradeSide.BUY else bar.open <= limit
        )

    def run(
        self,
        bars: list[DailyBar],
        signals: dict[date, SignalAction],
        *,
        target_position_pct: float = 0.95,
    ) -> BacktestResult:
        if not bars:
            raise ValueError("bars cannot be empty")
        cash = self.initial_cash
        position = Position()
        trades: list[Trade] = []
        rejected: list[str] = []
        curve: list[tuple[date, float]] = []
        pending: SignalAction | None = None
        peak = self.initial_cash
        max_drawdown = 0.0

        for bar in bars:
            position.available_quantity = position.quantity
            if pending is not None:
                if bar.suspended:
                    rejected.append(f"{bar.trading_date}: suspended")
                elif pending == SignalAction.BUY:
                    if self._locked_limit(bar, TradeSide.BUY):
                        rejected.append(f"{bar.trading_date}: locked limit-up")
                    else:
                        price = bar.open * (1.0 + self.slippage_bps / 10_000.0)
                        budget = cash * max(0.0, min(1.0, target_position_pct))
                        quantity = int(budget / price / self.lot_size) * self.lot_size
                        while quantity > 0:
                            notional = quantity * price
                            fee = self.fees.calculate(TradeSide.BUY, notional)
                            if notional + fee <= cash:
                                break
                            quantity -= self.lot_size
                        if quantity > 0:
                            notional = quantity * price
                            fee = self.fees.calculate(TradeSide.BUY, notional)
                            old_cost = position.average_cost * position.quantity
                            position.quantity += quantity
                            position.average_cost = (old_cost + notional + fee) / position.quantity
                            position.available_quantity = 0  # T+1
                            cash -= notional + fee
                            trades.append(Trade(bar.trading_date, TradeSide.BUY, quantity, price, fee, "signal"))
                elif pending in {SignalAction.REDUCE, SignalAction.SELL} and position.available_quantity > 0:
                    if self._locked_limit(bar, TradeSide.SELL):
                        rejected.append(f"{bar.trading_date}: locked limit-down")
                    else:
                        ratio = 0.5 if pending == SignalAction.REDUCE else 1.0
                        quantity = int(position.available_quantity * ratio / self.lot_size) * self.lot_size
                        if pending == SignalAction.SELL and quantity == 0:
                            quantity = position.available_quantity
                        if quantity > 0:
                            price = bar.open * (1.0 - self.slippage_bps / 10_000.0)
                            notional = quantity * price
                            fee = self.fees.calculate(TradeSide.SELL, notional)
                            cash += notional - fee
                            position.quantity -= quantity
                            position.available_quantity -= quantity
                            if position.quantity == 0:
                                position.average_cost = 0.0
                            trades.append(Trade(bar.trading_date, TradeSide.SELL, quantity, price, fee, "signal"))
                elif pending in {SignalAction.REDUCE, SignalAction.SELL}:
                    # A bearish view without a sellable long position maps to
                    # no-buy/cash; it never opens a short position.
                    rejected.append(f"{bar.trading_date}: no sellable long position; short sale prohibited")
            pending = signals.get(bar.trading_date)
            equity = cash + position.quantity * bar.close
            if position.quantity < 0 or position.available_quantity < 0 or position.available_quantity > position.quantity:
                raise AssertionError("long-only position invariant violated")
            if cash < -1e-8:
                raise AssertionError("cash cannot be negative")
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0.0)
            curve.append((bar.trading_date, equity))

        final_equity = curve[-1][1]
        return BacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            total_return=final_equity / self.initial_cash - 1.0,
            max_drawdown=max_drawdown,
            trades=trades,
            rejected_orders=rejected,
            equity_curve=curve,
            ending_cash=cash,
            ending_quantity=position.quantity,
        )
