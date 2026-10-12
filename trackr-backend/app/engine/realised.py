"""Realised gains -- docs/ENGINE_SPEC.md section 3.

One view of the FIFO replay in ``engine/lots.py``; see that module for the traversal.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.lots import DEFAULT_LONG_TERM_DAYS, _replay
from app.engine.types import Realised, Transaction


def compute_realised(
    transactions: list[Transaction],
    as_of: date | None = None,
    long_term_days: int = DEFAULT_LONG_TERM_DAYS,
) -> list[Realised]:
    """FIFO-matched realised gains, one entry per (disposal, lot) pair.

    Worked example from the spec::

        2024-01-10  BUY  100 @ 200
        2024-06-10  BUY  100 @ 300
        2025-03-01  SELL 150 @ 400

        100 from lot 1 @ 200 -> cost 20,000, proceeds 40,000, gain 20,000, LONG  (416 d)
         50 from lot 2 @ 300 -> cost 15,000, proceeds 20,000, gain  5,000, SHORT (264 d)

        remaining open: 50 @ 300

    A disposal spanning two lots produces two entries, each classified on its own
    holding period -- which is why the same sale can be part long-term and part short.

    ``long_term_days`` is a parameter rather than a file read, because the engine
    performs no I/O. 365 is stated in section 3 and matches ``schema/asset_classes.json``
    for EQ; a test asserts the two still agree. Thresholds differ by asset class and move
    with the annual budget, so a later phase reads it per class rather than hardcoding.

    The MVP computes and displays these. It does NOT produce a tax return -- section 112A
    grandfathering, bonus stripping and demerger cost ratios are a later phase.
    """
    _, realised = _replay(transactions, as_of, long_term_days)
    return realised


def tax_cost(realised: Realised) -> Decimal:
    """Cost of acquisition for capital-gains purposes. Income Tax Act Section 48
    disallows STT as a deductible expense, so this excludes the buy-side STT that
    ``realised.cost`` -- Phase 1's real-cash figure -- still includes.

    Equal to ``realised.cost`` whenever ``cost_stt`` is 0, which is every lot except
    one built from a contract-note-derived BUY (``app.ingest.zerodha_contract_note``
    is the only adapter that ever populates ``Transaction.stt``).
    """
    return realised.cost - realised.cost_stt


def tax_proceeds(realised: Realised) -> Decimal:
    """Full value of consideration for capital-gains purposes -- the mirror of
    ``tax_cost``. ``realised.proceeds`` already has ALL sale-side charges netted out,
    STT included (see ``lots.py``'s ``proceeds_per_unit``); Section 48 forbids netting
    STT specifically, so this adds it back.
    """
    return realised.proceeds + realised.proceeds_stt
