"""Dividend summaries -- docs/DATA_MODEL.md's DIVIDEND txn_type, ENGINE_SPEC.md section 4.

DIVIDEND is already a correct, first-class ledger row (qty=0, price=0, the amount lives
in ``dividend_amount``) and already flows into ``compute_xirr`` as a positive cash flow
(``app.engine.xirr._transaction_flows``) and into ``compute_twrr`` as a signed
sub-period outflow. This module adds nothing to that calculation -- it is the missing
aggregation over the same rows: how much a holding has actually paid, per ISIN and per
financial year, for display.

A holding with no ``DIVIDEND`` rows simply has no entry in the output. That is a true,
known zero -- it never declared one -- not the CLAUDE.md rule-4 "missing price defaulted
to zero" case, which is about substituting a value nobody has for one that is required.
Nothing here needs a substitute; there is nothing missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.engine.corporate_actions import CorpActionType, CorporateAction, Verification
from app.engine.dividend_events import DividendEvent
from app.engine.dividend_events import screen as screen_events
from app.engine.holdings import compute_holdings_partitioned, quantity_held_before
from app.engine.types import ZERO, DividendSummary, DividendYear, Transaction, TxnType
from app.engine.xirr import financial_year_start


def fy_label(fy_start: date) -> str:
    """1 April YYYY -> ``"YYYY-YY+1"``, e.g. 2025-04-01 -> ``"2025-26"``.

    The inverse of ``app.routers.transactions.financial_year_bounds``, which already
    parses this exact label back into a date range for the ``/realised?fy=`` filter --
    same convention, opposite direction.
    """
    year = fy_start.year
    return f"{year}-{(year + 1) % 100:02d}"


def compute_dividends(transactions: list[Transaction]) -> list[DividendSummary]:
    """Sum ``DIVIDEND.dividend_amount`` per ISIN, and per financial year within each ISIN.

    FY boundaries reuse ``app.engine.xirr.financial_year_start`` (1 April -> 31 March)
    rather than reimplementing that rule a second time.

    Only ISINs with at least one ``DIVIDEND`` row appear, sorted by ISIN; each entry's
    ``by_year`` is sorted ascending by FY start. Mirrors ``compute_lots``/
    ``compute_realised``, which likewise omit an ISIN with nothing to report rather than
    emitting a zero-valued row for it.
    """
    totals: dict[str, Decimal] = {}
    by_year: dict[str, dict[str, Decimal]] = {}

    for txn in transactions:
        if txn.txn_type is not TxnType.DIVIDEND:
            continue
        fy = fy_label(financial_year_start(txn.txn_date))
        totals[txn.isin] = totals.get(txn.isin, ZERO) + txn.dividend_amount
        year_totals = by_year.setdefault(txn.isin, {})
        year_totals[fy] = year_totals.get(fy, ZERO) + txn.dividend_amount

    return [
        DividendSummary(
            isin=isin,
            total=totals[isin],
            by_year=[DividendYear(fy=fy, amount=by_year[isin][fy]) for fy in sorted(by_year[isin])],
        )
        for isin in sorted(totals)
    ]




# ===========================================================================
# EXPECTED -- what a holder should receive, computed on read and never stored.
#
# THIS IS A PROJECTION, NOT A TRANSACTION, and the distinction is the point of the whole
# module. An expected dividend holds no fact that is not already stored: it is a declared
# amount (dividend_event) times a quantity the ledger already determines. Storing it would
# be the same mistake as an ``UPDATE holdings SET quantity`` -- a cache that can silently
# disagree with what it was derived from. Computed on read it self-corrects the moment a
# late contract note changes the eligible quantity, which is exactly the property a stored
# version would lose.
#
# NOTHING HERE MAY REACH XIRR, TWRR, CONTRIBUTION OR REALISED INCOME. Those consume
# ``DIVIDEND`` ledger rows and only those. An expectation that leaked into a return figure
# would be the product claiming money arrived when all that is known is that a company
# declared it -- see docs/ARCHITECTURE_INVARIANTS.md and migration 029's header.
# ===========================================================================


#: How far before a dividend's ex-date a share-count change is treated as possibly
#: predating the declaration -- see ``_intervening_share_change``.
#:
#: Indian boards declare a dividend at a results meeting and the ex-date follows two to
#: eight weeks later. 90 days covers that comfortably. It is deliberately generous because
#: the cost of the two errors is wildly asymmetric: an over-wide window withholds a
#: projection and says why, while a too-narrow one publishes a figure wrong by the whole
#: split multiplier -- a 5x error every component of which looks correct.
#:
#: A stored declaration date would make this exact instead of conservative. Neither feed
#: carries one reliably today; recorded as future work rather than guessed at.
DECLARATION_WINDOW = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class ExpectedDividend:
    """One declared dividend, sized to a holder. DERIVED, NEVER STORED.

    ``gross_expected`` is named gross deliberately: no TDS has been withheld from a
    projection, because no money has moved. It is what the company declared times what the
    ledger says was held, and nothing more.
    """

    isin: str
    ex_date: date
    kind: str
    amount_per_share: Decimal
    eligible_quantity: Decimal
    gross_expected: Decimal
    record_date: date | None = None
    payment_date: date | None = None
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class UnprojectableDividend:
    """A declared dividend this holder cannot be given a number for, and why.

    Mirrors ``compute_holdings_partitioned``'s exclusion shape rather than inventing a
    second one: a thing withheld must be visible with a reason, never dropped.
    """

    isin: str
    ex_date: date
    code: str
    detail: str


def _intervening_share_change(
    actions: list[CorporateAction], isin: str, ex_date: date
) -> CorporateAction | None:
    """A bonus or split whose ex-date falls close enough before this dividend's to have
    landed AFTER the dividend was declared.

    THE HAZARD, which is invisible unless looked for and is off by a whole multiplier
    rather than by a rounding. A company declares "Rs 1.50 per share" against the share as
    it exists at declaration. If a 1:4 split's ex-date then falls before the dividend's
    ex-date, ``quantity_held_before`` correctly returns the POST-split quantity -- and
    multiplying the two gives a figure five times too large, with every individual
    component correct and nothing anywhere reporting an error.

    Only the notice itself says which basis the amount is on, and this system does not
    store a declaration date to test it against. So the answer is to refuse: a projection
    withheld with a named reason costs a number nobody sees, and a projection published on
    the wrong basis costs trust in every number beside it.

    DEMERGER is not checked. It changes the cost pool and adds a second security; it does
    not restate the per-share basis of the security being paid on.
    """
    window_start = ex_date - DECLARATION_WINDOW
    for action in actions:
        if action.isin != isin:
            continue
        if action.action_type not in (CorpActionType.BONUS, CorpActionType.SPLIT):
            continue
        if window_start < action.ex_date <= ex_date:
            return action
    return None


#: UnprojectableDividend.code for a holding excluded because a derived corporate-action
#: row in it lost its evidence (``CorporateActionEvidenceError``, ENGINE_SPEC section 9).
EVIDENCE_WITHDRAWN = "CORPORATE_ACTION_EVIDENCE_WITHDRAWN"


def _withdrawn_evidence(
    transactions: list[Transaction], isin: str, ex_date: date
) -> str | None:
    """The replay reason code if ``isin``'s quantity at the cutoff before ``ex_date``
    cannot be computed because a corporate-action effect in it lost its evidence, else
    ``None``. ``quantity_held_before`` reads such an ISIN as zero; this is what tells an
    excluded holding apart from not being a holder."""
    rows = [t for t in transactions if t.isin == isin]
    _held, skipped = compute_holdings_partitioned(rows, ex_date - timedelta(days=1))
    return next((s.reason_code for s in skipped if s.isin == isin and s.reason_code), None)


#: ISINs of mutual-fund units, ETFs included ("INF..."). Outside this system's listed-equity
#: scope, and a fund's payouts (LIQUIDBEES: daily, reinvested as units) are not a company's
#: cash dividend -- so they are excluded from dividend tracking and NAMED as excluded,
#: never shown as "awaiting confirmation" (owner decision 2026-10-13).
FUND_UNIT_ISIN_PREFIX = "INF"


def split_fund_units(events: list[DividendEvent]) -> tuple[list[DividendEvent], list[str]]:
    """``(events to track, sorted fund-unit ISINs excluded)``."""
    kept = [e for e in events if not e.isin.startswith(FUND_UNIT_ISIN_PREFIX)]
    excluded = sorted({e.isin for e in events if e.isin.startswith(FUND_UNIT_ISIN_PREFIX)})
    return kept, excluded


def drop_unverified_twins(events: list[DividendEvent]) -> list[DividendEvent]:
    """One declaration recorded twice -> one event. GUJTHEM's 2025-09-04 dividend was
    filed by yfinance under the retired ISIN and by NSE under the current one, so the
    feeds never merged them; once the caller has resolved both to the same identity the
    second row is the same declaration, not a second one awaiting confirmation (seen in
    production 2026-09-29).

    Only non-VERIFIED rows are dropped, and only when a VERIFIED row shares their
    ``(isin, ex_date, amount_per_share)`` -- so nothing that could contribute money is
    touched. ``compute_expected_dividends`` projects VERIFIED events only, so this
    changes an "awaiting" count, never a figure.

    CALLERS MUST RESOLVE IDENTITIES FIRST. The key is compared in the caller's resolved
    space, which is the whole point; an unresolved caller would simply get its input
    back. Shared by ``app.routers.transactions.expected_dividends`` and
    ``app.db.corporate_action_timeline._dividend_events`` so the projection endpoint and
    the timeline cannot disagree about how many declarations exist.
    """
    verified_keys = {
        (e.isin, e.ex_date, e.amount_per_share)
        for e in events
        if e.verification is Verification.VERIFIED
    }
    return [
        e
        for e in events
        if e.verification is Verification.VERIFIED
        or (e.isin, e.ex_date, e.amount_per_share) not in verified_keys
    ]


def compute_expected_dividends(
    transactions: list[Transaction],
    events: list[DividendEvent],
    actions: list[CorporateAction] | None = None,
    as_of: date | None = None,
) -> tuple[list[ExpectedDividend], list[UnprojectableDividend]]:
    """Size every VERIFIED declared dividend to this holder's own ledger.

    Returns ``(expected, unprojectable)``. An event appears in exactly one of them, so a
    caller can render a total and a "could not be computed" note from the same call --
    the same contract ``compute_holdings_partitioned`` already has.

    ELIGIBILITY IS ``quantity_held_before``, REUSED VERBATIM. It computes the quantity held
    at the close before the ex-date, which is the correct reference point for a dividend
    for exactly the reason it is for a bonus: holding at that close is what puts a holder
    on the register at the record date. There is no second holdings engine here, and there
    must not be one -- see CLAUDE.md's one architectural rule.

    Events already realised in the ledger are omitted, matched EXACTLY through the
    ``source_ref`` an ingested dividend carries (``app.engine.dividends.event_source_ref``)
    -- never inferred from a nearby date and amount, which would quietly hide a real second
    dividend that happened to resemble one already received.

    ``as_of`` bounds which dividends are considered: an ex-date after it has not happened
    yet, so nobody is eligible and nothing is projected. Defaults to considering every
    event given, which is what a caller listing upcoming dividends wants.
    """
    safe, failures = screen_events(events)
    # Only events this holder was ELIGIBLE for are withheld-with-a-reason. A feed like
    # yfinance returns a security's whole dividend history (back to 2000), so without this
    # an unverified declaration from years before the first trade -- which could never
    # apply -- was reported as outstanding: 607 of 627 for the real YSL473 ledger
    # (2026-09-29). Same cutoff the projection itself uses, so the two cannot disagree.
    unprojectable = [
        UnprojectableDividend(f.event.isin, f.event.ex_date, f.code, f.detail)
        for f in failures
        if quantity_held_before(transactions, f.event.isin, f.event.ex_date) > ZERO
    ]
    already_realised = realised_event_ids(transactions)
    corporate_actions = actions or []
    expected: list[ExpectedDividend] = []

    for event in safe:
        if as_of is not None and event.ex_date > as_of:
            continue  # has not happened yet -- not withheld, simply not due
        if event.id is not None and event.id in already_realised:
            continue  # the money arrived; the ledger row is the truth now

        intervening = _intervening_share_change(corporate_actions, event.isin, event.ex_date)
        if intervening is not None:
            unprojectable.append(
                UnprojectableDividend(
                    event.isin, event.ex_date, "SHARE_COUNT_CHANGED",
                    f"a {intervening.action_type.value} with ex-date {intervening.ex_date} may "
                    f"postdate this dividend's declaration, so the declared amount per share "
                    f"and the held quantity may be on different bases",
                )
            )
            continue

        quantity = quantity_held_before(transactions, event.isin, event.ex_date)
        if quantity <= ZERO:
            withdrawn = _withdrawn_evidence(transactions, event.isin, event.ex_date)
            if withdrawn is not None:
                # Excluded, not zero: the holding cannot be sized until the action is
                # re-verified or cancelled, so the projection is withheld by name.
                unprojectable.append(
                    UnprojectableDividend(
                        event.isin, event.ex_date, EVIDENCE_WITHDRAWN,
                        f"a corporate-action effect on this holding lost its evidence "
                        f"({withdrawn}); the eligible quantity cannot be computed",
                    )
                )
            continue  # not a holder at the cutoff -- this dividend does not apply here

        expected.append(
            ExpectedDividend(
                isin=event.isin,
                ex_date=event.ex_date,
                kind=str(event.kind),
                amount_per_share=event.amount_per_share,
                eligible_quantity=quantity,
                gross_expected=event.amount_per_share * quantity,
                record_date=event.record_date,
                payment_date=event.payment_date,
                event_id=event.id,
            )
        )

    expected.sort(key=lambda e: (e.ex_date, e.isin))
    unprojectable.sort(key=lambda u: (u.ex_date, u.isin))
    return expected, unprojectable


#: Prefix identifying a ledger row created from a declared dividend event.
SOURCE_REF_PREFIX = "DIV"


def event_source_ref(event_id: str, revision: int = 1) -> str:
    """The ``source_ref`` a ``DIVIDEND`` ledger row carries: ``DIV:{event_id}:{revision}``.

    WHY THIS SHAPE IS FORCED, not chosen. ``app.engine.dedupe.make_dedupe_key`` hashes
    ``account_id | source | source_ref | isin | txn_date | txn_type | quantity | price``
    and deliberately EXCLUDES ``dividend_amount``; a DIVIDEND row also has zero quantity
    and zero price. So for a dividend the key collapses to account, source, source_ref,
    ISIN and date -- and ``source_ref`` is the ONLY field left that can distinguish one
    dividend row from another.

    Two consequences follow, and both are load-bearing:

      - Re-ingesting the same event at the same revision produces the same key, and the
        UNIQUE index refuses the duplicate. Idempotency is free, provided the id is stable.
      - A CORRECTION is only expressible by bumping the revision. The ledger is append-only
        by trigger, so a wrong amount cannot be updated; it must be reversed and re-added.
        Without a revision in the key, the reversing row would collide with the row it
        reverses and be silently skipped by ``dedupe()`` -- leaving the wrong figure
        standing and the correction discarded with no error anywhere.

    Two dividends on one date (an interim and a special) are likewise distinguished, since
    they are different events with different ids.
    """
    return f"{SOURCE_REF_PREFIX}:{event_id}:{revision}"


def parse_event_source_ref(source_ref: str | None) -> tuple[str, int] | None:
    """``(event_id, revision)`` from a dividend row's ``source_ref``, or ``None`` if it is
    not one. Tolerant by design: a manually entered dividend has a free-form
    ``source_ref``, and that is not an error, just an unlinked row."""
    if not source_ref or not source_ref.startswith(f"{SOURCE_REF_PREFIX}:"):
        return None
    parts = source_ref.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def realised_event_ids(transactions: list[Transaction]) -> set[str]:
    """Every declared-dividend event that already has a ledger row against it.

    Exact, through ``source_ref``. A manually entered dividend contributes nothing here --
    it is real income and counts in ``compute_dividends``, but it is not linked to a
    declared event, so the corresponding expectation is not suppressed. That is the honest
    behaviour: guessing the link from a nearby date and a similar amount would hide a real
    second dividend that happened to resemble one already received.
    """
    linked: set[str] = set()
    for txn in transactions:
        if txn.txn_type is not TxnType.DIVIDEND:
            continue
        parsed = parse_event_source_ref(txn.source_ref)
        if parsed is not None:
            linked.add(parsed[0])
    return linked


__all__ = [
    "DECLARATION_WINDOW",
    "EVIDENCE_WITHDRAWN",
    "SOURCE_REF_PREFIX",
    "ExpectedDividend",
    "UnprojectableDividend",
    "compute_dividends",
    "compute_expected_dividends",
    "drop_unverified_twins",
    "event_source_ref",
    "parse_event_source_ref",
    "realised_event_ids",
]
