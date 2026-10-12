"""Benchmark comparison -- docs/ENGINE_SPEC.md section 6.

A separate module from ``xirr.py`` on purpose: this is a distinct calculation -- what
would the SAME cash flows have returned in an index -- not a variant of portfolio XIRR.
It reuses ``xirr.py``'s cash-flow construction and solver rather than reimplementing
either, because the cash-flow universe is deliberately identical (docs/ENGINE_SPEC.md
section 4's BUY/IPO_ALLOT negative, SELL/BUYBACK/DIVIDEND positive convention).

MVP benchmarks: NIFTY 50 TRI only. TRI (total-return), never a price index -- a price
index excludes dividends and understates the benchmark. This module has no opinion about
where an index level came from; it is app.prices.index_sources' job to guarantee the
levels it fetches are genuinely TRI, and this module's caller (app.routers.analytical)
is what enforces which benchmark names are actually supported.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.errors import MissingIndexLevelError
from app.engine.ledger import ordered
from app.engine.types import ZERO, Benchmark, Transaction
from app.engine.xirr import _solve, _transaction_flows


def required_index_dates(transactions: list[Transaction], as_of: date) -> list[date]:
    """Every date the replication needs an index level for.

    Exactly the dates ``_transaction_flows`` produces a cash flow for, plus ``as_of``
    (the terminal-value date) -- lets a caller (the router) resolve or fetch the levels
    it needs BEFORE calling ``compute_benchmark``, keeping this module free of I/O.

    Mirrors ``compute_benchmark``'s own no-flows short-circuit: an empty transaction
    list needs no index levels at all, not even ``as_of`` -- there would be nothing to
    terminal-value. Keeping the two in sync matters because the router calls this
    FIRST to decide what to fetch; if it disagreed with ``compute_benchmark`` here, the
    router would fetch/read a level that then goes unused.
    """
    rows = ordered(transactions, as_of)
    flows = _transaction_flows(rows)
    if not flows:
        return []
    dates = {d for d, _ in flows}
    dates.add(as_of)
    return sorted(dates)


def compute_benchmark(
    transactions: list[Transaction],
    index_levels: dict[date, Decimal],
    as_of: date,
    name: str,
) -> Benchmark:
    """Replicate the portfolio's own cash flows into an index -- "what if I had bought
    the index on exactly the days I bought stocks".

    ::

        for each portfolio cash flow (date, amount):
              units = amount / index_level(date)
              accumulate units
        benchmark_terminal = total_units * index_level(as_of)
        benchmark_xirr     = xirr(same dates, same amounts, benchmark_terminal)

    The cash flows reused here are ``_transaction_flows``'s own signed convention (BUY
    negative, SELL/DIVIDEND positive -- section 4). Units accumulate with that sign
    FLIPPED: money that left the portfolio to buy stock buys index units; money that
    came back from a sale OR a dividend redeems units. Dividends redeeming rather than
    adding units is correct specifically because a TRI level already has dividends
    reinvested into it -- taking cash out as a real dividend is economically a
    redemption from the hypothetical index position, same as a sale. Reading the spec's
    ``units = amount / index_level(date)`` literally against the unflipped sign
    convention would make ``total_units`` (and therefore the terminal flow) negative for
    any net-bought portfolio, which breaks the XIRR solve outright (no sign change) --
    the flip is what makes the formula produce a sensible answer at all.

    The FINAL xirr solve reuses the original, UNflipped flows (same dates, same signed
    amounts as ``compute_xirr`` itself would see) with ``benchmark_terminal`` appended as
    the terminal flow -- exactly parallel to how ``xirr.py``'s ``_series`` appends the
    real portfolio's terminal market value.

    Raises:
        MissingIndexLevelError: any date ``required_index_dates`` would have named is
            absent from ``index_levels``. Never defaulted to the nearest available level
            -- see the docstring on the exception itself.
    """
    rows = ordered(transactions, as_of)
    flows = _transaction_flows(rows)
    if not flows:
        return Benchmark(name=name, xirr=None, terminal_value=None)

    needed = {d for d, _ in flows}
    needed.add(as_of)
    missing = sorted(d for d in needed if d not in index_levels)
    if missing:
        raise MissingIndexLevelError(name, missing)

    total_units = ZERO
    for flow_date, amount in flows:
        total_units += -amount / index_levels[flow_date]

    terminal = total_units * index_levels[as_of]

    final_flows = list(flows)
    if terminal != ZERO:
        final_flows.append((as_of, terminal))

    return Benchmark(name=name, xirr=_solve(final_flows), terminal_value=terminal)


def compute_period_benchmark(
    transactions: list[Transaction],
    index_levels: dict[date, Decimal],
    start_date: date,
    opening_value: Decimal,
    as_of: date,
    name: str,
) -> Benchmark:
    """Section 6 replication for section 4's period-XIRR cash flows.

    Opening portfolio value buys index units on the opening date. Subsequent recorded
    cash flows buy/redeem units with the same signs and dates as period XIRR. This is
    a hypothetical comparison, never a synthetic ledger transaction.
    """
    rows = [t for t in ordered(transactions, as_of) if t.txn_date > start_date]
    flows = [(start_date, -opening_value), *_transaction_flows(rows)]
    needed = {d for d, amount in flows if amount != ZERO} | {as_of}
    missing = sorted(d for d in needed if d not in index_levels)
    if missing:
        raise MissingIndexLevelError(name, missing)
    units = sum((-amount / index_levels[d] for d, amount in flows if amount != ZERO), ZERO)
    terminal = units * index_levels[as_of]
    return Benchmark(name=name, xirr=_solve([*flows, (as_of, terminal)]), terminal_value=terminal)


def required_series_index_dates(transactions: list[Transaction], dates: list[date]) -> list[date]:
    """Every date ``benchmark_series`` needs an index level for.

    Same contract as ``required_index_dates``, for the series rather than the single
    terminal figure: the cash-flow dates that fall on or before the LAST requested date,
    plus the requested dates themselves. Kept as its own function, and called by the
    router before ``benchmark_series``, for the same reason -- this module does no I/O.
    """
    if not dates:
        return []
    last = max(dates)
    rows = ordered(transactions, last)
    flows = _transaction_flows(rows)
    needed = {d for d, _ in flows}
    needed.update(dates)
    return sorted(needed)


def benchmark_series(
    transactions: list[Transaction],
    index_levels: dict[date, Decimal],
    dates: list[date],
    name: str,
) -> list[tuple[date, Decimal]]:
    """``compute_benchmark``'s replication, valued at every date in ``dates`` instead of
    only at ``as_of`` -- the line a chart draws beside market value and invested cost.

    This is section 6's rule and not a second, chart-only one. The portfolio's own cash
    flows buy and redeem index units on exactly the days they happened (units accumulate
    with the flow sign FLIPPED, for the reason ``compute_benchmark`` sets out at length),
    and each point is those units held at that date, marked at that date's index level::

        units(t)  = sum over flows on or before t of  -amount / index_level(flow_date)
        value(t)  = units(t) * index_level(t)

    Rebasing therefore needs no separate step and no anchor date: at the first
    contribution the line starts at exactly the amount contributed, which is what "rebased
    to your invested cost" means, and it stays comparable after every later contribution
    because those bought units too. A line rebased once at the start and left alone would
    silently ignore every rupee added since, and would sit below a portfolio that simply
    saved more.

    A date whose level is missing is OMITTED from the result rather than carried forward
    or interpolated -- the same posture ``app.db.prices`` takes, and the reason the plot
    can draw a genuine gap. Missing levels for the FLOW dates are a different matter and
    do raise, because a flow priced at the wrong level corrupts every later point.

    Raises:
        MissingIndexLevelError: a cash-flow date has no level. Never defaulted.
    """
    if not dates:
        return []

    targets = sorted(set(dates))
    rows = ordered(transactions, targets[-1])
    flows = sorted(_transaction_flows(rows))
    if not flows:
        return []

    missing = sorted({d for d, _ in flows} - set(index_levels))
    if missing:
        raise MissingIndexLevelError(name, missing)

    out: list[tuple[date, Decimal]] = []
    units = ZERO
    cursor = 0
    for target in targets:
        while cursor < len(flows) and flows[cursor][0] <= target:
            flow_date, amount = flows[cursor]
            units += -amount / index_levels[flow_date]
            cursor += 1
        level = index_levels.get(target)
        # Before the first contribution there is nothing invested and nothing to
        # replicate; a zero there would draw a line along the axis as if the benchmark
        # had lost everything.
        if level is None or units == ZERO:
            continue
        out.append((target, units * level))
    return out


__all__ = [
    "benchmark_series",
    "compute_benchmark",
    "required_index_dates",
    "required_series_index_dates",
]
