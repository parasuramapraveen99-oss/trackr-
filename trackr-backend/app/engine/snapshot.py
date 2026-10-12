"""Snapshots -- docs/ENGINE_SPEC.md section 8."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine.holdings import compute_holdings_partitioned
from app.engine.realised import compute_realised
from app.engine.types import ZERO, Snapshot, Transaction
from app.engine.valuation import value_holdings


def compute_snapshot(
    transactions: list[Transaction],
    prices: dict[str, Decimal],
    on_date: date,
    exclude_isins: set[str] | None = None,
) -> Snapshot:
    """Portfolio totals as at ``on_date``.

    Same functions as the live view, a historical ``as_of``, and historical prices.
    Note ``on_date`` is a parameter and not a clock read: the engine must never call
    ``date.today()``, because that makes tests non-deterministic and historical replay
    impossible.

    Persisting one of these per day is what later unlocks the portfolio-value chart and
    TWRR. They remain a cache of a derivation, always recomputable from the ledger --
    which is the property that makes backfilling one safe.

    PARTITIONED, AND THE EXCLUSIONS ARE RECORDED. An ISIN whose own ledger segment does
    not reconcile on ``on_date`` (a ``NegativeHoldingError``: missing buy history, not a
    short position) is left out of these totals and named in ``excluded_isins``, rather
    than aborting the snapshot entirely.

    The rule this used to enforce -- never compute a total while dropping a position --
    is not relaxed, because the operative word was SILENTLY. Naming what was left out is
    what keeps the figure honest: a reader can caveat it, and the exclusion clears itself
    from the record the moment the ledger gap is closed and the next rebuild replays
    cleanly. Aborting instead meant one broken holding out of twenty-four erased the
    entire value history, permanently, with no explanation attached to the absence.

    It also matches the live view, which already behaves exactly this way:
    ``app.routers.holdings._portfolio`` partitions the same broken ISINs out of the
    dashboard's own totals. Failing here instead left the dashboard's current value and
    this chart's last point disagreeing about which holdings exist.

    ``exclude_isins`` is the caller's own list of ISINs to leave out, merged into
    ``excluded_isins`` alongside the ones discovered here. It exists for the one case
    this function cannot judge for itself: a security that will NEVER have a price again
    (a retired ISIN after a reorganisation), which is indistinguishable from a price
    nobody has fetched yet without consulting ``price_absent`` -- a database this module
    must not touch. ``app.routers.analytical._run_snapshots`` makes that call and passes
    the answer down; see ``app.db.prices.absent_on_or_before``.

    Raises:
        MissingPriceError: a position that DID reconcile has no price on ``on_date`` and
            was not excluded by the caller. Still fatal, and deliberately so -- an
            unfetched price is a gap in the price pipe, fixable by fetching the bhavcopy,
            and quietly dropping the holding instead would turn a temporary gap into a
            permanently understated total that looks perfectly healthy.
    """
    exclude_isins = set(exclude_isins or ())
    ledger = (
        [t for t in transactions if t.isin not in exclude_isins] if exclude_isins else transactions
    )
    reconciled, skipped = compute_holdings_partitioned(ledger, on_date)
    excluded = exclude_isins | {s.isin for s in skipped}
    holdings = value_holdings(reconciled, prices)

    # The SAME exclusions have to apply to realised gain, not just to the holdings.
    # compute_realised matches FIFO lots, so a disposal with no matching acquisition
    # breaks it for exactly the same reason and on exactly the same ISINs -- partitioning
    # only the holdings would leave this line raising and the snapshot still impossible.
    # Filtering the ledger once, here, is what keeps both halves of a snapshot describing
    # the same set of holdings, which is what `excluded_isins` then truthfully names.
    usable = [t for t in transactions if t.isin not in excluded] if excluded else transactions

    invested = sum((h.invested for h in holdings), ZERO)
    market_value = sum((h.market_value for h in holdings), ZERO)
    realised_to_date = sum((r.gain for r in compute_realised(usable, on_date)), ZERO)

    return Snapshot(
        as_of=on_date,
        invested=invested,
        market_value=market_value,
        unrealised=market_value - invested,
        realised_to_date=realised_to_date,
        # ALL of it, caller-supplied included -- the docstring promises the merge, and
        # naming only the discovered half dropped a retired ISIN silently (rule 16).
        excluded_isins=tuple(sorted(excluded)),
    )
