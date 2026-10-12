"""Risk windows over fully observed holdings, with explicit exclusions.

Each window uses one calendar and one fixed set of holdings. A missing close excludes
that holding from that window; it never shortens everybody else's calendar or becomes
a fabricated zero-return day. These remain backtests of current weights.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.analytics import risk, series


@dataclass(frozen=True)
class CoveredWindow:
    stats: risk.RiskWindow
    dates: list[date]
    aligned: dict[str, list[float]]
    levels: list[float]
    benchmark_levels: list[float] | None
    excluded: list[str]


def portfolio_windows(
    history: dict[str, list[tuple[date, Decimal]]],
    weights: dict[str, float],
    benchmark: dict[date, Decimal] | None,
    risk_free: Callable[[date, date], float | None] | None = None,
) -> dict[str, CoveredWindow]:
    """1Y/3Y/5Y independently; an available window when 1Y cannot be measured.

    The benchmark calendar keeps both sides on identical dates, including when the
    feed lags. Without a usable benchmark, observed holding dates supply the calendar
    and beta/comparators remain unknown. Window labels also require elapsed time:
    240 sessions squeezed into a few months cannot masquerade as a year.
    """
    observed = sorted({d for i in weights for d, _ in history.get(i, [])})
    paired = [d for d in observed if benchmark and d in benchmark]
    use_benchmark = len(paired) > series.MIN_OBSERVATIONS
    calendar = paired if use_benchmark else observed
    by_isin = {i: dict(history.get(i, [])) for i in weights}

    def build(dates: list[date]) -> CoveredWindow | None:
        if len(dates) <= series.MIN_OBSERVATIONS:
            return None
        aligned = {
            i: [float(rows[d]) for d in dates]
            for i, rows in by_isin.items()
            if weights[i] > 0 and all(d in rows and rows[d] > 0 for d in dates)
        }
        if not aligned:
            return None
        levels = series.portfolio_series(aligned, weights)
        bench = [float(benchmark[d]) for d in dates] if use_benchmark else None
        # The risk-free rate is the one cash earned over exactly these dates; None (no
        # sourced level at either edge) withholds Sharpe and Sortino.
        rate = risk_free(dates[0], dates[-1]) if risk_free else None
        stats = risk.compute_window(
            series.daily_returns(levels),
            series.daily_returns(bench) if bench else None,
            risk_free=rate,
        )
        if stats is None:
            return None
        return CoveredWindow(
            stats, dates, aligned, levels, bench, [i for i in weights if i not in aligned]
        )

    out = {}
    for label, years in (("1y", 1), ("3y", 3), ("5y", 5)):
        want = series.TRADING_DAYS * years + 1
        dates = calendar[-want:]
        if len(dates) < want * risk.COVERAGE_TOLERANCE:
            continue
        if (dates[-1] - dates[0]).days < years * 365 * risk.COVERAGE_TOLERANCE:
            continue
        window = build(dates)
        if window:
            out[label] = window

    if "1y" not in out:
        # Longest fully observed trailing window, capped at one year's sessions.
        for count in range(
            min(len(calendar), series.TRADING_DAYS + 1), series.MIN_OBSERVATIONS, -1
        ):
            window = build(calendar[-count:])
            if window:
                out["available"] = window
                break
    return out
