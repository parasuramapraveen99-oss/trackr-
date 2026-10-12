"""TWRR (time-weighted rate of return) -- docs/ENGINE_SPEC.md section 5.

A genuinely different algorithm from XIRR, not a variant of it: XIRR solves for a single
rate across dated cash flows (a root-find, delegated to ``pyxirr``), while TWRR chains
sub-period returns geometrically between consecutive persisted snapshots, each closed by
that sub-period's external flow. No root-finding is needed for the chain itself -- only
annualising the final product needs a fractional power, done in Decimal the same way
``app/engine/valuation.py``'s ``_cagr`` already does.

The chaining convention (which events count as a flow, dividend treatment, annualising)
was NOT specified anywhere in this repo until the product decision pinned in
ENGINE_SPEC section 5 on 2026-08-20 -- this module implements that pin, not a guess.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.engine.types import ZERO, Snapshot, Transaction, TxnType
from app.engine.valuation import DAYS_PER_YEAR

#: Portfolio-side sign of every cash-moving transaction type -- the mirror image of
#: app.engine.xirr's investor-side CASH_OUT_TYPES/CASH_IN_TYPES/DIVIDEND handling
#: (ENGINE_SPEC section 4's flow table, negated). A BUY moves cash INTO the tracked
#: portfolio; a SELL or a DIVIDEND both move cash OUT of it the same way, because this
#: system has no cash sleeve -- compute_snapshot's market_value is holdings-only, so
#: proceeds from either leaving the account look identical from the portfolio's side.
PORTFOLIO_IN_TYPES = frozenset({TxnType.BUY, TxnType.IPO_ALLOT})
PORTFOLIO_OUT_TYPES = frozenset({TxnType.SELL, TxnType.BUYBACK})


def _period_flow(transactions: list[Transaction], after: Snapshot, through: Snapshot) -> Decimal:
    """Net external flow INTO the portfolio for transactions dated in
    ``(after.as_of, through.as_of]`` -- exactly the window ``through``'s snapshot already
    reflects, since ``compute_snapshot`` includes every transaction dated on or before
    its ``as_of``.
    """
    total = ZERO
    for txn in transactions:
        if not (after.as_of < txn.txn_date <= through.as_of):
            continue
        if txn.txn_type in PORTFOLIO_IN_TYPES:
            total += txn.quantity * txn.price + txn.charges
        elif txn.txn_type in PORTFOLIO_OUT_TYPES:
            total -= txn.quantity * txn.price - txn.charges
        elif txn.txn_type is TxnType.DIVIDEND and txn.dividend_amount != ZERO:
            total -= txn.dividend_amount
    return total


def compute_twrr(
    snapshot_series: list[Snapshot],
    transactions: list[Transaction],
) -> Decimal | None:
    """Annualised time-weighted return across a series of persisted snapshots.

    ``snapshot_series`` is consecutive PERSISTED rows, not necessarily daily-adjacent --
    a gap in the backfill (a missed day) simply widens that one sub-period rather than
    being filled or interpolated, matching ``list_snapshots_range``'s own "absent, never
    filled" contract.

    Returns ``None`` -- never zero, never a guess -- when the series cannot support an
    answer: fewer than two snapshots, a sub-period opening at zero value (the account
    held nothing yet, or emptied out completely -- the return-from-zero ratio is
    undefined), a non-positive total elapsed span, or a chained product that is
    non-positive (a total wipeout has no well-defined annualised rate, same reasoning
    ``_cagr`` uses for a non-positive base).
    """
    if len(snapshot_series) < 2:
        return None

    series = sorted(snapshot_series, key=lambda s: s.as_of)

    chain = Decimal(1)
    for prev, curr in zip(series, series[1:], strict=False):
        if prev.market_value == ZERO:
            return None
        flow = _period_flow(transactions, prev, curr)
        sub_return = (curr.market_value - flow) / prev.market_value - 1
        chain *= 1 + sub_return

    total_days = (series[-1].as_of - series[0].as_of).days
    if total_days <= 0 or chain <= ZERO:
        return None

    try:
        return chain ** (DAYS_PER_YEAR / Decimal(total_days)) - 1
    except InvalidOperation:
        return None


__all__ = ["PORTFOLIO_IN_TYPES", "PORTFOLIO_OUT_TYPES", "compute_twrr"]
