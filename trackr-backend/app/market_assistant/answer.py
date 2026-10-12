"""Turn an admission decision into what the user sees.

Answers come from local concepts without opt-in, or from admitted public evidence with
opt-in: checked model summaries and exact-quote fallbacks. ``Reply.text`` is the single
displayed answer, always fewer than 100 words. No user text is echoed in a block, so a
blocked value cannot be echoed back into a screen, a log that captures responses, or a
crash report. Nothing here names the services behind the assistant: sources are named
by their publisher and source kind, with dates and source-span provenance.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.market_assistant.admission import Admission, Outcome, PublicQuestion, QuestionType, Reason
from app.market_assistant.catalogue import (
    CATALOGUE_VERSION,
    CONCEPTS,
    SOURCE_DOMAINS,
    SOURCE_LABEL,
    SearchSpec,
)
from app.market_assistant.evidence import (
    Candidate,
    Doc,
    Excerpt,
    admit_documents,
    admit_open_documents,
    candidate_chunks,
    candidates_from_ranking,
    excerpt_from_span,
    excerpts_from_ranking,
    rank_passages,
    select_excerpts,
)
from app.market_assistant.exa import (
    SearchClient,
    SearchUnavailable,
    build_open_request,
    build_request,
)
from app.market_assistant.limits import DailyBudget, TTLCache
from app.market_assistant.model import ModelUnavailable, SelectorClient
from app.market_assistant.open_research import (
    ANSWER_PROMPT,
    PLANNER_PROMPT,
    ModelOutputRejected,
    Sentence,
    answer_content,
    parse_plan,
    planner_content,
    source_label,
    validate_answer,
)
from app.market_assistant.retrieval import Embedder, RetrievalMode
from app.market_assistant.selection import SelectionRejected, user_content, validate

logger = logging.getLogger("market_assistant.answer")

#: How the excerpts were chosen. Shown to nobody as "AI"; kept for audit and tests.
SELECTION_CHECKED = "checked"  # picked by the selector, every quote verified exact
SELECTION_RANKED = "ranked"  # picked by local word overlap; no model involved

#: Plain words for a portfolio manager, not a developer. One message per reason code.
MESSAGES: dict[Reason, str] = {
    Reason.LOOKS_LIKE_TABLE: "This looks like pasted data. Please ask one short question.",
    Reason.UNSUPPORTED_TEXT: "Please ask your question in plain English.",
    Reason.ABOUT_ASSISTANT: (
        "I can only help with market topics. Ask about a market term or the latest from "
        "RBI, SEBI, NSE or BSE."
    ),
    Reason.CONTAINS_IDENTIFIER: (
        "Please don't type IDs, phone numbers, emails or links here. Ask about a market "
        "topic instead."
    ),
    Reason.CONTAINS_CREDENTIAL: "Please remove credentials and personal details, then ask again.",
    Reason.PERSONAL_PORTFOLIO: (
        "I can't look at anyone's holdings or money. Ask about a market topic instead."
    ),
    Reason.ADVICE_REQUEST: "I can't say what to buy or sell. I can explain market terms.",
    Reason.CONTAINS_NUMBER: "Please leave out numbers. Pick a topic below or ask about a term.",
    Reason.UNKNOWN_TOPIC: "I don't know that topic yet. Try one of these:",
    Reason.MULTIPLE_TOPICS: "Pick one topic at a time:",
    Reason.EMPTY: "Type a question or pick a topic below.",
}

NOTE_SEARCH_OFF = "Checking official updates is turned off. Here is the basic explanation."
NOTE_NOT_ALLOWED = (
    "Official updates are checked only if you allow it in this panel. Here is the basic "
    "explanation."
)
NOTE_BUDGET = (
    "Today's limit for checking official updates is used up. Here is the basic "
    "explanation. Try again tomorrow."
)
NOTE_UNAVAILABLE = "Couldn't reach the official website just now. Here is the basic explanation."
NOTE_NOTHING_FOUND = (
    "No recent update found on the {publisher} website. Here is the basic explanation."
)

#: Public evidence is reused for this long, so repeat questions cost nothing.
EVIDENCE_TTL_SECONDS = 30 * 60

# --- open questions (owner amendment 11 Oct 2026) -------------------------------------
MSG_OPEN_NEEDS_OPT_IN = (
    "To answer this I need to search public sources. Turn Search On at the top and ask "
    "again, or pick a topic:"
)
MSG_OPEN_UNAVAILABLE = "Searching sources is not available right now. Try one of these:"
MSG_OUT_OF_SCOPE = (
    "I can help with money questions: stocks, funds, F&O, banking, tax, insurance and savings."
)
MSG_OPEN_ANSWER = "Here's what the sources say:"
MSG_ADVICE = (
    "I can't decide whether you should buy or sell. These facts can help you decide:"
)
MSG_OPEN_EXCERPTS = "Here are direct quotes from the sources:"
MSG_OPEN_NOTHING = (
    "I couldn't find reliable sources for this. Try a company or fund name, or rephrase."
)
MSG_OPEN_BUDGET = "Today's limit for searching sources is used up. Try again tomorrow."
MSG_OPEN_CLARIFY = (
    "Which company, fund or F&O contract do you mean? Please share only its public name."
)
OPEN_MAX_AGE_DAYS = 3650


@dataclass(frozen=True)
class Topic:
    topic_id: str
    label: str


@dataclass(frozen=True)
class Answer:
    topic_id: str
    title: str
    body: str
    source_label: str
    catalogue_version: str


@dataclass(frozen=True)
class Reply:
    outcome: Outcome
    message: str
    reason_code: str | None
    answer: Answer | None
    suggestions: tuple[Topic, ...]
    evidence: tuple[Excerpt, ...] = ()
    note: str | None = None
    checked_at: datetime | None = None
    selection: str | None = None
    #: How passages were ranked (``RetrievalMode``); None when no search ran.
    retrieval: str | None = None
    #: Open path: the checked, cited sentences. Citations index ``evidence`` (1-based).
    summary: tuple[Sentence, ...] = ()

    @property
    def text(self) -> str:
        """The one displayed answer, always fewer than 100 whitespace-separated words.

        Sources and exact excerpts are separate expandable metadata. Keep sentences
        whole so a length cap never removes an important qualification or negation.
        """
        parts = [self.message]
        if self.summary:
            parts.extend(s.text + " " + "".join(f"[{n}]" for n in s.cites)
                         for s in self.summary)
        elif self.evidence:
            for n, e in enumerate(self.evidence, 1):
                for sentence in re.split(r"(?<=[.!?])\s+", e.text):
                    if len(sentence.split()) + len(" ".join(parts).split()) + 1 < 100:
                        parts.append(f'"{sentence}" [{n}]')
                        break
        elif self.answer:
            parts.append(self.answer.body)
        if self.note:
            parts.append(self.note)
        kept: list[str] = []
        count = 0
        for part in parts:
            words = len(part.split())
            if count + words < 100:
                kept.append(part)
                count += words
        return " ".join(kept)


@dataclass
class SearchDeps:
    """What a LATEST question may use. ``client`` is None when searching is off;
    ``selector`` is None when the model step is off, and ranked excerpts are shown."""

    client: SearchClient | None
    budget: DailyBudget
    cache: TTLCache[tuple[tuple[Excerpt, ...], datetime, str | None, str]]
    selector: SelectorClient | None = None
    model_budget: DailyBudget | None = None
    today: Callable[[], date] = lambda: datetime.now(UTC).date()
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Phase 5 passage ranking. LEXICAL keeps the Phase 2 behaviour exactly.
    retrieval: RetrievalMode = RetrievalMode.LEXICAL
    embedder: Embedder | None = None


def topic(topic_id: str) -> Topic:
    return Topic(topic_id, CONCEPTS[topic_id].title)


def all_topics() -> tuple[Topic, ...]:
    return tuple(topic(topic_id) for topic_id in CONCEPTS)


def _answer(topic_id: str) -> Answer:
    concept = CONCEPTS[topic_id]
    return Answer(concept.topic_id, concept.title, concept.body, SOURCE_LABEL, CATALOGUE_VERSION)


def _newest_first(excerpts: list[Excerpt]) -> tuple[Excerpt, ...]:
    return tuple(
        sorted(
            excerpts,
            key=lambda e: (e.published is None, -(e.published or date.min).toordinal()),
        )
    )


def _choose(
    docs: list[Doc], spec: SearchSpec, deps: SearchDeps
) -> tuple[tuple[Excerpt, ...], str | None, str]:
    """Checked model selection if possible, ranked excerpts otherwise. Never neither when
    there are admitted documents: the model failing must not hide official text."""
    if not docs:
        return (), None, deps.retrieval.value
    if deps.retrieval is RetrievalMode.LEXICAL:
        ranked = tuple(select_excerpts(docs, spec))
        candidates = candidate_chunks(docs, spec)
        used = RetrievalMode.LEXICAL
    else:
        # One ranking (one embedding pass) feeds both the fallback excerpts and the
        # selector's candidates.
        order, used = rank_passages(docs, spec, deps.retrieval, deps.embedder)
        ranked = tuple(excerpts_from_ranking(order, spec))
        candidates = candidates_from_ranking(order)
    if deps.selector is None or deps.model_budget is None or not candidates:
        return ranked, SELECTION_RANKED, used.value
    if not deps.model_budget.reserve():
        logger.info("market_assistant selector skipped: daily model limit reached")
        return ranked, SELECTION_RANKED, used.value
    try:
        selections = validate(deps.selector.select(user_content(spec, candidates)), candidates)
    except ModelUnavailable as exc:
        # The reason is one of the adapter's fixed strings ("HTTP 429", "model response
        # has no text content"): no page or user text, safe to log.
        logger.info("market_assistant selector unavailable: %s", exc)
        return ranked, SELECTION_RANKED, used.value
    except SelectionRejected as exc:
        logger.info("market_assistant selector output rejected: %s", exc.reason)
        return ranked, SELECTION_RANKED, used.value
    if not selections:
        logger.info("market_assistant selector chose no passage")
        return ranked, SELECTION_RANKED, used.value
    chosen = [
        excerpt_from_span(s.candidate, s.start, s.end, s.template, spec.publisher)
        for s in selections
    ]
    return _newest_first(chosen), SELECTION_CHECKED, used.value


def _latest(
    topic_id: str, deps: SearchDeps | None, allow_external: bool
) -> tuple[tuple[Excerpt, ...], str | None, datetime | None, str | None, str | None]:
    spec = CONCEPTS[topic_id].search
    if spec is None:
        raise ValueError("LATEST requires a search spec")
    if not allow_external:
        return (), NOTE_NOT_ALLOWED, None, None, None
    if deps is None or deps.client is None:
        return (), NOTE_SEARCH_OFF, None, None, None
    today = deps.today()
    key = f"{topic_id}:{CATALOGUE_VERSION}:{deps.retrieval.value}:{today.isoformat()}"
    cached = deps.cache.get(key)
    if cached is not None:
        excerpts, checked_at, selection, retrieval = cached
    else:
        if not deps.budget.reserve():
            return (), NOTE_BUDGET, None, None, None
        try:
            question = PublicQuestion(topic_id, QuestionType.LATEST)
            results = deps.client.search(build_request(question, today))
        except SearchUnavailable:
            return (), NOTE_UNAVAILABLE, None, None, None
        excerpts, selection, retrieval = _choose(
            admit_documents(results, spec, today), spec, deps
        )
        checked_at = deps.now()
        deps.cache.put(key, (excerpts, checked_at, selection, retrieval))
    note = None if excerpts else NOTE_NOTHING_FOUND.format(publisher=spec.publisher)
    return excerpts, note, checked_at, selection, retrieval


def _open_reply(outcome: Outcome, message: str, admission: Admission, **kw) -> Reply:
    return Reply(
        outcome=outcome,
        message=message,
        reason_code=kw.pop("reason_code", None),
        answer=None,
        suggestions=tuple(topic(t) for t in admission.suggestions),
        **kw,
    )


def _excerpt(candidate: Candidate) -> Excerpt:
    publisher, kind = source_label(candidate)
    template = "announced_on" if candidate.doc.published else "states"
    return excerpt_from_span(
        candidate, candidate.start, candidate.end, template, publisher, source_kind=kind
    )


def _open(admission: Admission, deps: SearchDeps) -> Reply:
    """Plan, search approved sources, rank, answer with checked citations."""
    question = admission.open_text or ""
    if deps.model_budget is None or not deps.model_budget.reserve():
        return _open_reply(Outcome.CLARIFY, MSG_OPEN_BUDGET, admission, reason_code="budget")
    try:
        plan = parse_plan(
            deps.selector.complete(
                PLANNER_PROMPT, planner_content(question, deps.today(), admission.earlier)
            )
        )
    except (ModelUnavailable, ModelOutputRejected) as exc:
        logger.info("market_assistant planner failed: %s", getattr(exc, "reason", exc))
        return _open_reply(Outcome.CLARIFY, MSG_OPEN_UNAVAILABLE, admission,
                           reason_code="planner_unavailable")
    if not plan.in_scope:
        return _open_reply(Outcome.CLARIFY, MSG_OUT_OF_SCOPE, admission, reason_code="out_of_scope")
    if plan.clarify:
        return _open_reply(Outcome.CLARIFY, MSG_OPEN_CLARIFY, admission,
                           reason_code="missing_entity")

    today = deps.today()
    age = plan.max_age_days
    folded = question.casefold()
    historical = bool(re.search(r"\b(?:19|20)\d{2}\b", folded)) and not re.search(
        r"\b(?:today|latest|recent|now|current)\b", folded
    )
    if not historical:
        if re.search(r"\b(?:today|right now)\b", folded):
            age = min(age, 0)
        elif re.search(r"\bthis week\b", folded):
            age = min(age, 7)
        elif re.search(r"\b(?:latest|recent|news|current|update)\b", folded):
            age = min(age, 180 if re.search(r"\b(?:results|earnings|annual report)\b", folded)
                      else 30)
        elif admission.advice:
            age = min(age, 180)
    spec = SearchSpec(" ".join(plan.queries), tuple(sorted(SOURCE_DOMAINS)), "", age)
    raw_results = []
    # One trusted-source search plus one discovery search. The model chooses public
    # queries, never domains; unknown issuer hosts need an official website reference.
    searches = [(plan.queries[0], False), (plan.queries[-1], True)]
    start = today - timedelta(days=age) if age < OPEN_MAX_AGE_DAYS else None
    for query, discover in searches:
        if not deps.budget.reserve():
            break
        try:
            results = deps.client.search(build_open_request(
                query, discover=discover, start_published=start
            ))
        except (SearchUnavailable, ValueError) as exc:
            logger.info("market_assistant open search failed: %s", type(exc).__name__)
            continue
        raw_results.extend(results)
    docs = admit_open_documents(raw_results, spec, today)
    # Unknown publication dates cannot support a current-news claim. Publication and
    # check dates have different meanings; never substitute the check date.
    docs = [d for d in docs if (d.published is not None or age == OPEN_MAX_AGE_DAYS)
            and (d.published is None or d.published <= today)]
    checked_at = deps.now()
    if not docs:
        return _open_reply(Outcome.CLARIFY, MSG_OPEN_NOTHING, admission,
                           reason_code="nothing_found", checked_at=checked_at)

    order, used = rank_passages(docs, spec, deps.retrieval, deps.embedder)
    candidates = candidates_from_ranking(order)
    fallback = tuple(_excerpt(c) for c in candidates[:3])
    if not candidates:
        return _open_reply(Outcome.CLARIFY, MSG_OPEN_NOTHING, admission,
                           reason_code="nothing_found", checked_at=checked_at)
    sentences: list[Sentence] = []
    cited: list[Candidate] = []
    if deps.model_budget.reserve():
        try:
            sentences, cited = validate_answer(
                deps.selector.complete(
                    ANSWER_PROMPT, answer_content(
                        question, admission.advice, candidates, admission.earlier
                    )
                ),
                candidates,
            )
        except (ModelUnavailable, ModelOutputRejected) as exc:
            logger.info("market_assistant answer failed: %s", getattr(exc, "reason", exc))
    else:
        logger.info("market_assistant answer skipped: daily model limit reached")
    if sentences:
        message = MSG_ADVICE if admission.advice else MSG_OPEN_ANSWER
        return _open_reply(
            Outcome.ANSWER, message, admission,
            evidence=tuple(_excerpt(c) for c in cited), summary=tuple(sentences),
            checked_at=checked_at, selection=SELECTION_CHECKED, retrieval=used.value,
        )
    message = MSG_ADVICE if admission.advice else MSG_OPEN_EXCERPTS
    return _open_reply(
        Outcome.ANSWER, message, admission, evidence=fallback, checked_at=checked_at,
        selection=SELECTION_RANKED, retrieval=used.value,
    )


def render(
    admission: Admission, deps: SearchDeps | None = None, *, allow_external: bool = True
) -> Reply:
    """``allow_external`` is the user's opt-in; the route passes the request's value, which
    defaults to false. The default here is true only so pure unit tests of the search path
    need not repeat it; the route never relies on it."""
    suggestions = tuple(topic(t) for t in admission.suggestions)
    if admission.outcome is Outcome.OPEN:
        if not allow_external:
            return _open_reply(Outcome.CLARIFY, MSG_OPEN_NEEDS_OPT_IN, admission,
                               reason_code="needs_opt_in")
        if deps is None or deps.client is None or deps.selector is None:
            return _open_reply(Outcome.CLARIFY, MSG_OPEN_UNAVAILABLE, admission,
                               reason_code="search_unavailable")
        return _open(admission, deps)
    if admission.outcome is Outcome.ANSWER:
        if admission.question is None:
            raise ValueError("an ANSWER admission must carry a question")
        question = admission.question
        concept = CONCEPTS[question.topic_id]
        if question.question_type is QuestionType.LATEST:
            excerpts, note, checked_at, selection, retrieval = _latest(
                question.topic_id, deps, allow_external
            )
            spec = concept.search
            message = f"Latest from {spec.publisher}" if excerpts and spec else concept.title
            return Reply(
                outcome=Outcome.ANSWER,
                message=message,
                reason_code=None,
                answer=_answer(question.topic_id),
                suggestions=suggestions,
                evidence=excerpts,
                note=note,
                checked_at=checked_at,
                selection=selection,
                retrieval=retrieval,
            )
        return Reply(
            outcome=Outcome.ANSWER,
            message=concept.title,
            reason_code=None,
            answer=_answer(question.topic_id),
            suggestions=suggestions,
        )
    if admission.reason is None:
        raise ValueError("a non-ANSWER admission must carry a reason")
    return Reply(
        outcome=admission.outcome,
        message=MESSAGES[admission.reason],
        reason_code=admission.reason.value,
        answer=None,
        suggestions=suggestions,
    )


def lead(excerpt: Excerpt) -> str:
    """The server's own words above a quote. The selector picks a template ID; only this
    function turns it into text, so the model never writes anything the user reads."""
    if excerpt.source_kind == "issuer":
        return f"Company statement from {excerpt.publisher}:"
    if excerpt.source_kind == "news":
        if excerpt.published is not None:
            return f"Reported by {excerpt.publisher} on {excerpt.published.strftime('%d %b %Y')}:"
        return f"Reported by {excerpt.publisher}:"
    if excerpt.template == "announced_on" and excerpt.published is not None:
        return f"{excerpt.publisher} announced on {excerpt.published.strftime('%d %b %Y')}:"
    return f"{excerpt.publisher} states:"
