"""Holdings -- docs/ENGINE_SPEC.md sections 1 and 2.

Weighted-average cost. FIFO lots run alongside in ``engine/lots.py`` for tax purposes;
the two answer different questions and are deliberately not derived from each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.engine.errors import (
    CorporateActionEvidenceError,
    DemergerAdjustmentError,
    NegativeHoldingError,
)
from app.engine.ledger import ordered
from app.engine.lots import compute_lots, raise_if_evidence_withdrawn
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    FREE_SHARE_TYPES,
    ZERO,
    Holding,
    Transaction,
    TxnType,
)

#: A price older than this many days is not a current price. Carrying one forward
#: silently and labelling it "current" is worse than admitting the price is missing.
MAX_PRICE_STALENESS_DAYS = 5

#: Average cost is carried to 4 decimal places. Not an arbitrary choice: it is the
#: precision the depository and broker holdings statements themselves report, so a
#: portfolio reconciled against one of those has to agree at the same precision.
AVG_COST_PLACES = Decimal("0.0001")


def _rounded_pool(pool: Decimal, quantity: Decimal) -> Decimal:
    """Re-express a cost pool as (average rounded to 4dp) x quantity -- Rule B.

    Returns the pool unchanged when the position is closed, since there is no average
    to round and dividing would raise.
    """
    if quantity <= ZERO:
        return pool
    return (pool / quantity).quantize(AVG_COST_PLACES, rounding=ROUND_HALF_UP) * quantity


def compute_holdings(transactions: list[Transaction], as_of: date | None = None) -> list[Holding]:
    """Replay the ledger into current positions.

    Positions at or below zero quantity are closed and excluded from the output.

    The cost-pool rules, which are the whole of this function:

    ==========================================  ==========  =========================
    txn_type                                    quantity    cost pool
    ==========================================  ==========  =========================
    BUY, IPO_ALLOT, TRANSFER_IN                 +qty        + (qty * price) + charges
    SELL, TRANSFER_OUT, BUYBACK                 -qty        - pool * (qty / held)
    BONUS, SPLIT_ADJ, DEMERGER_IN               +qty        unchanged
    DEMERGER_COST_OUT                           0           - cost_transfer
    DEMERGER_COST_IN                            0           + cost_transfer
    DIVIDEND                                    0           unchanged
    ==========================================  ==========  =========================

    ``DEMERGER_COST_OUT``/``DEMERGER_COST_IN`` are the later, paired correction to
    ``DEMERGER_IN``'s zero-cost placeholder above, once a scheme document's cost ratio
    is known -- see ENGINE_SPEC section 11 and ``engine/demerger.py``.

    ``avg_cost = pool / quantity``.

    A disposal removes cost PROPORTIONALLY, which is why selling never moves the average:
    a sale changes what you own, not what the shares you kept cost you. Free shares raise
    quantity and leave the pool alone, which is the mechanism that automatically corrects
    cost basis after a corporate action -- no ratio arithmetic anywhere.

    Precision -- RULE B. The cost pool is carried at full precision through the replay,
    and the average is rounded to 4 decimal places ONCE, when the holding is emitted;
    ``invested`` is then derived as ``avg_cost * quantity``.

    Where the rounding happens matters, and was settled by measurement rather than
    preference. Rounding at every acquisition instead reproduces only 16 of the golden
    fixture's 20 invested values; rounding once at presentation reproduces 18, and
    matches the broker holdings statement's own 4dp average for every one of them.
    Per-event rounding accumulates its own error across a long replay -- 25 acquisitions
    means 25 roundings, each feeding the next -- which is exactly what those two
    regressions were.

    Four decimal places is not arbitrary: it is the precision the depository and broker
    statements report, so a portfolio reconciled against one has to agree there.

    Price-derived fields (``last_price``, ``market_value``, ``unrealised``,
    ``unrealised_pct``, ``weight_pct``) are left unset. Prices are a separate pipe;
    valuation is section 2 and lives in ``engine/valuation.py``.

    Raises:
        NegativeHoldingError: a disposal exceeds the quantity held. That means buy
            history is missing, not that the investor is short.
        CorporateActionEvidenceError: a derived corporate-action row whose evidence was
            withdrawn (``app.engine.action_replay``); names the ISIN.
    """
    quantities: dict[str, Decimal] = {}
    pools: dict[str, Decimal] = {}
    first_buy: dict[str, date] = {}
    estimated: dict[str, bool] = {}

    for txn in ordered(transactions, as_of):
        raise_if_evidence_withdrawn(txn)
        isin = txn.isin
        held = quantities.get(isin, ZERO)
        pool = pools.get(isin, ZERO)

        if txn.txn_type in ACQUIRING_TYPES:
            quantity = held + txn.quantity
            quantities[isin] = quantity
            # Full precision here; Rule B rounds once at emission. See the docstring for
            # why rounding at each acquisition instead costs two of the golden values.
            pools[isin] = pool + txn.quantity * txn.price + txn.charges
            first_buy.setdefault(isin, txn.txn_date)
            # Sticky, and deliberately so: once an estimated cost is averaged into the
            # pool there is no way to separate the estimated rupees from the verified
            # ones, so the whole position inherits the doubt.
            if (txn.raw_payload or {}).get("cost_estimated"):
                estimated[isin] = True

        elif txn.txn_type in DISPOSING_TYPES:
            if txn.quantity > held:
                raise NegativeHoldingError(isin, held - txn.quantity, txn.txn_date)
            if txn.quantity > ZERO:
                # Multiply before dividing, and divide by the quantity held BEFORE this
                # row. One division instead of a division followed by a multiplication
                # keeps materially less error out of a 324-row replay.
                pools[isin] = pool - (pool * txn.quantity / held)
                quantities[isin] = held - txn.quantity

        elif txn.txn_type in FREE_SHARE_TYPES:
            # Quantity rises, the pool does not move -- so the average falls, and is
            # re-expressed at 4dp for the same reason as an acquisition. The POOL is
            # what must stay put here; that is the invariant the bonus case checks.
            quantities[isin] = held + txn.quantity
            pools.setdefault(isin, ZERO)

        elif txn.txn_type is TxnType.DEMERGER_COST_OUT:
            # ENGINE_SPEC section 11. Quantity is untouched -- a demerger's cost ratio
            # moves cost basis, not shares. Requires a current position: this row
            # correcting DEMERGER_IN's zero-cost placeholder with nothing to correct
            # means the paired DEMERGER_IN/BUY history is missing or mis-ordered.
            if held <= ZERO:
                raise DemergerAdjustmentError(isin)
            pools[isin] = pool - txn.cost_transfer

        elif txn.txn_type is TxnType.DEMERGER_COST_IN:
            # The other half of the same pair, on the demerged security. Must follow the
            # DEMERGER_IN row that created this position -- see ENGINE_SPEC section 11's
            # ordering note.
            if held <= ZERO:
                raise DemergerAdjustmentError(isin)
            pools[isin] = pool + txn.cost_transfer

        # DIVIDEND moves neither quantity nor pool. It is a cash event, and shows up in
        # the XIRR series rather than here.

    # FIFO cost, computed alongside rather than instead. Same ledger, same ordering, a
    # different question: `invested` answers "what did the shares I still hold cost me on
    # average", fifo_cost answers "which specific lots survived and what did THOSE cost".
    # They coincide unless a position has been partly sold. See ENGINE_SPEC section 1.
    fifo: dict[str, Decimal] = {}
    for lot in compute_lots(transactions, as_of):
        fifo[lot.isin] = fifo.get(lot.isin, ZERO) + lot.quantity_open * lot.cost_per_unit

    holdings = []
    for isin in sorted(quantities):
        quantity = quantities[isin]
        if quantity <= ZERO:
            continue
        pool = pools.get(isin, ZERO)
        avg_cost = (pool / quantity).quantize(AVG_COST_PLACES, rounding=ROUND_HALF_UP)
        # Rule B applies to BOTH conventions, or they are not comparable. Without it the
        # two differ even for a position that was never sold -- purely because one is a
        # 4dp-average presentation and the other a raw lot sum -- which would bury the
        # real difference (partial disposals) under rounding noise.
        raw_fifo = fifo.get(isin)
        fifo_avg = (
            (raw_fifo / quantity).quantize(AVG_COST_PLACES, rounding=ROUND_HALF_UP)
            if raw_fifo is not None and quantity > ZERO
            else None
        )
        fifo_cost = fifo_avg * quantity if fifo_avg is not None else None
        holdings.append(
            Holding(
                isin=isin,
                quantity=quantity,
                avg_cost=avg_cost,
                invested=avg_cost * quantity,
                first_buy_date=first_buy.get(isin),
                cost_estimated=estimated.get(isin, False),
                fifo_cost=fifo_cost,
                fifo_avg=fifo_avg,
            )
        )
    return holdings


@dataclass
class SkippedHolding:
    """One ISIN whose replay could not complete -- excluded by
    ``compute_holdings_partitioned`` rather than that failure aborting every other
    holding. Carries whatever ``NegativeHoldingError``/``DemergerAdjustmentError``/
    ``CorporateActionEvidenceError`` itself carried, plus ``str(error)`` so a caller
    always has a ready-to-show message even for the fields that don't apply to every
    error type. ``reason_code`` is set only for withdrawn corporate-action evidence."""

    isin: str
    message: str
    deficit: Decimal | None = None
    as_of: date | None = None
    reason_code: str | None = None


def _end_of_day_shortfall(rows: list[Transaction], as_of: date | None) -> Decimal:
    """The largest end-of-day deficit in one ISIN's quantity history, in shares.

    Quantity only, netted per day: acquisitions and free shares add, disposals subtract,
    every other row moves no quantity. Same day-netting as ``app.engine.gap_resolution.
    find_gaps``, so a shortfall shown to the investor is the one a gap closes against.
    """
    by_day: dict[date, Decimal] = {}
    for t in rows:
        if as_of is not None and t.txn_date > as_of:
            continue
        if t.txn_type in ACQUIRING_TYPES or t.txn_type in FREE_SHARE_TYPES:
            delta = t.quantity
        elif t.txn_type in DISPOSING_TYPES:
            delta = -t.quantity
        else:
            continue
        by_day[t.txn_date] = by_day.get(t.txn_date, ZERO) + delta
    running, worst = ZERO, ZERO
    for day in sorted(by_day):
        running += by_day[day]
        worst = min(worst, running)
    return -worst


def compute_holdings_partitioned(
    transactions: list[Transaction], as_of: date | None = None
) -> tuple[list[Holding], list[SkippedHolding]]:
    """Same replay as ``compute_holdings``, except an ISIN whose ledger doesn't
    reconcile (a ``NegativeHoldingError``, ``DemergerAdjustmentError`` or
    ``CorporateActionEvidenceError``) is excluded
    from the result instead of that one broken segment blocking every other holding --
    the same "partition, don't block" posture ``routers/holdings.py``'s
    ``_portfolio``/``UnpricedBanner`` already gives a missing price, extended here to a
    broken ledger segment instead of a missing price.

    Deliberately NOT what ``compute_holdings`` itself does. The rule for anything that
    computes a number FROM the holdings is not "never partition" -- it is never partition
    SILENTLY, because a total computed while quietly dropping a position is a confidently
    wrong number, exactly what CLAUDE.md rule 4 exists to prevent for prices.

    Two callers satisfy that differently, and both are correct:

      - The holdings/summary screen just needs to LIST what it safely can, and names the
        excluded ISINs beside the list (``routers/holdings.py``'s ``_skipped``), so one
        bad ISIN does not take an investor's whole dashboard down with it.
      - ``engine/snapshot.py`` DOES use this, and records what it left out in
        ``Snapshot.excluded_isins`` (migration 026). Refusing to snapshot at all, which is
        what it used to do, erased an entire value history over one broken holding and
        attached no explanation to the absence -- strictly worse than a figure that says
        what it excludes. See docs/ARCHITECTURE_INVARIANTS.md #16.

    XIRR and contribution still use the strict ``compute_holdings``.

    Re-runs the full replay once per excluded ISIN rather than resuming mid-loop --
    each ISIN's state in ``compute_holdings``'s single pass is independent of every
    other ISIN's (all state is keyed by isin), so simply removing a broken ISIN's rows
    and replaying the rest from scratch is exactly equivalent to what a resumable
    replay would compute, without the two replay implementations risking drift.
    Terminates: every retry either returns or permanently removes one more ISIN's rows,
    and there are finitely many ISINs.
    """
    excluded: dict[str, SkippedHolding] = {}
    remaining = transactions
    while True:
        try:
            return compute_holdings(remaining, as_of), list(excluded.values())
        except NegativeHoldingError as exc:
            # The engine's own deficit is the shortfall at the first sell that ran past
            # the balance, so it depends on the order of same-day sells. Report the
            # end-of-day shortfall instead -- the figure ``find_gaps`` closes against.
            shortfall = _end_of_day_shortfall(
                [t for t in remaining if t.isin == exc.isin], as_of
            )
            corrected = NegativeHoldingError(exc.isin, -shortfall, exc.as_of)
            excluded[exc.isin] = SkippedHolding(
                isin=exc.isin, message=str(corrected), deficit=shortfall, as_of=exc.as_of
            )
        except DemergerAdjustmentError as exc:
            if exc.isin is None:
                raise
            excluded[exc.isin] = SkippedHolding(isin=exc.isin, message=str(exc))
        except CorporateActionEvidenceError as exc:
            excluded[exc.isin] = SkippedHolding(
                isin=exc.isin, message=str(exc), reason_code=exc.reason_code
            )
        remaining = [t for t in remaining if t.isin not in excluded]


def exclude_unreconciled(
    transactions: list[Transaction], as_of: date | None = None
) -> tuple[list[Transaction], list[SkippedHolding]]:
    """The ledger minus every ISIN whose replay does not reconcile by ``as_of``, plus the
    named exclusions -- the input XIRR, lots, realised and contribution compute on.

    Owner amendment 2026-10-08 (docs/ARCHITECTURE_INVARIANTS.md rules 5 and 16): one
    security with incomplete history (a bonus not yet on file, an IPO allotment no contract
    note shows) must not blank the whole portfolio. Every row of the broken ISIN is dropped
    -- its buys AND its sells -- so what remains is a self-consistent ledger, never one
    with half a position. Callers MUST surface the returned list; a figure computed on the
    remainder without naming what it left out is the silent exclusion rule 16 forbids.
    """
    _holdings, skipped = compute_holdings_partitioned(transactions, as_of)
    if not skipped:
        return transactions, []
    broken = {s.isin for s in skipped}
    return [t for t in transactions if t.isin not in broken], skipped


def quantity_held_before(transactions: list[Transaction], isin: str, ex_date: date) -> Decimal:
    """Quantity of ``isin`` actually held the day before ``ex_date``.

    The one correct reference point for applying a BONUS/SPLIT/DEMERGER multiplier: a
    corporate action must be applied to what was held AT the action, never to "whatever
    is held now" -- a later, unrelated purchase multiplied by the same ratio would
    manufacture shares that were never actually credited. See
    app.ingest.corporate_action_matcher, the first caller this was written for.

    Partitioned, not the strict ``compute_holdings``: an unrelated ISIN elsewhere in the
    transaction list being broken (a different security's NegativeHoldingError) must
    not stop THIS isin's own, independent quantity from being computed.
    """
    cutoff = ex_date - timedelta(days=1)
    holdings, _skipped = compute_holdings_partitioned(transactions, cutoff)
    return next((h.quantity for h in holdings if h.isin == isin), ZERO)
