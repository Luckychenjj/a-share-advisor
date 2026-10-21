from __future__ import annotations

import re
from dataclasses import dataclass


_SH_INDEX_CODES = {"000001", "000016", "000300", "000688", "000852", "000905"}
_MARKET_INDEX_SYMBOLS = {
    "SH.000001",  # 上证指数
    "SZ.399001",  # 深证成指
    "SZ.399006",  # 创业板指
    "SH.000688",  # 科创50
}


@dataclass(frozen=True)
class NormalizedSymbol:
    code: str
    exchange: str

    @property
    def canonical(self) -> str:
        return f"{self.exchange}.{self.code}"

    @property
    def tencent(self) -> str:
        return f"{self.exchange.lower()}{self.code}"


def normalize_symbol(value: str) -> NormalizedSymbol:
    """Normalize common A-share symbol forms without hiding ambiguity.

    Explicit prefixes/suffixes win. Bare six-digit symbols use conservative
    stock/ETF routing plus a small official Shanghai-index allow-list.
    """

    raw = str(value or "").strip().upper()
    if not raw:
        raise ValueError("symbol is empty")

    match = re.fullmatch(r"(SH|SZ|BJ)[.\-]?(\d{6})", raw)
    if match:
        return NormalizedSymbol(match.group(2), match.group(1))
    match = re.fullmatch(r"(\d{6})[.\-](SH|SS|SZ|BJ)", raw)
    if match:
        exchange = "SH" if match.group(2) == "SS" else match.group(2)
        return NormalizedSymbol(match.group(1), exchange)
    if not re.fullmatch(r"\d{6}", raw):
        raise ValueError(f"unsupported A-share symbol: {value!r}")

    if raw.startswith("92") or raw.startswith(("4", "8")):
        exchange = "BJ"
    elif raw in _SH_INDEX_CODES or raw.startswith(("5", "6", "9")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return NormalizedSymbol(raw, exchange)


def is_market_index(value: str) -> bool:
    """Return whether a symbol is one of the supported market benchmarks."""
    try:
        symbol = normalize_symbol(value)
        return (symbol.exchange == "SH" and symbol.code in _SH_INDEX_CODES) or (symbol.exchange == "SZ" and symbol.code.startswith("399"))
    except ValueError:
        return False

