"""FIFO lots and the shared lot replay -- docs/ENGINE_SPEC.md section 3.

``compute_lots`` and ``compute_realised`` are two views of one traversal, so the
traversal lives here once and ``engine/realised.py`` consumes it. Running them as
separate replays would let the open lots and the realised gains drift apart, which is
precisely the bug that makes a capital-gains statement fail to tie back to a holdings
report.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.engine.errors import (
    CorporateActionEvidenceError,
    DemergerAdjustmentError,
    NegativeHoldingError,
)
from app.engine.ledger import cash_paid, cash_received, ordered
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    FREE_SHARE_TYPES,
    ZERO,
    AcquisitionDateBasis,
    Lot,
    Realised,
    Term,
    Transaction,
    TxnType,
)

#: EQ's holding-period threshold, from schema/asset_classes.json and stated directly in
#: ENGINE_SPEC section 3. Passed as a parameter rather than read from that file, because
#: the engine performs no I/O -- see tests/test_lots.py, which asserts the two agree.
DEFAULT_LONG_TERM_DAYS = 365

#: Disposals that produce cash and therefore a realised gain. TRANSFER_OUT consumes lots
#: but is deliberately absent: DATA_MODEL gives it a cash effect of 0, and reporting a
#: realised loss equal to full cost on a transfer between one's own accounts would be
#: wrong. ENGINE_SPEC section 3 discusses only SELL, so this stays conservative.
REALISING_TYPES = frozenset({TxnType.SELL, TxnType.BUYBACK})


@dataclass
class _OpenLot:
    """A lot still open during replay. Mutable, unlike the Lot that is emitted."""

    open_date: date
    quantity: Decimal
    cost_per_unit: Decimal
    # STT embedded in cost_per_unit, tracked separately so a later disposal can report
    # a Section-48-compliant tax cost (app.engine.realised.tax_cost) without moving
    # cost_per_unit itself, which stays the real-cash figure. See ENGINE_SPEC and
    # docs/TAX_ENGINE.md's Session 10 status note.
    stt_per_unit: Decimal = ZERO
    acquisition_date_basis: str | None = None


#: raw_payload keys a derived corporate-action row carries in memory after
#: app.engine.action_replay rebuilds it. Read here; never stored by the engine.
ALLOTMENT_DATE_KEY = "corporate_action_allotment_date"
RECORD_DATE_KEY = "corporate_action_record_date"
EVIDENCE_WITHDRAWN_KEY = "evidence_withdrawn"


def bonus_acquisition_date(txn: Transaction) -> tuple[date, AcquisitionDateBasis]:
    """A BONUS lot's tax acquisition date and its basis -- ENGINE_SPEC section 3 (OD-1,
    6 Oct 2026, IMPLEMENTER_UNDER_OWNER_DELEGATION).

    The allotment date when evidence supplies one; otherwise the record date; otherwise
    the ex-date (``txn_date``). Both fallbacks are evidenced lower bounds, labelled
    PROVISIONAL, never an invented date. Quantity is unaffected: the lot is in the queue
    from ``txn_date`` either way.
    """
    payload = txn.raw_payload or {}
    if payload.get(ALLOTMENT_DATE_KEY):
        return date.fromisoformat(payload[ALLOTMENT_DATE_KEY]), AcquisitionDateBasis.ALLOTMENT
    if payload.get(RECORD_DATE_KEY):
        return (
            date.fromisoformat(payload[RECORD_DATE_KEY]),
            AcquisitionDateBasis.PROVISIONAL_RECORD_DATE,
        )
    return txn.txn_date, AcquisitionDateBasis.PROVISIONAL_EX_DATE


def raise_if_evidence_withdrawn(txn: Transaction) -> None:
    """Refuse to replay a derived row whose corporate-action evidence was withdrawn.

    ``app.engine.action_replay`` keeps such a row (rather than dropping it, which would
    silently change a quantity) and marks it; every strict replay stops here, and
    ``compute_holdings_partitioned`` names the ISIN in ``skipped``.
    """
    reason = (txn.raw_payload or {}).get(EVIDENCE_WITHDRAWN_KEY)
    if reason:
        raise CorporateActionEvidenceError(txn.isin, reason)


def _replay(
    transactions: list[Transaction],
    as_of: date | None = None,
    long_term_days: int = DEFAULT_LONG_TERM_DAYS,
) -> tuple[dict[str, deque[_OpenLot]], list[Realised]]:
    """Walk the ledger once, maintaining a FIFO queue per ISIN.

    An acquisition pushes a lot. A disposal consumes from the front, splitting the
    oldest lot when the disposal is smaller than it.
    """
    queues: dict[str, deque[_OpenLot]] = {}
    realised: list[Realised] = []

    for txn in ordered(transactions, as_of):
        raise_if_evidence_withdrawn(txn)
        queue = queues.setdefault(txn.isin, deque())

        if txn.txn_type in ACQUIRING_TYPES:
            if txn.quantity > ZERO:
                cost_total = cash_paid(txn)
                queue.append(
                    _OpenLot(
                        txn.txn_date,
                        txn.quantity,
                        cost_total / txn.quantity,
                        txn.stt / txn.quantity,
                    )
                )

        elif txn.txn_type is TxnType.SPLIT_ADJ:
            held = sum((lot.quantity for lot in queue), ZERO)
            if held <= ZERO:
                raise DemergerAdjustmentError(txn.isin)
            multiplier = (held + txn.quantity) / held
            for lot in queue:
                lot.quantity *= multiplier
                lot.cost_per_unit /= multiplier
                lot.stt_per_unit /= multiplier

        elif txn.txn_type in FREE_SHARE_TYPES:
            # Cost 0. This is what makes bonus-stripping rules computable later.
            # DEMERGER_IN is dated the corporate-action date and stays at cost 0 --
            # DEMERGER_COST_OUT/IN below are what correct that once a scheme document's
            # cost ratio is known, per ENGINE_SPEC section 11. A BONUS lot takes its
            # place in the queue at the ex-date (txn_date) but carries its tax
            # acquisition date -- see bonus_acquisition_date.
            if txn.quantity > ZERO:
                opened, basis = (
                    bonus_acquisition_date(txn)
                    if txn.txn_type is TxnType.BONUS
                    else (txn.txn_date, None)
                )
                queue.append(_OpenLot(opened, txn.quantity, ZERO, acquisition_date_basis=basis))

        elif txn.txn_type in DISPOSING_TYPES:
            _consume(txn, queue, realised, long_term_days)

        elif txn.txn_type is TxnType.DEMERGER_COST_OUT:
            _spread_cost_delta(queue, -txn.cost_transfer, txn.isin)

        elif txn.txn_type is TxnType.DEMERGER_COST_IN:
            _spread_cost_delta(queue, txn.cost_transfer, txn.isin)

        # DIVIDEND touches no lot.

    return queues, realised


def _spread_cost_delta(queue: deque[_OpenLot], delta: Decimal, isin: str) -> None:
    """Spread a demerger cost-ratio adjustment proportionally by quantity across every
    lot currently open on ``queue`` -- ENGINE_SPEC section 11.

    A scheme document gives one aggregate cost-ratio number for the whole position; it
    does not say which of several open lots loses (or gains) how much, so this mirrors
    ``holdings.py``'s disposal rule (``pool * qty / held``) rather than consuming the
    delta oldest-lot-first the way a SELL consumes quantity. Distributing ``delta``
    across lots weighted by each lot's own quantity, then re-expressing as a per-unit
    change, cancels the lot's own quantity out of the formula: every open lot moves by
    the same flat per-unit amount, ``delta / total_open_quantity``.

    Raises ``DemergerAdjustmentError`` if there is no open lot to adjust -- the paired
    DEMERGER_IN/BUY history for this ISIN is missing, or this row is mis-ordered or
    mis-dated relative to it.

    Only ``cost_per_unit`` moves here -- ``stt_per_unit`` is untouched. A demerger cost
    transfer reallocates cost basis per a scheme document; it is not itself an STT
    event, and this session does not extend STT bookkeeping across it.
    """
    total_quantity = sum((lot.quantity for lot in queue), ZERO)
    if total_quantity <= ZERO:
        raise DemergerAdjustmentError(isin)
    per_unit_shift = delta / total_quantity
    for lot in queue:
        lot.cost_per_unit += per_unit_shift


def _consume(
    txn: Transaction,
    queue: deque[_OpenLot],
    realised: list[Realised],
    long_term_days: int,
) -> None:
    """Consume ``txn.quantity`` from the front of ``queue``, recording realised gains."""
    remaining = txn.quantity
    if remaining <= ZERO:
        return

    available = sum((lot.quantity for lot in queue), ZERO)
    if remaining > available:
        raise NegativeHoldingError(txn.isin, available - remaining, txn.txn_date)

    # Charges reduce proceeds, spread across the disposal rather than charged to the
    # first lot consumed.
    proceeds_per_unit = cash_received(txn) / txn.quantity
    # Same treatment, but for the STT embedded in proceeds_per_unit above -- Section 48
    # disallows netting this out for tax purposes; see engine/realised.py's
    # tax_proceeds. Computed once per disposal, same as proceeds_per_unit.
    proceeds_stt_per_unit = txn.stt / txn.quantity

    while remaining > ZERO:
        lot = queue[0]
        taken = min(lot.quantity, remaining)

        if txn.txn_type in REALISING_TYPES:
            holding_days = (txn.txn_date - lot.open_date).days
            proceeds = taken * proceeds_per_unit
            cost = taken * lot.cost_per_unit
            realised.append(
                Realised(
                    quantity=taken,
                    proceeds=proceeds,
                    cost=cost,
                    gain=proceeds - cost,
                    term=Term.LONG if holding_days > long_term_days else Term.SHORT,
                    isin=txn.isin,
                    sell_date=txn.txn_date,
                    holding_days=holding_days,
                    cost_stt=taken * lot.stt_per_unit,
                    proceeds_stt=taken * proceeds_stt_per_unit,
                    acquisition_date_basis=lot.acquisition_date_basis,
                )
            )

        lot.quantity -= taken
        remaining -= taken
        if lot.quantity <= ZERO:
            queue.popleft()


def compute_lots(transactions: list[Transaction], as_of: date | None = None) -> list[Lot]:
    """Open FIFO lots, oldest first within each ISIN.

    Runs alongside weighted-average cost: the average is what the dashboard shows, the
    lots are what tax computation needs. After BUY 100 @ 200, BUY 100 @ 300, SELL 150,
    the average cost of what remains is 250 but the remaining LOT is 50 @ 300. Both are
    correct answers to different questions.
    """
    queues, _ = _replay(transactions, as_of)
    return [
        Lot(
            isin=isin,
            open_date=lot.open_date,
            quantity_open=lot.quantity,
            cost_per_unit=lot.cost_per_unit,
            acquisition_date_basis=lot.acquisition_date_basis,
        )
        for isin in sorted(queues)
        for lot in queues[isin]
        if lot.quantity > ZERO
    ]
