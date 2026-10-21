"""Optional independent fallbacks for quote and historical data.

These adapters are deliberately optional: the dashboard remains usable when
the packages are not installed, while the routers retain the original error
and can move to the next provider.
"""

from datetime import datetime, timezone
from urllib.request import Request

from xiao_a.data.http import open_market_url
from xiao_a.data.kline import KlineResult, _default_limit_rate
from xiao_a.data.router import ProviderResult
from xiao_a.data.symbols import normalize_symbol
from xiao_a.data.volume import SHANGHAI
from xiao_a.models import DailyBar, DataStatus, Provenance, Quote


def _number(value):
    try:
        return None if value in {None, "", "-", "--"} else float(value)
    except (TypeError, ValueError):
        return None


class PytdxQuoteProvider:
    name = "pytdx-fallback"

    def __init__(self, *, priority: int = 30, servers: tuple[tuple[str, int], ...] | None = None):
        self.priority = priority
        self.servers = servers or (("119.147.212.81", 7709), ("114.80.63.12", 7709))

    def fetch_quote(self, symbol: str) -> ProviderResult:
        from pytdx.hq import TdxHq_API

        normalized = normalize_symbol(symbol)
        market = 1 if normalized.exchange == "SH" else 0
        last_error = None
        for host, port in self.servers:
            api = TdxHq_API()
            try:
                connected = api.connect(host, port, time_out=4)
                if not connected:
                    raise ConnectionError(f"pytdx connection failed at {host}:{port}")
                rows = api.get_security_quotes([(market, normalized.code)])
                if not rows:
                    raise ValueError("pytdx returned no quote")
                row = rows[0]
                now = datetime.now(timezone.utc)
                quote = Quote(
                    symbol=normalized.canonical,
                    name=str(row.get("name") or normalized.code),
                    price=_number(row.get("price")) or 0.0,
                    previous_close=_number(row.get("last_close")) or 0.0,
                    open=_number(row.get("open")),
                    high=_number(row.get("high")),
                    low=_number(row.get("low")),
                    volume=_number(row.get("vol")),
                    amount=_number(row.get("amount")),
                    turnover_pct=None,
                    status=DataStatus.FRESH,
                    # No exchange date in this adapter: retrieval time must not
                    # be mistaken for a trade timestamp during holidays.
                    provenance=Provenance("pytdx", f"tcp://{host}:{port}", now, None),
                )
                return ProviderResult(quote, DataStatus.FRESH, warnings=["using pytdx fallback; turnover and valuation fields may be unavailable"])
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    api.disconnect()
                except Exception:
                    pass
        raise RuntimeError(f"pytdx servers unavailable: {last_error}")


class SinaQuoteProvider:
    """Pure-stdlib realtime fallback; no third-party package required."""

    name = "sina-fallback"

    def __init__(self, *, priority: int = 25, timeout: float = 8.0):
        self.priority = priority
        self.timeout = timeout

    def fetch_quote(self, symbol: str) -> ProviderResult:
        normalized = normalize_symbol(symbol)
        url = f"https://hq.sinajs.cn/list={normalized.tencent}"
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        with open_market_url(request, timeout=self.timeout) as response:
            raw = response.read()
        payload = raw.decode("gbk", errors="replace")
        quote = parse_sina_quote_payload(payload, symbol, source_url=url)
        return ProviderResult(quote, DataStatus.FRESH, warnings=["using Sina fallback; valuation and turnover fields may be unavailable"])


def parse_sina_quote_payload(payload: str, symbol: str, *, source_url: str = "https://hq.sinajs.cn") -> Quote:
    normalized = normalize_symbol(symbol)
    if '="' not in payload:
        raise ValueError("Sina returned no quote")
    fields = payload.split('="', 1)[1].rsplit('"', 1)[0].split(",")
    if len(fields) < 10:
        raise ValueError("Sina quote schema changed")
    raw_volume = _number(fields[8])
    now = datetime.now(timezone.utc)
    try:
        as_of = datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except (IndexError, TypeError, ValueError):
        as_of = None
    return Quote(
        symbol=normalized.canonical,
        name=fields[0] or normalized.code,
        price=_number(fields[3]) or 0.0,
        previous_close=_number(fields[2]) or 0.0,
        open=_number(fields[1]), high=_number(fields[4]), low=_number(fields[5]),
        # Sina's individual-stock quote volume is shares; the app uses lots.
        volume=raw_volume / 100 if raw_volume is not None else None,
        amount=_number(fields[9]),
        status=DataStatus.FRESH,
        provenance=Provenance("sina", source_url, now, as_of),
    )


class PytdxKlineProvider:
    name = "pytdx-kline-fallback"

    def __init__(self, *, priority: int = 30, servers: tuple[tuple[str, int], ...] | None = None):
        self.priority = priority
        self.servers = servers or (("119.147.212.81", 7709), ("114.80.63.12", 7709))

    def fetch_daily(self, symbol: str, *, limit: int = 360) -> KlineResult:
        from pytdx.hq import TdxHq_API
        from pytdx.params import TDXParams

        normalized = normalize_symbol(symbol)
        market = TDXParams.MARKET_SH if normalized.exchange == "SH" else TDXParams.MARKET_SZ
        last_error = None
        for host, port in self.servers:
            api = TdxHq_API()
            try:
                connected = api.connect(host, port, time_out=4)
                if not connected:
                    raise ConnectionError(f"pytdx connection failed at {host}:{port}")
                rows = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market, normalized.code, 0, max(30, min(800, int(limit))))
                if not rows:
                    raise ValueError("pytdx returned no daily bars")
                bars = []
                for row in rows:
                    trading_date = datetime.fromisoformat(str(row["datetime"])).date()
                    bars.append(DailyBar(
                        trading_date=trading_date,
                        open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        # pytdx daily-bar volume is in lots (手).
                        volume=float(row.get("vol") or 0),
                        suspended=float(row.get("vol") or 0) <= 0,
                        limit_rate=_default_limit_rate(normalized, trading_date),
                    ))
                bars.sort(key=lambda item: item.trading_date)
                previous = None
                with_previous = []
                for bar in bars:
                    with_previous.append(DailyBar(**{**bar.__dict__, "previous_close": previous}))
                    previous = bar.close
                now = datetime.now(timezone.utc)
                return KlineResult(with_previous, DataStatus.FRESH, Provenance("pytdx-kline", f"tcp://{host}:{port}", now, now))
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    api.disconnect()
                except Exception:
                    pass
        raise RuntimeError(f"pytdx kline servers unavailable: {last_error}")


class BaostockKlineProvider:
    name = "baostock-kline-fallback"

    def __init__(self, *, priority: int = 40):
        self.priority = priority

    def fetch_daily(self, symbol: str, *, limit: int = 360) -> KlineResult:
        import baostock as bs

        normalized = normalize_symbol(symbol)
        code = f"{normalized.exchange.lower()}.{normalized.code}"
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")
        try:
            result = bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume,preclose,turn,pctChg",
                start_date="2000-01-01", end_date=datetime.now().date().isoformat(),
                frequency="d", adjustflag="3",
            )
            if result.error_code != "0":
                raise RuntimeError(result.error_msg)
            rows = []
            while result.next():
                rows.append(dict(zip(result.fields, result.get_row_data())))
            rows = rows[-max(30, min(1000, int(limit))):]
            bars = []
            previous = None
            for row in rows:
                trading_date = datetime.fromisoformat(row["date"]).date()
                close = float(row["close"])
                bars.append(DailyBar(
                    trading_date=trading_date, open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=close, volume=float(row["volume"] or 0) / 100,
                    previous_close=previous, limit_rate=_default_limit_rate(normalized, trading_date),
                ))
                previous = close
            now = datetime.now(timezone.utc)
            return KlineResult(bars, DataStatus.FRESH, Provenance("baostock-kline", "https://baostock.com", now, now))
        finally:
            bs.logout()
