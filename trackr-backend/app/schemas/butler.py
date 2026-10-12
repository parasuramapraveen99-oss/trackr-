"""DTOs for ``app/routers/butler.py`` (BT 01, docs/BUTLER_ARCHITECTURE.md section 9).

Same conventions as ``app/schemas/workspace.py``. REQUESTS FORBID EXTRA FIELDS AND USE STRICT
SCALARS: a client cannot send an actor, a status, a timestamp, an attribution, a card text, a
field state or an approval -- the server derives every one. No request model has a free-text
field Butler would store as memory; the only human prose accepted is a review answer, a
not-applicable reason and a completion reason, which are the person's own words.

RESPONSES ENUMERATE THEIR FIELDS. Counts are integers where declared; anything financial would
be a decimal string (none is returned here). Card text is rendered by the server from reviewed
templates -- the client displays it as plain text and never as markup.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app.schemas.workspace import Identity


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- preferences ---------------------------------------------------------------------------


class PreferenceIn(_In):
    timezone: StrictStr = Field(min_length=1, max_length=64)
    quiet_start: StrictStr = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    quiet_end: StrictStr = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    digest_limit: StrictInt = Field(ge=1, le=10)
    expected_version: StrictInt = Field(ge=0)


class Preference(BaseModel):
    state: Literal["NOT_SET", "SET"]
    version: int
    timezone: str | None
    quiet_start: str
    quiet_end: str
    digest_limit: int
    recorded_at: datetime | None


# --- subscriptions -------------------------------------------------------------------------


class FollowIn(_In):
    subject_kind: Literal["COMPANY", "TOPIC"]
    #: Exact ISIN only. A ticker or name is refused (ISIN_REQUIRED): resolving one is a guess.
    isin: StrictStr | None = Field(default=None, min_length=12, max_length=12)
    topic: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    topic_aliases: list[StrictStr] = Field(default_factory=list, max_length=20)
    #: NULL unless the person explicitly chose a checkpoint. 30 is offered, never applied.
    checkpoint_days: StrictInt | None = Field(default=None, ge=1, le=366)


class SubscriptionEventIn(_In):
    action: Literal["PAUSE", "RESUME", "UNFOLLOW", "SET_CHECKPOINT"]
    expected_version: StrictInt = Field(ge=1)
    checkpoint_days: StrictInt | None = Field(default=None, ge=1, le=366)


class SubscriptionHistory(BaseModel):
    version: int
    action: str
    checkpoint_days: int | None
    recorded_at: datetime


class Checkpoint(BaseModel):
    state: Literal["NOT_SET", "TIMEZONE_REQUIRED", "SCHEDULED", "PAUSED", "ENDED"]
    every_days: int | None
    next_due_on: date | None
    offered_days: int | None


class SubscriptionCoverage(BaseModel):
    saved_workspace_records: str
    external_sources: str
    topic_matching: str | None


class Subscription(BaseModel):
    id: str
    subject_kind: str
    isin: str | None
    identity: Identity | None
    topic: str | None
    topic_aliases: list[str]
    state: Literal["ACTIVE", "PAUSED", "ENDED"]
    version: int
    checkpoint: Checkpoint
    recorded_at: datetime
    history: list[SubscriptionHistory]
    coverage: SubscriptionCoverage


# --- memory --------------------------------------------------------------------------------


class RememberIn(_In):
    ref_kind: Literal["NOTE", "ANSWER", "THESIS", "CHAT_MESSAGE"]
    ref_id: StrictStr = Field(min_length=1, max_length=64)


class MemoryEventIn(_In):
    action: Literal["FORGET", "SUPERSEDE"]
    superseded_by: StrictStr | None = None


class MemoryRecord(BaseModel):
    available: bool
    title: str | None
    preview: str | None
    recorded_at: datetime | None
    is_latest_version: bool | None


class Memory(BaseModel):
    id: str
    isin: str
    ref_kind: str
    ref_id: str
    #: Read from the referenced record: a user belief, hypothesis, statement, decision,
    #: question or answer; accepted assistant text; or an assistant suggestion.
    attribution: str
    status: Literal["ACTIVE", "FORGOTTEN", "SUPERSEDED"]
    superseded_by: str | None
    recorded_at: datetime
    ended_at: datetime | None
    record: MemoryRecord


# --- inbox and cards -----------------------------------------------------------------------


class CardStatement(BaseModel):
    text: str
    basis: str


class CardPassage(BaseModel):
    citation_id: str
    text: str
    truncated: bool
    author_kind: str
    trust: str
    label: str
    recorded_at: str


class CardCitation(BaseModel):
    id: str
    kind: str
    record_id: str
    label: str
    source_type: str | None = None
    published_on: str | None
    retrieved_at: str | None
    recorded_at: str | None
    content_sha256: str | None
    #: https only, resolved from an authorised stored source. Never model-supplied.
    href: str | None


class CatalogueText(BaseModel):
    id: str
    version: str
    text: str


class ItemStatus(BaseModel):
    version: int
    seen: bool
    snoozed_until: datetime | None
    dismissed: bool
    resolved: bool


class Card(BaseModel):
    id: str
    kind: str
    template_id: str
    template_version: str
    rule_version: str
    priority: str
    isin: str
    identity: Identity
    subscription_id: str | None
    recorded_at: str
    deliver_after: str
    supersedes_item_id: str | None
    superseded_by_item_id: str | None
    status: ItemStatus
    coverage: dict[str, str]
    model_involved: bool
    state: Literal["ACTIVE", "WITHDRAWN"]
    withdrawn_reason: str | None
    title: str
    statements: list[CardStatement]
    passages: list[CardPassage]
    question: CatalogueText | None
    lesson: CatalogueText | None
    citations: list[CardCitation]


class WorkerStatus(BaseModel):
    last_success_at: datetime | None
    last_run_at: datetime | None
    last_outcome: str
    pending_events: int
    oldest_pending_at: datetime | None
    lag_seconds: int
    delivery: str
    model_execution: str


class InboxPage(BaseModel):
    items: list[Card]
    next_cursor: str | None
    new_count: int
    worker: WorkerStatus
    preference: Preference


class InboxEventIn(_In):
    action: Literal["SEEN", "SNOOZE", "DISMISS", "RESOLVE", "RESTORE"]
    expected_version: StrictInt = Field(ge=0)
    snooze_until: datetime | None = None


class InboxEventOut(BaseModel):
    item_id: str
    status: ItemStatus
    replayed: bool


# --- reviews -------------------------------------------------------------------------------


class EvidenceRefIn(_In):
    source_id: StrictStr | None = None
    note_id: StrictStr | None = None


class ReviewStartIn(_In):
    isin: StrictStr = Field(min_length=12, max_length=12)
    decision_kind: Literal["RESEARCH_CHECKPOINT", "THESIS_CHANGE", "POSITION_DECISION"]
    trigger_item_id: StrictStr | None = None
    supersedes_review_id: StrictStr | None = None


class ReviewResponseIn(_In):
    question_id: StrictStr = Field(pattern=r"^[A-Z][A-Z_]{2,63}$")
    answer: StrictStr = Field(default="", max_length=65536)
    #: The person's explicit statement that they stand behind the answer without further
    #: evidence. Never inferred.
    attested: StrictBool = False
    not_applicable_reason: StrictStr | None = Field(default=None, max_length=8192)
    milestone_on: date | None = None
    evidence: list[EvidenceRefIn] = Field(default_factory=list, max_length=20)
    #: Required key; null means "first response to this question".
    expected_latest_response_id: StrictStr | None = Field(...)


class ReviewCompleteIn(_In):
    choice: Literal["CONTINUE", "PAUSE", "REVISE", "MORE_INFORMATION"]
    reason: StrictStr = Field(default="", max_length=8192)
    #: Every unresolved check, named back explicitly.
    acknowledged_unresolved: list[StrictStr] = Field(default_factory=list, max_length=7)


class ReviewEvidence(BaseModel):
    kind: str
    id: str
    available: bool
    reason: str | None
    label: str | None
    published_on: date | None


class ReviewResponse(BaseModel):
    id: str
    answer: str
    attested: bool
    not_applicable_reason: str | None
    milestone_on: date | None
    recorded_at: datetime
    actor_uid: str
    previous_id: str | None
    revision_ids: list[str]


class CodeCheck(BaseModel):
    id: str
    result: Literal["PASSED", "FAILED", "NOT_RUN"]


class ReviewField(BaseModel):
    question_id: str
    question_text: str
    question_version: str
    #: True for a BT 14 personalised question; the client shows "Why this question?".
    personalised: bool = False
    state: Literal["MISSING", "ANSWER_RECORDED", "EVIDENCE_ATTACHED", "CHECKED_BY_CODE",
                   "USER_ATTESTED", "UNKNOWN", "NOT_APPLICABLE_WITH_REASON"]
    resolved: bool
    response: ReviewResponse | None
    evidence: list[ReviewEvidence]
    code_check: CodeCheck | None
    follow_up: str | None


class ReviewNext(BaseModel):
    question_id: str
    follow_up: str | None
    follow_up_text: str | None


class ReviewDifference(BaseModel):
    code: str
    question_id: str | None
    record_id: str
    from_version: int | None = None
    to_version: int | None = None


class ReviewCompletion(BaseModel):
    choice: str
    reason: str
    unresolved: list[str]
    recorded_at: datetime
    actor_uid: str
    statement: str


class PinnedThesis(BaseModel):
    id: str
    version: int
    version_on: date
    intended_holding_period: str
    argument_excerpt: str


class Review(BaseModel):
    id: str
    isin: str
    identity: Identity | None
    decision_kind: str
    #: Derived. There is no APPROVED state and there never will be.
    status: Literal["DRAFT", "AWAITING_INPUT", "DOCUMENTED", "SUPERSEDED"]
    checklist: dict[str, str]
    pinned_thesis: PinnedThesis | None
    trigger_item_id: str | None
    supersedes_review_id: str | None
    superseded_by_review_id: str | None
    fields: list[ReviewField]
    next: ReviewNext | None
    unresolved: list[str]
    differences: list[ReviewDifference]
    stale: bool
    completion: ReviewCompletion | None
    recorded_at: datetime
    actor_uid: str


class ReviewSummary(BaseModel):
    id: str
    isin: str
    identity: Identity | None
    decision_kind: str
    status: str
    unresolved: list[str]
    recorded_at: datetime
    stale: bool


# --- sources -------------------------------------------------------------------------------


class RetractionIn(_In):
    source_id: StrictStr = Field(min_length=1, max_length=64)
    reason_code: Literal["RETRACTED_BY_ISSUER", "CORRECTED_BY_ISSUER", "WRONG_DOCUMENT",
                         "DUPLICATE_COPY", "RIGHTS_WITHDRAWN"]
    replacement_source_id: StrictStr | None = None


class Retraction(BaseModel):
    id: str
    source_id: str
    reason_code: str
    replacement_source_id: str | None
    recorded_at: datetime


# --- context (READ-class) ------------------------------------------------------------------


class ContextIn(_In):
    isin: StrictStr = Field(min_length=12, max_length=12)
    intent: Literal["review_evidence", "new_material", "checkpoint", "decision_review"]
    as_of: datetime | None = None


class ContextOut(BaseModel):
    packet: dict[str, Any]
    digest: str
    complete: bool
    expires_at: datetime
    versions: dict[str, str]
    #: No model task exists yet (BE 20/BT 08). Stated, never implied.
    task_id: None = None
    model_execution: Literal["NOT_CONNECTED"] = "NOT_CONNECTED"
    size_chars: int


class Catalogue(BaseModel):
    catalogue_version: str
    rule_version: str
    checklist_version: str
    questions: list[dict[str, Any]]
    lessons: list[dict[str, Any]]
    checklists: dict[str, list[str]]
    defaults: dict[str, Any]


# --- BT 08: browser model selection ---------------------------------------------------------


class SelectionIn(_In):
    """An explicit Review/Ask click. Seals a packet as a task; calls no model."""

    isin: StrictStr = Field(min_length=12, max_length=12)
    intent: Literal["review_evidence", "new_material", "checkpoint", "decision_review"]
    agent_id: StrictStr = Field(min_length=36, max_length=36)
    agent_version: StrictInt = Field(ge=1)
    #: A historical due item or an earlier selection this review follows up; never replayed.
    parent_task_id: StrictStr | None = None


class SelectionOut(BaseModel):
    task_id: str
    isin: str
    intent: str | None
    created_at: datetime
    context_cutoff: datetime
    task_state: str
    #: NO_RESULT | ASSIGNED | RUNNING | VALIDATED | ABSTAINED | STALE | WITHDRAWN |
    #: REJECTED_BY_VALIDATION | VALIDATION_UNAVAILABLE
    state: str
    cards: list[dict[str, Any]]
    reason_codes: list[str]
    coverage: dict[str, Any] | None
    abstain_code: str | None
    error_code: str | None
    sample: bool
    execution: Literal["NONE", "CLIENT_REPORTED_MODEL", "STORED_SAMPLE"]
    attempts: int
    latest_attempt: dict[str, Any] | None
    versions: dict[str, str]
    agent: dict[str, Any]


# --- BT 09: teaching preferences and feedback ----------------------------------------------


class TeachingIn(_In):
    detail: Literal["CONCISE", "DETAILED"]
    lesson_cadence: Literal["EVERY_REVIEW", "WEEKLY", "OFF"]
    follow_ups: Literal["ASK", "SKIP"]
    expected_version: StrictInt = Field(ge=0)


class Teaching(BaseModel):
    state: Literal["NOT_SET", "SET"]
    version: int
    detail: str
    lesson_cadence: str
    follow_ups: str
    recorded_at: datetime | None


class FeedbackIn(_In):
    subject_kind: Literal["QUESTION", "LESSON", "CARD"]
    subject_id: StrictStr = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    subject_version: StrictStr = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")
    inbox_item_id: StrictStr | None = None
    rating: Literal["USEFUL", "NOT_USEFUL"]


class Feedback(BaseModel):
    id: str
    subject_kind: str
    subject_id: str
    subject_version: str
    inbox_item_id: str | None
    rating: str
    recorded_at: datetime


# --- BT 10: operational truth ---------------------------------------------------------------


class ButlerStatus(BaseModel):
    worker: WorkerStatus
    switches: dict[str, bool]
    selections: dict[str, Any]
    coverage: dict[str, Any]
    limits: dict[str, Any]
