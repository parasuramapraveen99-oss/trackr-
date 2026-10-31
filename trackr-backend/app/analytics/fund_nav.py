"""What one scheme's own NAV history says: trailing returns, monthly volatility, drawdown.

FUND RETURN, NOT YOUR RETURN. Everything here is computed from the scheme's published NAV
alone. It answers "how did this fund do?", never "how did my money in it do?" -- that is
the investor XIRR in the funds router, which depends on when the investor paid in. The two
are shown side by side and must never be labelled as each other.

NAV IS TOTAL RETURN ONLY FOR A GROWTH OPTION. An IDCW option's NAV falls by every payout,
so the change in its NAV understates what a holder earned. Callers withhold these figures
for IDCW plans rather than show a number that is wrong by construction
(mutual-funds Analytics/analytics/RETURNS.md, NAV history).

MONTHLY, NOT DAILY, FOR FUND RISK. Volatility uses month-end NAVs over complete calendar
months, annualised by sqrt(12), with the sample (n-1) deviation -- the convention fund
data providers publish (Morningstar: 36 monthly returns). A carried-forward NAV is never
an observation: only dates that actually have a NAV are read. The equity risk board
(app/analytics/risk.py) is daily; the two windows are labelled, not mixed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.analytics.series import stdev

#: A window's start NAV may be this many calendar days before the target date and still
#: count as "the NAV on that day" -- the same tolerance the valuation route allows
#: (app.prices.routing, max 7 days). Further than that, the window is not the one named.
START_TOLERANCE_DAYS = 7

#: Months of returns behind the volatility figure.
RISK_MONTHS = 36

TRAILING_YEARS = (1, 3, 5)


@dataclass(frozen=True, slots=True)
class TrailingReturn:
    years: int
    start_date: date | None
    end_date: date | None
    cumulative: float | None
    #: Compound annual rate. None for a one-year window, where the cumulative figure IS
    #: the annual one and a second number would only invite the question of why they
    #: differ by a day's compounding.
    annualised: float | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MonthlyRisk:
    months: int
    start_date: date | None
    end_date: date | None
    volatility: float | None
    max_drawdown: float | None
    drawdown_peak: date | None
    drawdown_trough: date | None
    reason: str | None = None


def _years_before(on: date, years: int) -> date:
    try:
        return on.replace(year=on.year - years)
    except ValueError:  # 29 February in a non-leap target year
        return on.replace(year=on.year - years, day=28)


def _last_on_or_before(series: list[tuple[date, Decimal]], on: date) -> tuple[date, Decimal] | None:
    found = None
    for point in series:
        if point[0] > on:
            break
        found = point
    return found


def trailing_return(series: list[tuple[date, Decimal]], as_of: date, years: int) -> TrailingReturn:
    """Point-to-point NAV change over ``years`` ending at the last NAV on or before ``as_of``.

    ``series`` is (nav_date, nav) ascending, observed NAVs only.
    """
    end = _last_on_or_before(series, as_of)
    if end is None:
        return TrailingReturn(years, None, None, None, None, "No NAV on or before this date")
    target = _years_before(end[0], years)
    if not series or series[0][0] > target:
        return TrailingReturn(
            years, None, end[0], None, None, f"The NAV history starts after {target.isoformat()}"
        )
    start = _last_on_or_before(series, target)
    if start is None or (target - start[0]).days > START_TOLERANCE_DAYS:
        return TrailingReturn(
            years, None, end[0], None, None,
            f"No NAV within {START_TOLERANCE_DAYS} days before {target.isoformat()}",
        )
    if start[1] <= 0:
        return TrailingReturn(years, start[0], end[0], None, None, "Start NAV is not positive")
    growth = float(end[1] / start[1])
    days = (end[0] - start[0]).days
    annualised = growth ** (365.0 / days) - 1.0 if years > 1 and days > 0 and growth > 0 else None
    return TrailingReturn(years, start[0], end[0], growth - 1.0, annualised)


def month_end_navs(series: list[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    """The last observed NAV of each calendar month, on its own actual date."""
    out: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for point in series:
        out[(point[0].year, point[0].month)] = point
    return [out[k] for k in sorted(out)]


def _drawdown(levels: list[tuple[date, float]]) -> tuple[float, date | None, date | None]:
    peak_value, peak_date = levels[0][1], levels[0][0]
    worst, worst_peak, worst_trough = 0.0, None, None
    for on, value in levels:
        if value > peak_value:
            peak_value, peak_date = value, on
        if peak_value > 0 and value / peak_value - 1.0 < worst:
            worst, worst_peak, worst_trough = value / peak_value - 1.0, peak_date, on
    return worst, worst_peak, worst_trough


def monthly_risk(
    series: list[tuple[date, Decimal]], as_of: date, months: int = RISK_MONTHS
) -> MonthlyRisk:
    """Annualised volatility of ``months`` complete monthly returns ending before ``as_of``'s
    month, and the deepest daily-NAV drawdown over the same span."""
    last_complete = as_of.replace(day=1) - timedelta(days=1)
    ends = month_end_navs([p for p in series if p[0] <= last_complete])
    window = ends[-(months + 1):]
    if len(window) < months + 1:
        return MonthlyRisk(
            months, None, None, None, None, None, None,
            f"Needs {months + 1} month-end NAVs; {len(window)} available",
        )
    for (a, _), (b, _) in zip(window, window[1:], strict=False):
        if (b.year * 12 + b.month) - (a.year * 12 + a.month) != 1:
            return MonthlyRisk(
                months, None, None, None, None, None, None,
                f"No NAV at all in a month between {a.isoformat()} and {b.isoformat()}",
            )
    if any(nav <= 0 for _, nav in window):
        return MonthlyRisk(
            months, None, None, None, None, None, None, "A month-end NAV is not positive"
        )
    returns = [float(b / a) - 1.0 for (_, a), (_, b) in zip(window, window[1:], strict=False)]
    sd = stdev(returns)
    start, end = window[0][0], window[-1][0]
    daily = [(on, float(nav)) for on, nav in series if start <= on <= end]
    worst, peak, trough = _drawdown(daily)
    return MonthlyRisk(
        months,
        start,
        end,
        sd * math.sqrt(12) if sd is not None else None,
        worst,
        peak,
        trough,
    )
