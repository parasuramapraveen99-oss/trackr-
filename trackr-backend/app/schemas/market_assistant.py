"""/market-assistant -- app/routers/market_assistant.py.

Closed shapes on purpose. No ``investor_id``, no account, no screen ``context``. The only
history is ``history``: the user's own earlier questions in this drawer conversation, each
re-admitted by the server (owner amendment 11 Oct 2026). Unknown fields are rejected
(``extra="forbid"``) rather than silently dropped,
so a client that starts sending portfolio data fails loudly instead of appearing to work.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.market_assistant.admission import MAX_HISTORY, MAX_QUESTION_CHARS


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Inspected locally and never stored. Safe text can reach the LLM with opt-in.
    question: str | None = Field(default=None, min_length=1, max_length=MAX_QUESTION_CHARS)
    #: A suggestion chip: a catalogue ID instead of text.
    topic_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    #: Compatibility field: public research is always on for admitted money questions.
    allow_external: StrictBool = True
    #: Earlier typed questions of this conversation, oldest first, for follow-ups. The
    #: client resets them per drawer session and per portfolio; the server re-screens each
    #: one (``admit_history``), drops any it would refuse, and never logs or stores them.
    history: list[Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]] = Field(
        default_factory=list, max_length=MAX_HISTORY
    )

    @field_validator("allow_external")
    @classmethod
    def _search_is_always_on(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Public search is always on.")
        return value

    @model_validator(mode="after")
    def _exactly_one(self) -> AskIn:
        if (self.question is None) == (self.topic_id is None):
            raise ValueError("send exactly one of question or topic_id")
        return self


class TopicOut(BaseModel):
    topic_id: str
    label: str


class AnswerOut(BaseModel):
    topic_id: str
    title: str
    body: str
    source_label: str
    catalogue_version: str


class EvidenceOut(BaseModel):
    """An exact excerpt of an official page. ``start``/``end`` index the cleaned page text
    identified by ``doc_id``, so the quote always resolves to its source span."""

    publisher: str
    domain: str
    title: str
    url: str
    #: Null when the page carries no date. Never guessed.
    published: date | None
    excerpt: str
    #: The server's own words above the quote ("SEBI states:"). Never model-written.
    lead: str
    template: Literal["states", "announced_on"]
    #: "official" (regulator, exchange, government) or "news" (reported, not confirmed).
    source_kind: Literal["official", "news", "issuer"] = "official"
    verification_url: str | None = None
    doc_id: str
    chunk_id: str
    start: int
    end: int


class SummarySentenceOut(BaseModel):
    """One model-written sentence that passed the server's checks: every citation real,
    every number present in a cited source, no buy/sell call. ``cites`` are 1-based
    positions in ``evidence``."""

    text: str
    cites: list[int]


class MessageUsageOut(BaseModel):
    limit: int
    used: int
    remaining: int
    resets_at: datetime


class AskOut(BaseModel):
    outcome: Literal["answer", "blocked", "clarify"]
    message: str
    text: str
    reason_code: str | None
    answer: AnswerOut | None
    suggestions: list[TopicOut]
    evidence: list[EvidenceOut] = []
    #: Plain-language note when official sources could not be shown, and why.
    note: str | None = None
    #: When the official sources were checked. Null when they were not.
    checked_at: datetime | None = None
    #: "checked": a selector picked the passages and every quote was verified exact.
    #: "ranked": picked by local word overlap. Null when no official source was shown.
    selection: Literal["checked", "ranked"] | None = None
    #: How official passages were ranked (Phase 5): "lexical" baseline, "bm25", or
    #: "hybrid" (BM25 + local embeddings). "bm25" when hybrid was configured but the local
    #: model was unavailable. Null when no official source was searched.
    retrieval: Literal["lexical", "bm25", "hybrid"] | None = None
    #: Open questions only: a short cited summary. Empty when none passed the checks, in
    #: which case ``evidence`` holds the source excerpts on their own.
    summary: list[SummarySentenceOut] = []
    usage: MessageUsageOut | None = None
