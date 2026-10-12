"""Verified corporate-action reference data -- docs/ENGINE_SPEC.md section 7's
"notice the quantity changed" step, with a known, sourced explanation behind it instead
of only a text guess for a human.

WHY THIS EXISTS. ``reconcile()`` already names the likely cause of a quantity mismatch
("ratio ~2 -> 1:1 bonus, or 1:2 split") but stops there -- by design, it is "a hypothesis
for a human, never an instruction to act" (app.engine.types.Discrepancy). Nothing turned
that hypothesis into the actual ``BONUS``/``SPLIT_ADJ``/``DEMERGER_IN`` transaction. This
module is the bridge: given a maintained, sourced table of real corporate actions, decide
whether one of them explains a specific ``Discrepancy`` -- and if so, exactly what
transaction would close it.

NO RATIO STRINGS. Indian sources disagree on notation order for the same event -- the
same Kama Holdings bonus is written "4:1" by one provider and "1:4" by another, and a
record that guesses wrong is 5x off with no error raised. Each action stores the
primitive facts printed on the exchange notice (``bonus_new``/``bonus_for``, or
``face_value_before``/``face_value_after``) and derives the multiplier itself.

PRICE CORROBORATES EVERY RATIO. A bonus or split mechanically divides the price by the
multiplier. ``check_price_consistency`` compares the recorded multiplier against the
observed cum/ex price move and rejects the record when they disagree -- the only
automatic defence against an inverted ratio, which is why cum and ex prices are
mandatory for BONUS/SPLIT, not optional.

FAIL CLOSED. An action that trips any guard in ``screen()`` is quarantined, never
matched against a ``Discrepancy`` with a warning. This module never writes a
transaction itself -- see app.ingest.corporate_action_matcher and
app.db.corporate_actions for the staging step that proposes one through the same
review-then-promote flow every file adapter already uses.

SCOPE. Three action types only: BONUS, SPLIT (forward split; a reverse split/
CONSOLIDATION would need to REMOVE quantity while leaving the cost pool untouched, which
nothing in ``app.engine.holdings``/``lots`` can do today -- a separate engine/schema
decision), and DEMERGER (quantity leg only; the cost-ratio leg stays the existing,
separate, manual ``app.engine.demerger.demerger_cost_split`` step, since that number
comes from a scheme document no feed carries). MERGER is absent for the same reason as
CONSOLIDATION: ``TxnType`` has no merger transaction type today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from fractions import Fraction

from app.engine.types import Discrepancy

ZERO = Decimal("0")


class CorpActionType(StrEnum):
    BONUS = "BONUS"
    SPLIT = "SPLIT"
    DEMERGER = "DEMERGER"


class Verification(StrEnum):
    """A CorporateAction may not match a Discrepancy until it is VERIFIED."""

    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"


class CorporateActionRatioError(ValueError):
    """Raised when a record cannot yield an unambiguous multiplier -- at construction,
    not at match time, so an incomplete record fails at entry rather than silently
    never matching anything."""


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One verified corporate action, stored as the facts on the exchange notice.

    The multiplier is *derived*, never supplied -- see ``multiplier()``. Constructors
    validate that the fields needed for the given ``action_type`` are present, so an
    incomplete record fails at ingest rather than at match time.
    """

    isin: str
    action_type: CorpActionType
    ex_date: date
    source: str
    source_url: str
    verification: Verification = Verification.DRAFT

    # --- BONUS: "N new shares for every M held" -----------------------------
    bonus_new: int | None = None
    bonus_for: int | None = None

    # --- SPLIT: face value before and after ----------------------------------
    face_value_before: Decimal | None = None
    face_value_after: Decimal | None = None

    # --- DEMERGER: quantity leg only ------------------------------------------
    resulting_isin: str | None = None
    demerger_new: int | None = None
    demerger_for: int | None = None

    # --- audit ------------------------------------------------------------
    price_cum: Decimal | None = None  # close on the last cum day
    price_ex: Decimal | None = None  # close on the ex date
    # Set only by app.db.corporate_actions.waive_price_evidence, for an ex_date before
    # this system's bhavcopy archive begins -- see 025_price_evidence_waiver.sql. Not a
    # blanket bypass: check_price_consistency still applies BAD_PRICE/
    # RATIO_CONTRADICTS_PRICE whenever price_cum/price_ex happen to be present anyway.
    price_waived: bool = False
    price_waiver_reason: str = ""
    notes: str = ""
    id: str | None = None
    # Evidence dates for a BONUS lot's tax acquisition date (ENGINE_SPEC section 3,
    # app.engine.lots.bonus_acquisition_date). Data only: neither moves the ex-date,
    # quantity or valuation, and a missing one never blocks application.
    allotment_date: date | None = None
    record_date: date | None = None
    cancelled: bool = False
    automatic_application: bool = True

    def __post_init__(self) -> None:
        self.multiplier()  # raises CorporateActionRatioError if the record is unusable

    @property
    def dedup_key(self) -> tuple:
        return (self.isin, self.action_type, self.ex_date)

    def multiplier(self) -> Fraction:
        """Shares held after, per one share held before. ``Fraction(1)`` for DEMERGER --
        the parent's own quantity is untouched; the resulting company's share count is
        ``demerger_new``/``demerger_for``, read directly in ``explain()`` below."""
        t = self.action_type

        if t is CorpActionType.BONUS:
            if not self.bonus_new or not self.bonus_for:
                raise CorporateActionRatioError("BONUS needs bonus_new and bonus_for")
            if self.bonus_new < 1 or self.bonus_for < 1:
                raise CorporateActionRatioError("BONUS counts must be positive integers")
            # N new for every M held -> (N + M) held after, per M before.
            return Fraction(self.bonus_new + self.bonus_for, self.bonus_for)

        if t is CorpActionType.SPLIT:
            fb, fa = self.face_value_before, self.face_value_after
            if fb is None or fa is None:
                raise CorporateActionRatioError(
                    "SPLIT needs face_value_before and face_value_after"
                )
            if fb <= 0 or fa <= 0:
                raise CorporateActionRatioError("face values must be positive")
            m = Fraction(fb) / Fraction(fa)
            if m <= 1:
                raise CorporateActionRatioError(
                    "SPLIT must increase share count (face_value_before > face_value_after); "
                    "a reverse split/consolidation is not represented in this system"
                )
            return m

        if t is CorpActionType.DEMERGER:
            if not self.demerger_new or not self.demerger_for:
                raise CorporateActionRatioError("DEMERGER needs demerger_new and demerger_for")
            if not self.resulting_isin:
                raise CorporateActionRatioError("DEMERGER needs resulting_isin")
            return Fraction(1)

        raise CorporateActionRatioError(f"unhandled action type {t}")


@dataclass(frozen=True, slots=True)
class GuardFailure:
    action: CorporateAction
    code: str
    detail: str


# The ex-date price should fall by roughly the multiplier. Real markets move on the
# day, so the band is wide -- it is here to catch inverted ratios (5x vs 0.2x), not to
# police a 3% drift. Mirrors app.engine.reconcile.RATIO_TOLERANCE's spirit at a wider
# band, since this checks a price move, not a bare quantity ratio.
PRICE_BAND_LOW = Fraction(70, 100)
PRICE_BAND_HIGH = Fraction(140, 100)

#: How close an observed Discrepancy ratio must be to a CorporateAction's multiplier
#: before treating the action as the explanation. Same value as
#: app.engine.reconcile.RATIO_TOLERANCE, kept as an independent constant: this module
#: must not import from reconcile.py, which is ingest-adjacent and already layered
#: above plain engine types.
MATCH_TOLERANCE = Decimal("0.01")


def _as_decimal(frac: Fraction) -> Decimal:
    """Render a Fraction for a message without ever routing it through the banned
    binary numeric type. Four decimal places is plenty for "is this ratio inverted?"."""
    return (Decimal(frac.numerator) / Decimal(frac.denominator)).quantize(Decimal("0.0001"))


def check_price_consistency(action: CorporateAction) -> GuardFailure | None:
    """A bonus or split mechanically divides the price by the multiplier.

    If the observed ex/cum price ratio disagrees with the recorded multiplier, the
    *record* is wrong -- almost always an inverted ratio notation. DEMERGER is exempt:
    a demerger's value splits between parent and resulting company in a proportion this
    table does not carry, so there is no single expected price ratio to check.
    """
    if action.action_type not in (CorpActionType.BONUS, CorpActionType.SPLIT):
        return None
    if action.price_cum is None or action.price_ex is None:
        # A waiver only ever covers exactly this absence -- see price_waived's field
        # docstring and app.db.corporate_actions.waive_price_evidence, which is the only
        # place price_waived is ever set to True and only for an ex_date this system's
        # price archive can never reach.
        if action.price_waived:
            return None
        return GuardFailure(
            action, "NO_PRICE_EVIDENCE",
            "ratio cannot be corroborated without cum and ex closing prices",
        )
    if action.price_cum <= 0 or action.price_ex <= 0:
        return GuardFailure(action, "BAD_PRICE", "prices must be positive")

    m = action.multiplier()
    implied = Fraction(action.price_cum) / Fraction(action.price_ex)
    agreement = implied / m
    if not (PRICE_BAND_LOW <= agreement <= PRICE_BAND_HIGH):
        return GuardFailure(
            action,
            "RATIO_CONTRADICTS_PRICE",
            f"recorded multiplier {_as_decimal(m)} but the price move implies "
            f"{_as_decimal(implied)} -- ratio notation is probably inverted",
        )
    return None


def check_verified(action: CorporateAction) -> GuardFailure | None:
    if action.verification is not Verification.VERIFIED:
        return GuardFailure(
            action, "UNVERIFIED",
            f"verification is {action.verification.value}; will not match a Discrepancy",
        )
    return None


def check_sourced(action: CorporateAction) -> GuardFailure | None:
    if not action.source or not action.source_url:
        return GuardFailure(action, "UNSOURCED", "every action needs a provenance URL")
    return None


def check_no_duplicates(actions: list[CorporateAction]) -> list[GuardFailure]:
    counts: dict[tuple, int] = {}
    for a in actions:
        counts[a.dedup_key] = counts.get(a.dedup_key, 0) + 1
    return [
        GuardFailure(a, "DUPLICATE", f"{a.action_type.value} on {a.ex_date} recorded twice")
        for a in actions
        if counts[a.dedup_key] > 1
    ]


def screen(actions: list[CorporateAction]) -> tuple[list[CorporateAction], list[GuardFailure]]:
    """Split a batch into (safe to match, quarantined).

    Fails closed: anything that trips a guard is withheld, never matched against a
    Discrepancy with a warning.
    """
    failures: list[GuardFailure] = list(check_no_duplicates(actions))
    bad_ids = {id(f.action) for f in failures}

    for a in actions:
        for guard in (check_sourced, check_verified, check_price_consistency):
            f = guard(a)
            if f is not None:
                failures.append(f)
                bad_ids.add(id(a))

    safe = [a for a in actions if id(a) not in bad_ids]
    return safe, failures


@dataclass(frozen=True, slots=True)
class MatchedAction:
    """A verified CorporateAction that explains one Discrepancy, plus exactly what a
    resulting staged transaction needs: which ISIN it lands on, which TxnType, and the
    additional-share quantity. DERIVED, NEVER STORED -- app.ingest.corporate_action_matcher
    is what turns this into a StagedRow."""

    action: CorporateAction
    isin: str  # where the resulting transaction lands -- action.isin, or
    # action.resulting_isin for a DEMERGER
    txn_type: str  # "BONUS" | "SPLIT_ADJ" | "DEMERGER_IN"
    quantity: Decimal


#: ``raw_payload`` key on a derived bonus/split row whose entitlement had a fractional part:
#: the fraction, as a string. Owner decision 2026-10-08 (ARCHITECTURE_INVARIANTS rule 20,
#: "Fractional entitlements"): the whole shares are credited and the fraction is treated as
#: settled in cash -- the company consolidates fractions, sells them and pays the holders,
#: as SEBI/Companies Act practice requires. The cash amount is on no record Trackr+ has,
#: so it is NOT booked; the row names the fraction instead of hiding it.
FRACTION_SETTLED_IN_CASH_KEY = "fraction_settled_in_cash"


def whole_shares(quantity: Decimal) -> tuple[Decimal, Decimal]:
    """(shares credited, fraction settled in cash) -- floor, never round up: a holder is
    never credited a share the company did not allot."""
    whole = quantity.to_integral_value(rounding=ROUND_FLOOR)
    if quantity.as_tuple().exponent < 0:
        whole = whole.quantize(quantity)  # keep the ledger's scale: 100.000000, not 100
    return whole, quantity - whole


def free_share_effect(action: CorporateAction, held_qty: Decimal) -> tuple[str, str, Decimal]:
    """What a VERIFIED ``action`` credits, given ``held_qty`` at the action's own ex_date
    -- (isin the transaction lands on, txn_type, quantity), independent of any
    ``Discrepancy``.

    The one place the BONUS/SPLIT/DEMERGER arithmetic lives. ``explain`` (below) wraps
    this with the reconcile-vs-declared-holdings matching a human-supplied statement
    needs; ``app.db.corporate_actions.apply_known_actions_for_account`` calls this
    directly instead -- a VERIFIED, price-corroborated action applies to every eligible
    holder unconditionally (that is what "corporate action" means), so confirming it
    against a declared-holdings discrepancy is not a precondition for correctness, only
    a second, independent check available when one exists.
    """
    t = action.action_type
    if t in (CorpActionType.BONUS, CorpActionType.SPLIT):
        m = action.multiplier()
        quantity = held_qty * Decimal(m.numerator) / Decimal(m.denominator) - held_qty
        txn_type = "BONUS" if t is CorpActionType.BONUS else "SPLIT_ADJ"
        return action.isin, txn_type, quantity

    if t is CorpActionType.DEMERGER:
        share_m = Fraction(action.demerger_new, action.demerger_for)
        quantity = held_qty * Decimal(share_m.numerator) / Decimal(share_m.denominator)
        return action.resulting_isin, "DEMERGER_IN", quantity

    raise CorporateActionRatioError(f"unhandled action type {t}")


def explain(
    discrepancy: Discrepancy, action: CorporateAction, held_qty: Decimal
) -> MatchedAction | None:
    """Does ``action`` explain ``discrepancy``, and if so, what belongs on the
    resulting staged transaction?

    ``held_qty`` is the quantity the multiplier applies to: for BONUS/SPLIT that is
    ``discrepancy.computed_qty`` (the action's own ISIN is what moved) -- the caller
    passes it explicitly rather than this function reaching into a holdings map, to
    keep this a pure function of its three arguments. For DEMERGER it is the PARENT
    security's held quantity, which the caller must look up separately: the
    discrepancy here is on ``action.resulting_isin``, a security the ledger has never
    seen before, so ``discrepancy.computed_qty`` is always zero and cannot supply it.

    Returns ``None`` -- never raises -- when ``action`` simply does not address this
    discrepancy, which is the common, expected case when scanning many actions against
    one discrepancy.
    """
    t = action.action_type

    if t in (CorpActionType.BONUS, CorpActionType.SPLIT):
        if action.isin != discrepancy.isin or discrepancy.ratio is None:
            return None
        m = action.multiplier()
        if abs(Fraction(discrepancy.ratio) - m) > Fraction(MATCH_TOLERANCE):
            return None
        isin, txn_type, quantity = free_share_effect(action, held_qty)
        return MatchedAction(action, isin, txn_type, quantity)

    if t is CorpActionType.DEMERGER:
        if action.resulting_isin != discrepancy.isin or discrepancy.computed_qty != ZERO:
            return None
        isin, txn_type, quantity = free_share_effect(action, held_qty)
        # declared_qty is the depository's own count for the resulting company; an
        # action whose arithmetic does not land near it is not the explanation for
        # THIS discrepancy, even if it is a real, verified demerger for this ISIN.
        tolerance = Fraction(held_qty * MATCH_TOLERANCE)
        if abs(Fraction(quantity) - Fraction(discrepancy.declared_qty)) > tolerance:
            return None
        return MatchedAction(action, isin, txn_type, quantity)

    return None


def match_corporate_action(
    discrepancy: Discrepancy, actions: list[CorporateAction], held_qty: Decimal
) -> MatchedAction | None:
    """Find the first VERIFIED, guard-passing action that explains ``discrepancy``.

    Screens ``actions`` every call rather than requiring a pre-screened list: callers
    hold a small, per-ISIN candidate set (a handful of actions on one security, not the
    whole reference table), so re-screening here is cheap and removes an easy way to
    accidentally match against a quarantined record.
    """
    safe, _failures = screen(actions)
    for action in safe:
        matched = explain(discrepancy, action, held_qty)
        if matched is not None:
            return matched
    return None
