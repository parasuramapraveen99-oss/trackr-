"""The reviewed notebook catalogue (BT 12, docs/BUTLER_INVESTOR_MEMORY.md sections 2-5).

EVERYTHING A NOTEBOOK RECORD CAN SAY IS DEFINED HERE, AS REVIEWED TEXT WITH AN ID. A pattern,
an alternative explanation, a limitation, a correction and a coaching question are identifiers
into these tables; the words shown to the person are rendered from them at read time. A model
may choose ids (BT 14); it cannot add an entry, and an unknown id is rejected whole. Expanding
this catalogue is a reviewed code change with tests, never a model invention.

WHAT THE CATALOGUE DESCRIBES: recorded investment-research PROCESS -- what happened to a stated
criterion after contrary evidence was attached, whether a promised follow-up has a saved record,
whether a falsification question was answered. It never labels the person: no "dishonest",
"emotional", "impulsive", "gambler", no motive, no diagnosis, no sensitive trait. Every entry's
text is phrased as an observation about records and carries its counter-reading.

THE THRESHOLDS ARE PRODUCT HEURISTICS, NOT PSYCHOLOGY. Three distinct comparable episodes across
two review dates before a pattern may be called repeated; 90 days without a fresh eligible
episode before it is stale. Neither is a validated cutoff, and the UI says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

#: The disclosure the person accepted. A new wording is a new version and a new opt-in.
DISCLOSURE_VERSION = "butler-notebook-disclosure-1"
DISCLOSURE_TEXT = (
    "Butler can learn from the research conversations, saved decisions and follow-through you "
    "choose to include. It will keep private observations about your research habits to "
    "improve its questions. These observations can be wrong. You can inspect, correct, pause "
    "or delete them at any time. Company reports do not include these notes."
)
#: Said next to revocation, because it is true and easy to leave out.
REVOCATION_LIMIT_TEXT = (
    "Turning learning off stops new analysis immediately and discards results still in "
    "progress. It cannot recall anything already sent to a model provider during an earlier "
    "reflection you allowed."
)

#: Source categories a person may include. Portfolio trades/performance are their own category
#: and never implied by any other. No browsing, WhatsApp, social profile, keystroke or unsent
#: draft category exists -- there is nothing to opt into.
SOURCE_CATEGORIES: dict[str, str] = {
    "RESEARCH_NOTES": "Saved research notes",
    "REVIEW_RESPONSES": "Submitted decision-review answers",
    "BUTLER_CONVERSATION": "Saved Butler conversation turns",
    "DECISION_RECEIPTS": "Explicit decision and outcome records (thesis versions, review choices)",
    "PORTFOLIO_LINKED_DECISIONS": "Portfolio trades you explicitly link to a decision",
}

BUILDER_VERSION = "notebook-builder-1"
RULE_VERSION = "notebook-rules-1"

#: Product heuristics (section 4), named so the UI can state them.
MIN_REPEATED_EPISODES = 3
MIN_REPEATED_DATES = 2
RELEVANCE_WINDOW = timedelta(days=90)
#: How long a promised milestone is allowed to run over before its absence is even an episode.
#: A long horizon or a delayed filing is normal; this is a grace period, not a deadline.
FOLLOW_THROUGH_GRACE = timedelta(days=30)
#: Episodes looked at when evaluating a pattern. A window, not a retention period.
EVALUATION_WINDOW = timedelta(days=365)

POLARITIES = ("SUPPORTS", "COUNTERS", "UNDETERMINED")
STATUSES = ("TENTATIVE", "REPEATED_IN_RECORDS", "DISPUTED_BY_USER",
            "SUSPENDED_PENDING_RECHECK", "STALE", "RETIRED")
#: Statuses in which a pattern may shape which question is offered. DISPUTED, SUSPENDED,
#: STALE and RETIRED never do; a disabled or deleted pattern never does either.
COACHING_STATUSES = frozenset({"REPEATED_IN_RECORDS"})


@dataclass(frozen=True)
class PatternEntry:
    id: str
    version: str
    #: The reviewed observation text. Speaks about records, never about the person's mind.
    text: str
    #: What counts as an eligible opportunity (the denominator), in words the UI shows.
    eligible: str
    #: How episodes are compared -- the comparability key's meaning.
    comparison: str
    #: The consent category an episode of this kind reads.
    source_category: str
    #: Reviewed alternative explanations always shown with it.
    alternatives: tuple[str, ...]
    #: Reviewed questions this pattern can make applicable.
    questions: tuple[str, ...]
    #: True for an improvement pattern: tracked as carefully as a difficulty.
    positive: bool = False


@dataclass(frozen=True)
class Alternative:
    id: str
    text: str


@dataclass(frozen=True)
class NotebookQuestion:
    id: str
    version: str
    text: str
    #: What satisfies it. ANSWER means a written answer may be recorded; it never becomes a
    #: code check. EVIDENCE means only attached evidence moves the field past ANSWER_RECORDED.
    satisfied_by: str


ALTERNATIVES: dict[str, Alternative] = {a.id: a for a in (
    Alternative("NEW_INFORMATION_JUSTIFIED_REVISION",
                "New information can justify changing a criterion; changing it is not a fault."),
    Alternative("CIRCUMSTANCES_CHANGED",
                "The company or the investor's circumstances may have changed in ways the "
                "records do not show."),
    Alternative("OUTSIDE_WORK_NOT_RECORDED",
                "Research done outside this workspace is not visible to Butler."),
    Alternative("LONG_HORIZON_INTENDED",
                "A long intended horizon can make a quiet period expected rather than a lapse."),
    Alternative("SOURCE_DELAYED",
                "The expected filing or evidence may simply not have been published yet."),
    Alternative("SMALL_SAMPLE",
                "Few comparable reviews exist, so this could be coincidence."),
    Alternative("QUESTION_NOT_USEFUL_HERE",
                "The question may not have been useful for this particular decision."),
)}

LIMITATIONS: dict[str, str] = {
    "OUTSIDE_WORK_NOT_RECORDED": "Only work saved in this workspace is observed.",
    "SELECTED_REVIEWS_ONLY": "Only reviews the investor chose to run are observed.",
    "HORIZON_NOT_MACHINE_READABLE": "The stated horizon is free text code could not compare.",
    "NO_LINKED_REVIEW": "No decision review was linked to this change.",
    "OPPORTUNITIES_INCOMPLETE": "Not every comparable opportunity is recorded, so no rate is "
                                "stated.",
    "SOURCE_RETRACTED": "A cited source was later retracted.",
    "FEW_EPISODES": "Fewer than the minimum comparable episodes exist.",
}

QUESTIONS: dict[str, NotebookQuestion] = {q.id: q for q in (
    NotebookQuestion("WHAT_EVIDENCE_CHANGED_THE_CRITERION", "1",
                     "What evidence justifies changing this criterion? Attach it, or record "
                     "that the change rests on judgement alone.", "EVIDENCE"),
    NotebookQuestion("WHAT_CONTRARY_EVIDENCE_DID_YOU_CHECK", "1",
                     "Which saved source could count against this view? Attach one, or record "
                     "that none is saved yet.", "EVIDENCE"),
    NotebookQuestion("IS_THE_FOLLOW_UP_STILL_PLANNED", "1",
                     "An earlier review named a next piece of evidence. Is it still expected, "
                     "and by when? Changed plans are a valid answer.", "ANSWER"),
    NotebookQuestion("WHAT_WOULD_FALSIFY_THIS_NOW", "1",
                     "What future observation would show this view is wrong?", "ANSWER"),
    NotebookQuestion("DOES_THE_ORIGINAL_HORIZON_STILL_APPLY", "1",
                     "Your earlier stated horizon is quoted. Does it still apply, and if not, "
                     "what changed?", "ANSWER"),
    NotebookQuestion("KEEP_THE_EVIDENCE_HABIT", "1",
                     "Recent reviews attached evidence for each check. Which source would you "
                     "attach here?", "EVIDENCE"),
)}

CATALOGUE: dict[str, PatternEntry] = {p.id: p for p in (
    PatternEntry(
        "CRITERION_CHANGED_AFTER_COUNTEREVIDENCE", "1",
        "In saved records, a stated criterion changed after a recorded evaluation of it was "
        "triggered, without evidence attached to a linked review.",
        "Each thesis version whose recorded evaluation was triggered before the next version.",
        "Same company thesis series; consecutive versions.",
        "DECISION_RECEIPTS",
        ("NEW_INFORMATION_JUSTIFIED_REVISION", "CIRCUMSTANCES_CHANGED", "SMALL_SAMPLE"),
        ("WHAT_EVIDENCE_CHANGED_THE_CRITERION",),
    ),
    PatternEntry(
        "EVIDENCE_SELECTION_ASYMMETRY", "1",
        "In saved reviews, evidence was attached for the supporting question but not for the "
        "alternative-explanation question.",
        "Each completed review containing both the supporting-evidence and alternative "
        "questions.",
        "Completed reviews of the same decision kind.",
        "REVIEW_RESPONSES",
        ("QUESTION_NOT_USEFUL_HERE", "OUTSIDE_WORK_NOT_RECORDED", "SMALL_SAMPLE"),
        ("WHAT_CONTRARY_EVIDENCE_DID_YOU_CHECK",),
    ),
    PatternEntry(
        "OVERDUE_FOLLOW_THROUGH", "1",
        "A review named a dated next piece of evidence, and no saved note, source or review for "
        "that company exists between the review and the date plus a grace period.",
        "Each dated milestone whose date plus grace has passed.",
        "Milestones from completed reviews of the same company.",
        "REVIEW_RESPONSES",
        ("OUTSIDE_WORK_NOT_RECORDED", "SOURCE_DELAYED", "LONG_HORIZON_INTENDED",
         "CIRCUMSTANCES_CHANGED"),
        ("IS_THE_FOLLOW_UP_STILL_PLANNED",),
    ),
    PatternEntry(
        "REPEATED_UNANSWERED_FALSIFICATION_QUESTION", "1",
        "Reviews were completed with the 'what would change your mind' question left open.",
        "Each completed review that asked the question.",
        "Completed reviews that asked the same question.",
        "REVIEW_RESPONSES",
        ("QUESTION_NOT_USEFUL_HERE", "SMALL_SAMPLE"),
        ("WHAT_WOULD_FALSIFY_THIS_NOW",),
    ),
    PatternEntry(
        "SHORTENED_STATED_HORIZON", "1",
        "A new thesis version stated a shorter intended holding period than the version before.",
        "Each thesis version after the first whose horizon code could compare.",
        "Same company thesis series; consecutive versions.",
        "DECISION_RECEIPTS",
        ("CIRCUMSTANCES_CHANGED", "NEW_INFORMATION_JUSTIFIED_REVISION", "SMALL_SAMPLE"),
        ("DOES_THE_ORIGINAL_HORIZON_STILL_APPLY",),
    ),
    PatternEntry(
        "IMPROVED_EVIDENCE_COVERAGE", "1",
        "Completed reviews attached evidence to every question that expects it.",
        "Each completed review with at least one evidence-expecting question.",
        "Completed reviews of any decision kind.",
        "REVIEW_RESPONSES",
        ("SMALL_SAMPLE",),
        ("KEEP_THE_EVIDENCE_HABIT",),
        positive=True,
    ),
)}

#: Which decision reviews a pattern's question is useful in. A question about criterion changes
#: belongs where criteria are being changed; the evidence-habit question fits any review.
_ALL_DECISIONS = ("RESEARCH_CHECKPOINT", "THESIS_CHANGE", "POSITION_DECISION")
DECISION_KINDS: dict[str, tuple[str, ...]] = {
    "CRITERION_CHANGED_AFTER_COUNTEREVIDENCE": ("THESIS_CHANGE", "POSITION_DECISION"),
    "EVIDENCE_SELECTION_ASYMMETRY": _ALL_DECISIONS,
    "OVERDUE_FOLLOW_THROUGH": ("RESEARCH_CHECKPOINT", "POSITION_DECISION"),
    "REPEATED_UNANSWERED_FALSIFICATION_QUESTION": _ALL_DECISIONS,
    "SHORTENED_STATED_HORIZON": ("THESIS_CHANGE", "POSITION_DECISION"),
    "IMPROVED_EVIDENCE_COVERAGE": _ALL_DECISIONS,
}
MAX_PERSONAL_QUESTIONS_PER_REVIEW = 1

#: Model abstention codes (docs/BUTLER_REFLECTION_PROMPT.md). An abstention is a valid answer
#: and leaves deterministic behaviour untouched.
ABSTAIN_CODES = ("INSUFFICIENT_EVIDENCE", "NO_COMPARABLE_EPISODES", "COUNTEREXAMPLES_DOMINATE",
                 "OUT_OF_SCOPE")

#: Correction codes a person may give for an episode.
CORRECTION_CODES = ("WRONG_RECORD", "NOT_COMPARABLE", "CIRCUMSTANCES_CHANGED",
                    "OUTSIDE_WORK_NOT_RECORDED", "LONG_HORIZON_INTENDED", "OTHER")

_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

#: Words that would turn a process observation into a statement about the person. Used only to
#: refuse catalogue edits and self-checks (the catalogue is the only prose); model output is
#: ids and never reaches this.
_DIAGNOSTIC = re.compile(
    r"\b(dishonest|liar|lying|rationaliz|rationalis|emotional|impulsive|gambler|addict|"
    r"anxious|depress|disorder|narciss|bias(ed)? person|irrational|defensive|untrustworth)\w*",
    re.IGNORECASE)


def known(kind: str, value: str) -> bool:
    table = {"pattern": CATALOGUE, "alternative": ALTERNATIVES, "question": QUESTIONS,
             "limitation": LIMITATIONS, "abstain": dict.fromkeys(ABSTAIN_CODES)}[kind]
    return isinstance(value, str) and bool(_ID.match(value)) and value in table


def assert_catalogue_is_process_language() -> None:
    """Import-time self-check: no reviewed text labels a person. A catalogue edit that did would
    fail at import, before it could ever be shown."""
    texts = [DISCLOSURE_TEXT, REVOCATION_LIMIT_TEXT]
    texts += [p.text for p in CATALOGUE.values()] + [a.text for a in ALTERNATIVES.values()]
    texts += [q.text for q in QUESTIONS.values()] + list(LIMITATIONS.values())
    for text in texts:
        if _DIAGNOSTIC.search(text):
            raise RuntimeError(f"notebook catalogue text labels a person: {text!r}")
    for p in CATALOGUE.values():
        if p.source_category not in SOURCE_CATEGORIES:
            raise RuntimeError(f"{p.id}: unknown source category")
        for a in p.alternatives:
            if a not in ALTERNATIVES:
                raise RuntimeError(f"{p.id}: unknown alternative {a}")
        for q in p.questions:
            if q not in QUESTIONS:
                raise RuntimeError(f"{p.id}: unknown question {q}")


assert_catalogue_is_process_language()


def manifest() -> dict[str, Any]:
    """The catalogue as the UI shows it, so every id on screen can be explained."""
    return {
        "disclosure_version": DISCLOSURE_VERSION,
        "disclosure_text": DISCLOSURE_TEXT,
        "revocation_limit_text": REVOCATION_LIMIT_TEXT,
        "source_categories": [{"id": k, "label": v} for k, v in SOURCE_CATEGORIES.items()],
        "patterns": [{"id": p.id, "version": p.version, "text": p.text, "eligible": p.eligible,
                      "comparison": p.comparison, "source_category": p.source_category,
                      "alternatives": list(p.alternatives), "questions": list(p.questions),
                      "positive": p.positive} for p in CATALOGUE.values()],
        "alternatives": [{"id": a.id, "text": a.text} for a in ALTERNATIVES.values()],
        "questions": [{"id": q.id, "version": q.version, "text": q.text,
                       "satisfied_by": q.satisfied_by} for q in QUESTIONS.values()],
        "limitations": [{"id": k, "text": v} for k, v in LIMITATIONS.items()],
        "heuristics": {
            "min_repeated_episodes": MIN_REPEATED_EPISODES,
            "min_repeated_dates": MIN_REPEATED_DATES,
            "stale_after_days": RELEVANCE_WINDOW.days,
            "follow_through_grace_days": FOLLOW_THROUGH_GRACE.days,
            "note": "Product heuristics, not validated psychological cutoffs. The stale rule "
                    "stops assertive coaching; it is not a retention or deletion period.",
        },
        "rule_version": RULE_VERSION,
        "builder_version": BUILDER_VERSION,
    }
