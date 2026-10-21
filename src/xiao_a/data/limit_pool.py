"""Source-specific limit-up parsing; never replace missing observations with zero."""
import json
import math
from datetime import datetime

from .symbols import normalize_symbol


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def clock(value):
    if value is None:
        return None
    try:
        return datetime.strptime(str(int(value)).zfill(6), "%H%M%S").strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return None


def summarize(items, symbol, trade_date, source):
    items = sorted(items, key=lambda x: (
        -(x.get("continuous") or 0),
        x.get("reopen_count") if x.get("reopen_count") is not None else float("inf"),
        -(x.get("seal_amount") or 0), x.get("first_limit_time") or "99", x["symbol"],
    ))
    target = normalize_symbol(symbol).canonical
    return dict(available=True, count=len(items),
                max_continuous=max((x["continuous"] for x in items if x.get("continuous") is not None), default=None),
                selected=next((x for x in items if x["symbol"] == target), None),
                items=items, top=items[:10], trade_date=trade_date, source_label=source,
                ranking_description="按连板数降序、炸板次数升序、封单金额降序、首次封板时间排序；缺失项排后。排序仅反映这些指标。")


def parse_cls(payload, symbol):
    raw = json.loads(payload)
    if raw.get("code") != 200:
        raise ValueError("CLS limit-up response failed")
    rows = raw.get("data") or []
    dates = {str(x.get("time", ""))[:10] for x in rows if x.get("time")}
    if len(dates) > 1:
        raise ValueError("CLS pool contains mixed trading dates")
    items = {}
    for row in rows:
        code = normalize_symbol(row["secu_code"]).canonical
        change = number(row.get("change"))
        items[code] = dict(symbol=code, name=str(row.get("secu_name", "")).strip(),
            price=number(row.get("last_px")), change_pct=change * 100 if change is not None else None,
            continuous=number(row.get("limit_up_days", row.get("continuous"))),
            sector=" / ".join(dict.fromkeys(p["secu_name"] for p in row.get("plate", []) if p.get("secu_name"))),
            reason=row.get("up_reason", ""))
    return summarize(list(items.values()), symbol, next(iter(dates), None), "财联社涨停池")


def parse_eastmoney(payload, symbol):
    raw = json.loads(payload)
    data = raw.get("data")
    if raw.get("rc") != 0 or not isinstance(data, dict) or "pool" not in data:
        raise ValueError("Eastmoney limit-up pool unavailable")
    trade_date = datetime.strptime(str(data["qdate"]), "%Y%m%d").date().isoformat()
    rows = data["pool"]
    if data.get("tc") is not None and int(data["tc"]) != len(rows):
        raise ValueError("Eastmoney pool incomplete")
    items = []
    for row in rows:
        code = normalize_symbol(("SH" if row["m"] == 1 else "SZ") + row["c"]).canonical
        price = number(row.get("p"))
        stats = row.get("zttj") or {}
        items.append(dict(symbol=code, name=row.get("n", ""), price=price / 1000 if price is not None else None,
            change_pct=number(row.get("zdp")), continuous=number(row.get("lbc")),
            sector=row.get("hybk", ""), turnover_pct=number(row.get("hs")), amount=number(row.get("amount")),
            float_market_cap=number(row.get("ltsz")), seal_amount=number(row.get("fund")),
            reopen_count=number(row.get("zbc")), first_limit_time=clock(row.get("fbt")),
            last_limit_time=clock(row.get("lbt")), recent_limits=f"{stats['days']}天{stats['ct']}板" if stats else None))
    return summarize(items, symbol, trade_date, "东方财富涨停池（按该接口覆盖范围）")


def enrich_pool(primary, extra, symbol):
    if primary.get("trade_date") != extra.get("trade_date"):
        return primary
    index = {x["symbol"]: x for x in extra["items"]}
    items = [{**item, **index.get(item["symbol"], {}), "reason": item.get("reason", ""),
              "concepts": item.get("sector", "")} for item in primary["items"]]
    return summarize(items, symbol, primary["trade_date"], "财联社全池 · 东方财富同日指标补充；—表示接口未提供")
