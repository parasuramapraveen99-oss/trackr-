"""DTOs for /investors/{id}/corporate-actions* -- verified corporate-action reference
data, and the matcher's /match "Apply" step (ENGINE_SPEC section 7's reconciliation,
turned into a staged transaction instead of only a text hint)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.db.action_ingestion import REASON_CODES
from app.ingest.action_source import ActionBatch
from app.schemas.decimals import OptionalDecimalText
from app.schemas.reconcile import DeclaredHoldingIn


class CorporateActionIn(BaseModel):
    """Primitive facts off the exchange notice -- never a ratio string. Which fields
    are required depends on ``action_type``; ``app.engine.corporate_actions
    .CorporateAction.__post_init__`` is what actually enforces that and raises a 422
    for an incomplete record, not this schema."""

    isin: str = Field(pattern=r"^IN[A-Z0-9]{10}$")
    action_type: str = Field(description="BONUS | SPLIT | DEMERGER")
    ex_date: date
    allotment_date: date | None = None

    # --- BONUS ---------------------------------------------------------------
    bonus_new: int | None = None
    bonus_for: int | None = None

    # --- SPLIT -----------------------------------------------------------------
    # Exact decimal text or null -- app/schemas/decimals.py. A face value is the SPLIT
    # RATIO's own numerator and denominator (10 -> 2 is a five-for-one), so a rounded one
    # is a wrong ratio applied to every lot the security has.
    face_value_before: OptionalDecimalText = None
    face_value_after: OptionalDecimalText = None

    # --- DEMERGER: quantity leg only --------------------------------------------
    resulting_isin: str | None = Field(default=None, pattern=r"^IN[A-Z0-9]{10}$")
    demerger_new: int | None = None
    demerger_for: int | None = None

    # --- audit: mandatory for BONUS/SPLIT, the only automatic defence against an
    # inverted ratio ---------------------------------------------------------
    price_cum: OptionalDecimalText = None
    price_ex: OptionalDecimalText = None

    source: str
    source_url: str
    verification: str = "DRAFT"
    notes: str = ""


class CorporateActionOut(BaseModel):
    """Mirrors ``CorporateActionIn`` plus ``id``. Money/ratio fields cross as strings,
    same convention as every other decimal field in this API (``app.database.money``)."""

    id: str | None = None
    isin: str
    action_type: str
    ex_date: date
    allotment_date: date | None = None
    cancelled: bool = False
    automatic_application: bool = True
    bonus_new: int | None = None
    bonus_for: int | None = None
    face_value_before: str | None = None
    face_value_after: str | None = None
    resulting_isin: str | None = None
    demerger_new: int | None = None
    demerger_for: int | None = None
    price_cum: str | None = None
    price_ex: str | None = None
    source: str
    source_url: str
    verification: str
    notes: str = ""
    price_waived: bool = False
    price_waiver_reason: str = ""


class MatchCorporateActionsRequest(BaseModel):
    """Body for POST /investors/{id}/corporate-actions/match -- the MProfit "Apply"
    step. ``holdings`` is the same declared-quantity shape ``/reconcile`` already
    takes (CAS- or broker-declared), since matching runs reconciliation first."""

    account_id: str
    as_of: date | None = None
    holdings: list[DeclaredHoldingIn] = Field(default_factory=list)


class MatchCorporateActionsOut(BaseModel):
    """What got staged. Review and promotion happen through the existing
    /ingest/stage/{batch_id} endpoints -- this never applies anything silently."""

    batch_id: str
    parsed: int
    rejected: int
    notes: list[str] = Field(default_factory=list)


class DisputeCorporateActionIn(BaseModel):
    """Body for POST /ops/corporate-actions/{id}/dispute -- why a human rejected an
    Exceptions entry, kept on the row (never deleted) as the record of what was
    rejected and why."""

    reason: str = ""


class WaivePriceEvidenceIn(BaseModel):
    """Body for POST /ops/corporate-actions/{id}/waive-price-evidence -- why a human is
    accepting a BONUS/SPLIT whose ex_date predates this system's price archive without
    the usual cum/ex corroboration. See ``app.db.corporate_actions.waive_price_evidence``
    for what this does and does not bypass."""

    reason: str


class RefreshCorporateActionFeedOut(BaseModel):
    """Result of one automated feed run -- see
    ``app.db.corporate_actions.refresh_corporate_action_feed``.

    Deliberately has NO ``error`` field: a fetch or format failure raises (plan H3) and
    a row that failed is recorded REJECTED then re-raised, so a 200 by construction
    means nothing failed. A field that can only ever be null invites a caller to read
    null as "succeeded" rather than as "not modelled"."""

    fetched: int
    verified: int
    exceptions: int
    #: Notices whose terms contradict a stored action: recorded as CONFLICT observations,
    #: never merged (the stored row is not mutated).
    conflicts: int = 0


class DiscoverYfinanceOut(BaseModel):
    """Result of one yfinance discovery run -- see
    ``app.db.corporate_actions.discover_from_yfinance``."""

    isins_checked: int
    discovered: int
    verified: int
    exceptions: int
    #: Candidates already on file from another path (or a prior run) -- the two discovery
    #: sources agreeing, which is the system working rather than anything to act on.
    duplicates: int = 0
    #: NSE-listed securities not asked about: Yahoo split lookup is BSE-only (2026-10-31).
    skipped_nse_listed: int = 0


# ---------------------------------------------------------------------------
# Operator-only routes (POST /ops/corporate-actions/import, activation, dividend
# issuer verification). Global reference writes; never reachable by an investor login.
# ---------------------------------------------------------------------------


class AuthorizedImport(BaseModel):
    """Body for POST /ops/corporate-actions/import: one operator file plus the data-rights
    reference that authorizes storing it."""

    rights_reference: str = Field(min_length=1, max_length=2000)
    batch: ActionBatch


class ActionActivation(BaseModel):
    enabled: bool


class DividendVerification(BaseModel):
    evidence_reference: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# GET /investors/{id}/corporate-actions/timeline. Derived on read from the reference
# tables and each account's own ledger; nothing here is stored.
# ---------------------------------------------------------------------------

#: Per-account (and, aggregated, per-event) state. Fixed; clients map it to copy.
TimelineState = Literal[
    "APPLIED", "PENDING", "REVIEW", "EXCLUDED", "NOT_HELD", "CANCELLED", "CONFLICT"
]

#: Reason codes the timeline derives itself, beyond the observation reason codes.
TIMELINE_DERIVED_REASON_CODES = frozenset({
    # Reference state
    "AWAITING_VERIFICATION", "REFERENCE_DISPUTED", "CANCELLED_BY_SOURCE", "UPCOMING",
    "AUTOMATIC_APPLICATION_OFF", "AWAITING_APPLICATION",
    # Engine guards (app.engine.corporate_actions.screen)
    "NO_PRICE_EVIDENCE", "BAD_PRICE", "RATIO_CONTRADICTS_PRICE", "UNSOURCED", "DUPLICATE",
    "UNVERIFIED",
    # Withdrawn evidence on an applied row (app.engine.action_replay, app.engine.dividends)
    "ACTION_MISSING", "FRACTIONAL_ENTITLEMENT", "CORPORATE_ACTION_EVIDENCE_WITHDRAWN",
    # Account eligibility
    "COMBINED_ACTIONS", "LEDGER_INCOMPLETE",
    # Not automated (OD-7): detection and REVIEW only
    "ACTION_NOT_AUTOMATED", "MANUAL_LEDGER_ENTRY_REQUIRED",
    # Dividends (app.engine.dividends / app.engine.dividend_events)
    "EXPECTED_NOT_RECEIVED", "SHARE_COUNT_CHANGED", "UNCORROBORATED", "SELF_CORROBORATED",
    "BAD_AMOUNT", "AMOUNT_IMPLAUSIBLE",
})

#: Every reason code the timeline may emit: the observation reason codes
#: (``app.db.action_ingestion.REASON_CODES``, the single source for anything recorded
#: from a source) plus the derived codes above. Frozen into the response schema; clients
#: map these to copy and never match on free text.
TIMELINE_REASON_CODES = REASON_CODES | TIMELINE_DERIVED_REASON_CODES
TimelineReasonCode = Literal[tuple(sorted(TIMELINE_REASON_CODES))]  # type: ignore[valid-type]

TimelineKind = Literal[
    "BONUS",
    "SPLIT",
    "DEMERGER",
    "DIVIDEND",
    "ISIN_CHANGE",
    "RIGHTS",
    "BUYBACK",
    "MERGER",
    "CAPITAL_REDUCTION",
    "CONSOLIDATION",
]

#: Where a BONUS lot's tax acquisition date came from -- ENGINE_SPEC section 3, OD-1.
#: The three values are fixed there (and in app.engine.types.AcquisitionDateBasis, the
#: same set as a StrEnum); this Literal is the wire form, and mirrors
#: packages/trackr-core/src/api-types.ts's AcquisitionDateBasis exactly.
AcquisitionDateBasis = Literal[
    "ALLOTMENT", "PROVISIONAL_RECORD_DATE", "PROVISIONAL_EX_DATE"
]


class TimelineEffectOut(BaseModel):
    """What the event did (or, ``projected=True``, is expected to do) to one account.
    ``quantity`` for share entitlements, ``amount`` for dividends; decimal strings."""

    txn_type: str
    isin: str
    quantity: str | None = None
    amount: str | None = None
    projected: bool = False


class TimelineAccountOut(BaseModel):
    account_id: str
    account_label: str
    state: TimelineState
    reason_code: TimelineReasonCode | None = None
    #: Held at the close before the ex-date, from this account's own ledger only.
    #: Null when no ex-date is known.
    eligible_quantity: str | None = None
    effect: TimelineEffectOut | None = None
    #: BONUS lots only (ENGINE_SPEC section 3, OD-1).
    acquisition_date_basis: AcquisitionDateBasis | None = None


class TimelineEventOut(BaseModel):
    id: str
    kind: TimelineKind
    isin: str
    symbol: str | None = None
    #: DEMERGER resulting ISIN, or the replacement ISIN of an ISIN_CHANGE.
    related_isin: str | None = None
    ex_date: date | None = None
    record_date: date | None = None
    allotment_date: date | None = None
    payment_date: date | None = None
    bonus_new: int | None = None
    bonus_for: int | None = None
    face_value_before: str | None = None
    face_value_after: str | None = None
    amount_per_share: str | None = None
    source: str
    source_url: str | None = None
    #: Latest time a source observation of this event was recorded, if any.
    observed_at: datetime | None = None
    #: DRAFT / VERIFIED / DISPUTED for reference rows; null for a notice-only event.
    reference_state: str | None = None
    #: The most significant account state (CONFLICT > REVIEW > EXCLUDED > PENDING >
    #: APPLIED > CANCELLED > NOT_HELD) and that account's reason code.
    state: TimelineState
    reason_code: TimelineReasonCode | None = None
    accounts: list[TimelineAccountOut]


class TimelineCoverageRangeOut(BaseModel):
    provider: str
    start: date
    end: date
    exchanges: list[str]
    checked_at: datetime


class TimelineCoverageOut(BaseModel):
    """Observed source ranges. Never a claim that a range is complete."""

    state: Literal["OBSERVED_RANGES_ONLY", "NO_OBSERVED_RANGES"]
    #: Latest end date of any observed range; not a completeness claim.
    checked_through: date | None = None
    ranges: list[TimelineCoverageRangeOut]


class CorporateActionTimelineOut(BaseModel):
    events: list[TimelineEventOut]
    coverage: TimelineCoverageOut
    has_more: bool
    offset: int
