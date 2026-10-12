"""DTOs for ``app/routers/workspace.py`` (BLUEPRINT_BACKEND.md sections 7.1-7.2).

REQUEST MODELS FORBID EXTRA FIELDS AND USE STRICT SCALARS. A client cannot send an actor,
a status, a timestamp, a sequence, ``done``, ``ritual_complete`` or ``reviewer_uid`` -- the
server derives all of them, and an unknown key is a 422 rather than silently ignored. Text is
``StrictStr`` (a JSON number is not a title) and flags are ``StrictBool`` (``1`` is not
``true``). Dates stay ordinary ``date`` fields so ISO strings from JSON parse.

RESPONSE MODELS ENUMERATE THEIR FIELDS. Internal markers (``creation_xid``, ``dedupe_key``,
``seq``) never appear. Identifiers are UUID strings; dates ISO; numeric values strings.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

T = TypeVar("T")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None


class Notice(BaseModel):
    code: str
    message: str


# --- identity / companies ------------------------------------------------------------------


class Identity(BaseModel):
    isin: str
    canonical_isin: str
    symbol: str | None
    name: str | None


class CompanyListItem(Identity):
    ritual_complete: bool


class CompanyLinkIn(_In):
    isin: StrictStr = Field(min_length=12, max_length=12)


# --- questions / answers -------------------------------------------------------------------


class RitualQuestionIn(_In):
    text: StrictStr = Field(min_length=1)
    #: Explicit, never inferred: every question states whether it blocks a thesis.
    blocking: StrictBool


class RitualIn(_In):
    questions: list[RitualQuestionIn] = Field(min_length=1, max_length=20)
    written_on: date


class QuestionIn(_In):
    text: StrictStr = Field(min_length=1)
    blocking: StrictBool
    written_on: date


class AnswerIn(_In):
    answer: StrictStr = Field(min_length=1)
    answered_on: date
    #: Required key; null means "this is the first answer".
    expected_latest_answer_id: StrictStr | None = Field(...)


class Answer(BaseModel):
    id: str
    question_id: str
    answer: str
    answered_on: date
    recorded_at: datetime
    previous_id: str | None
    actor_uid: str


class AnswerReview(BaseModel):
    """An ANSWER_REVIEW proposal about EXACTLY the latest answer. Null when none exists."""

    answer_id: str
    task_id: str
    result_id: str
    ok: list[str]
    gap: list[str]
    decision: Literal["ACCEPT", "REJECT"] | None
    sample: bool


class Question(BaseModel):
    id: str
    isin: str
    note_id: str
    ritual_id: str | None
    blocking: bool
    recorded_at: datetime
    text: str
    written_on: date
    status: Literal["OPEN", "ANSWERED"]
    answer_ids: list[str]
    latest_answer: Answer | None
    review: AnswerReview | None


class RitualOut(BaseModel):
    ritual_id: str
    questions: list[Question]
    state: Literal["READY"]


# --- sources (read shape shared with research.py) ------------------------------------------


class SourceContentState(BaseModel):
    state: Literal["METADATA_ONLY", "EXTRACTED", "UNAVAILABLE"]
    unavailable_reason: str | None
    extractor_id: str | None
    extractor_version: str | None
    normalization: str | None
    text_sha256: str | None
    page_count: int | None


class Source(BaseModel):
    id: str
    isin: str
    source: str
    source_type: str
    source_url: str
    publication_date: date
    period: str
    retrieved_at: datetime
    document_reference: str
    document_id: str | None
    content_sha256: str | None
    recorded_at: datetime
    content: SourceContentState


# --- notes ---------------------------------------------------------------------------------

Relation = Literal["READS", "SUPPORTS", "CONTRADICTS", "CITES"]


class NoteLinkIn(_In):
    relation: Relation
    to_note_id: StrictStr | None = None
    source_id: StrictStr | None = None
    thesis_id: StrictStr | None = None


class NoteCreateIn(_In):
    #: Validated server-side: OBSERVATION | INTERPRETATION | THESIS_NOTE | LITERATURE |
    #: DECISION. QUESTION is refused (422 on create, 409 on revision) -- it has its own route.
    kind: StrictStr
    title: StrictStr
    body: StrictStr = ""
    written_on: date
    primary_source_id: StrictStr | None = None
    links: list[NoteLinkIn] = Field(default_factory=list, max_length=20)


class NoteRevisionIn(NoteCreateIn):
    expected_latest_id: StrictStr


class NoteLink(BaseModel):
    id: str
    from_note_id: str
    to_note_id: str | None
    source_id: str | None
    thesis_id: str | None
    relation: Relation
    recorded_at: datetime
    actor_uid: str
    direction: Literal["OUT", "IN"]


class AcceptedFrom(BaseModel):
    task_id: str
    result_id: str
    sample: bool


class Note(BaseModel):
    id: str
    isin: str
    kind: str
    title: str
    body: str
    written_on: date
    recorded_at: datetime
    actor_uid: str
    authorship: Literal["HUMAN", "ACCEPTED_AGENT"]
    primary_source_id: str | None
    previous_id: str | None
    source: Source | None
    links: list[NoteLink]
    revision_ids: list[str]
    latest_id: str
    is_latest: bool
    accepted_from: AcceptedFrom | None


# --- theses --------------------------------------------------------------------------------


class ThesisIn(_In):
    intent: Literal["SAVE", "RETIRE"]
    expected_version: StrictInt = Field(ge=0)
    formed_on: date
    version_on: date
    argument: StrictStr = ""
    anti_thesis: StrictStr = ""
    pre_mortem: StrictStr = ""
    intended_holding_period: StrictStr = ""
    #: Validated against the closed rule grammar in app/engine/thesis_versions.py; a JSON
    #: number anywhere inside is a 422 INVALID_RULE, never coerced.
    variables: list[Any] = Field(default_factory=list, max_length=50)
    exits: list[Any] = Field(default_factory=list, max_length=50)


class ThesisEventIn(_In):
    kind: Literal["CONVICTION", "REVIEWED"]
    conviction: Literal["HIGH", "MEDIUM", "LOW", "UNDER_REVIEW"] | None = None
    reason: StrictStr = Field(min_length=1)
    occurred_on: date


class OverrideIn(_In):
    evaluation_id: StrictStr
    reason: StrictStr = Field(min_length=1)
    occurred_on: date


class FieldReason(BaseModel):
    field: str
    code: str


class ThesisEvent(BaseModel):
    id: str
    thesis_id: str
    kind: Literal["CONVICTION", "OVERRIDE_EXIT_TRIGGER", "REVIEWED"]
    conviction: str | None
    reason: str
    evaluation_id: str | None
    actor_uid: str
    occurred_on: date
    recorded_at: datetime


class ThesisLink(BaseModel):
    id: str
    from_note_id: str
    thesis_id: str
    relation: Relation
    recorded_at: datetime


class EvaluationSummary(BaseModel):
    id: str
    thesis_id: str
    state: str
    evaluated_at: datetime
    evidence_cutoff: datetime


class Thesis(BaseModel):
    id: str
    isin: str
    version: int
    status: Literal["DRAFT", "ACTIVE", "RETIRED"]
    display_status: Literal["DRAFT", "ACTIVE", "RETIRED", "NEEDS_REVIEW"]
    formed_on: date
    version_on: date
    recorded_at: datetime
    actor_uid: str
    argument: str
    anti_thesis: str
    pre_mortem: str
    intended_holding_period: str
    variables: list[dict[str, str]]
    exits: list[dict[str, str]]
    draft_reasons: list[FieldReason]
    events: list[ThesisEvent]
    links: list[ThesisLink]
    latest_evaluation: EvaluationSummary | None
    is_latest: bool


class ThesisValidation(BaseModel):
    draft_reasons: list[FieldReason]


class ThesisPublishOut(BaseModel):
    thesis: Thesis
    validation: ThesisValidation


# --- company view / home -------------------------------------------------------------------


class CompanyViewLocked(BaseModel):
    """``RITUAL_REQUIRED`` carries identity ONLY -- no note, question text, thesis, trigger,
    position, count or preview. The key set itself is the contract."""

    state: Literal["RITUAL_REQUIRED"]
    identity: Identity


class CompanyViewReady(BaseModel):
    state: Literal["READY"]
    identity: Identity
    #: OPEN questions first, blocking first. Answered questions live at /questions.
    open_questions: list[Question]
    #: The ACTIVE commitment in force now, or null (none, retired, or alias conflict --
    #: a conflict is also reported in ``notices``).
    thesis: Thesis | None
    #: The in-force thesis's latest STORED evaluation receipt. Never computed on read.
    trigger: EvaluationSummary | None
    notes: Page[Note]
    #: Existing engine read on the financial connection; never stored here.
    position: dict[str, Any] | None
    notices: list[Notice]
    as_of: date


CompanyView = Annotated[CompanyViewLocked | CompanyViewReady, Field(discriminator="state")]


class ReturnItem(BaseModel):
    kind: Literal["NOTE", "THESIS", "OVERRIDE", "CONTRADICTION"]
    reason: Literal["MONTH_6", "MONTH_12", "MONTH_24", "CONTRADICTION"]
    isin: str
    original_date: date
    content_ref: str
    note_id: str | None = None
    thesis_id: str | None = None
    event_id: str | None = None
    link_id: str | None = None
    other_note_id: str | None = None
    other_date: date | None = None


class HomeOut(BaseModel):
    as_of: date
    open_questions: Page[Question]
    returns: list[ReturnItem]
    reviews: list[dict[str, Any]]
    notices: list[Notice]
