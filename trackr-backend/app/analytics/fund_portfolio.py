"""Risk and return of the investor's mutual funds as they are held today.

WHAT THIS IS. The current mix of funds, held at today's weights through each past month:
a hypothetical, rebalanced-monthly portfolio built from published NAVs. It answers "how has
the mix I hold now behaved?", which is what a return-versus-risk chart and the Sharpe /
Sortino / beta figures need. It is NOT the investor's own return -- that is the fund XIRR,
which depends on when money went in -- and every caller labels it "current mix"
(mutual-funds Analytics/analytics/RETURNS.md separates investor, fund, hypothetical and
benchmark returns; this is the third).

MONTHLY, LIKE THE SCHEME PAGE. Returns are month-end to month-end over complete calendar
months, annualised by sqrt(12) with the sample (n-1) deviation -- the convention of
``app.analytics.fund_nav`` and of published fund factsheets. Every series in one window
(each fund, the mix, the index) covers the SAME calendar months, so a fund with a shorter
history is left out of that window by name rather than compared over a different span.

NOTHING IS STRETCHED TO 100%. A fund without the window's full history is excluded and
its weight reported as uncovered (``covered_weight``); the mix is the covered funds at
their weights relative to each other, and the response says so. A growth-option NAV is a
total-return series; an IDCW NAV is not, so callers exclude IDCW options here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.analytics.fund_nav import month_end_navs
from app.analytics.series import covariance, mean, stdev

#: Trailing windows offered, in years.
WINDOW_YEARS = (1, 3, 5)


@dataclass(frozen=True, slots=True)
class MonthlyStats:
    """One series over one window. Every figure is None-able: a ratio that needs the
    risk-free rate is withheld when no rate is stored, never computed against zero."""

    months: int
    annual_return: float | None
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    beta: float | None
    alpha: float | None
    max_drawdown: float | None


@dataclass(frozen=True, slots=True)
class MonthlySeries:
    """Monthly returns over consecutive calendar months ending at ``months[-1]``."""

    months: list[tuple[int, int]]
    returns: list[float]


@dataclass(frozen=True, slots=True)
class CurrentMix:
    series: MonthlySeries | None
    #: Share of today's fund value that has the window's full NAV history.
    covered_weight: Decimal
    included: list[str]
    excluded: dict[str, str] = field(default_factory=dict)
    reason: str | None = None


def month_keys(as_of: date, months: int) -> list[tuple[int, int]]:
    """``months + 1`` calendar months ending with the last COMPLETE month before ``as_of``."""
    last = as_of.replace(day=1) - timedelta(days=1)
    index = last.year * 12 + (last.month - 1)
    return [divmod(i, 12) for i in range(index - months, index + 1)]


def month_end(key: tuple[int, int]) -> date:
    """The calendar last day of a (year, month0) key."""
    year, month0 = key
    first_next = date(year + (month0 + 1) // 12, (month0 + 1) % 12 + 1, 1)
    return first_next - timedelta(days=1)


def monthly_series(
    levels: list[tuple[date, Decimal]], as_of: date, months: int
) -> MonthlySeries | str:
    """Month-end-to-month-end returns over the window, or the reason there are none.

    ``levels`` is observed (date, level) ascending: NAVs or index closes, never carried.
    """
    keys = month_keys(as_of, months)
    by_month = {(on.year, on.month - 1): level for on, level in month_end_navs(levels)}
    missing = [k for k in keys if k not in by_month]
    if missing:
        first = month_end(missing[0]).strftime("%b %Y")
        return f"No published value for {first}; the {months // 12}-year window needs every month"
    values = [by_month[k] for k in keys]
    if any(v <= 0 for v in values):
        return "A month-end value is not positive"
    returns = [float(b / a) - 1.0 for a, b in zip(values, values[1:], strict=False)]
    return MonthlySeries(keys[1:], returns)


def current_mix(
    levels_by_isin: dict[str, list[tuple[date, Decimal]]],
    weights: dict[str, Decimal],
    as_of: date,
    months: int,
    excluded: dict[str, str] | None = None,
) -> CurrentMix:
    """Today's funds at today's weights, rebalanced to those weights every month.

    ``weights`` is each held fund's current value; ``excluded`` names funds the caller has
    already ruled out (an IDCW option, no NAV history), whose weight counts as uncovered.
    """
    out_excluded = dict(excluded or {})
    total = sum(weights.values(), Decimal(0))
    series: dict[str, MonthlySeries] = {}
    for isin in weights:
        if isin in out_excluded:
            continue
        found = monthly_series(levels_by_isin.get(isin, []), as_of, months)
        if isinstance(found, str):
            out_excluded[isin] = found
        else:
            series[isin] = found
    covered = sum((weights[i] for i in series), Decimal(0))
    covered_weight = covered / total if total > 0 else Decimal(0)
    if not series or covered <= 0:
        return CurrentMix(None, covered_weight, [], out_excluded,
                          "No fund held today has a full NAV history for this window")
    keys = next(iter(series.values())).months
    share = {i: float(weights[i] / covered) for i in series}
    returns = [
        sum(share[i] * series[i].returns[t] for i in series) for t in range(len(keys))
    ]
    return CurrentMix(MonthlySeries(keys, returns), covered_weight, sorted(series), out_excluded)


def levels_base_100(series: MonthlySeries) -> list[tuple[date, float]]:
    """Growth of 100 at each month end, starting from the month before the first return."""
    first = series.months[0]
    start_key = divmod(first[0] * 12 + first[1] - 1, 12)
    out = [(month_end(start_key), 100.0)]
    for key, r in zip(series.months, series.returns, strict=True):
        out.append((month_end(key), out[-1][1] * (1.0 + r)))
    return out


def stats(
    returns: list[float],
    benchmark: list[float] | None = None,
    risk_free: float | None = None,
) -> MonthlyStats:
    """Annualised monthly statistics. ``benchmark`` must cover the same months.

    Sharpe and Sortino measure against the risk-free rate (Sortino's downside deviation is
    below that rate, as in ``app.analytics.risk``); alpha is Jensen's alpha, annualised
    arithmetically (x12). All three are None without a stored rate.
    """
    n = len(returns)
    if n < 2:
        return MonthlyStats(n, None, None, None, None, None, None, None)
    growth = 1.0
    levels = [1.0]
    for r in returns:
        growth *= 1.0 + r
        levels.append(growth)
    annual_return = growth ** (12.0 / n) - 1.0 if growth > 0 else None
    sd = stdev(returns)
    volatility = sd * math.sqrt(12) if sd is not None else None
    peak, worst = levels[0], 0.0
    for level in levels:
        peak = max(peak, level)
        worst = min(worst, level / peak - 1.0)

    rf_m = (1.0 + risk_free) ** (1.0 / 12.0) - 1.0 if risk_free is not None else None
    avg = mean(returns)
    sharpe = sortino = None
    if rf_m is not None and sd:
        sharpe = (avg - rf_m) / sd * math.sqrt(12)
    if rf_m is not None:
        downside = math.sqrt(sum(min(r - rf_m, 0.0) ** 2 for r in returns) / n)
        if downside:
            sortino = (avg - rf_m) / downside * math.sqrt(12)

    beta = alpha = None
    if benchmark is not None and len(benchmark) == n:
        bench_sd = stdev(benchmark)
        cov = covariance(returns, benchmark)
        if cov is not None and bench_sd:
            beta = cov / bench_sd**2
            if rf_m is not None:
                alpha = ((avg - rf_m) - beta * (mean(benchmark) - rf_m)) * 12.0
    return MonthlyStats(n, annual_return, volatility, sharpe, sortino, beta, alpha, worst)
