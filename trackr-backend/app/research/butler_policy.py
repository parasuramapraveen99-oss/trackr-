"""Butler's reviewed catalogue, card templates, review checklists and presentation budgets.

docs/BUTLER_ARCHITECTURE.md sections 4, 6 and 7; docs/BUTLER_CHARACTER.md sections 3-6.

EVERY SENTENCE BUTLER CAN SHOW LIVES IN THIS FILE. Cards, questions and lessons are fixed,
versioned templates with named arguments; the renderer (``butler_validation.py``) fills the
arguments from re-read, permission-checked records and nothing else. A model -- when one is
connected in a later session -- can only SELECT an id from the allowlists below. It cannot
author a sentence, and nothing here accepts one.

PREMISE-SAFE BY CONSTRUCTION. A question never asserts something the packet does not hold:
each declares what must exist before it may be asked (``requires``), and ``question_allowed``
refuses it otherwise. "Your recorded holding period is quoted above" is only asked when a
holding period was recorded. No template predicts a price, recommends buy/sell/hold, attributes
a motive, labels the person, or says a belief is right or wrong.

PRODUCT CHOICES, NOT FINANCIAL RULES. The digest size, quiet hours, cooldown and checkpoint
offer are defaults from architecture section 6; each is user-configurable where stated. None of
them affects a number, and none is a claim about the investor.

Pure: no database, no framework, no network, no model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Version of this whole catalogue. A wording change is a NEW version: a stored inbox item keeps
#: the template version it was created under and is rendered with that version's text.
#: v2 (BT 09): lessons carry applicability conditions, provenance and forbidden inferences, and
#: a packet offers only the lessons whose conditions its own observations meet.
CATALOGUE_VERSION = "butler-catalogue-2"
#: Version of the deterministic trigger rules the worker applies (BT 05).
RULE_VERSION = "butler-rules-1"
#: Contract versions (schema/butler_context.schema.json, schema/butler_proposal.schema.json).
CONTEXT_SCHEMA = "butler.context.v1"
PROPOSAL_SCHEMA = "butler.proposal.v1"

# --- proposal limits (architecture section 4) -----------------------------------------------
MAX_OBSERVATION_REFS = 8
MAX_COMPARISONS = 3
MAX_QUESTIONS = 1
MAX_LESSONS = 1
ABSTAIN_CODES = (
    "INSUFFICIENT_EVIDENCE",
    "STALE_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "INCOMPLETE_COVERAGE",
    "OUT_OF_SCOPE",
)

# --- packet budgets (architecture sections 3-4, 8) -------------------------------------------
#: A conservative 3 characters per token against the 8,000-input-token default. Over budget is
#: narrowed or marked incomplete; it is never silently truncated.
MAX_PACKET_CHARS = 24_000
MAX_EXCERPTS = 16
MAX_EXCERPT_CHARS = 1_200
MAX_OBSERVATIONS = 24
#: How long a sealed packet may be used for a submission.
PACKET_TTL = timedelta(minutes=15)

# --- presentation (architecture section 6) --------------------------------------------------
DEFAULT_TIMEZONE_OFFERED: str | None = None  # never inferred; the person must choose one
DEFAULT_QUIET_START = time(21, 0)
DEFAULT_QUIET_END = time(8, 0)
DEFAULT_DIGEST_LIMIT = 3
UNCHANGED_ISSUE_COOLDOWN = timedelta(days=7)
MAX_SNOOZE = timedelta(days=90)
#: Offered when following a company; applied only if the person accepts it.
CHECKPOINT_DAYS_OFFERED = 30

# --- worker (BT 05) -------------------------------------------------------------------------
MAX_EVENT_ATTEMPTS = 5
TICK_EVENT_BATCH = 50

PRIORITY_INFORMATIONAL = "INFORMATIONAL"
PRIORITY_REQUIRED = "REQUIRED"


# =========================================================================================
# questions and lessons
# =========================================================================================

#: What a question needs before it may be asked. Checked against the packet or the review.
REQUIRES_BELIEF = "BELIEF"  # a quoted, saved user belief (thesis version, note, answer)
REQUIRES_HORIZON = "HORIZON"  # a recorded intended holding period on the pinned thesis
REQUIRES_PAIR = "PAIR"  # two sealed excerpts to compare
REQUIRES_COVERAGE_GAP = "COVERAGE_GAP"  # a recorded coverage limitation


@dataclass(frozen=True)
class QuestionTemplate:
    id: str
    version: str
    text: str
    requires: frozenset[str]
    #: Whether a review expects evidence to be attached to an answer.
    evidence_expected: bool
    #: The ONE approved deterministic check for this question, or None.
    code_check: str | None = None


#: Lesson applicability conditions (BT 09). Each is computed from the packet's OWN typed
#: observations and excerpts -- never from sentiment, tone or a model's reading of the person.
LESSON_ACTIVITY = "RECORDED_ACTIVITY_OBSERVED"  # the packet counts saved research activity
LESSON_TWO_SIGNALS = "TWO_OR_MORE_SIGNALS"  # two excerpts, or two sourced facts/evaluations
LESSON_PRIOR_EFFORT = "PRIOR_SAVED_EFFORT"  # at least one saved note, answer or linked evidence


@dataclass(frozen=True)
class LessonTemplate:
    id: str
    version: str
    text: str
    #: Conditions that must ALL hold in the packet for the lesson to be offered.
    requires: frozenset[str] = frozenset()
    #: Where the lesson's claim comes from. Definitional lessons state a general distinction;
    #: they make no factual claim about this company, this price or this person.
    provenance: str = "AUTHORED_DEFINITION"
    #: What the lesson must never be used to imply. Shown with the lesson in detailed view.
    forbidden_inferences: tuple[str, ...] = ()


QUESTIONS: dict[str, QuestionTemplate] = {
    q.id: q
    for q in (
        QuestionTemplate(
            "EVIDENCE_FOR_BELIEF", "1",
            "Which saved evidence supports the belief quoted here? Attach it, or say that none "
            "is saved in this workspace yet.",
            frozenset({REQUIRES_BELIEF}), evidence_expected=True,
        ),
        QuestionTemplate(
            "ALTERNATIVE_EXPLANATION", "1",
            "Besides the belief quoted here, what else could explain the same observations?",
            frozenset({REQUIRES_BELIEF}), evidence_expected=False,
        ),
        QuestionTemplate(
            "WHAT_WOULD_CHANGE_YOUR_MIND", "1",
            "What observable result would lead you to revise the belief quoted here?",
            frozenset({REQUIRES_BELIEF}), evidence_expected=False,
        ),
        QuestionTemplate(
            "NEXT_EVIDENCE_MILESTONE", "1",
            "What is the next piece of evidence you expect, and by what date?",
            frozenset(), evidence_expected=False, code_check="MILESTONE_DATE_AFTER_REVIEW",
        ),
        QuestionTemplate(
            "HORIZON_STILL_APPLIES", "1",
            "Your recorded holding period is quoted here. Does it still apply?",
            frozenset({REQUIRES_HORIZON}), evidence_expected=False,
        ),
        QuestionTemplate(
            "COMPARE_TWO_PASSAGES", "1",
            "These two dated passages are shown side by side. Do they describe the same thing, "
            "and does the earlier one still hold for you?",
            frozenset({REQUIRES_PAIR}), evidence_expected=False,
        ),
        QuestionTemplate(
            "SOURCE_COVERAGE_GAP", "1",
            "Some sources were not checked; they are listed here. Is there a source you want "
            "to add before deciding?",
            frozenset({REQUIRES_COVERAGE_GAP}), evidence_expected=True,
        ),
    )
}

LESSONS: dict[str, LessonTemplate] = {
    lesson.id: lesson
    for lesson in (
        LessonTemplate(
            "RESEARCH_PROGRESS_VS_PRICE_RETURN", "1",
            "Research progress, business progress and price return are different measures. A "
            "price over a short window says nothing on its own about the business or about "
            "what you have learned.",
            requires=frozenset({LESSON_ACTIVITY}),
            forbidden_inferences=("THAT_PRICE_MOVEMENT_REFLECTS_RESEARCH_QUALITY",
                                  "THAT_NO_SAVED_ACTIVITY_MEANS_NO_WORK_WAS_DONE",
                                  "THAT_THE_BUSINESS_IS_OR_IS_NOT_GROWING"),
        ),
        LessonTemplate(
            "CORRELATION_VS_CAUSATION", "1",
            "Two things moving together does not show that one caused the other. It can help "
            "to ask what else changed in the same period.",
            requires=frozenset({LESSON_TWO_SIGNALS}),
            forbidden_inferences=("THAT_THE_TWO_SIGNALS_ARE_UNRELATED",
                                  "THAT_THE_PERSON_CONFUSED_CORRELATION_WITH_CAUSE"),
        ),
        LessonTemplate(
            "SUNK_COST_QUESTION", "1",
            "Setting aside the time already spent: if you were starting today, would you begin "
            "this research?",
            requires=frozenset({LESSON_PRIOR_EFFORT}),
            forbidden_inferences=("THAT_THE_PERSON_HAS_A_SUNK_COST_BIAS",
                                  "THAT_THE_RESEARCH_SHOULD_STOP",
                                  "ANY_DIAGNOSIS_OF_MOTIVE_OR_PERSONALITY"),
        ),
    )
}

#: Follow-ups asked after an answer, keyed by what is missing. Fixed text; never a judgment of
#: the answer's sincerity, length or style.
FOLLOW_UPS: dict[str, str] = {
    "ATTACH_EVIDENCE": "Your answer is recorded. Is there saved evidence you can attach? If "
    "not, you can mark the answer as your own attestation.",
    "MILESTONE_DATE": "Your answer is recorded. If there is a date by which you expect this "
    "evidence, adding it lets the date be checked.",
}


def _personal_questions() -> dict[str, QuestionTemplate]:
    """BT 14's personalised questions, from the reviewed NOTEBOOK catalogue. Kept OUT of
    ``QUESTIONS`` on purpose: they are never in a checklist, never in the model-selectable
    catalogue and never offered without an active notebook consent and a current pattern.
    Evidence-satisfied ones are ``evidence_expected``, so a fluent answer alone stays
    ANSWER_RECORDED -- the same verification standard as every other field."""
    from app.research import butler_notebook_policy as np

    return {q.id: QuestionTemplate(q.id, f"notebook-{q.version}", q.text, frozenset(),
                                   evidence_expected=q.satisfied_by == "EVIDENCE")
            for q in np.QUESTIONS.values()}


PERSONAL_QUESTIONS: dict[str, QuestionTemplate] = _personal_questions()


def question_template(question_id: str) -> QuestionTemplate:
    """A review field's template: checklist catalogue first, then personalised questions."""
    template = QUESTIONS.get(question_id) or PERSONAL_QUESTIONS.get(question_id)
    if template is None:
        raise KeyError(question_id)
    return template


def lesson_allowed(lesson_id: str, available: set[str]) -> bool:
    """Is this lesson in the catalogue AND does the packet meet every condition it needs?"""
    template = LESSONS.get(lesson_id)
    return template is not None and template.requires <= available


def question_allowed(question_id: str, available: set[str]) -> bool:
    """Is this question in the catalogue AND is every premise it states present?"""
    template = QUESTIONS.get(question_id)
    return template is not None and template.requires <= available


# =========================================================================================
# card templates
# =========================================================================================


@dataclass(frozen=True)
class CardTemplate:
    id: str
    version: str
    card_kind: str
    priority: str
    #: Title and statement templates. ``{name}`` placeholders only; every value is supplied by
    #: the renderer from typed, re-read records.
    title: str
    statements: tuple[str, ...]
    question_id: str | None = None
    lesson_id: str | None = None


CARDS: dict[str, CardTemplate] = {
    c.id: c
    for c in (
        CardTemplate(
            "NEW_MATERIAL", "1", "NEW_MATERIAL", PRIORITY_INFORMATIONAL,
            "New document saved for {company}",
            (
                "{source_label} ({source_type}), period {period}.",
                "Published {publication_date}; recorded in this workspace {recorded_on}.",
                "Text: {content_state}.",
            ),
        ),
        CardTemplate(
            "REVIEW_DUE", "1", "REVIEW_DUE", PRIORITY_REQUIRED,
            "Research checkpoint due for {company} ({due_on})",
            (
                "Recorded in this workspace from {window_start} to {window_end}: "
                "{notes_saved} notes saved, {evidence_linked} evidence links, "
                "{answers_recorded} answers recorded.",
                "{open_questions} of your questions for this company have no saved answer.",
                "These are counts of records saved here. They do not measure time spent or "
                "research done elsewhere.",
            ),
            question_id="NEXT_EVIDENCE_MILESTONE",
            lesson_id="RESEARCH_PROGRESS_VS_PRICE_RETURN",
        ),
        CardTemplate(
            "RULE_STATE_CHANGED", "1", "RULE_STATE_CHANGED", PRIORITY_REQUIRED,
            "A recorded condition changed state for {company}",
            (
                "Thesis version {thesis_version}: the deterministic evaluation with evidence "
                "cut off at {evidence_cutoff} is {state}.",
                "The previous recorded evaluation was {previous_state}.",
            ),
            question_id="WHAT_WOULD_CHANGE_YOUR_MIND",
        ),
        CardTemplate(
            "COVERAGE_NO_FEED", "1", "COVERAGE_WARNING", PRIORITY_INFORMATIONAL,
            "Source coverage for {company} is limited",
            (
                "No permitted external source feed is configured, so only documents saved in "
                "this workspace are checked.",
                "An empty inbox is not an all-clear: sources outside this workspace were not "
                "checked.",
            ),
        ),
        CardTemplate(
            "COVERAGE_TEXT_UNAVAILABLE", "1", "COVERAGE_WARNING", PRIORITY_INFORMATIONAL,
            "A saved document's text could not be read for {company}",
            (
                "{source_label} ({source_type}) has no extracted text ({reason}).",
                "Its contents were not checked by anything that relies on text.",
            ),
        ),
        CardTemplate(
            "SOURCE_CORRECTION", "1", "SOURCE_CORRECTION", PRIORITY_REQUIRED,
            "A saved source was marked {reason} for {company}",
            (
                "{source_label} ({source_type}), published {publication_date}, was marked "
                "{reason} on {recorded_on}.",
                "Cards and review evidence that cite it are withdrawn.",
            ),
        ),
        CardTemplate(
            "THESIS_CHANGED", "1", "THESIS_CHANGED", PRIORITY_INFORMATIONAL,
            "Your thesis for {company} has a new version",
            (
                "Version {old_version} (dated {old_version_on}) and version {new_version} "
                "(dated {new_version_on}) are shown side by side.",
            ),
            question_id="COMPARE_TWO_PASSAGES",
        ),
    )
}

#: Human labels for enums a template prints. Closed maps: an unknown value renders "UNKNOWN",
#: never a guess.
SOURCE_TYPE_LABELS = {
    "EXCHANGE_FILING": "exchange filing",
    "ANNUAL_REPORT": "annual report",
    "RESULTS": "results",
    "PRESENTATION": "presentation",
    "TRANSCRIPT": "transcript",
    "SHAREHOLDING": "shareholding",
    "NEWS": "news",
    "MANAGEMENT_COMMENTARY": "management commentary",
    "USER_DOCUMENT": "your document",
    "BOOK": "book",
}
RETRACTION_LABELS = {
    "RETRACTED_BY_ISSUER": "retracted by the issuer",
    "CORRECTED_BY_ISSUER": "corrected by the issuer",
    "WRONG_DOCUMENT": "the wrong document",
    "DUPLICATE_COPY": "a duplicate copy",
    "RIGHTS_WITHDRAWN": "no longer usable (rights withdrawn)",
}
CONTENT_STATE_LABELS = {
    "EXTRACTED": "extracted and retained",
    "UNAVAILABLE": "not available",
    "METADATA_ONLY": "not stored; this is a citation only",
}
EVALUATION_STATE_LABELS = {
    "TRIGGERED": "TRIGGERED",
    "NOT_TRIGGERED": "NOT TRIGGERED",
    "UNKNOWN": "UNKNOWN (evidence missing)",
    "CONFLICT": "CONFLICT (sources disagree)",
    "NONE": "none recorded",
}


def label(table: dict[str, str], value: str | None) -> str:
    return table.get(value or "", "UNKNOWN")


# =========================================================================================
# decision-review checklists (architecture section 7, character section 5)
# =========================================================================================

CHECKLIST_VERSION = "butler-checklist-1"

CHECKLISTS: dict[str, tuple[str, ...]] = {
    "RESEARCH_CHECKPOINT": (
        "NEXT_EVIDENCE_MILESTONE", "EVIDENCE_FOR_BELIEF", "WHAT_WOULD_CHANGE_YOUR_MIND",
        "SOURCE_COVERAGE_GAP",
    ),
    "THESIS_CHANGE": (
        "EVIDENCE_FOR_BELIEF", "ALTERNATIVE_EXPLANATION", "WHAT_WOULD_CHANGE_YOUR_MIND",
        "HORIZON_STILL_APPLIES",
    ),
    "POSITION_DECISION": (
        "EVIDENCE_FOR_BELIEF", "ALTERNATIVE_EXPLANATION", "WHAT_WOULD_CHANGE_YOUR_MIND",
        "HORIZON_STILL_APPLIES", "NEXT_EVIDENCE_MILESTONE", "SOURCE_COVERAGE_GAP",
    ),
}

#: Field-level states (character section 5). Status is derived from these, never from prose.
MISSING = "MISSING"
ANSWER_RECORDED = "ANSWER_RECORDED"
EVIDENCE_ATTACHED = "EVIDENCE_ATTACHED"
CHECKED_BY_CODE = "CHECKED_BY_CODE"
USER_ATTESTED = "USER_ATTESTED"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE_WITH_REASON = "NOT_APPLICABLE_WITH_REASON"
#: States that close a field. EVIDENCE_ATTACHED is deliberately NOT here: attaching evidence is
#: not evidence being sufficient, and only the person (attestation) or an approved code check
#: can close the field.
RESOLVED_STATES = frozenset({CHECKED_BY_CODE, USER_ATTESTED, NOT_APPLICABLE_WITH_REASON})

REVIEW_CHOICES = ("CONTINUE", "PAUSE", "REVISE", "MORE_INFORMATION")


def checklist_for(decision_kind: str, available: set[str]) -> tuple[str, ...]:
    """The applicable question ids, in priority order, dropping any whose premise is absent."""
    return tuple(q for q in CHECKLISTS[decision_kind] if question_allowed(q, available))


def field_state(
    question_id: str,
    response: dict[str, Any] | None,
    evidence_available: bool | None,
    check_passed: bool | None,
) -> str:
    """One review field's state from recorded facts only.

    ``evidence_available`` is None when nothing was attached, False when something attached is
    no longer available (retracted, forgotten, erased), True when all of it is. ``check_passed``
    is None when the question has no approved check or the check could not run.
    """
    if response is None:
        return MISSING
    if response.get("not_applicable_reason"):
        return NOT_APPLICABLE_WITH_REASON
    if evidence_available is False:
        return UNKNOWN
    if check_passed:
        return CHECKED_BY_CODE
    if response.get("attested"):
        return USER_ATTESTED
    if evidence_available:
        return EVIDENCE_ATTACHED
    return ANSWER_RECORDED


def follow_up_for(question_id: str, state: str, response: dict[str, Any] | None) -> str | None:
    """A fixed follow-up code for an answered-but-open field, or None."""
    template = question_template(question_id)
    if state == ANSWER_RECORDED and template.evidence_expected:
        return "ATTACH_EVIDENCE"
    if (state == ANSWER_RECORDED and template.code_check == "MILESTONE_DATE_AFTER_REVIEW"
            and response is not None and response.get("milestone_on") is None):
        return "MILESTONE_DATE"
    return None


def run_code_check(
    check_id: str | None, response: dict[str, Any] | None, review_on: date
) -> bool | None:
    """The approved deterministic checks. Returns None when no check applies or it cannot run.

    Code confirms a DATE comparison and nothing else: it never judges whether prose is sincere,
    sufficient or right (character section 5).
    """
    if check_id is None or response is None:
        return None
    if check_id == "MILESTONE_DATE_AFTER_REVIEW":
        milestone = response.get("milestone_on")
        if milestone is None:
            return None
        return milestone > review_on
    return None


# =========================================================================================
# presentation: timezone, quiet hours, digest, cooldown
# =========================================================================================


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def in_quiet_hours(moment: datetime, tz: ZoneInfo, start: time, end: time) -> bool:
    local = moment.astimezone(tz).time()
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def next_quiet_end(moment: datetime, tz: ZoneInfo, start: time, end: time) -> datetime:
    """The first instant at or after ``moment`` that is outside quiet hours."""
    if not in_quiet_hours(moment, tz, start, end):
        return moment
    local = moment.astimezone(tz)
    candidate = datetime.combine(local.date(), end, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), end, tzinfo=tz)
    return candidate.astimezone(UTC)


def local_day_bounds(moment: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = moment.astimezone(tz).date()
    start = datetime.combine(local, time(0, 0), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def deliver_after(
    now: datetime,
    priority: str,
    delivered_today: int,
    tz: ZoneInfo,
    quiet_start: time,
    quiet_end: time,
    digest_limit: int,
) -> datetime:
    """When an item becomes visible as NEW. Capture is immediate; presentation is budgeted.

    Quiet hours move every item to the quiet period's end. An INFORMATIONAL item beyond the
    day's digest limit moves to the next local day; a REQUIRED item (due explicit review,
    material rule change, correction) is never held back by the digest.
    """
    moment = next_quiet_end(now, tz, quiet_start, quiet_end)
    if priority == PRIORITY_INFORMATIONAL and delivered_today >= digest_limit:
        _, next_day = local_day_bounds(moment, tz)
        moment = next_quiet_end(next_day, tz, quiet_start, quiet_end)
    return moment


def checkpoint_due_on(anchor: date, every_days: int, today: date) -> date | None:
    """The most recent checkpoint date on or before ``today``, or None before the first.

    Only the latest missed occurrence is produced: no catch-up replay of every skipped one
    (architecture section 8), and the card says which window it covers.
    """
    if every_days <= 0 or today < anchor + timedelta(days=every_days):
        return None
    elapsed = (today - anchor).days // every_days
    return anchor + timedelta(days=elapsed * every_days)


def catalogue_manifest() -> dict[str, Any]:
    """The public, versioned catalogue -- what a client may display and a model may select."""
    return {
        "catalogue_version": CATALOGUE_VERSION,
        "rule_version": RULE_VERSION,
        "checklist_version": CHECKLIST_VERSION,
        "questions": [
            {"id": q.id, "version": q.version, "text": q.text,
             "requires": sorted(q.requires), "evidence_expected": q.evidence_expected,
             "code_check": q.code_check}
            for q in QUESTIONS.values()
        ],
        "lessons": [{"id": x.id, "version": x.version, "text": x.text,
                     "requires": sorted(x.requires), "provenance": x.provenance,
                     "forbidden_inferences": list(x.forbidden_inferences)}
                    for x in LESSONS.values()],
        "checklists": {k: list(v) for k, v in CHECKLISTS.items()},
        "defaults": {
            "quiet_start": DEFAULT_QUIET_START.isoformat(timespec="minutes"),
            "quiet_end": DEFAULT_QUIET_END.isoformat(timespec="minutes"),
            "digest_limit": DEFAULT_DIGEST_LIMIT,
            "checkpoint_days_offered": CHECKPOINT_DAYS_OFFERED,
            "timezone": None,
        },
    }
