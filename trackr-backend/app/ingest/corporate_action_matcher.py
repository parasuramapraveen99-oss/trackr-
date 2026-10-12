"""Turns a reconciliation mismatch plus verified reference data into staged
transactions -- the bridge between app.engine.reconcile's text hypothesis and an actual
ledger row, without ever writing to the ledger itself.

Shaped like every other Layer-1 producer (``app.ingest.base.ParseResult``): a
``StagedRow`` per match, a ``Rejection`` -- carrying ``reconcile()``'s own
``likely_cause`` hint -- for every discrepancy nothing verified explains. This is NOT
an ``Adapter``: there is no file, no content bytes, no filename. Nothing here writes to
the ledger; ``app.db.corporate_actions.stage_corporate_actions`` is what calls this and
writes the result into the same ``import_batch``/``import_row`` tables every file
adapter's ``StagedRow`` lands in, so review and promote need no new code path.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.corporate_actions import CorporateAction, explain, screen
from app.engine.holdings import quantity_held_before
from app.engine.reconcile import reconcile
from app.engine.types import Source, Transaction, TxnType
from app.ingest.base import CONFIDENCE_DERIVED, ParseResult, Rejection, StagedRow

ZERO = Decimal("0")

_TXN_TYPE = {
    "BONUS": TxnType.BONUS,
    "SPLIT_ADJ": TxnType.SPLIT_ADJ,
    "DEMERGER_IN": TxnType.DEMERGER_IN,
}


def stage_corporate_actions(
    transactions: list[Transaction],
    declared_holdings: dict[str, Decimal],
    actions: list[CorporateAction],
    account_id: str,
    as_of: date | None = None,
) -> ParseResult:
    """Reconcile, then explain every mismatch a verified action can.

    ``actions`` is every corporate action on file that might be relevant to this
    account's securities -- callers load that from ``app.db.corporate_actions`` for
    exactly the ISINs this account holds (plus, for a DEMERGER, its own ISIN filed
    under the resulting company is what makes the match), not the whole reference
    table.

    A discrepancy nothing verified explains becomes a ``Rejection`` carrying
    ``reconcile()``'s own text hint -- existing behaviour for that case is unchanged,
    the same "a hypothesis for a human" outcome as today.
    """
    safe_actions, _failures = screen(actions)
    discrepancies = reconcile(transactions, declared_holdings, as_of=as_of)

    by_isin: dict[str, list[CorporateAction]] = {}
    for a in safe_actions:
        by_isin.setdefault(a.isin, []).append(a)

    rows: list[StagedRow] = []
    rejections: list[Rejection] = []
    notes: list[str] = []

    for row_number, d in enumerate(discrepancies, start=1):
        # A DEMERGER's discrepancy is on the RESULTING isin, filed under the parent
        # action's own isin -- so candidates come from actions on this isin directly,
        # plus any action elsewhere whose resulting_isin is this isin.
        candidates = list(by_isin.get(d.isin, [])) + [
            a
            for isin, group in by_isin.items()
            if isin != d.isin
            for a in group
            if a.resulting_isin == d.isin
        ]

        matched = None
        for a in candidates:
            # Each candidate carries its OWN ex_date -- recomputed per action, not
            # once for the whole discrepancy, since two candidates for the same ISIN
            # (e.g. a bonus and a later demerger) are not held-as-of the same day.
            held_qty = quantity_held_before(transactions, a.isin, a.ex_date)
            matched = explain(d, a, held_qty)
            if matched is not None:
                break

        if matched is None:
            rejections.append(
                Rejection(
                    row_number,
                    d.likely_cause or "no verified corporate action explains this quantity",
                    {
                        "isin": d.isin,
                        "ratio": str(d.ratio) if d.ratio is not None else None,
                        "difference": str(d.difference),
                    },
                )
            )
            continue

        rows.append(
            StagedRow(
                row_number=row_number,
                transaction=Transaction(
                    isin=matched.isin,
                    txn_date=matched.action.ex_date,
                    txn_type=_TXN_TYPE[matched.txn_type],
                    quantity=matched.quantity,
                    price=ZERO,
                    account_id=account_id,
                    source=Source.CORP_ACTION,
                    source_ref=f"{matched.action.source} {matched.action.source_url}",
                ),
                # Never CERTAIN: matched against reconciliation, not read off a document.
                confidence=CONFIDENCE_DERIVED,
                raw={
                    "matched_discrepancy_isin": d.isin,
                    "ex_date": matched.action.ex_date.isoformat(),
                    "action_type": matched.action.action_type.value,
                },
            )
        )
        notes.append(
            f"{matched.txn_type} on {matched.isin}: {matched.quantity} shares, matched to "
            f"{matched.action.source} ({matched.action.ex_date})"
        )

    return ParseResult(rows=rows, rejections=rejections, notes=notes)
