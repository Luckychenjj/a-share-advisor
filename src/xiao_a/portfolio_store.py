from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class PortfolioStore:
    """Small local, server-side store shared by every dashboard browser."""

    def __init__(self, path: str | Path | None = None) -> None:
        default_path = Path(__file__).resolve().parents[2] / "data" / "portfolio.json"
        self.path = Path(path or os.environ.get("XIAO_A_PORTFOLIO_FILE", default_path))
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            if not isinstance(state, dict) or not isinstance(state.get("holdings"), list):
                return None
            return state

    def save(self, holdings: list[dict[str, Any]], cash: float) -> dict[str, Any]:
        with self._lock:
            previous = self.load() or {}
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            day_open = previous.get("day_open")
            if not isinstance(day_open, dict) or day_open.get("date") != today:
                day_open = {
                    "date": today,
                    "holdings": previous.get("holdings", []),
                    "cash": max(0.0, float(previous.get("cash") or 0.0)),
                }
            state = {
                "version": int(previous.get("version", 0)) + 1,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "holdings": [self._normalize_holding(item) for item in holdings],
                "cash": max(0.0, float(cash)),
                "day_open": day_open,
                "transactions": list(previous.get("transactions") or [])[-200:],
                "equity_history": list(previous.get("equity_history") or [])[-500:],
            }
            if previous.get("cash_adjustments"):
                state["cash_adjustments"] = list(previous.get("cash_adjustments") or [])[-200:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            return state

    def record_equity_snapshot(self, trading_date: str, equity: float) -> dict[str, Any] | None:
        """Upsert a valid, locally observed end-of-day account valuation."""
        try:
            parsed_date = datetime.strptime(str(trading_date), "%Y-%m-%d").date().isoformat()
            equity = float(equity)
        except (TypeError, ValueError):
            return None
        if equity <= 0 or not math.isfinite(equity):
            return None
        with self._lock:
            state = self.load()
            if state is None:
                return None
            history = [item for item in state.get("equity_history", []) if isinstance(item, dict)]
            snapshot = {"date": parsed_date, "equity": round(equity, 2)}
            by_date = {str(item.get("date")): item for item in history if item.get("date")}
            by_date[parsed_date] = snapshot
            state["equity_history"] = [by_date[key] for key in sorted(by_date)][-500:]
            state["equity_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._write(state)
            return state

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def ensure_day_open(self) -> dict[str, Any] | None:
        """Create a once-per-day baseline before live P&L is calculated."""
        with self._lock:
            current = self.load()
            if not current:
                return None
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            day_open = current.get("day_open")
            if isinstance(day_open, dict) and day_open.get("date") == today:
                return current
            return self.save(current.get("holdings", []), current.get("cash", 0.0))

    # ---- account fee defaults -------------------------------------------------
    # Legacy defaults (used when no account_fees.json exists) keep the exact
    # behaviour of earlier versions: commission rate from the request (else 0),
    # minimum commission 5, transfer fee 0.001%, sell-side stamp duty 0.05%.
    LEGACY_FEE_DEFAULTS: dict[str, Any] = {
        "commission_rate": 0.0,
        "minimum_commission": 5.0,
        "free_five": False,
        "transfer_fee_rate": 0.00001,
        "stamp_tax_rate": 0.0005,
        "fund_no_stamp_or_transfer": False,
    }

    @property
    def fee_config_path(self) -> Path:
        return self.path.parent / "account_fees.json"

    @property
    def cash_audit_path(self) -> Path:
        return self.path.parent / "cash_audit.jsonl"

    def fee_config(self) -> dict[str, Any]:
        config = dict(self.LEGACY_FEE_DEFAULTS)
        try:
            stored = json.loads(self.fee_config_path.read_text(encoding="utf-8-sig"))
            if isinstance(stored, dict):
                config.update({key: stored[key] for key in stored})
        except (OSError, ValueError, TypeError):
            config["source"] = "legacy_defaults"
        else:
            config.setdefault("source", str(self.fee_config_path.name))
        if config.get("free_five"):
            config["minimum_commission"] = 0.0
        return config

    def save_fee_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"commission_rate", "minimum_commission", "free_five", "transfer_fee_rate",
                   "stamp_tax_rate", "fund_no_stamp_or_transfer", "note"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"不支持的费率字段：{sorted(unknown)}")
        with self._lock:
            current = self.fee_config()
            current.pop("source", None)
            for key in ("commission_rate", "minimum_commission", "transfer_fee_rate", "stamp_tax_rate"):
                if key in payload:
                    value = float(payload[key])
                    if value < 0 or not math.isfinite(value) or (key.endswith("_rate") and value > 0.01):
                        raise ValueError(f"{key} 数值不合理：{payload[key]}")
                    current[key] = value
            for key in ("free_five", "fund_no_stamp_or_transfer"):
                if key in payload:
                    current[key] = bool(payload[key])
            if "note" in payload:
                current["note"] = str(payload["note"])[:500]
            if current.get("free_five"):
                current["minimum_commission"] = 0.0
            current["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.fee_config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.fee_config_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.fee_config_path)
            return self.fee_config()

    @staticmethod
    def is_fund(symbol: str) -> bool:
        """Exchange-traded funds (ETF/LOF): no stamp duty, no transfer fee."""
        code = PortfolioStore._bare_symbol(symbol)
        return len(code) == 6 and code[:2] in {"15", "16", "18", "50", "51", "52", "56", "58"}

    @staticmethod
    def parse_trade_time(value: Any) -> datetime | None:
        """Parse an ISO timestamp or 'YYYY-MM-DD HH:MM[:SS]'; naive = Asia/Shanghai."""
        if value in {None, ""}:
            return None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            when = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
                try:
                    when = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if when is None:
                raise ValueError(f"成交时间格式不正确：{value}（用 ISO 或 YYYY-MM-DD HH:MM:SS，北京时间）")
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        now = datetime.now(timezone.utc)
        if when.astimezone(timezone.utc) > now + timedelta(minutes=5):
            raise ValueError("成交时间不能晚于当前时间")
        return when

    def adjust_cash(
        self,
        *,
        note: str,
        cash: Any = None,
        amount: Any = None,
        source: str = "api",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Set (``cash``) or shift (``amount``) ledger cash with an audit record."""
        note = str(note or "").strip()
        if not note:
            raise ValueError("现金调整必须填写备注 note")
        if (cash is None) == (amount is None):
            raise ValueError("cash（目标现金）和 amount（调整额）必须二选一")
        with self._lock:
            previous = self.load()
            if previous is None:
                raise ValueError("台账不存在，无法调整现金")
            before = max(0.0, float(previous.get("cash") or 0.0))
            if cash is not None:
                after = float(cash)
                mode = "set"
            else:
                after = before + float(amount)
                mode = "delta"
            if not math.isfinite(after):
                raise ValueError("现金数值不合理")
            if after < -1e-9:
                raise ValueError(f"调整后现金为负（{after:.2f}），已拒绝")
            after = max(0.0, after)
            entry = {
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mode": mode,
                "before": round(before, 6),
                "after": round(after, 6),
                "delta": round(after - before, 6),
                "note": note[:500],
                "source": str(source or "api")[:100],
                "version_before": previous.get("version"),
            }
            extra = {key: previous[key] for key in ("broker_sellable_snapshot", "equity_updated_at") if key in previous}
            state = self.save(previous.get("holdings", []), after)
            state.update(extra)
            entry["version_after"] = state["version"]
            state["cash_adjustments"] = (list(previous.get("cash_adjustments") or []) + [entry])[-200:]
            self._write(state)
            with self.cash_audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return state, entry

    def apply_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        commission_rate: float | None = None,
        *,
        stamp_tax_rate: float | None = None,
        transfer_fee_rate: float | None = None,
        minimum_commission: float | None = None,
        fee: float | None = None,
        traded_at: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply one A-share trade and return the new state plus fee details.

        Fee parameters left as None come from the account fee config
        (account_fees.json, else the legacy defaults). ``fee`` is the broker's
        actual total fee and overrides the calculation. ``traded_at`` is the fill
        time (naive = Asia/Shanghai); entry time is used when omitted.
        """
        symbol = str(symbol).strip().upper()
        side = str(side).strip().lower()
        quantity = int(quantity)
        price = float(price)
        config = self.fee_config()
        fund = bool(config.get("fund_no_stamp_or_transfer")) and self.is_fund(symbol)
        commission_rate = float(config["commission_rate"] if commission_rate is None else commission_rate)
        minimum_commission = float(config["minimum_commission"] if minimum_commission is None else minimum_commission)
        if stamp_tax_rate is None:
            stamp_tax_rate = 0.0 if fund else float(config["stamp_tax_rate"])
        if transfer_fee_rate is None:
            transfer_fee_rate = 0.0 if fund else float(config["transfer_fee_rate"])
        stamp_tax_rate = float(stamp_tax_rate)
        transfer_fee_rate = float(transfer_fee_rate)
        explicit_fee = None if fee in {None, ""} else float(fee)
        if explicit_fee is not None and (explicit_fee < 0 or not math.isfinite(explicit_fee)):
            raise ValueError("实际费用 fee 不能为负数")
        fill_time = self.parse_trade_time(traded_at)
        if not symbol or side not in {"buy", "sell"}:
            raise ValueError("交易方向或股票代码不正确")
        if quantity <= 0:
            raise ValueError("交易数量必须大于0")
        if price <= 0:
            raise ValueError("成交单价必须大于0")
        if commission_rate < 0:
            raise ValueError("佣金比例不能为负数")

        with self._lock:
            previous = self.load() or {"holdings": [], "cash": 0.0}
            holdings = [self._normalize_holding(item) for item in previous.get("holdings", [])]
            cash = max(0.0, float(previous.get("cash") or 0.0))
            index = next((i for i, item in enumerate(holdings) if item["symbol"].upper() == symbol), None)
            existing = holdings[index] if index is not None else {"symbol": symbol, "quantity": 0, "average_cost": None}
            old_quantity = int(existing["quantity"])
            if side == "buy" and quantity % 100 != 0:
                raise ValueError("买入数量必须是100股的整数倍")
            if side == "sell" and quantity % 100 != 0 and quantity != old_quantity:
                raise ValueError("卖出数量须为100股整数倍；卖出全部剩余持仓时可使用实际剩余股数")
            old_cost = existing.get("average_cost")
            amount = quantity * price
            commission = max(amount * commission_rate, minimum_commission)
            stamp_tax = amount * stamp_tax_rate if side == "sell" else 0.0
            transfer_fee = amount * transfer_fee_rate
            total_fee = commission + stamp_tax + transfer_fee
            fee_source = "calculated"
            if explicit_fee is not None:
                fee_source = "explicit"
                total_fee = explicit_fee
                commission = explicit_fee - stamp_tax - transfer_fee
                if commission < -1e-9:
                    # Breakdown unknown; keep only the broker's total.
                    commission, stamp_tax, transfer_fee = explicit_fee, 0.0, 0.0
                commission = max(0.0, commission)
            realized_profit = None
            if side == "buy":
                total_cash = amount + total_fee
                if total_cash > cash + 1e-9:
                    raise ValueError(f"可用现金不足，还需要{total_cash - cash:.2f}元")
                if old_cost is not None and old_quantity > 0:
                    new_cost = (old_quantity * float(old_cost) + amount + total_fee) / (old_quantity + quantity)
                else:
                    new_cost = (amount + total_fee) / quantity
                updated = {"symbol": symbol, "quantity": old_quantity + quantity, "average_cost": new_cost}
                cash -= total_cash
            else:
                if index is None or old_quantity < quantity:
                    raise ValueError("卖出数量不能超过当前持仓")
                sellable = self.sellable_quantity(symbol, state=previous)
                if sellable is None:
                    raise ValueError("无法核实T+1可卖数量，暂不记录卖出")
                if quantity > sellable:
                    raise ValueError(f"卖出数量超过T+1可卖数量：当前最多可卖{sellable}股")
                if old_cost is not None:
                    realized_profit = amount - total_fee - quantity * float(old_cost)
                remaining_quantity = old_quantity - quantity
                if remaining_quantity > 0 and old_cost is not None:
                    # Use the broker-style diluted cost: sale proceeds (after
                    # fees) reduce the total cost pool. A sale below cost
                    # therefore raises the cost of the shares that remain,
                    # instead of silently discarding the realized loss.
                    remaining_cost_pool = old_quantity * float(old_cost) - (amount - total_fee)
                    remaining_cost = remaining_cost_pool / remaining_quantity
                else:
                    remaining_cost = old_cost
                updated = {"symbol": symbol, "quantity": remaining_quantity, "average_cost": remaining_cost}
                cash += amount - total_fee

            if updated["quantity"] > 0:
                if index is None:
                    holdings.append(updated)
                else:
                    holdings[index] = updated
            elif index is not None:
                holdings.pop(index)

            entry_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
            transaction = {
                "time": fill_time.astimezone(timezone.utc).isoformat(timespec="seconds") if fill_time else entry_time,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "amount": amount,
                "commission": commission,
                "stamp_tax": stamp_tax,
                "transfer_fee": transfer_fee,
                "total_fee": total_fee,
                "cash_delta": -amount - total_fee if side == "buy" else amount - total_fee,
                "realized_profit": realized_profit,
                "fee_source": fee_source,
                "time_source": "traded_at" if fill_time else "entry",
                "recorded_at": entry_time,
            }
            transactions = list(previous.get("transactions") or [])
            transactions.append(transaction)
            state = self.save(holdings, cash)
            state["transactions"] = transactions[-200:]
            # Preserve the transaction ledger when save() rewrites the file.
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            return state, transaction

    def sellable_quantity(self, symbol: str, *, state: dict[str, Any] | None = None) -> int | None:
        """Conservatively estimate shares carried from the prior session.

        Same-day purchases are excluded. Missing holdings baselines or malformed
        transaction history return unknown so a sell cannot be assumed valid.
        """
        current = state if state is not None else self.load()
        if not isinstance(current, dict) or not isinstance(current.get("holdings"), list):
            return None
        bare = self._bare_symbol(symbol)
        holding = next((item for item in current["holdings"] if self._bare_symbol(item.get("symbol", "")) == bare), None)
        if holding is None:
            return 0
        quantity = max(0, int(holding.get("quantity") or 0))
        shanghai_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        confirmed = current.get("broker_sellable_snapshot", {})
        if confirmed.get("date") == shanghai_today.isoformat():
            verified = confirmed.get("quantities", {}).get(bare)
            if verified is not None:
                return max(0, min(quantity, int(verified)))
        transactions = current.get("transactions", [])
        if not isinstance(transactions, list):
            return None
        bought_today = 0
        sold_today = 0
        try:
            for transaction in transactions:
                if self._bare_symbol(transaction.get("symbol", "")) != bare:
                    continue
                when = datetime.fromisoformat(str(transaction["time"]).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when.astimezone(ZoneInfo("Asia/Shanghai")).date() != shanghai_today:
                    continue
                amount = int(transaction.get("quantity") or 0)
                if transaction.get("side") == "buy":
                    bought_today += amount
                elif transaction.get("side") == "sell":
                    sold_today += amount
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        day_open = current.get("day_open")
        if not isinstance(day_open, dict) or day_open.get("date") != shanghai_today.isoformat():
            # Legacy state can be reconstructed only when today's transactions
            # account for the position change; otherwise the T+1 lot is unknown.
            if not transactions and quantity > 0:
                return None
            return max(0, quantity - bought_today)
        opening = next((item for item in day_open.get("holdings", [])
                        if self._bare_symbol(item.get("symbol", "")) == bare), None)
        opening_quantity = int((opening or {}).get("quantity") or 0)
        return max(0, min(quantity - bought_today, opening_quantity - sold_today))

    def buys_on(self, symbol: str, session: str, *, state: dict[str, Any] | None = None) -> list[tuple[int, float]] | None:
        """Return (quantity, price) for each buy of ``symbol`` on ``session`` (Shanghai date).

        None means the transaction history is malformed and the split is unknown.
        """
        current = state if state is not None else self.load()
        if not isinstance(current, dict):
            return None
        transactions = current.get("transactions", [])
        if not isinstance(transactions, list):
            return None
        bare = self._bare_symbol(symbol)
        buys: list[tuple[int, float]] = []
        try:
            for transaction in transactions:
                if transaction.get("side") != "buy" or self._bare_symbol(transaction.get("symbol", "")) != bare:
                    continue
                when = datetime.fromisoformat(str(transaction["time"]).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != session:
                    continue
                quantity = int(transaction.get("quantity") or 0)
                price = float(transaction["price"])
                if quantity > 0 and price > 0:
                    buys.append((quantity, price))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return buys

    @staticmethod
    def _bare_symbol(value: Any) -> str:
        return str(value).strip().upper().replace("SH.", "").replace("SZ.", "").replace("BJ.", "")

    @staticmethod
    def _normalize_holding(item: dict[str, Any]) -> dict[str, Any]:
        symbol = str(item.get("symbol", "")).strip()
        quantity = max(0, int(float(item.get("quantity") or 0)))
        raw_cost = item.get("average_cost")
        average_cost = None if raw_cost in {None, ""} else max(0.0, float(raw_cost))
        return {"symbol": symbol, "quantity": quantity, "average_cost": average_cost}
