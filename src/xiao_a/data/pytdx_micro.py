"""通达信 pytdx 分时 / 分笔 / 五档微观行情，补东财分时缺口。

休市日：历史分时、历史分笔可用；当日 quotes / bars / 当日分时不可信，降级并标注。
开盘后实时报价与 bars 待周一复测——接口已写好，空结果不抛错。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from threading import Lock

from xiao_a.data.http import tcp_reachable
from xiao_a.data.symbols import is_market_index, normalize_symbol
from xiao_a.data.volume import SHANGHAI
from xiao_a.models import Provenance

# 优先用财财实测可通的杭州/上海，再回落到常见主站。
_DEFAULT_SERVERS: tuple[tuple[str, int], ...] = (
    ("60.191.117.167", 7709),  # 杭州电信主站 J1
    ("180.153.18.170", 7709),  # 上海电信主站 Z1
    ("115.238.90.165", 7709),  # 杭州备用
    ("180.153.18.171", 7709),  # 上海备用
    ("119.147.212.81", 7709),
    ("114.80.63.12", 7709),
)

_BUYORSELL_LABEL = {
    0: "买",
    1: "卖",
    2: "平",
}


def _market_id(exchange: str) -> int:
    return 1 if exchange == "SH" else 0


def _session_times() -> list[str]:
    """A 股常规连续竞价 240 分钟对应的 HH:MM 标签（不含集合竞价）。"""
    times: list[str] = []
    for hour, start, end in ((9, 31, 60), (10, 0, 60), (11, 0, 31)):
        for minute in range(start, end):
            times.append(f"{hour:02d}:{minute:02d}")
    for hour, start, end in ((13, 1, 60), (14, 0, 60), (15, 0, 1)):
        for minute in range(start, end):
            times.append(f"{hour:02d}:{minute:02d}")
    return times


_SESSION_TIMES = _session_times()


def _candidate_trade_dates(*, lookback: int = 10) -> list[date]:
    today = datetime.now(SHANGHAI).date()
    days: list[date] = []
    for offset in range(lookback):
        day = today - timedelta(days=offset)
        if day.weekday() < 5:  # 周一至周五；是否交易日由接口有无数据决定
            days.append(day)
    return days


def summarize_minute_rows(rows: list, *, trade_date: date) -> dict:
    """把 pytdx 历史分时点压成分析结论用的摘要（不全量回传）。"""
    prices = [float(row.get("price") or 0) for row in rows if row.get("price") is not None]
    vols = [float(row.get("vol") or 0) for row in rows]
    if not prices:
        raise ValueError("empty minute series")
    open_price = prices[0]
    last_price = prices[-1]
    high = max(prices)
    low = min(prices)
    total_vol = sum(vols)
    mid = len(prices) // 2
    morning_last = prices[mid - 1] if mid else last_price
    change_pct = None
    if open_price:
        change_pct = (last_price - open_price) / open_price * 100.0
    sample = []
    for idx in (0, mid // 2, mid, mid + mid // 2, len(prices) - 1):
        if 0 <= idx < len(prices):
            label = _SESSION_TIMES[idx] if idx < len(_SESSION_TIMES) else f"#{idx}"
            sample.append({"time": label, "price": prices[idx], "vol": vols[idx] if idx < len(vols) else None})
    return {
        "trade_date": trade_date.isoformat(),
        "points": len(prices),
        "open": open_price,
        "last": last_price,
        "high": high,
        "low": low,
        "total_vol": total_vol,
        "morning_last": morning_last,
        "change_pct_vs_open": change_pct,
        "sample": sample,
    }


def summarize_ticks(rows: list, *, trade_date: date, limit: int = 8) -> dict:
    if not rows:
        raise ValueError("empty transaction series")
    buy_vol = sell_vol = other_vol = 0.0
    for row in rows:
        vol = float(row.get("vol") or 0)
        raw_side = row.get("buyorsell")
        side = int(raw_side) if raw_side is not None else -1
        if side == 0:
            buy_vol += vol
        elif side == 1:
            sell_vol += vol
        else:
            other_vol += vol
    recent = []
    for row in rows[-limit:]:
        raw_side = row.get("buyorsell")
        side = int(raw_side) if raw_side is not None else -1
        recent.append({
            "time": str(row.get("time") or ""),
            "price": float(row.get("price") or 0),
            "vol": float(row.get("vol") or 0),
            "side": _BUYORSELL_LABEL.get(side, f"其他({side})"),
        })
    return {
        "trade_date": trade_date.isoformat(),
        "count": len(rows),
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "other_vol": other_vol,
        "recent": recent,
    }


def summarize_order_book(quote_row: dict) -> dict | None:
    if not quote_row:
        return None
    levels = []
    for i in range(1, 6):
        bid = quote_row.get(f"bid{i}")
        ask = quote_row.get(f"ask{i}")
        if bid is None and ask is None:
            continue
        levels.append({
            "level": i,
            "bid": float(bid) if bid is not None else None,
            "bid_vol": float(quote_row.get(f"bid_vol{i}") or 0) or None,
            "ask": float(ask) if ask is not None else None,
            "ask_vol": float(quote_row.get(f"ask_vol{i}") or 0) or None,
        })
    if not levels or all(item.get("bid") in (None, 0.0) and item.get("ask") in (None, 0.0) for item in levels):
        return None
    return {"levels": levels, "price": float(quote_row.get("price") or 0) or None}


class PytdxMicroProvider:
    """Fetch minute / tick / order-book summaries via pytdx with fast skip."""

    name = "pytdx-micro"

    def __init__(
        self,
        *,
        servers: tuple[tuple[str, int], ...] | None = None,
        connect_timeout: float = 3.0,
        probe_timeout: float = 2.0,
    ):
        self.servers = servers or _DEFAULT_SERVERS
        self.connect_timeout = connect_timeout
        self.probe_timeout = probe_timeout
        self._server_lock = Lock()
        self._preferred: tuple[str, int] | None = None

    def _connect(self):
        from pytdx.hq import TdxHq_API

        last_error: Exception | None = None
        ordered: list[tuple[str, int]] = []
        with self._server_lock:
            if self._preferred is not None:
                ordered.append(self._preferred)
        for host, port in self.servers:
            if (host, port) not in ordered:
                ordered.append((host, port))

        for host, port in ordered:
            if not tcp_reachable(host, port, timeout=self.probe_timeout):
                last_error = ConnectionError(f"pytdx server {host}:{port} unreachable (fast skip)")
                continue
            api = TdxHq_API(multithread=False, raise_exception=False)
            try:
                ok = api.connect(host, port, time_out=self.connect_timeout)
                if not ok:
                    raise ConnectionError(f"pytdx connection failed at {host}:{port}")
                with self._server_lock:
                    self._preferred = (host, port)
                return api, host, port
            except Exception as exc:
                last_error = exc
                try:
                    api.disconnect()
                except Exception:
                    pass
        raise RuntimeError(f"pytdx micro servers unavailable: {last_error}")

    def fetch_history_minutes(self, symbol: str, trade_date: date) -> list[dict]:
        normalized = normalize_symbol(symbol)
        market = _market_id(normalized.exchange)
        date_int = int(trade_date.strftime("%Y%m%d"))
        api, _, _ = self._connect()
        try:
            rows = api.get_history_minute_time_data(market, normalized.code, date_int)
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
        return [dict(row) for row in (rows or [])]

    def fetch(self, symbol: str) -> tuple[dict, Provenance]:
        if is_market_index(symbol):
            raise ValueError("指数不适用个股微观行情")
        normalized = normalize_symbol(symbol)
        market = _market_id(normalized.exchange)
        api, host, port = self._connect()
        source_url = f"tcp://{host}:{port}"
        warnings: list[str] = []
        minute_summary = None
        tick_summary = None
        order_book = None
        used_date: date | None = None
        mode = "history"
        try:
            for day in _candidate_trade_dates():
                date_int = int(day.strftime("%Y%m%d"))
                try:
                    rows = api.get_history_minute_time_data(market, normalized.code, date_int)
                except Exception as exc:
                    warnings.append(f"history minute {day}: {type(exc).__name__}")
                    continue
                if not rows:
                    continue
                minute_summary = summarize_minute_rows(rows, trade_date=day)
                used_date = day
                try:
                    ticks = api.get_history_transaction_data(market, normalized.code, 0, 200, date_int)
                    if ticks:
                        tick_summary = summarize_ticks(ticks, trade_date=day)
                except Exception as exc:
                    warnings.append(f"history ticks: {type(exc).__name__}: {exc}")
                break

            # 实时五档 / 当日分时：开盘可用时填入；休市空结果则标注降级，不抛错。
            try:
                quotes = api.get_security_quotes([(market, normalized.code)])
                if quotes:
                    order_book = summarize_order_book(dict(quotes[0]))
                    if order_book:
                        mode = "live+history" if minute_summary else "live"
                else:
                    warnings.append("get_security_quotes 空（休市或未开盘时正常，周一开盘复测）")
            except Exception as exc:
                warnings.append(f"quotes: {type(exc).__name__}: {exc}")

            try:
                live_minute = api.get_minute_time_data(market, normalized.code)
                # 休市日可能返回乱价，仅在尚无历史摘要且价格看起来合理时采用。
                if live_minute and minute_summary is None:
                    prices = [float(r.get("price") or 0) for r in live_minute]
                    if prices and min(prices) > 0 and max(prices) / max(min(prices), 1e-6) < 5:
                        minute_summary = summarize_minute_rows(live_minute, trade_date=datetime.now(SHANGHAI).date())
                        used_date = datetime.now(SHANGHAI).date()
                        mode = "live"
                    else:
                        warnings.append("当日分时价格异常，已跳过（休市不可信）")
            except Exception as exc:
                warnings.append(f"live minute: {type(exc).__name__}: {exc}")
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

        if minute_summary is None and tick_summary is None and order_book is None:
            raise ValueError("pytdx micro: no usable minute/tick/order-book data")

        result = {
            "available": True,
            "source": "pytdx",
            "source_label": (
                "通达信(pytdx) · 分时/分笔为交易所行情镜像；"
                "五档仅开盘有效；休市用最近交易日历史数据"
            ),
            "source_url": source_url,
            "mode": mode,
            "trade_date": (used_date or datetime.now(SHANGHAI).date()).isoformat(),
            "minute": minute_summary,
            "ticks": tick_summary,
            "order_book": order_book,
            "warnings": warnings,
            "note": "实时报价与分钟K待交易时段复测；当前以历史分时/分笔为主",
        }
        as_of = None
        if used_date is not None:
            as_of = datetime(used_date.year, used_date.month, used_date.day, 15, 0, tzinfo=SHANGHAI)
        provenance = Provenance("pytdx-micro", source_url, datetime.now(SHANGHAI).astimezone(), as_of)
        return result, provenance

    def fetch_chart_minutes(self, symbol: str) -> tuple[list[dict], str | None, str]:
        """Return chart-ready minute points from the latest available history day."""
        rows: list = []
        used_day = None
        host_label = ""
        for day in _candidate_trade_dates():
            try:
                api, host, port = self._connect()
                host_label = f"tcp://{host}:{port}"
                try:
                    normalized = normalize_symbol(symbol)
                    market = _market_id(normalized.exchange)
                    date_int = int(day.strftime("%Y%m%d"))
                    raw = api.get_history_minute_time_data(market, normalized.code, date_int)
                finally:
                    try:
                        api.disconnect()
                    except Exception:
                        pass
                if raw:
                    rows = [dict(item) for item in raw]
                    used_day = day
                    break
            except Exception:
                continue
        points: list[dict] = []
        for idx, row in enumerate(rows):
            try:
                price = float(row.get("price") or 0)
                vol = float(row.get("vol") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            label = _SESSION_TIMES[idx] if idx < len(_SESSION_TIMES) else f"{idx}"
            stamp = f"{used_day.isoformat()} {label}" if used_day else label
            points.append({"time": stamp, "price": price, "average": None, "volume": vol, "amount": None})
        return points, (used_day.isoformat() if used_day else None), host_label
