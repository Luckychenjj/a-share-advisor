from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, time as clock_time
from math import isfinite

from xiao_a.models import DailyBar


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def trading_progress(as_of: datetime | None) -> float | None:
    """Use the quote's exchange time, never the wall clock or retrieval time."""
    if as_of is None:
        return None
    current = as_of
    if current.tzinfo is not None:
        current = current.astimezone(SHANGHAI)
    current_time = current.time()
    morning_start, morning_end = clock_time(9, 30), clock_time(11, 30)
    afternoon_start, afternoon_end = clock_time(13, 0), clock_time(15, 0)
    if current_time <= morning_start:
        return None
    if current_time <= morning_end:
        elapsed_minutes = (current.hour - 9) * 60 + current.minute - 30 + current.second / 60
    elif current_time < afternoon_start:
        elapsed_minutes = 120.0
    elif current_time <= afternoon_end:
        elapsed_minutes = 120.0 + (current.hour - 13) * 60 + current.minute + current.second / 60
    else:
        elapsed_minutes = 240.0
    return min(1.0, elapsed_minutes / 240.0)


def projected_volume_ratio(current_volume: float, average_volume: float, progress: float | None) -> float | None:
    """Compare cumulative volume with a full-session baseline on matching units."""
    if (progress is None or not all(isfinite(v) for v in (current_volume, average_volume, progress))
            or current_volume <= 0 or average_volume <= 0 or not 0 < progress <= 1):
        return None
    return current_volume / progress / average_volume


def average_prior_volume(bars: list[DailyBar], session: date, *, count: int = 5) -> float | None:
    """Average completed sessions strictly before the quote's own trading date."""
    by_date = {bar.trading_date: bar for bar in bars if bar.trading_date < session}
    completed = [bar.volume for _, bar in sorted(by_date.items())
                 if not bar.suspended and isfinite(bar.volume) and bar.volume > 0][-count:]
    if len(completed) < count:
        return None
    return sum(completed) / count
