"""Local admission: decide, in this process, whether a typed question may be handled.

The current owner direction in docs/AI_ASSISTANT_PRIVACY_PLAN.md supersedes the old
closed-topic-only policy. Local privacy checks run before any external classification.
Blocked text is never forwarded, logged, persisted or echoed in a reason message.

Order matters, and it is strict-first:

1. normalise a detection copy (NFKC, zero-width characters removed, case folded)
2. refuse non-ASCII and pasted tables before anything tries to read them
3. refuse identifiers, credentials and personal holdings/amounts in the complete input
4. exempt explicit public years, quarters, ratios, percentages and option parameters
5. tag trade-advice requests for research with a short decision boundary
6. resolve catalogue topics for offline use or retain the safe question in request memory

OWNER AMENDMENT, 11 October 2026 (docs/AI_ASSISTANT_PRIVACY_PLAN.md "Open questions").
The privacy goal is to keep PERSONAL information from leaving, not to restrict wording. So
a safe question retains cleaned ``open_text`` only in request memory. With external
opt-in, ``as_research`` routes even catalogue definitions to the LLM relevance/planning
step. Without opt-in the local concept library stays available. Privacy detection is
conservative, not infallible: bare personal names and novel disguises have no guaranteed
shape. These public-parameter exceptions never override private ownership or credentials.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from app.ingest.redaction import BARE_PAN, DEMAT, EMAIL, GSTIN, PHONE, UCC_LABELLED
from app.market_assistant.catalogue import CONCEPTS, DEFAULT_SUGGESTIONS

#: Matches the request schema's own cap. Repeated here so this module is safe to call
#: directly, and so every regex below runs on bounded input.
MAX_QUESTION_CHARS = 2000

_ZERO_WIDTH = re.compile("[­​-‏⁠-⁤﻿]")
#: Typographic punctuation that pasted news text carries, read as its ASCII form. Without
#: this, "Kore Digital\u2019s" fails the ASCII check and is refused as "not plain English".
_TYPOGRAPHIC = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-",
})
#: Earlier questions of the same drawer conversation sent as follow-up context.
MAX_HISTORY = 4
_AADHAAR = re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
_LINK = re.compile(r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|in|net|org|io|co)\b)", re.I)
#: A long token mixing letters and digits: account numbers, client codes, tokens.
_MIXED_TOKEN = re.compile(r"\b(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{8,}\b", re.I)
#: Long unbroken runs: encoded payloads, keys.
_ENCODED = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_SPLIT_PAN = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z][\s._-]*){5}(?:[0-9][\s._-]*){4}[A-Z](?![A-Z0-9])"
)
_CREDENTIAL = re.compile(
    r"\b(?:password|passwd|passphrase|otp|pin|cvv|credential|secret|access\s+token|"
    r"api\s+key|private\s+key|seed\s+phrase|recovery\s+phrase|bearer|authorization|"
    r"login\s+(?:id|details)|"
    r"account\s+(?:number|no)|bank\s+(?:details|account)|ifsc)\b|"
    r"\b(?:my|our|home|residential|postal)\s+address\b|\baddress\s*[:=]|"
    r"(?:\\u[0-9a-f]{4}|%[0-9a-f]{2}){2,}", re.I,
)
_CREDENTIAL_DEFINITION = re.compile(
    r"(?:what\s+(?:is|are)|explain|define)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?:otp|pin|cvv|password|ifsc|account\s+number)(?:\s+scams?)?\s*[?.!]?", re.I,
)


def _has_credential(text: str) -> bool:
    return bool(_CREDENTIAL.search(text) and not _CREDENTIAL_DEFINITION.fullmatch(text))


_PRIVATE_POSITION = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s+(?:shares?|units?|lots?)|"
    r"(?:qty|quantity|holding|holdings|balance|cost\s+basis)\s*[:=]|"
    r"(?:portfolio|holdings)\s*[:=]|(?:my|our)\s+(?:sip|salary|income|savings|debt|"
    r"loan|balance|net\s+worth)|(?:i|we)\s+(?:am|are)\s+holding|"
    r"(?:portfolio|holdings?|positions?)\s+(?:contains?|includes?|consists?)|"
    r"[a-z]{2,12}\s*[:=]\s*\d|[a-z]{2,12}\s+\d{1,2}\s+\d)", re.I,
)

_PERSONAL = re.compile(
    r"\b(?:"
    r"(?:i|we)\s+(?:own|hold|have(?!\s+(?:a\s+)?(?:question|doubt|query|confusion))|"
    r"bought|sold|invested|purchased)"
    r"|i'?ve\s+(?:bought|sold|invested)"
    r"|my\s+(?:portfolio|holdings?|shares?|stocks?|investments?|loss(?:es)?|profits?"
    r"|gains?|returns?|account|demat|broker|pan|clients?|money|positions?"
    r"|xirr|cagr|twrr|p&l|pnl|allocation|dividends?|cost|average"
    r"|wife|husband|father|mother|son|daughter|family)"
    r"|our\s+(?:portfolio|holdings?|clients?)"
    r"|clients?'?s?"
    r"|bought\s+at|sold\s+at|average\s+price\s+of"
    r")\b",
    re.I,
)
#: Questions about the assistant's own make-up. Matched on the folded copy.
_ABOUT_ASSISTANT = re.compile(
    r"\b(?:"
    r"(?:which|what)\s+(?:ai|a\.i|model|llm|language\s+model|gpt|chatbot|bot|engine)"
    r"|are\s+you\s+(?:an?\s+)?(?:ai|bot|robot|human|real|chatgpt|gpt|claude|gemini|llama)"
    r"|(?:who|which\s+company)\s+(?:made|built|created|trained|owns|developed|runs)\s+you"
    r"|what\s+are\s+you|who\s+are\s+you"
    r"|your\s+(?:model|prompt|system\s+prompt|instructions|training|creator|maker|provider|source\s+code)"
    r"|powered\s+by"
    r"|(?:are\s+you|powered\s+by)\s+(?:chatgpt|openai|openrouter|anthropic|claude|"
    r"gemini|llama|mistral|deepseek|groq|qwen)"
    r"|(?:do|are)\s+you\s+(?:use|using|based\s+on)\s+(?:exa|openai|openrouter|groq)"
    r")\b",
    re.I,
)
#: Asking for news rather than a definition. Only meaningful for a topic with a search spec.
_LATEST = re.compile(
    r"\b(?:latest|recent|recently|new|newest|announce[ds]?|announcement|circulars?|"
    r"updates?|news|today|this\s+(?:week|month))\b",
    re.I,
)
_ADVICE = re.compile(
    r"\b(?:"
    r"(?:should|can|could|shall)\s+(?:i|we)\s+(?:buy|sell|hold|exit|invest|book)"
    r"|(?:buy|sell)\s+or\s+(?:hold|sell|buy)"
    r"|target\s+price|stock\s+tips?|which\s+(?:stock|share)s?\s+(?:to|should)"
    r"|best\s+(?:stock|share)s?|(?:good|safe|worth)\s+(?:to\s+)?(?:buy|buying|invest)"
    r")\b",
    re.I,
)


class Outcome(StrEnum):
    ANSWER = "answer"
    BLOCKED = "blocked"
    CLARIFY = "clarify"
    #: Passed every privacy check; not a plain catalogue definition. Internal only: the
    #: renderer turns it into an answer (open research path) or a request to opt in.
    OPEN = "open"


class Reason(StrEnum):
    """Stable codes. Logged and returned; never accompanied by the matched text."""

    LOOKS_LIKE_TABLE = "looks_like_table"
    UNSUPPORTED_TEXT = "unsupported_text"
    CONTAINS_IDENTIFIER = "contains_identifier"
    CONTAINS_CREDENTIAL = "contains_credential"
    ABOUT_ASSISTANT = "about_assistant"
    PERSONAL_PORTFOLIO = "personal_portfolio"
    ADVICE_REQUEST = "advice_request"
    CONTAINS_NUMBER = "contains_number"
    UNKNOWN_TOPIC = "unknown_topic"
    MULTIPLE_TOPICS = "multiple_topics"
    EMPTY = "empty"


class QuestionType(StrEnum):
    DEFINITION = "definition"
    LATEST = "latest"


@dataclass(frozen=True)
class PublicQuestion:
    """The only shape a question takes after admission. Built from catalogue values
    alone: there is no field that could hold the user's text."""

    topic_id: str
    question_type: QuestionType = QuestionType.DEFINITION

    def __post_init__(self) -> None:
        if self.topic_id not in CONCEPTS:
            raise ValueError("topic_id is not an approved catalogue topic")
        if self.question_type is QuestionType.LATEST and CONCEPTS[self.topic_id].search is None:
            raise ValueError("this topic has no approved official source to search")


@dataclass(frozen=True)
class Admission:
    outcome: Outcome
    question: PublicQuestion | None = None
    reason: Reason | None = None
    #: Catalogue topic IDs to offer as chips. Public values only.
    suggestions: tuple[str, ...] = field(default_factory=tuple)
    #: OPEN only: the cleaned question, for the open research path. Held in request
    #: memory; never logged, stored or returned.
    open_text: str | None = field(default=None, repr=False)
    #: OPEN only: the question asks for a buy/sell/hold call; the answer declines it.
    advice: bool = False
    #: OPEN only: earlier admitted questions of this conversation (``admit_history``), so
    #: a follow-up such as "what subsidiaries?" can be resolved. Request memory only.
    earlier: tuple[str, ...] = field(default=(), repr=False)


def _blocked(reason: Reason) -> Admission:
    return Admission(Outcome.BLOCKED, reason=reason, suggestions=DEFAULT_SUGGESTIONS)


def _clarify(reason: Reason, suggestions: tuple[str, ...] = DEFAULT_SUGGESTIONS) -> Admission:
    return Admission(Outcome.CLARIFY, reason=reason, suggestions=suggestions)


def normalise(text: str) -> str:
    """The detection copy: NFKC (folds full-width digits and look-alike forms), zero-width
    characters removed, hyphens and underscores read as spaces, whitespace collapsed,
    case folded. Typographic quotes and dashes are read as ASCII first."""
    text = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHIC)
    text = _ZERO_WIDTH.sub("", text)
    text = re.sub(r"[-_‐-―]", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(normalise(alias)) + r"(?![a-z0-9])")


#: Longest phrase first, so "nifty 50" is consumed before "nifty" can match inside it.
_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (_alias_pattern(alias), concept.topic_id)
    for concept, alias in sorted(
        ((c, a) for c in CONCEPTS.values() for a in c.aliases),
        key=lambda pair: -len(pair[1]),
    )
)


def _strip_topics(folded: str) -> tuple[str, list[str]]:
    """Remove every known topic phrase; return what is left and the topics found."""
    remaining = folded
    matched: list[str] = []
    for pattern, topic_id in _ALIASES:
        if pattern.search(remaining):
            remaining = pattern.sub(" ", remaining)
            if topic_id not in matched:
                matched.append(topic_id)
    return remaining, matched


def _has_identifier(raw: str, folded: str, remaining: str) -> bool:
    """``remaining`` is the folded text with topic phrases removed, so a topic name that
    mixes letters and digits ("nifty50") is not mistaken for an account code."""
    upper = unicodedata.normalize("NFKC", _ZERO_WIDTH.sub("", raw)).upper()
    # Detect deliberately separated PAN characters without accepting punctuation as a
    # way to bypass the ordinary shape checks. Never forward a redacted remainder.
    shape_checks = (
        BARE_PAN.search(upper),
        _SPLIT_PAN.search(upper),
        GSTIN.search(upper),
        DEMAT.search(upper),
        UCC_LABELLED.search(upper),
        EMAIL.search(folded),
        PHONE.search(folded),
        _AADHAAR.search(folded),
        _LINK.search(folded),
        _MIXED_TOKEN.search(remaining),
        _ENCODED.search(raw),
    )
    return any(shape_checks)


#: Numbers that may stay in an open question: a year, a fiscal year ("2026-27", "FY26"), a
#: quarter or half ("Q2", "H1"), a ratio ("1:1"), or a count under 100 ("top 10").
_SAFE_NUMBER = re.compile(
    r"^(?:(?:19|20)\d{2}(?:-\d{2})?|fy\d{2,4}|[a-z]{1,3}\d{1,2}|"
    r"\d{1,2}:\d{1,2}|\d{1,2})$"
)
_NUMBER_TOKEN = re.compile(r"[a-z]*\d[\d,.:/-]*[a-z]*")
_CURRENCY = re.compile(r"(?:₹|\$|\brs\.?\s*\d|\binr\s*\d|\d\s*(?:crore|cr|lakh|lac|k)\b)")


def has_risky_number(folded: str) -> bool:
    """True for a number that could be an amount, price, quantity or account fragment."""
    if _CURRENCY.search(folded):
        return True
    for token in _NUMBER_TOKEN.findall(folded):
        token = token.strip(".,:/-")
        if token and not _SAFE_NUMBER.match(token):
            return True
    return False


def _strip_public_parameters(folded: str) -> str:
    """Public percentages/ratios and option strikes are not holdings or credentials.

    Only explicit financial parameter forms are exempted. Personal wording, quantities,
    credential shapes and structured pastes are checked against the complete question.
    """
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3})?\s*(?:%|percent\b|per\s+cent\b|bps\b)",
                  " public percentage ", folded)
    text = re.sub(r"\b(?:p/e|pe|p/b|pb)(?:\s+ratio)?\s*(?:of\s+)?"
                  r"\d{1,3}(?:\.\d{1,3})?\b", " public ratio ", text)
    if re.search(r"\b(?:f&o|options?|calls?|puts?|strike|nifty|banknifty|sensex)\b", folded):
        text = re.sub(r"\bstrike(?:\s+price)?\s*[:=]?\s*\d{3,6}(?:\.\d{1,2})?\b",
                      " public option strike ", text)
        text = re.sub(r"\b[a-z]{2,15}\d{3,6}(?:ce|pe)\b", " public option contract ", text)
        text = re.sub(r"\b\d{3,6}\s+(?:call|put)\b", " public option contract ", text)
    return text


#: Words that, with a single topic, make a plain "what is X" question.
_DEFINITION_WORDS = frozenset(
    "what is are a an the does do mean means meaning of explain define definition tell me "
    "about how work works please in simple terms term what's whats".split()
)


def _is_plain_definition(remaining: str) -> bool:
    words = re.findall(r"[a-z']+", remaining)
    return all(w in _DEFINITION_WORDS for w in words)


def clean_open_text(raw: str) -> str:
    """The text sent on the open path: NFKC, zero-width and control characters removed,
    whitespace collapsed, typographic quotes and dashes as ASCII. Case and punctuation
    kept, so the planner reads it as typed."""
    text = unicodedata.normalize("NFKC", _ZERO_WIDTH.sub("", raw)).translate(_TYPOGRAPHIC)
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_QUESTION_CHARS]


def is_clean_query(text: str) -> bool:
    """The same privacy checks, for text the planner wrote: a search query must carry no
    identifier, no personal-portfolio language and no risky number."""
    folded = normalise(text)
    if not folded or not folded.isascii() or len(folded) > 200:
        return False
    remaining, _ = _strip_topics(_strip_public_parameters(folded))
    return not (
        _has_identifier(text, folded, remaining)
        or _PERSONAL.search(folded)
        or _has_credential(folded)
        or _PRIVATE_POSITION.search(folded)
        or has_risky_number(remaining)
    )


def admit_text(raw: str) -> Admission:
    """Decide what to do with one typed question. Pure: no I/O, no logging."""
    if len(raw) > MAX_QUESTION_CHARS:
        return _blocked(Reason.LOOKS_LIKE_TABLE)
    if (raw.count("\n") >= 2 or "\t" in raw or raw.count(",") >= 4
            or raw.count("|") >= 2 or re.search(r"[{}]|\[\s*[\"'{]", raw)):
        return _blocked(Reason.LOOKS_LIKE_TABLE)

    folded = normalise(raw)
    if not folded:
        return _clarify(Reason.EMPTY)
    if not folded.isascii() or any(unicodedata.category(ch)[0] == "C" for ch in folded):
        return _blocked(Reason.UNSUPPORTED_TEXT)

    remaining, matched = _strip_topics(_strip_public_parameters(folded))
    if _has_identifier(raw, folded, remaining):
        return _blocked(Reason.CONTAINS_IDENTIFIER)
    if _has_credential(folded):
        return _blocked(Reason.CONTAINS_CREDENTIAL)
    if _PERSONAL.search(folded) or _PRIVATE_POSITION.search(folded):
        return _blocked(Reason.PERSONAL_PORTFOLIO)
    advice = bool(_ADVICE.search(folded))
    if has_risky_number(remaining):
        return _blocked(Reason.CONTAINS_NUMBER)

    if len(matched) == 1 and not advice:
        topic_id = matched[0]
        if CONCEPTS[topic_id].search is not None and _LATEST.search(remaining):
            return Admission(Outcome.ANSWER, question=PublicQuestion(topic_id, QuestionType.LATEST),
                             open_text=clean_open_text(raw))
        if _is_plain_definition(remaining):
            return Admission(Outcome.ANSWER, question=PublicQuestion(topic_id),
                             open_text=clean_open_text(raw))
    reason = (
        Reason.ADVICE_REQUEST
        if advice
        else Reason.MULTIPLE_TOPICS
        if len(matched) > 1
        else Reason.UNKNOWN_TOPIC
    )
    return Admission(
        Outcome.OPEN,
        reason=reason,
        suggestions=tuple(matched) or DEFAULT_SUGGESTIONS,
        open_text=clean_open_text(raw),
        advice=advice,
    )


def admit_history(earlier: list[str]) -> tuple[str, ...]:
    """Earlier questions from the same conversation, as follow-up context for the planner.

    Each one passes the same local admission as a new question and is dropped, silently,
    if it would have been refused: a blocked question cannot reach the model by being
    re-sent as history. Only the last ``MAX_HISTORY`` survivors are kept, oldest first.
    The client holds them for one drawer session and one portfolio; the server stores
    none of them.
    """
    kept = [
        clean_open_text(text)
        for text in earlier
        if admit_text(text).outcome not in (Outcome.BLOCKED, Outcome.CLARIFY)
    ]
    return tuple(t for t in kept if t)[-MAX_HISTORY:]


def admit_topic(topic_id: str) -> Admission:
    """A suggestion chip: the client sends a catalogue ID, not text. An ID that is not in
    the catalogue raises ``KeyError`` so the route can answer 404."""
    if topic_id not in CONCEPTS:
        raise KeyError(topic_id)
    return Admission(Outcome.ANSWER, question=PublicQuestion(topic_id))


def as_research(admission: Admission) -> Admission:
    """With opt-in, classify *every* safe question with the LLM, including definitions.

    Without opt-in the existing public concept library remains available offline. No
    personal text can reach this conversion: blocked/clarify decisions stay unchanged.
    """
    if admission.outcome is not Outcome.ANSWER:
        return admission
    question = admission.question
    if question is None:
        raise ValueError("answer requires a public question")
    concept = CONCEPTS[question.topic_id]
    text = admission.open_text or f"What is {concept.title}?"
    return Admission(Outcome.OPEN, open_text=text, suggestions=(question.topic_id,))
