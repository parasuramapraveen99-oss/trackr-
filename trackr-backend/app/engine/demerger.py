"""Demerger cost ratio -- docs/ENGINE_SPEC.md section 11.

A demerger carries two ratios: a quantity ratio (already applied by the depository and
recorded as ``DEMERGER_IN``) and a cost ratio, which comes from the company's scheme
document and is not applied automatically -- see docs/TAX_ENGINE.md "Corporate action
tax nuances". Until the scheme document is sourced, ``DEMERGER_IN`` leaves the original
security's cost pool untouched and the new security enters at zero cost (ENGINE_SPEC
sections 1 and 3).

``demerger_cost_split`` computes the correction once the cost ratio is known, and
returns it as a pair of new ``DEMERGER_COST_OUT``/``DEMERGER_COST_IN`` transactions --
never an edit to the historical ``BUY`` or the existing ``DEMERGER_IN`` row, per
docs/TAX_ENGINE.md's "Never mutate history" rule.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.types import ZERO, Source, Transaction, TxnType

ONE = Decimal("1")


def demerger_cost_split(
    original_isin: str,
    new_isin: str,
    txn_date: date,
    original_cost_basis: Decimal,
    cost_ratio_retained: Decimal,
    source_ref: str | None = None,
) -> tuple[Transaction, Transaction]:
    """Split ``original_cost_basis`` between the original and demerged security.

    ``original_cost_basis`` is the original security's cost pool at the demerger --
    read via ``compute_holdings`` (or the FIFO sum from ``compute_lots``) as of
    ``txn_date``, including the existing ``DEMERGER_IN`` row's quantity effect but
    before either of the two transactions this function returns. ``cost_ratio_retained``
    is the fraction of that cost basis the scheme document assigns to the original
    security; the remainder moves to ``new_isin``.

    Returns ``(cost_out, cost_in)``:
      - ``cost_out``: ``DEMERGER_COST_OUT`` on ``original_isin``.
      - ``cost_in``:  ``DEMERGER_COST_IN`` on ``new_isin``.

    Both are dated ``txn_date`` -- the demerger's ex-date, the same date as the existing
    ``DEMERGER_IN`` row, not whenever the scheme document happens to be confirmed. Since
    holdings and lots are always a full replay of the ledger and never stored, dating the
    correction at the ex-date makes it retroactive for free: any SELL between the ex-date
    and confirmation is priced correctly the next time anything replays, with no need to
    touch that SELL row.

    **Caller's responsibility, not enforced here**: when ``cost_in`` is appended to the
    transaction list, it must come *after* the existing ``DEMERGER_IN`` row for
    ``new_isin``. ``engine/ledger.py``'s ``ordered()`` sorts by ``txn_date`` alone and
    breaks same-date ties by insertion order (no ``id`` tiebreak) -- inserted out of
    order, the FIFO replay would try to spread cost across a lot that does not exist yet.
    This function only sees the two already-extracted values, not the full ledger, so it
    cannot check or fix that ordering itself.

    Raises:
        ValueError: ``cost_ratio_retained`` is not strictly between 0 and 1. A ratio of
            0 or 1 means nothing actually needs to move, which means this function
            should not have been called, not that the split should be a no-op.
    """
    if not (ZERO < cost_ratio_retained < ONE):
        raise ValueError(
            f"cost_ratio_retained must be strictly between 0 and 1, got {cost_ratio_retained}"
        )

    cost_transfer = original_cost_basis * (ONE - cost_ratio_retained)

    cost_out = Transaction(
        isin=original_isin,
        txn_date=txn_date,
        txn_type=TxnType.DEMERGER_COST_OUT,
        quantity=ZERO,
        price=ZERO,
        cost_transfer=cost_transfer,
        source=Source.CORP_ACTION,
        source_ref=source_ref,
    )
    cost_in = Transaction(
        isin=new_isin,
        txn_date=txn_date,
        txn_type=TxnType.DEMERGER_COST_IN,
        quantity=ZERO,
        price=ZERO,
        cost_transfer=cost_transfer,
        source=Source.CORP_ACTION,
        source_ref=source_ref,
    )
    return cost_out, cost_in
