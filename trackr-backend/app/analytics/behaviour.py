"""Patterns in how a ledger was TRADED, as distinct from what it now holds.

TWO RULES GOVERN EVERYTHING IN THIS MODULE, and they are the reason it can exist at all.

**1. A detector may report a pattern in the ledger. It may never report a state of mind.**
Same shape as invariant 3 -- detection may infer, resolution may not -- and the same
principle ``verdict.py`` states as "NO VERDICT LANGUAGE ... that is a judgement on the
person, not a description of a portfolio". A label like "disposition effect" is a
diagnosis of a person from their trade history, and this data cannot support one: the
ledger records what was done, never what was considered and declined, never why, never
what else the household owns. So the functions here are named for the MEASUREMENT --
``holding_period_asymmetry``, not "loss aversion" -- and the prose layer above them is
constrained to the same vocabulary.

**2. Every detector reports its own sample.** A ratio computed from four disposals is
noise wearing a number's clothes. Each result carries the counts behind it and a
``sufficient`` flag against a published minimum, so a caller can refuse to speak rather
than qualify heavily.

WHAT IS DELIBERATELY NOT HERE, and why each is refused rather than approximated:

  - **Loss aversion / risk tolerance as traits.** Need counterfactual choices. The
    ledger holds no record of an option declined, so no utility curve is recoverable.
  - **Overconfidence.** Operationalised as turnover against a STATED belief. A
    buy-and-hold shadow portfolio measures an outcome, not a belief, and naming that
    outcome "overconfidence" is the diagnosis rule 1 forbids.
  - **Herding.** Needs other investors. This is single-tenant data by design.
  - **Anchoring, as a separate detector.** Not separable from holding-period asymmetry
    on this data; a second name for the same measurement double-counts it.
  - **Panic selling.** "Sold within k days of a drawdown" is computable and is a
    coincidence, not a motive. Motive is refused.
  - **Recency / performance chasing.** Would need each purchase ranked against the
    universe AS IT STOOD on the buy date. The universe history exists
    (``universe_member.added_on``) but factor ranks are computed as-of today only.
    NOT YET DETECTABLE, BUILDABLE LATER -- approximating it with today's ranks would be
    inventing financial history.

Layering: this is ``app/analytics/``, so floats are fine for the statistics and money
arrives as Decimal. It imports only from ``app.engine``, never a database.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.engine.types import Realised, Transaction, TxnType

#: Disposals needed before a holding-period comparison means anything. Below this a
#: single unusual exit moves the median.
MIN_DISPOSALS = 8

#: Distinct trading dates needed before an activity pattern means anything.
MIN_ACTIVE_DAYS = 4

#: A repurchase inside this window is a round trip rather than an unrelated later
#: decision. Ninety days is the conventional window and is stated, not tuned.
ROUND_TRIP_DAYS = 90

#: Acquisition types that put shares in at a price the investor paid. Free-share events
#: (BONUS, SPLIT_ADJ, DEMERGER_IN) are excluded everywhere in this module: they are not
#: decisions, and counting them as purchases would report activity nobody chose.
_PURCHASES = (TxnType.BUY, TxnType.IPO_ALLOT)


@dataclass(frozen=True, slots=True)
class HoldingPeriodAsymmetry:
    """How long gains were held against how long losses were.

    Reported separately either side of the long-term boundary, because a gain held just
    past it may be a tax decision rather than a behavioural one, and folding the two
    together would attribute one to the other.
    """

    median_gain_days: float | None
    median_loss_days: float | None
    #: loss median / gain median. Above 1 means losses were held longer.
    ratio: float | None
    gains: int
    losses: int
    #: Disposals at a gain held past the long-term boundary -- the tax-driven subset.
    long_term_gains: int
    sufficient: bool


@dataclass(frozen=True, slots=True)
class ActivityConcentration:
    """How bunched the buying was. Primarily a GATE: a six-trade book supports no
    behavioural claim at all, and this is what says so."""

    purchases: int
    active_days: int
    #: Share of purchase rupees falling on the single busiest day.
    busiest_day_share_pct: float | None
    longest_gap_days: int | None
    sufficient: bool


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """One sell followed by a buy of the same ISIN inside the window."""

    isin: str
    sold_on: date
    bought_on: date
    days_between: int
    sold_at_a_loss: bool
    #: True when 31 March fell between the two. Reported, never interpreted: it is what
    #: separates a round trip from deliberate tax-loss harvesting, and only the investor
    #: knows which it was.
    crossed_financial_year_end: bool


@dataclass(frozen=True, slots=True)
class RoundTrips:
    trips: list[RoundTrip]
    purchases: int
    share_of_purchases_pct: float | None
    sufficient: bool


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def holding_period_asymmetry(
    realised: list[Realised], long_term_days: int = 365
) -> HoldingPeriodAsymmetry:
    """Detector 1. Cheapest and most robust, so it is the one to trust first.

    Uses only ``compute_realised`` output -- ``gain`` and ``holding_days`` are already
    populated there, so this needs no price lookup and no second ledger replay.
    """
    gains = [
        float(r.holding_days)
        for r in realised
        if r.holding_days is not None and r.gain > 0
    ]
    losses = [
        float(r.holding_days)
        for r in realised
        if r.holding_days is not None and r.gain < 0
    ]
    long_term_gains = sum(
        1
        for r in realised
        if r.holding_days is not None and r.gain > 0 and r.holding_days >= long_term_days
    )

    gain_median = _median(gains)
    loss_median = _median(losses)
    ratio = (
        loss_median / gain_median
        if gain_median and loss_median is not None and gain_median > 0
        else None
    )
    return HoldingPeriodAsymmetry(
        median_gain_days=gain_median,
        median_loss_days=loss_median,
        ratio=round(ratio, 3) if ratio is not None else None,
        gains=len(gains),
        losses=len(losses),
        long_term_gains=long_term_gains,
        # BOTH sides need a sample. A ratio built on one loss is arithmetic, not evidence.
        sufficient=len(gains) + len(losses) >= MIN_DISPOSALS
        and len(gains) >= 2
        and len(losses) >= 2,
    )


def activity_concentration(transactions: list[Transaction]) -> ActivityConcentration:
    """Detector 5. The gate every other behavioural claim should pass first."""
    buys = [t for t in transactions if t.txn_type in _PURCHASES]
    if not buys:
        return ActivityConcentration(0, 0, None, None, sufficient=False)

    by_day: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for txn in buys:
        by_day[txn.txn_date] += txn.quantity * txn.price

    total = sum(by_day.values())
    busiest = max(by_day.values()) if by_day else Decimal(0)
    days = sorted(by_day)
    longest_gap = max(
        ((b - a).days for a, b in zip(days, days[1:], strict=False)), default=0
    )
    return ActivityConcentration(
        purchases=len(buys),
        active_days=len(by_day),
        busiest_day_share_pct=(
            round(float(busiest / total * 100), 1) if total > 0 else None
        ),
        longest_gap_days=longest_gap,
        sufficient=len(by_day) >= MIN_ACTIVE_DAYS,
    )


def _financial_year_end_between(start: date, end: date) -> bool:
    """Did a 31 March fall in (start, end]? The Indian financial year, not a calendar one."""
    for year in range(start.year, end.year + 1):
        boundary = date(year, 3, 31)
        if start < boundary <= end:
            return True
    return False


def round_trips(
    transactions: list[Transaction],
    realised: list[Realised],
    window_days: int = ROUND_TRIP_DAYS,
) -> RoundTrips:
    """Detector 3. Sell then re-buy the same security inside the window.

    Reports the pattern and the two facts that most change its reading -- whether the
    sale was at a loss and whether a financial-year end fell between -- and interprets
    neither. Those two together are what separate a round trip from deliberate tax-loss
    harvesting, and only the investor knows which it was.
    """
    sells: dict[str, list[date]] = defaultdict(list)
    buys: dict[str, list[date]] = defaultdict(list)
    for txn in sorted(transactions, key=lambda t: t.txn_date):
        if txn.txn_type == TxnType.SELL:
            sells[txn.isin].append(txn.txn_date)
        elif txn.txn_type in _PURCHASES:
            buys[txn.isin].append(txn.txn_date)

    loss_dates: set[tuple[str, date]] = {
        (r.isin, r.sell_date)
        for r in realised
        if r.isin is not None and r.sell_date is not None and r.gain < 0
    }

    trips: list[RoundTrip] = []
    for isin, sold_days in sells.items():
        for sold_on in sold_days:
            horizon = sold_on + timedelta(days=window_days)
            following = [d for d in buys.get(isin, []) if sold_on < d <= horizon]
            if not following:
                continue
            bought_on = min(following)
            trips.append(
                RoundTrip(
                    isin=isin,
                    sold_on=sold_on,
                    bought_on=bought_on,
                    days_between=(bought_on - sold_on).days,
                    sold_at_a_loss=(isin, sold_on) in loss_dates,
                    crossed_financial_year_end=_financial_year_end_between(
                        sold_on, bought_on
                    ),
                )
            )

    purchases = sum(len(v) for v in buys.values())
    return RoundTrips(
        trips=sorted(trips, key=lambda t: t.sold_on),
        purchases=purchases,
        share_of_purchases_pct=(
            round(100.0 * len(trips) / purchases, 1) if purchases else None
        ),
        sufficient=purchases >= MIN_DISPOSALS,
    )


@dataclass(frozen=True, slots=True)
class DispositionSpread:
    """Odean's PGR/PLR. The rigorous form of holding-period asymmetry.

    For each sell, the OTHER positions held that day are partitioned into paper winners
    and paper losers, and the sold one is classified as a realised gain or loss.
    ``PGR = RG/(RG+PG)``, ``PLR = RL/(RL+PL)``, spread = PGR - PLR.

    Needs a close on each sell date for every position held, so unlike the detectors
    above it cannot be computed from the ledger alone -- prices are passed in rather
    than fetched, keeping this layer pure.
    """

    realised_gains: int
    paper_gains: int
    realised_losses: int
    paper_losses: int
    pgr: float | None
    plr: float | None
    spread: float | None
    sell_dates: int
    sufficient: bool


def disposition_spread(
    realised: list[Realised],
    holdings_on: dict[date, dict[str, tuple[Decimal, Decimal]]],
) -> DispositionSpread:
    """Detector 2.

    ``holdings_on`` maps a sell date -> {isin: (average_cost, close_that_day)} for every
    position held that day. The caller assembles it; this function does the counting,
    which is the part worth pinning with tests.
    """
    rg = pg = rl = pl = 0
    dates: set[date] = set()

    sold_by_date: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for r in realised:
        if r.isin is None or r.sell_date is None:
            continue
        sold_by_date[r.sell_date][r.isin] = (
            sold_by_date[r.sell_date].get(r.isin, Decimal(0)) + r.gain
        )

    for sell_date, sold in sold_by_date.items():
        held = holdings_on.get(sell_date)
        if not held:
            continue
        dates.add(sell_date)
        for gain in sold.values():
            if gain > 0:
                rg += 1
            elif gain < 0:
                rl += 1
        for isin, (avg_cost, close) in held.items():
            if isin in sold:
                continue  # counted above as realised, not paper
            if close > avg_cost:
                pg += 1
            elif close < avg_cost:
                pl += 1

    pgr = rg / (rg + pg) if (rg + pg) else None
    plr = rl / (rl + pl) if (rl + pl) else None
    spread = pgr - plr if pgr is not None and plr is not None else None
    return DispositionSpread(
        realised_gains=rg,
        paper_gains=pg,
        realised_losses=rl,
        paper_losses=pl,
        pgr=round(pgr, 4) if pgr is not None else None,
        plr=round(plr, 4) if plr is not None else None,
        spread=round(spread, 4) if spread is not None else None,
        sell_dates=len(dates),
        # Odean's own guidance: the statistic is unstable on a handful of disposals
        # spread over a couple of days. Both bounds, not either.
        sufficient=(rg + rl) >= 20 and len(dates) >= 8,
    )
