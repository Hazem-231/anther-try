"""NYSE regular-session clock for dry-run mode (Alpaca's clock is used otherwise)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Full-day NYSE closures. Early closes (1 pm) are not modelled.
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

OPEN, CLOSE = time(9, 30), time(16, 0)


def now_utc() -> datetime:
    return datetime.now(UTC)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def is_market_open(now: datetime | None = None) -> bool:
    ny = (now or now_utc()).astimezone(NY)
    return is_trading_day(ny.date()) and OPEN <= ny.time() < CLOSE


def next_market_open(now: datetime | None = None) -> datetime:
    ny = (now or now_utc()).astimezone(NY)
    d = ny.date()
    if not (is_trading_day(d) and ny.time() < OPEN):
        d += timedelta(days=1)
        while not is_trading_day(d):
            d += timedelta(days=1)
    return datetime.combine(d, OPEN, NY).astimezone(UTC)


def in_window(now: datetime, window: list[str] | None) -> bool:
    """True when New York wall-clock time falls inside ["HH:MM", "HH:MM"]."""
    if not window:
        return True
    t = now.astimezone(NY).time()
    lo = time.fromisoformat(window[0])
    hi = time.fromisoformat(window[1])
    return lo <= t <= hi
