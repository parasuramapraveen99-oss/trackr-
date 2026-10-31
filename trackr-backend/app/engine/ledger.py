"""Shared ledger traversal.

Both the weighted-average replay (section 1) and the FIFO replay (section 3) walk the
same rows in the same order. That ordering is defined once, here, because two replays
that disagree about sequence would disagree about cost basis in ways that only surface
on a portfolio large enough to be painful to debug.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.types import Transaction, TxnType


def cash_paid(txn: Transaction) -> Decimal:
    """Rupees an acquisition cost: the printed amount when a statement gave one
    (``Transaction.cash_amount``), else quantity x price + charges.

    ONE DEFINITION for every reader -- XIRR, TWRR, the cost pool, FIFO lots, contribution
    and the cash bridge. Units x NAV is a check on a registrar's amount, not a substitute
    for it: units are rounded to 3 decimals, so the product misses by a fraction of a
    paisa (docs/MF_ETF_PLAN.md section 6).
    """
    if txn.cash_amount is not None:
        return txn.cash_amount
    return txn.quantity * txn.price + txn.charges


def cash_received(txn: Transaction) -> Decimal:
    """Rupees a disposal returned: the printed net amount when a statement gave one,
    else quantity x price - charges. See ``cash_paid``."""
    if txn.cash_amount is not None:
        return txn.cash_amount
    return txn.quantity * txn.price - txn.charges

#: The one same-date tiebreak this module applies on top of insertion order -- see
#: ``ordered``'s docstring. Deliberately narrower than ``DISPOSING_TYPES``/
#: ``ACQUIRING_TYPES``: it must never reach TRANSFER_IN/TRANSFER_OUT, where insertion
#: order can be the only record of which leg of an account-to-account move happened
#: first, and reordering it changes cost basis with no correctness gain (confirmed
#: against fixtures/golden_portfolio.json -- see below).
_SAME_DAY_LAST = (TxnType.SELL,)


def ordered(transactions: list[Transaction], as_of: date | None = None) -> list[Transaction]:
    """Ledger rows in replay order, optionally truncated at ``as_of``.

    ENGINE_SPEC section 1 specifies ``(txn_date, id)``. Sorting on ``txn_date`` alone is
    equivalent here and more robust: Python's sort is stable, so rows sharing a date keep
    their input order, which IS insertion order. The golden fixture carries no ``id``
    field at all, and 67 of its date/ISIN groups hold more than one row, so a sort that
    demanded ``id`` would have to invent a tiebreak.

    One further, narrow tiebreak on top of that: a same-date ``SELL`` sorts after every
    other same-date row for the same reason NSE cash equity has no way to end a day
    genuinely short -- an intraday SELL-before-BUY round trip is squared off by the
    broker before close, so by the time a contract note is issued the day's BUY and SELL
    legs are known to net non-negative. A source file that happens to list the SELL rows
    first (common -- a contract note is typically laid out as a Purchase table and a
    Sale table, not one table sorted by time) would otherwise raise
    ``NegativeHoldingError`` on a position that was never actually short.

    Scoped to ``SELL`` alone, not the wider ``DISPOSING_TYPES``
    (``TRANSFER_OUT``/``BUYBACK`` stay exactly where insertion order puts them):
    confirmed against ``fixtures/golden_portfolio.json``, whose only other same-date
    mixed-type group is a ``TRANSFER_OUT``-then-``TRANSFER_IN`` pair (an account
    migration, not a market trade) where reordering flips which balance the ``TRANSFER_
    OUT`` proportion is taken against and produces a different, wrong cost basis --
    the golden fixture's own six BUY/SELL mixed-date groups are, independently, already
    listed BUY-before-SELL, so this tiebreak changes nothing there and is a no-op change
    against every acceptance number. A ``BUYBACK`` is a company-initiated corporate
    action too, not an ordinary sale a same-day BUY could be expected to cover, so it is
    left out of scope for the same reason as ``TRANSFER_OUT``.

    Truncating at ``as_of`` is what makes historical replay work: the same functions,
    an earlier date, and prices from that date reproduce the portfolio as it stood.
    """
    rows = transactions if as_of is None else [t for t in transactions if t.txn_date <= as_of]
    # Splits transform opening lots, before any purchase on the ex-date.
    return sorted(
        rows,
        key=lambda t: (
            t.txn_date,
            -1 if t.txn_type is TxnType.SPLIT_ADJ else int(t.txn_type in _SAME_DAY_LAST),
        ),
    )
