"""DTOs for ``app/routers/research.py`` -- sources, facts and evaluations
(BLUEPRINT_BACKEND.md section 7.3). Same conventions as ``app/schemas/workspace.py``: strict,
closed request models; enumerated response fields; every numeric value a decimal string.

A FactCreate CANNOT carry ``verification``, ``verified_by``, a sequence or a trigger state,
and an evaluation request carries ONLY its cutoff: state, digest, engine version, inputs and
``evaluated_at`` are all computed by the server.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

SourceType = Literal[
    "EXCHANGE_FILING", "ANNUAL_REPORT", "RESULTS", "PRESENTATION", "TRANSCRIPT",
    "SHAREHOLDING", "NEWS", "MANAGEMENT_COMMENTARY", "USER_DOCUMENT", "BOOK",
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCreateIn(_In):
    source: StrictStr = Field(min_length=1, max_length=512)
    source_type: SourceType
    #: A public canonical URL or isbn:/doi:/urn: reference. Never fetched by the server.
    source_url: StrictStr = Field(min_length=1, max_length=2048)
    #: REQUIRED. An unknown publication date is not provenance; the upload time is never
    #: substituted for it.
    publication_date: date
    period: StrictStr = Field(min_length=1, max_length=64)
    retrieved_at: AwareDatetime
    #: Page, section or note inside the source. "The annual report" is not a citation.
    document_reference: StrictStr = Field(min_length=1, max_length=512)
    document_id: StrictStr | None = None
    #: Optional client assertion, checked against the server's own hash; never trusted.
    content_sha256: StrictStr | None = None


class SourceContentOut(BaseModel):
    source_id: str
    content_sha256: str
    text_sha256: str
    extractor_id: str
    extractor_version: str
    normalization: str
    page: int | None
    page_count: int
    text: str
    document_reference: str


class FactCreateIn(_In):
    source_id: StrictStr
    metric: StrictStr = Field(min_length=1, max_length=128)
    basis: StrictStr = Field(min_length=1, max_length=64)
    unit: StrictStr = Field(min_length=1, max_length=32)
    period_start: date
    period_end: date
    cadence: Literal["QUARTER", "YEAR", "POINT"]
    #: A finite decimal STRING fitting NUMERIC(20,6). A JSON number is refused.
    numeric_value: StrictStr | None = None
    boolean_value: StrictBool | None = None
    text_value: StrictStr | None = Field(default=None, min_length=1, max_length=4096)


class FactRevisionIn(FactCreateIn):
    expected_fact_id: StrictStr


class SourceRefreshIn(_In):
    """BE 29. A bounded refresh through ONE approved adapter. Periods are the adapter's own
    period labels (e.g. FY2024); at most ten -- a historical sweep is a separate operation."""

    adapter_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    periods: list[StrictStr] = Field(min_length=1, max_length=10)


class SourceRefreshOut(BaseModel):
    adapter_id: str
    synthetic: bool
    isin: str
    periods: list[str] = []
    sources: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    replayed: bool = False
    source_ids: list[str] = []
    fact_ids: list[str] = []
    event_ids: list[str] = []
    duplicate_source_ids: list[str] = []
    unknown_codes: list[str] = []


class EventVerificationIn(_In):
    verification_reference: StrictStr = Field(min_length=1, max_length=512)
    expected_event_id: StrictStr


class ResearchEvent(BaseModel):
    id: str
    isin: str
    source_id: str
    event_type: str
    status: Literal["SCHEDULED", "OBSERVED", "CLAIMED", "CANCELLED"]
    event_date: str | None
    date_precision: Literal["EXACT", "IMPRECISE", "UNKNOWN"]
    period_start: str | None
    period_end: str | None
    description: str
    locator: str
    verification: Literal["UNVERIFIED", "VERIFIED"]
    verification_reference: str | None
    supersedes_id: str | None
    superseded_by: str | None
    reschedules_id: str | None
    rescheduled_by: list[str]
    source_type: str
    publication_date: str
    retrieved_at: str
    recorded_at: str
    classification_verified: bool


class FactVerificationIn(_In):
    verification_reference: StrictStr = Field(min_length=1, max_length=512)
    expected_fact_id: StrictStr


class Fact(BaseModel):
    id: str
    isin: str
    source_id: str
    metric: str
    basis: str
    unit: str
    period_start: date
    period_end: date
    cadence: str
    numeric_value: str | None
    boolean_value: bool | None
    text_value: str | None
    verification: Literal["UNVERIFIED", "VERIFIED"]
    verified_by: str | None
    verification_reference: str | None
    supersedes_id: str | None
    registry_version: str | None
    recorded_at: datetime
    superseded_by: str | None
    conflicts_with: list[str]


class EvaluationRequestIn(_In):
    evidence_cutoff: AwareDatetime


class WindowOut(BaseModel):
    period_start: str
    period_end: str
    state: str
    value: str | bool | None
    fact_ids: list[str]
    source_ids: list[str]
    publication_date: str | None
    reason: str | None
    comparison: bool | None


class RuleResultOut(BaseModel):
    rule_id: str
    kind: Literal["VARIABLE", "EXIT"]
    state: Literal["MET", "NOT_MET", "UNKNOWN", "CONFLICT"]
    ok: bool | None
    required_windows: list[dict[str, str]]
    windows: list[WindowOut]
    fact_ids: list[str]
    source_ids: list[str]
    comparator: str
    threshold: str
    latest: str | bool | None
    latest_publication_date: str | None
    met_on: str | None
    reason_codes: list[str]


class Evaluation(BaseModel):
    """READ evaluations have ``id``/``evaluated_at``/``replay_manifest`` null: nothing was
    stored. A stored receipt carries all three."""

    id: str | None
    thesis_id: str
    state: Literal["TRIGGERED", "NOT_TRIGGERED", "UNKNOWN", "CONFLICT"]
    variables: list[RuleResultOut]
    exits: list[RuleResultOut]
    evidence_cutoff: datetime
    evaluated_at: datetime | None
    engine_version: str
    input_digest: str
    replay_manifest: dict[str, Any] | None
    fact_ids: list[str]


class ReplayOut(BaseModel):
    status: Literal["VERIFIED", "DRIFT", "UNAVAILABLE"]
    reasons: list[str]


class EvaluationDetail(Evaluation):
    replay: ReplayOut | None = None


# --- methods (BE 16-17) ---------------------------------------------------------------------


class SkillCreateIn(_In):
    """A publication of a KNOWN catalogue method. There is no field for the house-rules
    version, a provider, a model, a key or executable code; the catalogue boundary
    (``app/research/skills.py::check_create``) decides what the lists may contain."""

    expected_version: StrictInt = Field(ge=0)
    name: StrictStr = Field(min_length=1, max_length=200)
    summary: StrictStr = Field(min_length=1, max_length=2000)
    method: StrictStr = Field(min_length=1)
    reads: list[StrictStr] = Field(min_length=1, max_length=20)
    citation_rules: list[StrictStr] = Field(min_length=1, max_length=40)
    permitted_tools: list[StrictStr] = Field(max_length=20)
    forbidden_conclusions: list[StrictStr] = Field(min_length=1, max_length=60)
    output_contract: dict[str, Any]
    enabled: StrictBool


class MissingItem(BaseModel):
    id: str
    kind: str
    code: str
    gate: str | None


class Availability(BaseModel):
    state: Literal["READY", "UNKNOWN"]
    missing: list[MissingItem]


class Skill(BaseModel):
    id: str
    version: int
    name: str
    summary: str
    method: str
    reads: list[str]
    citation_rules: list[str]
    permitted_tools: list[str]
    forbidden_conclusions: list[str]
    output_contract: dict[str, Any]
    house_rules_version: str
    enabled: bool
    recorded_at: datetime
    method_key: str | None
    task_route: str | None
    availability: Availability


class CatalogueOut(BaseModel):
    catalogue_version: str
    house_rules_version: str
    house_rules: list[dict[str, str]]
    methods: list[dict[str, Any]]
    undeclared_methods: list[str]
    #: BE 35 cross-method audit: findings per method against the shared house rules.
    audit: list[dict[str, Any]] = []
    formulas: dict[str, Any] = {}
    source_adapters: list[dict[str, Any]] = []
    tools: dict[str, Any]


# --- read tools (BE 18) --------------------------------------------------------------------


class ToolRequestIn(_In):
    isin: StrictStr = Field(min_length=12, max_length=12)
    task_id: StrictStr | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class ToolReference(BaseModel):
    kind: str
    id: str
    publication_date: date | None = None
    retrieved_at: datetime | None = None


class ToolConflict(BaseModel):
    claim: str
    source_ids: list[str]
    description: str


class ToolNotice(BaseModel):
    code: str
    message: str


class ToolResponse(BaseModel):
    status: Literal["OK", "UNKNOWN", "CONFLICT"]
    data: Any
    references: list[ToolReference]
    calculation: dict[str, Any] | None = None
    conflicts: list[ToolConflict]
    notices: list[ToolNotice]
