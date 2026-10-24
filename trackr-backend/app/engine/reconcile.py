"""Reconciliation -- docs/ENGINE_SPEC.md section 7."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.holdings import compute_holdings_partitioned
from app.engine.types import ZERO, Discrepancy, Transaction

#: How close a ratio must be to a whole number before it is read as that corporate
#: action. Tight, because 2.0 and 2.02 mean different things and a loose match would
#: hand a human a confident wrong diagnosis.
RATIO_TOLERANCE = Decimal("0.01")

#: ratio -> the corporate action that most often produces it.
RATIO_CAUSES = {
    Decimal(2): "1:1 bonus, or 1:2 split",
    Decimal(3): "1:2 bonus, or 1:3 split",
    Decimal(5): "1:5 split",
    Decimal(10): "1:10 split",
}


def _likely_cause(computed: Decimal, declared: Decimal, ratio: Decimal | None) -> str:
    """Diagnose a mismatch. A hypothesis for a human, never an instruction to act."""
    if computed == ZERO:
        return "transfer-in never recorded, or a new account"
    if declared == ZERO:
        return "full exit, or transfer-out not recorded"
    if ratio is not None:
        for candidate, cause in RATIO_CAUSES.items():
            if abs(ratio - candidate) <= RATIO_TOLERANCE:
                return cause
    if declared > computed:
        return "missed buy contract note"
    return "transfer-out, buyback, or missed sell"


def reconcile(
    transactions: list[Transaction],
    declared_holdings: dict[str, Decimal],
    as_of: date | None = None,
) -> list[Discrepancy]:
    """Compare computed holdings against CAS or broker-declared quantities.

    Cause heuristics from the ratio ``declared / computed``:

    ================  ==================================================
    ratio             likely cause
    ================  ==================================================
    about 2           1:1 bonus, or 1:2 split
    about 3           1:2 bonus, or 1:3 split
    about 5 or 10     split
    computed = 0      transfer-in never recorded, or a new account
    declared = 0      full exit, or transfer-out not recorded
    other > 1         missed buy contract note
    other < 1         transfer-out, buyback, or missed sell
    ================  ==================================================

    This single check catches corporate actions, off-market transfers, missed contract
    notes and parser bugs. It is the cheapest high-value component in the system, and
    the golden fixture is the standing proof: every corporate action recovered for it
    was found by exactly this comparison.

    Reconcile on TRADE DATE, and expect the last day or two before a statement date to
    differ -- that is settlement timing, not a bug. Validated against production data:
    June CAS plus tradebook movements reproduced the July CAS across all 17 positions
    with zero mismatches, correctly excluding three trades dated 31-Jul that settled
    3-Aug.

    Only ISINs that actually disagree are returned. A clean reconciliation is an empty
    list, not a list of zeroes.

    Broken ledger segments are reported as UNCOMPUTABLE, with null quantities and
    their original error. They must survive even when the declared balance is zero.
    """
    computed_holdings, broken = compute_holdings_partitioned(transactions, as_of)
    computed = {h.isin: h.quantity for h in computed_holdings}
    broken_by_isin = {s.isin: s for s in broken}
    broken_isins = set(broken_by_isin)

    discrepancies = []
    for isin in sorted(set(computed) | set(declared_holdings) | broken_isins):
        computed_qty = computed.get(isin, ZERO)
        declared_qty = declared_holdings.get(isin, ZERO)
        if isin in broken_by_isin:
            failure = broken_by_isin[isin]
            discrepancies.append(
                Discrepancy(
                    isin=isin,
                    computed_qty=None,
                    declared_qty=declared_qty,
                    difference=None,
                    status="UNCOMPUTABLE",
                    error=failure.message,
                    error_date=failure.as_of,
                    deficit=failure.deficit,
                    reason_code=failure.reason_code or "LEDGER_INCOMPLETE",
                    likely_cause=(
                        "Cannot reconcile—missing history. Acquisition or corporate-action "
                        "history is missing or invalid. " + failure.message
                    ),
                )
            )
            continue
        if computed_qty == declared_qty:
            continue

        ratio = declared_qty / computed_qty if computed_qty != ZERO else None
        discrepancies.append(
            Discrepancy(
                isin=isin,
                computed_qty=computed_qty,
                declared_qty=declared_qty,
                difference=declared_qty - computed_qty,
                ratio=ratio,
                likely_cause=_likely_cause(computed_qty, declared_qty, ratio),
            )
        )
    return discrepancies
