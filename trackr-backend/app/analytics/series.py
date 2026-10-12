"""Turning stored closes into aligned return series.

The whole analytics layer rests on this module being honest about gaps, so the rule is
stated once here rather than re-argued per metric:

    A DATE MISSING FROM A PRICE SERIES IS NOT A ZERO-RETURN DAY.

``app.db.prices.price_series`` deliberately does not carry prices forward. A scrip that did
not trade, a date nobody fetched, and a security that had not listed yet all arrive the same
way -- as an absence. Filling any of them with the previous close manufactures a day of
exactly zero return, which drags measured volatility down and correlation up. Both errors
flatter the portfolio, which is the direction a risk number must never be wrong in.

So: returns are computed between CONSECUTIVE OBSERVATIONS THAT EXIST, and a security is
excluded from a cross-sectional calculation when it does not cover the window, with the
exclusion reported as coverage rather than hidden.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from statistics import stdev as sample_stdev

#: Trading days per year on the NSE, near enough. Used to annualise a daily standard
#: deviation and to convert a window length into years.
TRADING_DAYS = 252

#: A window needs at least this many observations before a statistic computed from it is
#: worth showing. Below it, the answer is None -- not a number with a caveat attached,
#: because a caveat on a chart is not read and a missing value is.
MIN_OBSERVATIONS = 30


def to_float_series(series: list[tuple[date, Decimal]]) -> tuple[list[date], list[float]]:
    """Decimal closes -> floats, at the single point where the boundary is crossed.

    ``float(Decimal)`` is exact for any value a price can take, and this is the only place
    in the package that performs the conversion, so the boundary is auditable in one grep.
    """
    dates = [d for d, _ in series]
    values = [float(c) for _, c in series]
    return dates, values


def daily_returns(values: list[float]) -> list[float]:
    """Simple returns between consecutive observations.

    A non-positive close cannot produce a return and ends the chain rather than producing
    an infinity: ``app.prices.udiff.parse`` already refuses to store a zero close, so one
    arriving here means something upstream changed.
    """
    out: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous <= 0:
            continue
        out.append(current / previous - 1.0)
    return out


def align(
    series_by_isin: dict[str, list[tuple[date, Decimal]]],
    isins: list[str],
) -> tuple[list[date], dict[str, list[float]]]:
    """Restrict every series to the dates ALL of them share, preserving order.

    Correlation and beta are only defined on paired observations. Aligning on the
    intersection is the conservative choice: pairing a scrip's Tuesday with another's
    Wednesday because both are "the next row" would compute a correlation between two
    different days and report it as fact.

    An ISIN absent from ``series_by_isin`` simply does not appear in the result, and the
    caller reports it as coverage.
    """
    present = [i for i in isins if series_by_isin.get(i)]
    if not present:
        return [], {}

    common: set[date] | None = None
    for isin in present:
        dates = {d for d, _ in series_by_isin[isin]}
        common = dates if common is None else (common & dates)
    shared = sorted(common or set())
    if not shared:
        return [], {}

    aligned: dict[str, list[float]] = {}
    for isin in present:
        by_date = {d: float(c) for d, c in series_by_isin[isin]}
        aligned[isin] = [by_date[d] for d in shared]
    return shared, aligned


def portfolio_series(
    aligned: dict[str, list[float]],
    weights: dict[str, float],
) -> list[float]:
    """A daily constant-weight simulation of the portfolio's current exposures.

    HONEST LABEL, enforced by the caller: this is a BACKTEST of today's weights applied
    backwards, NOT what the portfolio actually returned. The realised figure is TWRR from
    the persisted snapshot series and XIRR from the ledger, both of which already exist.
    Presenting this as realised history would be inventing financial history, which
    docs/ARCHITECTURE_INVARIANTS.md forbids outright.

    It is still worth computing: it is the only way to get a risk profile for the book as
    it stands today, which is the question "how risky is my portfolio?" actually asks.
    """
    total = sum(weights.get(i, 0.0) for i in aligned)
    if total <= 0:
        return []
    length = len(next(iter(aligned.values()))) if aligned else 0
    if not length:
        return []
    out: list[float] = [1.0]
    for step in range(1, length):
        daily = sum(
            weights.get(isin, 0.0) / total * (values[step] / values[step - 1] - 1)
            for isin, values in aligned.items()
        )
        out.append(out[-1] * (1.0 + daily))
    return out


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float | None:
    """Sample standard deviation. ``None`` below two observations -- not zero, which would
    read as "this never moves"."""
    if len(values) < 2:
        return None
    return sample_stdev(values)


def covariance(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or len(a) < 2:
        return None
    ma, mb = mean(a), mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (len(a) - 1)


def correlation(a: list[float], b: list[float]) -> float | None:
    sa, sb = stdev(a), stdev(b)
    cov = covariance(a, b)
    if cov is None or not sa or not sb:
        return None
    return cov / (sa * sb)


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile, for historical VaR.

    Historical rather than parametric, deliberately: a parametric VaR assumes normality,
    and Indian mid- and small-caps have fatter tails than that assumption allows. The
    method travels with the number to the UI.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
