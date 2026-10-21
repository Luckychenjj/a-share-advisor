from __future__ import annotations

from dataclasses import dataclass, field

from xiao_a.models import DataStatus, SignalAction


@dataclass(frozen=True)
class RiskPolicy:
    max_single_position_pct: float = 0.25
    hard_position_pct: float = 0.40
    max_portfolio_drawdown_pct: float = 0.12
    minimum_daily_amount: float = 30_000_000.0
    allow_st: bool = False
    stale_confidence_cap: float = 0.55


@dataclass
class RiskContext:
    data_status: DataStatus = DataStatus.FRESH
    total_position_pct: float = 0.0
    position_pct: float = 0.0
    portfolio_drawdown_pct: float = 0.0
    daily_amount: float | None = None
    cash_available: float | None = None
    is_st: bool = False
    suspended: bool = False
    limit_up_locked: bool = False
    limit_down_locked: bool = False
    high_severity_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RiskResult:
    action: SignalAction
    confidence: float
    executable: bool
    warnings: tuple[str, ...]
    vetoes: tuple[str, ...]


class RiskEngine:
    def __init__(self, policy: RiskPolicy | None = None):
        self.policy = policy or RiskPolicy()

    def apply(self, action: SignalAction, confidence: float, context: RiskContext) -> RiskResult:
        warnings: list[str] = []
        vetoes: list[str] = []
        executable = True

        if context.data_status in {DataStatus.UNAVAILABLE, DataStatus.INVALID}:
            vetoes.append("核心行情数据不可用或无效")
            action = SignalAction.HOLD
            executable = False
            confidence = min(confidence, 0.20)
        elif context.data_status in {DataStatus.STALE, DataStatus.PARTIAL}:
            warnings.append("核心行情数据陈旧或不完整")
            confidence = min(confidence, self.policy.stale_confidence_cap)

        if context.suspended:
            vetoes.append("股票处于停牌状态")
            action = SignalAction.HOLD
            executable = False
        if context.is_st and not self.policy.allow_st and action == SignalAction.BUY:
            vetoes.append("风控规则禁止买入ST股票")
            action = SignalAction.HOLD
        if context.high_severity_flags and action == SignalAction.BUY:
            vetoes.extend(context.high_severity_flags)
            action = SignalAction.HOLD
        if context.position_pct >= self.policy.hard_position_pct:
            warnings.append("单股仓位超过硬性集中度阈值")
            if action in {SignalAction.BUY, SignalAction.HOLD}:
                action = SignalAction.REDUCE
        elif context.position_pct >= self.policy.max_single_position_pct and action == SignalAction.BUY:
            vetoes.append("单股仓位上限禁止继续加仓")
            action = SignalAction.HOLD
        if context.portfolio_drawdown_pct >= self.policy.max_portfolio_drawdown_pct:
            warnings.append("账户回撤熔断已触发")
            if action == SignalAction.BUY:
                action = SignalAction.HOLD
        if context.daily_amount is not None and context.daily_amount < self.policy.minimum_daily_amount:
            warnings.append("流动性低于风控阈值")
            if action == SignalAction.BUY:
                action = SignalAction.HOLD
        if context.limit_up_locked and action == SignalAction.BUY:
            warnings.append("涨停封死，保守假设下无法成交")
            action = SignalAction.HOLD
            executable = False
        if context.limit_down_locked and action in {SignalAction.REDUCE, SignalAction.SELL}:
            warnings.append("跌停封死，可能无法卖出")
            executable = False

        return RiskResult(action, max(0.0, min(1.0, confidence)), executable, tuple(warnings), tuple(vetoes))
