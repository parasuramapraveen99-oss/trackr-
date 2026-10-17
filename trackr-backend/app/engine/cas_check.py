"""Quantity corroboration of demat movements against an account's trade ledger.

A settlement date is not a trade date. Without a shared settlement identifier this
check reports CANDIDATE, never MATCHED. A date window only finds candidates; it cannot
establish settlement. Same-day buys/sells are netted, and multiple demat rows settling
on one day are grouped. A candidate trade day cannot explain two different settlement
days. Unknown narrations and corporate actions stay separate; no transactions are made.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from app.engine.types import Transaction, TxnType


class Movement(Protocol):
    isin: str
    settle_date: date
    credit: Decimal
    debit: Decimal
    kind: str


@dataclass(frozen=True)
class MovementCheck:
    isin: str
    settle_date: date
    net_quantity: Decimal
    status: str
    candidate_dates: tuple[date, ...] = ()


_MARKET = {"PURCHASE", "SALE", "FUND_PURCHASE", "FUND_REDEMPTION"}


def check_movements(
    movements: list[Movement],
    transactions: list[Transaction],
    account_id: str,
    candidate_days: int = 7,
) -> list[MovementCheck]:
    """Find quantity candidates within an explicit calendar window, without resolving
    any financial history. The caller supplies date-aware, verified ISIN identities.
    Account filtering also lives here so a second broker cannot close this broker's gap.
    """
    settlements: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal(0))
    for m in movements:
        if m.kind in _MARKET:
            settlements[m.isin, m.settle_date] += m.credit - m.debit
    trades: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal(0))
    for t in transactions:
        if t.account_id != account_id or t.txn_type not in {TxnType.BUY, TxnType.SELL}:
            continue
        trades[t.isin, t.txn_date] += t.quantity if t.txn_type == TxnType.BUY else -t.quantity
    candidates = {}
    for (isin, sd), qty in settlements.items():
        candidates[isin, sd] = tuple(
            sorted(
                td
                for (ti, td), tq in trades.items()
                if ti == isin and sd - timedelta(days=candidate_days) <= td <= sd and tq == qty
            )
        )
    usage = Counter((i, td) for (i, _), days in candidates.items() for td in days)
    results = []
    for (isin, sd), qty in sorted(settlements.items()):
        days = candidates[isin, sd]
        if not days:
            status = "NO_QUANTITY_CANDIDATE"
        elif len(days) != 1 or usage[isin, days[0]] != 1:
            status = "AMBIGUOUS"
        else:
            status = "CANDIDATE"
        results.append(MovementCheck(isin, sd, qty, status, days))
    return results
