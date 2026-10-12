"""Risk and return statistics over an aligned return series.

Every convention that could reasonably be chosen another way is named in the output, not
just in this docstring, because a Sharpe ratio with an unstated risk-free rate is not
comparable to anything and a VaR with an unstated method is not checkable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.analytics.series import (
    MIN_OBSERVATIONS,
    TRADING_DAYS,
    covariance,
    daily_returns,
    mean,
    percentile,
    stdev,
)

#: How much of a trailing window must actually be present before it is reported. NSE loses
#: roughly a dozen sessions a year to holidays, so demanding a full 252 per year would drop
#: a legitimate window; demanding much less would let a materially short series be labelled
#: with a period it does not cover.
COVERAGE_TOLERANCE = 0.94


@dataclass(frozen=True, slots=True)
class RiskWindow:
    """One trailing window's statistics. Every field is None-able on purpose: a portfolio
    with two years of history gets a real 1Y and a null 5Y, never a 5Y quietly computed
    from two years and labelled as five."""

    observations: int
    cum_return: float | None
    cagr: float | None
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    beta: float | None
    var_1d_95: float | None
    var_1d_99: float | None
    cvar_1d_95: float | None
    max_drawdown: float | None
    bench_cum_return: float | None = None
    bench_cagr: float | None = None
    bench_volatility: float | None = None
    bench_sharpe: float | None = None
    #: Carried so the UI can state them beside the numbers rather than in a footnote.
    risk_free_rate: float | None = None
    var_method: str = "historical"
    sortino_target: str = "risk_free_rate"


def max_drawdown(levels: list[float]) -> float | None:
    """Deepest peak-to-trough fall, as a negative fraction.

    The number that actually tests whether a plan survives contact -- volatility says how
    much it wobbles, this says how much it took away and made you sit through.
    """
    if len(levels) < 2:
        return None
    peak = levels[0]
    worst = 0.0
    for level in levels:
        peak = max(peak, level)
        if peak > 0:
            worst = min(worst, level / peak - 1.0)
    return worst


def _annualised(returns: list[float]) -> tuple[float, float]:
    """(cumulative return, CAGR) for a daily return series."""
    growth = 1.0
    for r in returns:
        growth *= 1.0 + r
    cumulative = growth - 1.0
    years = len(returns) / TRADING_DAYS
    cagr = (growth ** (1.0 / years) - 1.0) if years > 0 and growth > 0 else None
    return cumulative, cagr


def compute_window(
    returns: list[float],
    benchmark_returns: list[float] | None = None,
    risk_free: float | None = None,
) -> RiskWindow | None:
    """Statistics for one window. ``None`` when the window is too short to mean anything.

    ``benchmark_returns``, when given, must already be aligned to the same dates --
    ``app.analytics.series.align`` is what guarantees that, and pairing unaligned series
    would compute a beta between two different days.
    """
    if len(returns) < MIN_OBSERVATIONS:
        return None

    cumulative, cagr = _annualised(returns)
    daily_sd = stdev(returns)
    volatility = daily_sd * math.sqrt(TRADING_DAYS) if daily_sd is not None else None

    sharpe = None
    daily_target = (
        (1.0 + risk_free) ** (1.0 / TRADING_DAYS) - 1.0 if risk_free is not None else None
    )
    if daily_target is not None and daily_sd:
        sharpe = (mean(returns) - daily_target) / daily_sd * math.sqrt(TRADING_DAYS)

    # Downside deviation against the RISK-FREE RATE, not zero. The choice changes the
    # number materially, so it is stated in RiskWindow.sortino_target and on the tooltip.
    sortino = None
    if daily_target is not None:
        downside_deviation = math.sqrt(
            sum(min(r - daily_target, 0.0) ** 2 for r in returns) / len(returns)
        )
        if downside_deviation:
            sortino = (mean(returns) - daily_target) / downside_deviation * math.sqrt(TRADING_DAYS)

    beta = None
    bench_cum = bench_cagr = bench_vol = bench_sharpe = None
    if benchmark_returns and len(benchmark_returns) == len(returns):
        bench_sd = stdev(benchmark_returns)
        bench_var = bench_sd**2 if bench_sd else None
        cov = covariance(returns, benchmark_returns)
        if cov is not None and bench_var:
            beta = cov / bench_var
        bench_cum, bench_cagr = _annualised(benchmark_returns)
        bench_vol = bench_sd * math.sqrt(TRADING_DAYS) if bench_sd is not None else None
        if daily_target is not None and bench_sd:
            bench_sharpe = (
                (mean(benchmark_returns) - daily_target) / bench_sd * math.sqrt(TRADING_DAYS)
            )

    # Historical VaR: the empirical 5th/1st percentile of realised daily returns, negated
    # so a loss reads as a positive magnitude. Parametric would assume normality, which
    # Indian mid- and small-caps do not obey.
    p5 = percentile(returns, 0.05)
    p1 = percentile(returns, 0.01)
    # Empirical expected shortfall uses exactly the worst 5% of probability mass.
    # A fractional final observation avoids changing the confidence level when n*5%
    # is not an integer; averaging just values <= an interpolated quantile does that.
    ordered_losses = sorted(returns)
    mass = len(returns) * 0.05
    whole = int(mass)
    tail_sum = sum(ordered_losses[:whole])
    if mass > whole:
        tail_sum += (mass - whole) * ordered_losses[whole]
    expected_shortfall = max(0.0, -tail_sum / mass)

    levels = [1.0]
    for r in returns:
        levels.append(levels[-1] * (1.0 + r))

    return RiskWindow(
        observations=len(returns),
        cum_return=cumulative,
        cagr=cagr,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        beta=beta,
        var_1d_95=max(0.0, -p5) if p5 is not None else None,
        var_1d_99=max(0.0, -p1) if p1 is not None else None,
        cvar_1d_95=expected_shortfall,
        max_drawdown=max_drawdown(levels),
        bench_cum_return=bench_cum,
        bench_cagr=bench_cagr,
        bench_volatility=bench_vol,
        bench_sharpe=bench_sharpe,
        risk_free_rate=risk_free,
    )


def windows(
    levels: list[float],
    benchmark_levels: list[float] | None = None,
    risk_free: float | None = None,
) -> dict[str, RiskWindow]:
    """1Y / 3Y / 5Y, each independently null when history does not reach.

    Taking the TAIL of the level series rather than re-slicing by date is correct because
    the series is already aligned to shared trading dates -- see series.align.
    """
    out: dict[str, RiskWindow] = {}
    for label, years in (("1y", 1), ("3y", 3), ("5y", 5)):
        want = int(TRADING_DAYS * years) + 1
        # A window is emitted ONLY when the series actually reaches back that far.
        #
        # Falling back to "whatever history exists" is the tempting version and it is
        # wrong: with 2.7 years of prices it labels the SAME 2.7 years as both 3Y and 5Y,
        # so the screen shows two identical rows under two different headings and a reader
        # concludes the portfolio was flat over the extra two years. A missing window is a
        # fact; a mislabelled one is a false claim. COVERAGE_TOLERANCE allows the few
        # sessions a year genuinely loses to holidays without accepting a short window.
        if len(levels) < want * COVERAGE_TOLERANCE:
            continue
        slice_levels = levels[-want:]
        bench_slice = (
            daily_returns(benchmark_levels[-want:])
            if benchmark_levels and len(benchmark_levels) >= want
            else None
        )
        window = compute_window(daily_returns(slice_levels), bench_slice, risk_free=risk_free)
        if window is not None:
            out[label] = window
    return out
