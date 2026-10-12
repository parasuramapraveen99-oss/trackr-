"""Other return measures -- docs/ENGINE_SPEC.md section 5."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.ledger import ordered
from app.engine.types import ZERO, Transaction
from app.engine.xirr import CASH_IN_TYPES, CASH_OUT_TYPES


def compute_net_invested(transactions: list[Transaction], as_of: date) -> Decimal:
    """Net lifetime cash contributed.

    Every BUY/IPO_ALLOT cash outflow minus every SELL/BUYBACK cash inflow, across the
    entire transaction history -- not just currently-held positions. Same type sets and
    sign convention as ``xirr._transaction_flows``: dividends and transfers are excluded,
    because neither is capital the client contributed.
    """
    rows = ordered(transactions, as_of)
    invested = ZERO
    for txn in rows:
        if txn.txn_type in CASH_OUT_TYPES:
            invested += txn.quantity * txn.price + txn.charges
        elif txn.txn_type in CASH_IN_TYPES:
            invested -= txn.quantity * txn.price - txn.charges
    return invested


def compute_absolute_return(
    net_invested: Decimal, market_value: Decimal, realised_to_date: Decimal
) -> Decimal | None:
    """(market_value + realised_to_date - net_invested) / net_invested.

    None when net_invested <= 0. Heavy net withdrawals -- or a full exit at a profit --
    can drive lifetime net contributions to zero or negative even though real money was
    made; dividing by that would be undefined or actively misleading, not just a
    divide-by-zero crash. Same convention as ``unrealised_pct`` in valuation.py.
    """
    if net_invested <= ZERO:
        return None
    return (market_value + realised_to_date - net_invested) / net_invested
