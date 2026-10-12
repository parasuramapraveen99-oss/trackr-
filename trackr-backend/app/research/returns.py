"""Calendar-month anniversaries for resurfacing prior writing. Pure; explicit dates only.

BLUEPRINT_BACKEND.md section 6.3 ("Re-encounter"): prior writing returns at 6, 12 and 24
calendar months after its ORIGINAL date, with an invalid destination day clamped to the end
of the destination month. Two rules this module exists to get right:

1. **Each offset is applied to the original date**, never chained through an intermediate
   clamped date. 2024-08-31 + 6 months is 2025-02-28, and + 12 months is 2025-08-31 -- not
   2026-02-28 + 6 months of something already clamped.
2. **No day-count approximation.** "Six months" is not 182 days; a leap day is not a
   rounding error. 2024-02-29 + 12 months is 2025-02-28.

The route supplies ``as_of`` (defaulting to today in Asia/Kolkata); this module never reads
a clock, a database or the network.
"""

from __future__ import annotations

import calendar
from datetime import date

#: The offsets, and the reason label the Home feed carries for each.
ANNIVERSARY_MONTHS: tuple[tuple[int, str], ...] = (
    (6, "MONTH_6"),
    (12, "MONTH_12"),
    (24, "MONTH_24"),
)


def add_months(original: date, months: int) -> date:
    """``original`` moved by whole calendar months, clamping to the month's last day."""
    if months < 0:
        raise ValueError("months must be non-negative")
    index = original.month - 1 + months
    year = original.year + index // 12
    month = index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(original.day, last_day))


def anniversary_reason(original: date, as_of: date) -> str | None:
    """``MONTH_6`` / ``MONTH_12`` / ``MONTH_24`` when ``as_of`` is exactly that
    anniversary of ``original``; otherwise None. A near miss is a miss."""
    for months, reason in ANNIVERSARY_MONTHS:
        if add_months(original, months) == as_of:
            return reason
    return None


def anniversaries(original: date) -> dict[str, date]:
    """All three anniversary dates of ``original``, keyed by reason."""
    return {reason: add_months(original, months) for months, reason in ANNIVERSARY_MONTHS}


def originals_due(as_of: date) -> dict[str, list[date]]:
    """Every original date whose anniversary falls on ``as_of``, per reason.

    The inverse of ``anniversary_reason`` for querying: because of month-end clamping
    several originals can map onto one destination (Aug 29, 30 and 31 all land on Feb 28
    six months later in a common year), so each reason yields a LIST of originals. Computed
    by candidate enumeration over the source month, not by subtracting months from
    ``as_of`` -- subtraction cannot recover which clamped originals collapsed.
    """
    due: dict[str, list[date]] = {}
    for months, reason in ANNIVERSARY_MONTHS:
        index = as_of.month - 1 - months
        year = as_of.year + index // 12
        month = index % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        hits = [
            candidate
            for day in range(1, last_day + 1)
            if add_months(candidate := date(year, month, day), months) == as_of
        ]
        due[reason] = hits
    return due
