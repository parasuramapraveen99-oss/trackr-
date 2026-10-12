"""Company research report contract DTOs (RP 00, docs/BUTLER_REPORTS.md sections 4-9).

STRICT AND CLOSED. Requests and documents forbid unknown fields and use strict scalars; every
number is a decimal STRING (never a JSON number, never a float) with an explicit unit, scale,
currency, period and basis. A cell or claim missing one of those is invalid, not defaulted.

NO NOTEBOOK FIELD CAN EXIST HERE. This module must never import ``app.schemas.butler_notebook``
or anything under ``app.db.butler_notebook`` / ``app.db.butler_episodes`` (``make lint``), and
``app/research/report_contract.py`` additionally refuses notebook-shaped keys anywhere in a
packet or document. The same source data, scope and as-of must yield the same company findings
whatever Butler has recorded about the reader.

NO RECOMMENDATION FIELD CAN EXIST HERE EITHER. There is no rating, target, fair value,
conviction, suitability or "flawless/complete" field; claim text is additionally screened.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

REPORT_CONTRACT_VERSION = "report-contract-1"
DECIMAL = r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?$"
POSITIVE_DECIMAL = r"^(0|[1-9][0-9]{0,17})(\.[0-9]{1,6})?$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
_SHA = r"^[0-9a-f]{64}$"

ReportMode = Literal["CURRENT_WITH_HISTORY", "POINT_IN_TIME"]
ReportScope = Literal["ISSUER_ONLY", "ISSUER_PLUS_PRIVATE_RESEARCH"]
Statement = Literal["INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW", "SEGMENTS",
                    "SHAREHOLDING", "AUDITOR_REPORT"]
Basis = Literal["CONSOLIDATED", "STANDALONE"]
Scale = Literal["UNITS", "THOUSANDS", "LAKHS", "MILLIONS", "CRORES", "BILLIONS"]
CellStatus = Literal["SUPPORTED", "RESTATED", "MISSING", "NOT_COMPARABLE", "CONFLICT",
                     "RIGHTS_DENIED", "NOT_REQUESTED"]
ClaimClass = Literal["SOURCE_STATEMENT", "CALCULATED_VALUE", "INTERPRETATION",
                     "HYPOTHETICAL_RISK", "UNKNOWN"]
ClaimVerification = Literal["CHECKED_BY_CODE", "EXTRACTIVE", "AI_INTERPRETATION",
                            "HUMAN_REVIEWED_INTERPRETATION", "NEEDS_REVIEW", "UNKNOWN"]
ReportStatus = Literal["READY_WITH_LIMITATIONS", "READY_CHECKED_SCOPE", "NEEDS_REVIEW",
                       "BLOCKED"]
JobState = Literal["QUEUED", "RUNNING", "WAITING_FOR_SOURCE", "BUDGET_PAUSED", "NEEDS_REVIEW",
                   "READY_WITH_LIMITATIONS", "READY_CHECKED_SCOPE", "FAILED",
                   "CANCEL_REQUESTED", "CANCELLED", "EXPIRED"]

#: The nine report sections, in order (section 6). A document may omit a section only by
#: carrying it with status UNKNOWN and a limitation; it may never add one.
SECTION_IDS = ("COVER_AND_SCOPE", "BUSINESS", "HISTORICAL_FINANCIALS", "CASH_FUNDING_OBLIGATIONS",
               "GOVERNANCE_ACCOUNTING", "WHAT_CHANGED", "EVIDENCE_FOR_AND_AGAINST",
               "RISKS_AND_OPEN_QUESTIONS", "METHODS_AND_SOURCES")
SectionId = Literal["COVER_AND_SCOPE", "BUSINESS", "HISTORICAL_FINANCIALS",
                    "CASH_FUNDING_OBLIGATIONS", "GOVERNANCE_ACCOUNTING", "WHAT_CHANGED",
                    "EVIDENCE_FOR_AND_AGAINST", "RISKS_AND_OPEN_QUESTIONS", "METHODS_AND_SOURCES"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =========================================================================================
# request -- what a person asks for (section 1-2)
# =========================================================================================


class ReportRequestIn(_Strict):
    """An explicit, owned, bounded request. The person decides afterwards; nothing here asks
    for, or can carry, a recommendation."""

    isin: StrictStr = Field(min_length=12, max_length=12)
    #: The person's own question. "Help me understand this business" is enough.
    question: StrictStr = Field(min_length=3, max_length=500)
    mode: ReportMode = "CURRENT_WITH_HISTORY"
    #: POINT_IN_TIME only: documents published after this date are excluded (no hindsight).
    cutoff: date | None = None
    #: A requested MAXIMUM, not an availability promise.
    window_years: StrictInt = Field(default=10, ge=1, le=10)
    statements: list[Statement] = Field(
        default_factory=lambda: ["INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW"],
        min_length=1, max_length=6)
    basis: Basis = "CONSOLIDATED"
    #: ISSUER_ONLY is the report-first brief (no ritual needed, no private corpus read).
    #: ISSUER_PLUS_PRIVATE_RESEARCH needs a completed ritual and is an explicit choice.
    scope: ReportScope = "ISSUER_ONLY"
    #: Maximum billable budget, decimal string, in the operator's declared currency.
    budget: StrictStr = Field(pattern=POSITIVE_DECIMAL)
    #: Wall-clock stop bound. At most the operator maximum (default four hours).
    deadline_minutes: StrictInt = Field(default=240, ge=5, le=240)


class ReportRevisionIn(_Strict):
    """A new bounded request linked to a prior report. There is no in-place rewrite."""

    question: StrictStr | None = Field(default=None, min_length=3, max_length=500)
    window_years: StrictInt | None = Field(default=None, ge=1, le=10)
    budget: StrictStr = Field(pattern=POSITIVE_DECIMAL)
    deadline_minutes: StrictInt = Field(default=240, ge=5, le=240)


# =========================================================================================
# coverage matrix -- company x fiscal period x statement x basis (section 5)
# =========================================================================================


class FiscalPeriod(_Strict):
    label: StrictStr = Field(pattern=r"^FY[0-9]{4}(-S[0-9])?$")
    start: date
    end: date
    #: Months in the period. 12 is comparable; anything else is a comparability gap.
    months: StrictInt = Field(ge=1, le=24)


class SourceRef(_Strict):
    source_id: StrictStr = Field(pattern=_ID)
    source_version: StrictStr = Field(pattern=_ID)
    #: Page/table/cell locator as printed in the SOURCE, never the PDF page of the report.
    locator: StrictStr = Field(min_length=1, max_length=120)


class CoverageCell(_Strict):
    cell_id: StrictStr = Field(pattern=_ID)
    period: FiscalPeriod
    statement: Statement
    basis: Basis
    status: CellStatus
    #: Why a cell is not SUPPORTED, as a reviewed code. Required unless SUPPORTED.
    gap_code: StrictStr | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=8)
    #: For RESTATED: the original figure's source is kept and named here.
    original_source_refs: list[SourceRef] = Field(default_factory=list, max_length=8)


class CoverageMatrix(_Strict):
    isin: StrictStr = Field(min_length=12, max_length=12)
    requested_years: StrictInt = Field(ge=1, le=10)
    basis: Basis
    cells: list[CoverageCell] = Field(max_length=200)


# =========================================================================================
# claim ledger (section 7)
# =========================================================================================


class ClaimValue(_Strict):
    value: StrictStr = Field(pattern=DECIMAL)
    unit: StrictStr = Field(pattern=r"^[A-Z]{3}$|^(PERCENT|RATIO|SHARES|COUNT|DAYS)$")
    scale: Scale
    period_label: StrictStr = Field(pattern=r"^FY[0-9]{4}(-S[0-9])?$")
    basis: Basis
    #: For a figure the source restated: which version is shown.
    version_shown: Literal["ORIGINAL", "RESTATED", "ONLY"] = "ONLY"


class Claim(_Strict):
    claim_id: StrictStr = Field(pattern=_ID)
    section_id: SectionId
    claim_class: ClaimClass
    text: StrictStr = Field(min_length=1, max_length=2000)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=8)
    #: SOURCE_STATEMENT: the exact excerpt the text rests on (checked against the source).
    excerpt: StrictStr | None = Field(default=None, max_length=2000)
    value: ClaimValue | None = None
    #: CALCULATED_VALUE: an approved formula id and VERSION (BE-Q03; there is no implicit
    #: latest) and the claim ids of its inputs, in the definition's input order.
    calculation_ref: StrictStr | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    calculation_version: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,32}$")
    input_claim_ids: list[StrictStr] = Field(default_factory=list, max_length=12)
    #: INTERPRETATION / HYPOTHETICAL_RISK: the premises, as claim ids.
    premise_claim_ids: list[StrictStr] = Field(default_factory=list, max_length=12)
    verification: ClaimVerification
    #: UNKNOWN: the named coverage/failure reason. Never filled by model knowledge.
    unknown_reason: StrictStr | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,63}$")


# =========================================================================================
# typed report document (sections 6-8)
# =========================================================================================


class Limitation(_Strict):
    code: StrictStr = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    #: Where it applies: a section id, a cell id, or COVER.
    applies_to: StrictStr = Field(pattern=_ID)


class Section(_Strict):
    section_id: SectionId
    status: Literal["COVERED", "PARTIAL", "UNKNOWN"]
    claim_ids: list[StrictStr] = Field(default_factory=list, max_length=400)
    limitations: list[Limitation] = Field(default_factory=list, max_length=40)


class RegisteredSource(_Strict):
    source_id: StrictStr = Field(pattern=_ID)
    source_version: StrictStr = Field(pattern=_ID)
    title: StrictStr = Field(min_length=1, max_length=300)
    origin: Literal["ISSUER_FILING", "EXCHANGE_FILING", "ANNUAL_REPORT", "AUDITOR_REPORT",
                    "RESULTS", "REGULATOR_ORDER", "NEWS", "USER_DOCUMENT"]
    #: Issuer claims are not independent confirmation (section 5).
    attribution: Literal["AUDITED", "UNAUDITED", "ISSUER_CLAIM", "NEWS_REPORT", "REGULATOR"]
    published_on: date | None
    retrieved_at: datetime
    content_sha256: StrictStr = Field(pattern=_SHA)
    fiscal_period: StrictStr | None = Field(default=None, pattern=r"^FY[0-9]{4}(-S[0-9])?$")
    basis: Basis | None = None
    #: Rights verdicts per use (BE-Q04). UNDECIDED is never treated as allowed.
    rights: dict[Literal["FETCH", "STORE", "EXTRACT", "MODEL", "EXPORT"],
                 Literal["ALLOWED", "DENIED", "UNDECIDED", "METADATA_ONLY"]]
    link: StrictStr | None = Field(default=None, max_length=500)


class ReportDocument(_Strict):
    schema_version: Literal["report.document.v1"]
    report_id: StrictStr = Field(pattern=_ID)
    report_version: StrictInt = Field(ge=1)
    isin: StrictStr = Field(min_length=12, max_length=12)
    question: StrictStr = Field(min_length=3, max_length=500)
    mode: ReportMode
    scope: ReportScope
    as_of: datetime
    cutoff: date | None
    status: ReportStatus
    #: The statement every cover carries. Constant; the model cannot word it.
    decision_notice: Literal["Research information only. The investor decides."]
    coverage: CoverageMatrix
    sections: list[Section] = Field(min_length=9, max_length=9)
    claims: list[Claim] = Field(max_length=2000)
    sources: list[RegisteredSource] = Field(max_length=500)
    limitations: list[Limitation] = Field(max_length=200)
    check_receipts: list[StrictStr] = Field(default_factory=list, max_length=50)


# =========================================================================================
# artifact manifest (section 8)
# =========================================================================================


class ArtifactManifest(_Strict):
    schema_version: Literal["report.artifact.v1"]
    artifact_id: StrictStr = Field(pattern=_ID)
    report_id: StrictStr = Field(pattern=_ID)
    report_version: StrictInt = Field(ge=1)
    format: Literal["PDF", "HTML"]
    sha256: StrictStr = Field(pattern=_SHA)
    byte_size: StrictInt = Field(ge=1)
    page_count: StrictInt | None = Field(default=None, ge=1)
    document_sha256: StrictStr = Field(pattern=_SHA)
    claim_ids: list[StrictStr] = Field(max_length=2000)
    check_receipts: list[StrictStr] = Field(min_length=1, max_length=50)
    #: Every registered source's EXPORT verdict must be ALLOWED or METADATA_ONLY for an artifact
    #: to exist; raw unlicensed market data never appears (invariant 17).
    contains_raw_source_rows: StrictBool
    renderer: StrictStr = Field(pattern=_ID)
    created_at: datetime


# =========================================================================================
# job DTOs (RP 01)
# =========================================================================================


class ReportJob(BaseModel):
    id: str
    purpose: Literal["COMPANY_REPORT"]
    isin: str
    request: dict
    state: JobState
    state_reason: str
    stage: str | None
    revision_of: str | None
    provider: str
    model: str
    currency: str
    budget_limit: str
    spent_worst_case: str
    deadline_at: datetime
    recorded_at: datetime
    completed_stages: list[str]
    coverage: dict | None


class JobEvent(BaseModel):
    version: int
    state: JobState
    stage: str | None
    reason_code: str
    detail: dict
    recorded_at: datetime


class JobEventsPage(BaseModel):
    items: list[JobEvent]
    next_cursor: int


# =========================================================================================
# published document and artifacts (RP 05-07)
# =========================================================================================


class ReportArtifactOut(BaseModel):
    id: str
    format: Literal["HTML", "PDF"]
    sha256: str
    byte_size: int
    page_count: int | None
    renderer: str
    recorded_at: datetime


class ReportRetractionOut(BaseModel):
    id: str
    reason: str
    detail: str | None
    recorded_at: datetime


class ReportDocumentOut(BaseModel):
    """The published, validated document and what may be downloaded. ``withheld`` lists claim
    ids held back at publication with their reason codes (codes only, no draft text)."""

    report_id: str
    report_document_id: str
    status: Literal["READY_WITH_LIMITATIONS", "READY_CHECKED_SCOPE"]
    document_sha256: str
    retracted: bool
    retractions: list[ReportRetractionOut]
    rights_changed: list[str]
    document: dict
    artifacts: list[ReportArtifactOut]
    withheld: dict[str, list[str]]


class ReportRetractionIn(_Strict):
    """The owner withdraws a report. Downloads stop; files already downloaded cannot be
    recalled, and the UI says so."""

    detail: StrictStr | None = Field(default=None, min_length=1, max_length=500)
