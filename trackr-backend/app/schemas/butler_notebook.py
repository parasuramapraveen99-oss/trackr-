"""DTOs for the Butler notebook routes (BT 12/15) and the reflection proposal (BT 14).

Same conventions as ``app/schemas/butler.py``: requests forbid extra fields and use strict
scalars. A client cannot send a status, a count, an origin, a consent version, an episode or a
pattern -- the server derives every one. The only prose accepted is the person's own: a
self-description and an optional note on a control. Nothing here is a report DTO, and
``app/schemas/reports.py`` must never import it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

SourceCategory = Literal["RESEARCH_NOTES", "REVIEW_RESPONSES", "BUTLER_CONVERSATION",
                         "DECISION_RECEIPTS", "PORTFOLIO_LINKED_DECISIONS"]
_CODE = r"^[A-Z][A-Z0-9_]{2,63}$"
_UUID = r"^[0-9a-fA-F-]{36}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentIn(_In):
    action: Literal["OPT_IN", "UPDATE_SCOPE", "PAUSE", "RESUME", "REVOKE"]
    expected_version: StrictInt = Field(ge=0)
    #: Required for OPT_IN / UPDATE_SCOPE: the disclosure version the UI actually displayed.
    disclosure_version: StrictStr | None = Field(default=None, max_length=40)
    sources: list[SourceCategory] = Field(default_factory=list, max_length=5)
    #: Explicit retrospective start. Omitted -> from the moment of opt-in; nothing earlier.
    include_from: date | None = None
    #: The separate opt-in for the bounded hosted daily reflection.
    reflection_enabled: StrictBool = False


class ConsentHistoryEntry(BaseModel):
    version: int
    action: str
    sources: list[str]
    include_from: date | None
    reflection_enabled: bool
    recorded_at: datetime


class Consent(BaseModel):
    state: Literal["NOT_SET", "ACTIVE", "PAUSED", "REVOKED"]
    version: int
    sources: list[str]
    include_from: date | None
    effective_include_from: date | None
    reflection_enabled: bool
    disclosure_version: str | None
    opted_in_at: datetime | None
    recorded_at: datetime | None
    history: list[ConsentHistoryEntry]


class SelfDescriptionIn(_In):
    statement: StrictStr = Field(min_length=1, max_length=1000)
    isin: StrictStr | None = Field(default=None, min_length=12, max_length=12)
    previous_id: StrictStr | None = Field(default=None, pattern=_UUID)


class SelfDescription(BaseModel):
    id: str
    isin: str | None
    statement: str
    previous_id: str | None
    recorded_at: datetime
    origin: Literal["USER"]
    retracted: bool = False
    superseded: bool = False


class ControlIn(_In):
    action: Literal["DISPUTE", "WITHDRAW_DISPUTE", "DISABLE", "ENABLE", "DELETE",
                    "CORRECT_EPISODE", "EXCLUDE_EPISODE", "RETRACT_SELF_DESCRIPTION"]
    target_id: StrictStr = Field(pattern=_UUID)
    correction_code: Literal["WRONG_RECORD", "NOT_COMPARABLE", "CIRCUMSTANCES_CHANGED",
                             "OUTSIDE_WORK_NOT_RECORDED", "LONG_HORIZON_INTENDED",
                             "OTHER"] | None = None
    #: The person's own words, optional. Never read as evidence about them.
    note: StrictStr | None = Field(default=None, min_length=1, max_length=1000)


class ControlOut(BaseModel):
    event_id: str
    action: str


class DeleteAllIn(_In):
    #: The UI must show what is deleted and what is kept; the client echoes that it did.
    confirm: Literal["DELETE_MY_NOTEBOOK"]


class DeleteAllOut(BaseModel):
    fence_id: str
    purge: dict[str, Any]
    kept: list[str]
    note: str


class Episode(BaseModel):
    id: str
    catalogue_id: str
    polarity: Literal["SUPPORTS", "COUNTERS", "UNDETERMINED"]
    observation_code: str
    source_category: str
    comparability_key: str
    isin: str
    occurred_on: date
    knowable_at: datetime
    anchor_kind: str
    anchor_id: str
    review_id: str | None
    thesis_id: str | None
    prior_thesis_id: str | None
    response_id: str | None
    evaluation_id: str | None
    limitations: list[str]
    builder_version: str
    consent_version: int
    recorded_at: datetime
    corrected: bool
    correction_code: str | None
    origin: Literal["CODE"]


class Pattern(BaseModel):
    pattern_id: str
    catalogue_id: str
    catalogue_version: str | None
    text: str
    eligible: str
    comparison: str
    positive: bool
    scope: str
    revision: int
    revision_id: str
    origin: Literal["CODE", "MODEL"]
    stored_status: str
    status: str
    status_reason: str
    supporting: list[Episode]
    counterexamples: list[Episode]
    counts: dict[str, Any]
    window: dict[str, date]
    alternatives: list[dict[str, str]]
    questions: list[dict[str, str]]
    limitations: list[dict[str, str]]
    user_state: dict[str, bool]
    user_notes: list[dict[str, Any]]
    consent_version: int
    reflection_job_id: str | None
    rule_version: str
    last_evaluated_at: datetime
    influence: list[dict[str, Any]]
    used_for_coaching: bool


class Notebook(BaseModel):
    consent: Consent
    profiling_active: bool
    self_descriptions: list[SelfDescription]
    patterns: list[Pattern]
    episodes: list[Episode]
    episode_counts: dict[str, int]
    retention: dict[str, str]
    generated_at: datetime
    reflection: dict[str, Any] | None = None


class WhyQuestion(BaseModel):
    """The "Why this question?" answer: the exact revision and episodes that made a
    personalised question applicable, or a plain statement that it was not personalised."""

    review_id: str
    question_id: str
    personalised: bool
    pattern: Pattern | None
    statement: str


# --- BT 14 reflection proposal -- ids only -----------------------------------------------


class ReflectionPatternProposal(_In):
    catalogue_id: StrictStr = Field(pattern=_CODE)
    supporting_episode_ids: list[StrictStr] = Field(default_factory=list, max_length=20)
    counter_episode_ids: list[StrictStr] = Field(default_factory=list, max_length=20)
    alternative_ids: list[StrictStr] = Field(default_factory=list, max_length=6)
    question_ids: list[StrictStr] = Field(default_factory=list, max_length=2)
    retire: StrictBool = False


class ReflectionProposal(_In):
    """butler.reflection_proposal.v1 -- what the reflection model may return. Identifiers and
    one abstention code. No prose field exists: a diagnosis, motive or personality label has
    nowhere to go, and extra fields are refused whole."""

    schema_version: Literal["butler.reflection_proposal.v1"]
    packet_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    patterns: list[ReflectionPatternProposal] = Field(default_factory=list, max_length=6)
    abstain_code: Literal["INSUFFICIENT_EVIDENCE", "NO_COMPARABLE_EPISODES",
                          "COUNTEREXAMPLES_DOMINATE", "OUT_OF_SCOPE"] | None = None
