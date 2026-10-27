"""Synthetic working calendar: Mon-Fri minus a fixed list of company holidays (master_data.yaml)."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

_HOLIDAYS: frozenset[str] = frozenset()


def configure(holidays_mm_dd: list[str]) -> None:
    global _HOLIDAYS
    _HOLIDAYS = frozenset(holidays_mm_dd)
    workdays_in_month.cache_clear()


def is_workday(day: dt.date) -> bool:
    return day.weekday() < 5 and day.strftime("%m-%d") not in _HOLIDAYS


@lru_cache(maxsize=None)
def workdays_in_month(year: int, month: int) -> tuple[dt.date, ...]:
    day = dt.date(year, month, 1)
    out = []
    while day.month == month:
        if is_workday(day):
            out.append(day)
        day += dt.timedelta(days=1)
    return tuple(out)


def month_end(year: int, month: int) -> dt.date:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def previous_workday_on_or_before(day: dt.date) -> dt.date:
    while not is_workday(day):
        day -= dt.timedelta(days=1)
    return day


def next_workday_on_or_after(day: dt.date) -> dt.date:
    while not is_workday(day):
        day += dt.timedelta(days=1)
    return day
