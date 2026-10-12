"""31-January-2018 grandfathering -- docs/TAX_ENGINE.md "Grandfathering".

For equity (and equity mutual funds, once Phase 4 reaches that class) acquired before
1 Feb 2018:

    cost_of_acquisition = max( actual_buy_price,
                               min( fmv_31jan2018, actual_sell_price ) )

This caps a paper gain that accrued before LTCG was reintroduced (the 31 Jan 2018 FMV
substitutes for the real buy price, but never below what was actually paid, and never
above what was actually realised) -- and it must never move a lot's actual buy price
either later or earlier than what was really paid.

The date gate matters as much as the formula. Applicability is decided by the LOT's own
acquisition date -- from FIFO, i.e. ``app.engine.lots.compute_lots``'s ``Lot.open_date``
-- never the security's listing date or any other proxy. A security carrying a sourced
``fmv_31jan2018`` says nothing about any individual lot: a lot opened in 2021 in a
security that happens to have an FMV value (bought pre-2018, sold, then bought again
later) must not have this formula applied to it, which is exactly what the date check
below guards against.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.types import Lot

#: "Before 1 Feb 2018" per docs/TAX_ENGINE.md -- a lot opened on this date or later
#: never qualifies, no matter what fmv_31jan2018 the security carries. Named so a
#: future reader (or session) can find and question it rather than a bare literal
#: buried in a comparison.
GRANDFATHERING_CUTOFF_DATE = date(2018, 2, 1)


def grandfathered_cost_of_acquisition(
    lot: Lot,
    fmv_31jan2018: Decimal | None,
    sell_price: Decimal,
) -> Decimal:
    """Cost of acquisition to use for a realised lot's gain, applying the grandfathering
    formula only where it is actually reachable.

    Falls back to ``lot.cost_per_unit`` -- the lot's real buy price, i.e. no
    grandfathering applied -- in both cases the formula does not reach:
      - the lot was opened on or after ``GRANDFATHERING_CUTOFF_DATE``, or
      - ``fmv_31jan2018`` is ``None`` (most securities: never sourced, or listed after
        the cutoff and so never needed one).

    Otherwise: ``max(buy_price, min(fmv_31jan2018, sell_price))`` exactly as
    docs/TAX_ENGINE.md states it.
    """
    if lot.open_date >= GRANDFATHERING_CUTOFF_DATE:
        return lot.cost_per_unit
    if fmv_31jan2018 is None:
        return lot.cost_per_unit
    return max(lot.cost_per_unit, min(fmv_31jan2018, sell_price))
