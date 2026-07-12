"""
trading_calendar.py — US equity (NYSE/Nasdaq) trading-day calendar, no deps.

Rules covered:
  - Weekends
  - Fixed holidays with Sat->Fri / Sun->Mon observation:
      New Year's Day, Juneteenth, Independence Day, Christmas
  - Floating holidays:
      MLK (3rd Mon Jan), Presidents (3rd Mon Feb), Memorial (last Mon May),
      Labor (1st Mon Sep), Thanksgiving (4th Thu Nov)
  - Good Friday (Easter via anonymous Gregorian algorithm)

Half-days (early 13:00 ET close: day after Thanksgiving, some Jul 3 / Dec 24)
still count as trading days — a post-close pipeline run sees final bars either way.
"""

from datetime import date, datetime, timedelta, timezone

# US Eastern offset without pytz: EDT (UTC-4) 2nd Sun Mar .. 1st Sun Nov, else EST (UTC-5)
def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d):
    """Sat -> Fri before, Sun -> Mon after."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def market_holidays(year):
    """Set of dates the US equity market is fully closed."""
    hol = {
        _observed(date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),          # MLK Day
        _nth_weekday(year, 2, 0, 3),          # Presidents' Day
        _easter(year) - timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),            # Memorial Day
        _observed(date(year, 6, 19)),         # Juneteenth
        _observed(date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),          # Labor Day
        _nth_weekday(year, 11, 3, 4),         # Thanksgiving
        _observed(date(year, 12, 25)),        # Christmas
    }
    # Sat-observed New Year's rolls into the PRIOR year (e.g. 2022-01-01 -> 2021-12-31)
    nyd_next = _observed(date(year + 1, 1, 1))
    if nyd_next.year == year:
        hol.add(nyd_next)
    return hol


def is_trading_day(d):
    """d: datetime.date -> True if US equities trade that day."""
    if d.weekday() >= 5:
        return False
    return d not in market_holidays(d.year)


def _eastern_now(utc_now=None):
    """UTC -> US Eastern using the statutory DST rule (2nd Sun Mar .. 1st Sun Nov)."""
    now = utc_now or datetime.now(timezone.utc)
    year = now.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time(),
                                 tzinfo=timezone.utc) + timedelta(hours=7)   # 2:00 EST
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time(),
                               tzinfo=timezone.utc) + timedelta(hours=6)     # 2:00 EDT
    offset = -4 if dst_start <= now < dst_end else -5
    return now + timedelta(hours=offset)


def last_completed_trading_day(utc_now=None, close_hour=16, close_minute=30):
    """
    Most recent trading day whose close is settled (>= 16:30 ET buffer).
    Before today's close -> previous trading day.
    """
    et = _eastern_now(utc_now)
    d = et.date()
    if not is_trading_day(d) or (et.hour, et.minute) < (close_hour, close_minute):
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d
