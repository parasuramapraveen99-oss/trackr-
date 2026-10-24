"""Evidence-bounded comparisons. Never produces or modifies financial history.

Transaction evidence may originate with the same broker as the ledger. Agreement
proves consistency with the supplied document only, not independent corroboration or
complete financial history. The caller must preserve that coverage label.
"""

from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.engine.holdings import compute_holdings_partitioned
from app.engine.reconcile import reconcile
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    ZERO,
    Discrepancy,
    Transaction,
)


def holdings_report(transactions, declared, as_of: date, *, complete=False, pending=()):
    """All positions, including closed, unchecked and uncomputable ones.

    Pending transactions must be individually evidenced and validated by the caller.
    They explain a difference; the trade-date computed quantity is never overwritten.
    """
    positions, _ = compute_holdings_partitioned(transactions, as_of)
    computed = {h.isin: h.quantity for h in positions}
    differences = {d.isin: d for d in reconcile(transactions, declared, as_of)}
    isins = set(computed) | set(declared) | set(differences)
    isins.update(t.isin for t in transactions if t.txn_date <= as_of)
    pending_delta = {}
    for t in pending:
        if t.txn_date > as_of or t.txn_type not in ACQUIRING_TYPES | DISPOSING_TYPES:
            raise ValueError("Pending settlement must identify an in-scope acquisition/disposal")
        direction = 1 if t.txn_type in ACQUIRING_TYPES else -1
        pending_delta[t.isin] = pending_delta.get(t.isin, ZERO) + direction * t.quantity
    result = []
    for isin in sorted(isins):
        row = differences.get(isin) or Discrepancy(
            isin, computed.get(isin, ZERO), declared.get(isin, ZERO), ZERO, status="MATCHED"
        )
        if row.status == "UNCOMPUTABLE":
            if isin not in declared and not complete:
                row = replace(row, declared_qty=None)
        elif isin not in declared and not complete:
            row = replace(
                row,
                declared_qty=None,
                difference=None,
                ratio=None,
                status="NOT_CHECKED",
                likely_cause="Absent from partial statement",
            )
        elif (
            pending_delta.get(isin, ZERO) != ZERO
            and row.computed_qty - pending_delta[isin] == row.declared_qty
        ):
            row = replace(
                row,
                status="PENDING_SETTLEMENT",
                likely_cause="Difference explained by evidenced unsettled trades",
            )
        result.append(row)
    return result


HISTORY_FIELDS = (
    "isin",
    "txn_date",
    "txn_type",
    "quantity",
    "price",
    "charges",
    "stt",
    "dividend_amount",
    "tds_amount",
    "cost_transfer",
)


def compare_history(ledger: list[Transaction], evidence: list[Transaction]) -> dict:
    """Exact multiset comparison, preserving duplicates and zero-net round trips.

    No fuzzy pairing: a differing date/amount appears on both unmatched sides. Input
    must already be bounded to one account, date range and declared transaction types.
    """

    def key(t):
        return tuple(getattr(t, field) for field in HISTORY_FIELDS)

    actual, external = Counter(map(key, ledger)), Counter(map(key, evidence))
    matched = actual & external
    rows = []
    for status, counter in [
        ("MATCHED", matched),
        ("MISSING_FROM_LEDGER", external - actual),
        ("MISSING_FROM_EVIDENCE", actual - external),
    ]:
        for values, count in sorted(counter.items(), key=lambda item: str(item[0])):
            rows.append(
                {
                    "status": status,
                    "count": count,
                    **{
                        field: (
                            format(value, "f")
                            if isinstance(value, Decimal)
                            else str(value)
                            if value is not None
                            else None
                        )
                        for field, value in zip(HISTORY_FIELDS, values, strict=True)
                    },
                }
            )
    return {
        "rows": rows,
        "matched": sum(matched.values()),
        "missing_from_ledger": sum((external - actual).values()),
        "missing_from_evidence": sum((actual - external).values()),
    }
