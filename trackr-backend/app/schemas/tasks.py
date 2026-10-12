"""DTOs for ``app/routers/tasks.py``: agents, tasks, attempts, results, review and chat
(BE 19-25, BLUEPRINT_BACKEND.md section 7.4).

CREDENTIAL-FREE BY CONSTRUCTION. ``AgentCreateIn`` accepts exactly eight fields and forbids
everything else, so ``key``, ``api_key``, ``token``, ``secret``, ``authorization``, ``headers``,
``credential`` or any nested configuration object is an unknown property and a 422. FastAPI's
validation error lists the offending LOCATION; ``app/main.py``'s handler strips input values
from those errors so a pasted key never echoes back into a response or a log.

SERVER-DERIVED FIELDS ARE NOT INPUTS. A client cannot send ``state``, ``digest``,
``context_cutoff``, ``sample``, ``created_by``, ``reviewer_uid``, ``guardrail_status``,
``finished_at`` or ``duration_ms``; every request model forbids extra keys.

THE PROPOSAL IS CLOSED. Every level of ``Proposal`` forbids extra keys, headings are the
AllowedHeading enum, and a value reference carries exactly one of ``context_id``/``query_ref``.
A proposal that fails this shape is 422 and is not stored; one that passes the shape but breaks
a house rule is stored BLOCKED (``app/research/guardrails.py``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.schemas.workspace import NoteLinkIn


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


Provider = Literal["ANTHROPIC", "OPENAI_COMPATIBLE"]
Purpose = Literal["RESEARCH", "ANSWER_REVIEW", "CHAT", "INLINE", "MONITOR"]
TaskState = Literal["assigned", "running", "review", "done", "rejected"]
Heading = Literal[
    "FACTS", "INTERPRETATION", "UNCERTAINTIES", "NEXT_QUESTIONS", "ASSUMPTIONS",
    "COUNTERARGUMENTS", "MATERIAL_CHANGE", "WHY_IT_MATTERS", "ORIGINAL_COMMITMENT",
    "SUPPORTED_OR_CHALLENGED",
]
Classification = Literal["FILED", "INTERPRETATION", "UNKNOWN", "CONFLICT"]
FailureCode = Literal[
    "PROVIDER_UNREACHABLE", "AUTH_FAILED", "RATE_LIMITED", "CLIENT_CLOSED", "INVALID_OUTPUT",
]

_DECIMAL = r"^-?[0-9]{1,14}(\.[0-9]{1,6})?$"
_HEX64 = r"^[a-f0-9]{64}$"


# --- agents (BE 19) --------------------------------------------------------------------------


class AgentCreateIn(_In):
    name: StrictStr = Field(min_length=1, max_length=200)
    provider: Provider
    model: StrictStr = Field(min_length=1, max_length=200)
    base_url: StrictStr = Field(min_length=9, max_length=512)
    role_description: StrictStr = Field(min_length=1, max_length=4000)
    skill_id: StrictStr = Field(min_length=36, max_length=36)
    skill_version: StrictInt = Field(ge=1)
    enabled: StrictBool


class AgentVersionIn(AgentCreateIn):
    expected_version: StrictInt = Field(ge=1)


class Agent(BaseModel):
    """Declared business fields only. No readiness, connection, key or status field: the
    server cannot know whether a key exists or works, and does not pretend to."""

    id: str
    version: int
    name: str
    provider: Provider
    model: str
    base_url: str
    role_description: str
    skill_id: str
    skill_version: int
    enabled: bool
    actor_uid: str
    recorded_at: datetime


# --- tasks and context (BE 20) --------------------------------------------------------------


class AssumptionIn(_In):
    name: StrictStr = Field(min_length=1, max_length=120)
    value: StrictStr = Field(pattern=_DECIMAL)
    unit: StrictStr = Field(min_length=1, max_length=32)
    reason: StrictStr = Field(min_length=1, max_length=2000)


class CalculationSpecIn(_In):
    formula_id: StrictStr = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    version: StrictStr = Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")
    fact_ids: list[StrictStr] = Field(max_length=50)
    assumptions: list[AssumptionIn] = Field(default_factory=list, max_length=20)


class PositionQueryIn(_In):
    as_of: date
    advised_only: StrictBool


class ContextSelectionIn(_In):
    note_ids: list[StrictStr] = Field(default_factory=list, max_length=100)
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=50)
    fact_ids: list[StrictStr] = Field(default_factory=list, max_length=100)
    evaluation_ids: list[StrictStr] = Field(default_factory=list, max_length=20)
    calculation_specs: list[CalculationSpecIn] = Field(default_factory=list, max_length=10)
    include_position_query: PositionQueryIn | None = None
    expected_thesis_id: StrictStr | None = None


class TaskCreateIn(_In):
    isin: StrictStr = Field(min_length=12, max_length=12)
    #: CHAT is created only by the chat turn route and MONITOR only by the scheduler.
    purpose: Literal["RESEARCH", "INLINE", "ANSWER_REVIEW"]
    title: StrictStr = Field(min_length=1, max_length=300)
    brief: StrictStr = Field(min_length=1)
    agent_id: StrictStr = Field(min_length=36, max_length=36)
    agent_version: StrictInt = Field(ge=1)
    skill_id: StrictStr = Field(min_length=36, max_length=36)
    skill_version: StrictInt = Field(ge=1)
    thesis_id: StrictStr | None = None
    answer_id: StrictStr | None = None
    parent_task_id: StrictStr | None = None
    context_selection: ContextSelectionIn


class Attempt(BaseModel):
    id: str
    task_id: str
    attempt_no: int
    mode: Literal["BROWSER_MODEL", "STORED_SAMPLE"]
    sample_ref: str | None
    started_at: datetime
    lease_until: datetime
    #: Server-side projection: the lease has passed and no terminal result exists yet.
    lease_expired: bool


class Citation(BaseModel):
    context_id: str
    claim_ref: str


class Result(BaseModel):
    id: str
    task_id: str
    attempt_id: str
    outcome: Literal["PROPOSED", "FAILED", "CANCELLED", "EXPIRED"]
    guardrail_status: Literal["PASS", "BLOCKED", "NOT_APPLICABLE"]
    guardrail_reasons: list[str]
    error_code: str | None
    finished_at: datetime
    duration_ms: int
    citations: list[Citation]
    sample: bool
    invocation_evidence: Literal["CLIENT_REPORTED", "STORED_SAMPLE"]
    #: The proposal exactly as submitted (untrusted), for inspection.
    proposal: dict[str, Any] | None
    #: The proposal as a person reads it: values filled from pinned rows by code.
    rendered: dict[str, Any] | None


class Review(BaseModel):
    id: str
    task_id: str
    result_id: str
    decision: Literal["ACCEPT", "REJECT"]
    reason: str
    accepted_note_id: str | None
    reviewer_uid: str
    reviewed_at: datetime


class Task(BaseModel):
    id: str
    isin: str
    purpose: Purpose
    title: str
    brief: str
    skill_id: str
    skill_version: int
    agent_id: str
    agent_version: int
    thesis_id: str | None
    answer_id: str | None
    chat_thread_id: str | None
    parent_task_id: str | None
    created_by: str | None
    created_at: datetime
    context_cutoff: datetime
    context_digest: str
    house_rules_version: str
    method_key: str | None
    state: TaskState
    latest_attempt: Attempt | None
    result: Result | None
    review: Review | None
    sample: bool
    error_code: str | None
    attempts: int


class ContextPacket(BaseModel):
    task_id: str
    context_digest: str
    house_rules_version: str
    skill: dict[str, Any]
    agent: dict[str, Any]
    purpose: Purpose
    brief: str
    system_instructions: str
    ordered_context: list[dict[str, Any]]
    prior_chat_turns: list[dict[str, Any]]
    calculation_results: list[dict[str, Any]]
    position_query: dict[str, Any] | None
    allowed_tools: list[str]
    exclusions: list[dict[str, str]]
    disclosure: dict[str, Any]
    precondition: dict[str, Any]
    butler_packet: dict[str, Any] | None = None


# --- attempts and results (BE 21) ------------------------------------------------------------


class AttemptIn(_In):
    mode: Literal["BROWSER_MODEL", "STORED_SAMPLE"]
    sample_ref: StrictStr | None = Field(default=None, pattern=r"^[a-z0-9_]{3,64}$")


class AttemptOut(BaseModel):
    attempt: Attempt
    context_digest: str
    #: True when this response replays an earlier start. The browser must NOT treat a replay
    #: as permission to send a second provider request.
    replayed: bool


class ValueRefIn(_In):
    context_id: StrictStr | None = None
    query_ref: StrictStr | None = Field(default=None, max_length=64)
    field: StrictStr = Field(pattern=r"^[a-z_]{2,40}$")

    @model_validator(mode="after")
    def _exactly_one(self) -> ValueRefIn:
        if (self.context_id is None) == (self.query_ref is None):
            raise ValueError("a value reference needs exactly one of context_id or query_ref")
        return self


class QuoteIn(_In):
    context_id: StrictStr
    text: StrictStr = Field(min_length=1, max_length=2000)


class CitedParagraphIn(_In):
    text_template: StrictStr = Field(min_length=1, max_length=4000)
    classification: Classification
    context_ids: list[StrictStr] = Field(default_factory=list, max_length=16)
    value_refs: list[ValueRefIn] = Field(default_factory=list, max_length=16)
    exact_quotes: list[QuoteIn] = Field(default_factory=list, max_length=8)


class SectionIn(_In):
    heading: Heading
    paragraphs: list[CitedParagraphIn] = Field(min_length=1, max_length=24)


class NoteProposalIn(_In):
    kind: Literal["OBSERVATION", "INTERPRETATION"]
    title: StrictStr = Field(min_length=1, max_length=300)
    body_template: StrictStr = Field(min_length=1, max_length=65536)
    source_context_ids: list[StrictStr] = Field(default_factory=list, max_length=16)


class AnswerReviewIn(_In):
    ok: list[CitedParagraphIn] = Field(default_factory=list, max_length=16)
    gap: list[CitedParagraphIn] = Field(default_factory=list, max_length=16)


class ProposalIn(_In):
    schema_version: Literal["1"]
    summary_template: StrictStr = Field(min_length=1, max_length=2000)
    sections: list[SectionIn] = Field(default_factory=list, max_length=16)
    note_proposal: NoteProposalIn | None = None
    answer_review: AnswerReviewIn | None = None


class ResultSubmissionIn(_In):
    context_digest: StrictStr = Field(pattern=_HEX64)
    #: Validated per the pinned method: ProposalIn for research methods, butler.proposal.v1
    #: for the Butler selector.
    proposal: dict[str, Any]


class FailureIn(_In):
    code: FailureCode


class CancellationIn(_In):
    pass


# --- review (BE 22) ----------------------------------------------------------------------------


class AcceptedNoteIn(_In):
    kind: Literal["OBSERVATION", "INTERPRETATION"]
    title: StrictStr = Field(min_length=1)
    body: StrictStr = Field(min_length=1)
    written_on: date
    primary_source_id: StrictStr | None = None
    links: list[NoteLinkIn] = Field(default_factory=list, max_length=20)


class AcceptanceIn(_In):
    result_id: StrictStr = Field(min_length=36, max_length=36)
    reason: StrictStr = Field(min_length=1)
    note: AcceptedNoteIn


class RejectionIn(_In):
    result_id: StrictStr = Field(min_length=36, max_length=36)
    reason: StrictStr = Field(min_length=1)


class ReviewOut(BaseModel):
    review: Review
    note: dict[str, Any] | None
    task: Task


# --- chat (BE 24) ------------------------------------------------------------------------------


class ThreadCreateIn(_In):
    title: StrictStr = Field(min_length=1, max_length=300)


class Thread(BaseModel):
    id: str
    isin: str
    title: str
    actor_uid: str
    created_at: datetime
    messages: int


class ChatMessage(BaseModel):
    id: str
    thread_id: str
    role: Literal["HUMAN", "ASSISTANT", "REFUSAL"]
    #: HUMAN and REFUSAL text. ASSISTANT rows store no body; see ``rendered``.
    body: str
    actor_uid: str | None
    result_id: str | None
    task_id: str | None
    recorded_at: datetime
    #: For ASSISTANT turns: the linked result rendered from its template and pinned
    #: references, with its guardrail status and sample attribution.
    rendered: dict[str, Any] | None
    guardrail_status: str | None
    review_decision: str | None
    sample: bool


class TurnIn(_In):
    text: StrictStr = Field(min_length=1)
    agent_id: StrictStr = Field(min_length=36, max_length=36)
    agent_version: StrictInt = Field(ge=1)
    skill_id: StrictStr = Field(min_length=36, max_length=36)
    skill_version: StrictInt = Field(ge=1)
    context_selection: ContextSelectionIn


class Notice(BaseModel):
    code: str
    message: str


class TurnOut(BaseModel):
    message: ChatMessage
    task: Task | None
    refusal: Notice | None


class HomeReview(BaseModel):
    task_id: str
    isin: str
    title: str
    purpose: Purpose
    result_id: str
    guardrail_status: str
    sample: bool
    finished_at: datetime
    continuation: str
