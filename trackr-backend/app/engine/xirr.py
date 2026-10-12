"""XIRR -- docs/ENGINE_SPEC.md section 4.

The solver is ``pyxirr``. Hand-rolling Newton-Raphson here would be a way to introduce a
convergence bug into the one number the whole product is judged on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pyxirr

from app.engine.holdings import compute_holdings
from app.engine.ledger import ordered
from app.engine.types import (
    ACQUIRING_TYPES,
    ZERO,
    Transaction,
    TxnType,
)
from app.engine.valuation import total_market_value

#: Below this span, an annualised return is arithmetically valid and practically
#: nonsense. Section 4 says compute anyway, but say so.
PERIOD_TOO_SHORT_DAYS = 90

PERIOD_TOO_SHORT_WARNING = "period too short to annualise meaningfully"

#: Acquisitions that move cash OUT. TRANSFER_IN is deliberately absent although it is an
#: acquiring type for cost purposes: DATA_MODEL gives it a cash effect of 0, because the
#: shares arrive from elsewhere rather than being bought.
CASH_OUT_TYPES = frozenset({TxnType.BUY, TxnType.IPO_ALLOT})

#: Disposals that move cash IN. TRANSFER_OUT is likewise absent, for the same reason.
CASH_IN_TYPES = frozenset({TxnType.SELL, TxnType.BUYBACK})


def _transaction_flows(
    transactions: list[Transaction],
) -> list[tuple[date, Decimal]]:
    """Dated cash flows for transactions, excluding the terminal value.

    ====================  ======  =====================================
    event                 sign    amount
    ====================  ======  =====================================
    BUY, IPO_ALLOT          -     qty * price + charges
    SELL, BUYBACK           +     qty * price - charges
    DIVIDEND                +     dividend_amount
    corporate actions       0     omitted -- shares moved, not cash
    transfers               0     omitted -- shares moved, not cash
    ====================  ======  =====================================
    """
    flows: list[tuple[date, Decimal]] = []
    for txn in transactions:
        if txn.txn_type in CASH_OUT_TYPES:
            flows.append((txn.txn_date, -(txn.quantity * txn.price + txn.charges)))
        elif txn.txn_type in CASH_IN_TYPES:
            flows.append((txn.txn_date, txn.quantity * txn.price - txn.charges))
        elif txn.txn_type is TxnType.DIVIDEND and txn.dividend_amount != ZERO:
            flows.append((txn.txn_date, txn.dividend_amount))
    return flows


def _solve(flows: list[tuple[date, Decimal]]) -> Decimal | None:
    """Solve for the rate where the series nets to zero.

    Returns None -- never zero -- when the series cannot support an answer: fewer than
    two flows, no sign change, or a solver that does not converge. Zero would be a lie
    the caller cannot distinguish from a real result.
    """
    if len(flows) < 2:
        return None
    try:
        result = pyxirr.xirr([d for d, _ in flows], [a for _, a in flows])
    except pyxirr.InvalidPaymentsError:
        return None
    if result is None:
        return None
    return Decimal(str(result))


def _series(
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    as_of: date,
) -> list[tuple[date, Decimal]]:
    """Transaction flows plus the terminal market value of everything still held."""
    rows = ordered(transactions, as_of)
    flows = _transaction_flows(rows)
    terminal = total_market_value(compute_holdings(rows, as_of), prices)
    if terminal != ZERO:
        flows.append((as_of, terminal))
    return flows


def compute_xirr(
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    as_of: date,
) -> Decimal | None:
    """Money-weighted return across every transaction ever.

    Answers "what has my investing returned, all in?".

    Raises:
        MissingPriceError: any held position lacks a price. Never valued at zero to keep
            the calculation running.
    """
    return _solve(_series(transactions, prices, as_of))


def compute_xirr_holdings(
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    as_of: date,
) -> Decimal | None:
    """Money-weighted return across only the ISINs still held.

    Answers "how is the live book performing?".

    The two variants MUST BE REPORTED TOGETHER. In the golden fixture ``xirr_all`` is
    -17.44% while this is -34.81%: the live book is WORSE, meaning the positions that
    were sold outperformed the ones retained. Most portfolios show the opposite bias --
    sell the losers, keep the winners -- which flatters a holdings-only number. Showing
    both removes the choice to present whichever looks better.
    """
    rows = ordered(transactions, as_of)
    held = {h.isin for h in compute_holdings(rows, as_of)}
    return _solve(_series([t for t in rows if t.isin in held], prices, as_of))


def compute_period_xirr(
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    start_date: date,
    opening_value: Decimal,
    as_of: date,
) -> Decimal | None:
    """Money-weighted return over a window, for the adviser-onboarding case.

    ::

        flow 1     : (start_date, -opening_value)      portfolio value at onboarding
        flows 2..n : every transaction after start_date
        final flow : (as_of, +current market value)

    ``opening_value`` must be the MARKET value on ``start_date``, not cost. Sources: a
    CAS month-end table, or a stored Snapshot.

    EVERY transaction between ``start_date`` and ``as_of`` -- deposits, buys,
    withdrawals -- appears as its own dated flow. They are never folded into the opening
    or closing value. An implementation that uses only those two endpoints and drops the
    intermediate rows SILENTLY INFLATES the return whenever a deposit lands mid-period:
    the added money is counted as if it were growth. See the
    ``period_xirr_mid_period_deposit_not_inflated`` fixture case, where that bug turns a
    5% return into 205%.

    The terminal flow is the CURRENT market value of EVERYTHING held at ``as_of`` --
    including a position bought before ``start_date`` and never traded during the window.
    That replay needs the full transaction history, not only the rows after
    ``start_date``: a buy-and-hold position with no in-period transaction still has to
    appear in the closing value, or its entire contribution silently disappears from the
    flows and the reported return is wrong in the same direction the mid-period-deposit
    guard above protects against -- money the client already had is dropped rather than
    double-counted, but it is exactly as silent.

    Use ``period_warning`` alongside this for the short-window guard; section 4 describes
    one object carrying both, and the API composes them into ``Performance``.
    """
    all_rows = ordered(transactions, as_of)
    period_rows = [t for t in all_rows if t.txn_date > start_date]

    flows: list[tuple[date, Decimal]] = [(start_date, -opening_value)]
    flows.extend(_transaction_flows(period_rows))

    terminal = total_market_value(compute_holdings(all_rows, as_of), prices)
    if terminal != ZERO:
        flows.append((as_of, terminal))

    return _solve(flows)


def years_before(as_of: date, n: int) -> date:
    """``as_of`` minus ``n`` calendar years -- real date arithmetic, not ``days=365*n``,
    which drifts by a day every leap year and would make a "1Y XIRR" label subtly wrong.

    Falls back to 28 February when ``as_of`` is 29 February and the target year is not
    itself a leap year (mirrors ``dateutil.relativedelta``'s own convention) -- the only
    case ``date.replace(year=...)`` can raise here, since every other ``(month, day)``
    pair is valid in every year ``date`` supports.
    """
    try:
        return as_of.replace(year=as_of.year - n)
    except ValueError:
        return as_of.replace(month=2, day=28, year=as_of.year - n)


def financial_year_start(as_of: date) -> date:
    """1 April of the Indian financial year containing ``as_of``.

    April-to-March, so anything in January to March belongs to the FY that began the
    previous April.
    """
    year = as_of.year if as_of.month >= 4 else as_of.year - 1
    return date(year, 4, 1)


def compute_fy_xirr(
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    opening_value: Decimal,
    as_of: date,
) -> Decimal | None:
    """XIRR for the financial year to date -- the "XIRR this FY" headline.

    A useful default when onboarding a client whose full history is thin: it asks how the
    money has done since April, which is answerable even when the older ledger is not.

    WHAT THIS DOES NOT DO. It changes only WHICH CASH FLOWS the displayed percentage
    counts. It does not touch ``compute_holdings``, does not alter any cost basis, and
    fixes no cost-basis gap. A position whose cost is estimated is still estimated here;
    a shorter window does not make an unverified number verified.

    ``opening_value`` must be the portfolio's MARKET VALUE ON 1 APRIL -- not today's
    value, and not cost. Passing today's value would count this year's entire gain as if
    it had already existed on day one, pushing the reported return toward zero no matter
    how the year actually went. Get it from a stored ``Snapshot`` for 1 April, or from
    ``compute_snapshot`` with that date's prices; where neither exists, the honest move is
    to omit this metric rather than substitute a value that changes what it means.

    Never shown alone. It sits beside ``xirr_all`` and ``xirr_holdings``, which answer
    different questions over different windows.
    """
    return compute_period_xirr(
        transactions, prices, financial_year_start(as_of), opening_value, as_of
    )


def period_warning(start_date: date, as_of: date) -> str | None:
    """Flag a window too short for annualising to mean anything.

    Section 4 says compute it anyway -- refusing would be its own kind of dishonesty --
    but never present it unqualified.
    """
    if (as_of - start_date).days < PERIOD_TOO_SHORT_DAYS:
        return PERIOD_TOO_SHORT_WARNING
    return None


# Re-exported so callers building a cost pool can tell acquisitions apart from cash
# movements without reaching into two modules.
__all__ = [
    "ACQUIRING_TYPES",
    "compute_fy_xirr",
    "financial_year_start",
    "years_before",
    "CASH_IN_TYPES",
    "CASH_OUT_TYPES",
    "PERIOD_TOO_SHORT_DAYS",
    "PERIOD_TOO_SHORT_WARNING",
    "compute_period_xirr",
    "compute_xirr",
    "compute_xirr_holdings",
    "period_warning",
]
