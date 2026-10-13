"""DECLARED dividends as reference data, and the guards that decide whether one is
trustworthy enough to project from -- docs/ENGINE_SPEC.md section 7, migration 029.

WHY THIS IS A SEPARATE MODULE FROM app.engine.corporate_actions. The full argument is in
migration 029's header; the one-line version is that adding ``DIVIDEND`` to
``CorpActionType`` would inherit ``app.db.corporate_actions.apply_known_actions_for_account``'s
auto-apply path, and the three-independent-checks argument that makes that path safe does
not survive the move: ``check_price_consistency`` returns ``None`` on its first line for
any type that is not BONUS or SPLIT, so it would pass vacuously.

FOUR CONCEPTS, AND ONLY THE LAST ONE IS A LEDGER ROW. Stated here because conflating any
two of them is the defining mistake in dividend tracking:

    DECLARED   the company announced an amount per share, with an ex-date.
               A fact about the COMPANY. This module, and the dividend_event table.
    EXPECTED   what a particular holder should receive -- declared amount x the quantity
               they held at the cutoff. A PROJECTION, computed on read, never stored.
               See app.engine.dividends.compute_expected_dividends.
    RECEIVED   evidence says cash actually arrived. Neither feed behind this module
               carries it; see docs/INGESTION.md "Adapter 6".
    REALISED   a DIVIDEND row exists in the ledger, and therefore enters XIRR, TWRR,
               contribution and realised income.

There is deliberately NO path in this codebase from EXPECTED to REALISED. "You should
have received 10,000" and "you received 10,000" are different claims, and only the second
belongs in a performance figure.

PRICE CANNOT CORROBORATE A DIVIDEND, AND THIS IS ARITHMETIC. ``check_price_consistency``
works because a bonus or split divides the price by its multiplier -- a 2x, 5x or 10x
move, which a 0.70-1.40 agreement band catches when the ratio notation is inverted. A
dividend moves the price by roughly ``amount / price``: 0.3%-2% for a typical Indian
large-cap, inside one day's ordinary noise. No band recovers a signal that is not there,
so corroboration here is cross-source instead -- with the honest caveat
``check_cross_sourced`` spells out at length.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from fractions import Fraction

# Reused, never re-declared: this system has one vocabulary for how much a reference
# record is trusted, shared by corporate_action, security_alias and dividend_event. A
# parallel three-state enum would be two standards of evidence pretending to be one.
from app.engine.corporate_actions import CorpActionType, CorporateAction, Verification

ZERO = Decimal("0")


class DividendKind(StrEnum):
    """What the notice called it.

    ``UNSPECIFIED`` is NOT a fourth kind of dividend -- it means THIS SOURCE DID NOT SAY.
    That is the normal case for ``yfinance`` (``Ticker.dividends`` carries an ex-date and
    an amount, nothing else) and for NSE subjects whose free text omits it. It is a real
    value rather than ``None`` because it participates in the table's uniqueness; see
    ``app.db.dividend_events.match_unspecified`` for the rule that stops an unspecified
    candidate from duplicating an already-classified event.
    """

    INTERIM = "INTERIM"
    FINAL = "FINAL"
    SPECIAL = "SPECIAL"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True, slots=True)
class DividendEvent:
    """One declared dividend. Reference data about a company, not about an investor.

    Carries no quantity and no rupee total, deliberately: multiplying by a holder's
    quantity is what makes it a projection, and that happens on read in
    ``app.engine.dividends``, never here and never in storage.
    """

    isin: str
    ex_date: date
    amount_per_share: Decimal
    kind: DividendKind = DividendKind.UNSPECIFIED
    record_date: date | None = None
    payment_date: date | None = None
    source: str = ""
    source_url: str = ""
    corroborated_by: str | None = None
    corroborated_url: str | None = None
    verification: Verification = Verification.DRAFT
    notes: str = ""
    id: str | None = None

    @property
    def dedup_key(self) -> tuple[str, date, DividendKind]:
        """The event's identity -- mirrors the table's ``UNIQUE (isin, ex_date, kind)``.

        The amount is absent on purpose. A corrected amount is the SAME event with a
        better number, not a second event; including it would leave two contradictory
        claims about one fact with nothing able to choose between them at read time --
        the failure ``security_alias``'s single-row primary key exists to prevent.
        """
        return (self.isin, self.ex_date, self.kind)


@dataclass(frozen=True, slots=True)
class GuardFailure:
    """Mirrors ``app.engine.corporate_actions.GuardFailure`` -- same shape so both kinds
    of reference record land in the same ``Exceptions`` surface with the same fields."""

    event: DividendEvent
    code: str
    detail: str


#: Implied yield ceiling for ``check_amount_plausible``: amount_per_share / close.
#:
#: This is a UNIT-ERROR guard, not a valuation judgement, and it is calibrated as one.
#: The realistic parse failure is a factor of 100 -- "Rs 1.50 Per Share" read as 150, or
#: paise recorded as rupees -- and that is what this catches. Real Indian equity dividend
#: yields sit well under 10% even for the heaviest payers; a genuine special dividend can
#: legitimately run higher (a company distributing sale proceeds), which is why the
#: ceiling is set far above any ordinary yield rather than near it. Anything above this is
#: not a generous dividend, it is a misread decimal point.
#:
#: DELIBERATELY WEAK, and documented as such: a 10x error on a low-yield scrip passes
#: this guard. It is the difference between a 100x error and a plausible-looking wrong
#: number, not a correctness proof.
MAX_PLAUSIBLE_YIELD = Decimal("0.50")


#: Dividends are declared in rupees and paise -- two decimal places -- so that is the
#: granularity at which two sources' readings of the SAME declaration are compared.
#:
#: This is not a tolerance for disagreement, which would be this module deciding that two
#: different amounts are close enough. It is the comparison granularity the underlying fact
#: actually has. The reason it must be stated explicitly: one of the two sources
#: (``yfinance``) arrives as a binary numeric value, and a true 1.10 can surface as
#: 1.0999999999 -- see app.ingest.corporate_action_yfinance for where that boundary is
#: crossed and converted. Compared
#: exactly, that is a conflict and the event would be DISPUTED -- turning every
#: such a corroboration into a false contradiction and defeating the only
#: verification a dividend has. Compared at paise, it is the same declaration.
#:
#: A genuine disagreement is never smaller than a paisa, so nothing real is hidden by this.
AMOUNT_QUANTUM = Decimal("0.01")


def amounts_agree(left: Decimal, right: Decimal) -> bool:
    """Whether two readings describe the same declared amount, at paise granularity.

    ``ROUND_HALF_UP`` rather than Python's default banker's rounding: this compares money
    as a human reading a notice would, and the asymmetry of banker's rounding at exact
    half-paise would make agreement depend on which source reported first.
    """
    return left.quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_UP) == right.quantize(
        AMOUNT_QUANTUM, rounding=ROUND_HALF_UP
    )


#: The actions Yahoo scales past dividends by: the ones that change the share count.
#: (A DEMERGER leaves the parent's count alone; a rights issue's factor depends on the
#: cum-rights price, which this rule deliberately does not reconstruct.)
SHARE_COUNT_ACTIONS = (CorpActionType.BONUS, CorpActionType.SPLIT)


def later_share_multiplier(actions: Iterable[CorporateAction], ex_date: date) -> Fraction:
    """Shares now per share held on ``ex_date``: the product of the multipliers of every
    VERIFIED, uncancelled bonus/split dated strictly AFTER it (owner decision 2026-10-13).

    Yahoo serves a past dividend divided by exactly this factor; NSE serves it as
    declared. Multiplying Yahoo's figure back by it is what lets the two be compared at
    all. DRAFT actions never count -- an unproven ratio would turn a real conflict into a
    false agreement -- and neither does an action ON the dividend's ex-date, whose order
    relative to the dividend the record cannot tell. One action filed under both its old
    and its new ISIN is one action, counted once."""
    factor = Fraction(1)
    seen: set[tuple[CorpActionType, date]] = set()
    for action in actions:
        if (
            action.action_type not in SHARE_COUNT_ACTIONS
            or action.verification is not Verification.VERIFIED
            or action.cancelled
            or action.ex_date <= ex_date
            or (action.action_type, action.ex_date) in seen
        ):
            continue
        seen.add((action.action_type, action.ex_date))
        factor *= action.multiplier()
    return factor


def restated_amount(amount: Decimal, factor: Fraction) -> Decimal:
    """``amount`` times ``factor``, in exact Decimal arithmetic (never float)."""
    return amount * Decimal(factor.numerator) / Decimal(factor.denominator)


def check_sourced(event: DividendEvent) -> GuardFailure | None:
    """Provenance exists. Identical in spirit and in weakness to
    ``app.engine.corporate_actions.check_sourced`` -- it tests that two strings are
    non-empty and nothing more, which is exactly why it cannot be the only guard."""
    if not event.source or not event.source_url:
        return GuardFailure(event, "UNSOURCED", "every dividend event needs a provenance URL")
    return None


def check_amount_plausible(
    event: DividendEvent, close_on_ex_date: Decimal | None
) -> GuardFailure | None:
    """The amount is not a misplaced decimal point.

    Passes when no close is available rather than failing: absence of a price is the
    ``NO_PRICE_EVIDENCE`` case for a bonus, but here the price was never the evidence in
    the first place (see the module docstring), so withholding an event for want of one
    would quarantine most of the archive for no gain. ``check_cross_sourced`` is what
    actually gates verification.
    """
    if event.amount_per_share <= ZERO:
        # The table's CHECK refuses this too; reaching it here means a candidate was
        # built in memory and never inserted, which is where it should be rejected.
        return GuardFailure(event, "BAD_AMOUNT", "amount per share must be positive")
    if close_on_ex_date is None or close_on_ex_date <= ZERO:
        return None
    implied_yield = event.amount_per_share / close_on_ex_date
    if implied_yield > MAX_PLAUSIBLE_YIELD:
        return GuardFailure(
            event,
            "AMOUNT_IMPLAUSIBLE",
            f"amount {event.amount_per_share} against a close of {close_on_ex_date} implies "
            f"a {implied_yield:.1%} yield -- probably a misread decimal point, not a dividend",
        )
    return None


def check_cross_sourced(event: DividendEvent) -> GuardFailure | None:
    """A second, DISTINCT source read the same amount and ex-date.

    THE PROVENANCE RELATIONSHIP BETWEEN THE TWO SOURCES, STATED PLAINLY, because calling
    them "independent" would overclaim and this guard is the whole verification story for
    a dividend.

    The two feeds behind this module are ``app.ingest.corporate_action_feed`` (NSE's own
    corporate-filings API) and ``app.ingest.corporate_action_yfinance``
    (``Ticker.dividends``, i.e. Yahoo Finance). They are INDEPENDENTLY OPERATED but they
    are NOT INDEPENDENTLY ORIGINATED: Yahoo's Indian corporate-action data is itself
    redistributed from exchange and registrar filings, the same filings NSE publishes. So
    the two agreeing rules out a transcription or parse error ON ONE SIDE -- a regex
    misreading "Rs 1.50" as 150, a column shifted, a date in the wrong format -- and does
    NOT rule out an error in the upstream filing both derive from, nor a systematic
    convention difference both inherit.

    That is a real but LIMITED guarantee, and it is materially weaker than
    ``check_price_consistency``, which corroborates a claim against a physically separate
    observation (the actual traded close). It is offered as the best available substitute,
    not as an equivalent. Two sources agreeing is CORROBORATION; it is not proof.

    Consequences that follow, and that the rest of this system must respect:
      - Agreement promotes an event to VERIFIED, which makes it eligible to produce a
        PROJECTION. It never makes it eligible to produce a ledger row -- receipt is a
        separate question this guard says nothing about.
      - A single source is not a failure to be worked around. It stays DRAFT, visible in
        Exceptions, and self-promotes if and when a second source lands -- the same
        self-correcting retry posture ``refresh_corporate_action_feed`` already has.
      - A human verifying an event by hand is a THIRD, stronger path, because a person can
        read the company's own filing. That is why ``verify_dividend_event`` exists and
        does not require this guard to pass.
      - NSE's notice checked against NSE's own bhavcopy is a FOURTH path (owner decision
        2026-10-13: NSE is the authoritative source; what needs checking is this system's
        READING of it). ``corroborated_by`` then names ``NSE bhavcopy (self-check)``, which
        is not a second publisher of the amount: it proves the ISIN traded that session
        and that the amount is plausible against the close. See
        ``app.db.dividend_reconcile._nse_self_check`` for the four conditions.
      - Should a third source ever be added, it does not make this argument stronger by
        arithmetic. Whether it is genuinely separate in ORIGIN is the only question that
        matters, and it has to be answered in writing, here, before it counts.

    This is an instance of a general rule, not a dividend-specific one -- see
    docs/ARCHITECTURE_INVARIANTS.md #19, which states it for any reference data this system
    verifies: corroboration requires independence of ORIGIN, that independence must be
    argued in the code that relies on it, and same-origin agreement may unlock a projection
    but never a ledger row.
    """
    if not event.corroborated_by or not event.corroborated_url:
        return GuardFailure(
            event,
            "UNCORROBORATED",
            "only one source has reported this dividend; a declared amount is not "
            "projected from until a second, distinct source agrees or a human verifies it",
        )
    if event.corroborated_by == event.source:
        # The table's CHECK refuses this as well. Two runs of one feed are one
        # observation, and counting the second as confirmation is the precise mistake
        # this whole guard exists to prevent.
        return GuardFailure(
            event,
            "SELF_CORROBORATED",
            f"{event.source} cannot corroborate itself -- two reads of one source are "
            "one observation",
        )
    return None


def check_verified(event: DividendEvent) -> GuardFailure | None:
    if event.verification is not Verification.VERIFIED:
        return GuardFailure(
            event,
            "UNVERIFIED",
            f"verification is {event.verification.value}; will not produce a projection",
        )
    return None


def screen(
    events: list[DividendEvent], closes: dict[tuple[str, date], Decimal] | None = None
) -> tuple[list[DividendEvent], list[GuardFailure]]:
    """Split events into those safe to project from and the failures that quarantine the
    rest -- the same fail-closed shape as ``app.engine.corporate_actions.screen``.

    ``closes`` is an optional ``(isin, ex_date) -> close`` lookup for
    ``check_amount_plausible``; absent, that guard passes and the others still apply.
    An event trips at most one guard here: the first failure is reported and the event is
    quarantined, because a second opinion on an already-rejected record tells a reader
    nothing they can act on.
    """
    prices = closes or {}
    safe: list[DividendEvent] = []
    failures: list[GuardFailure] = []

    for event in events:
        failure = (
            check_sourced(event)
            or check_verified(event)
            or check_cross_sourced(event)
            or check_amount_plausible(event, prices.get((event.isin, event.ex_date)))
        )
        if failure is None:
            safe.append(event)
        else:
            failures.append(failure)

    return safe, failures


__all__ = [
    "AMOUNT_QUANTUM",
    "MAX_PLAUSIBLE_YIELD",
    "DividendEvent",
    "DividendKind",
    "GuardFailure",
    "amounts_agree",
    "check_amount_plausible",
    "check_cross_sourced",
    "check_sourced",
    "check_verified",
    "screen",
]
