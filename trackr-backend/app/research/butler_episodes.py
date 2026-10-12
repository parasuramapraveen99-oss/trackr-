"""Comparable episodes, counterexamples and change -- the deterministic half (BT 13).

docs/BUTLER_INVESTOR_MEMORY.md sections 3-6. PURE FUNCTIONS: typed rows in, episode and
revision proposals out. No SQL, no model, no network, no float. ``app/db/butler_episodes.py``
gathers the rows under the current consent and writes what this module returns.

WHAT AN EPISODE IS. One attributable opportunity, anchored to exactly one saved record, judged
against exactly one reviewed catalogue entry, using only what was recorded AT THE TIME: a review
is judged from the responses and evidence sealed when it was completed; a thesis change from the
evaluation recorded before the next version; a follow-up from saved activity up to its due date
plus a grace period. An opportunity code cannot decide stays UNDETERMINED rather than guessed.

WHAT IT IS NOT. Not a trait, not a motive, not a sincerity check, not an outcome grade. Profit
and loss are never read. Silence is not a lapse until a stated date and a grace period pass,
and even then the episode says OUTSIDE_WORK_NOT_RECORDED. A counterexample is recorded exactly as
carefully as a supporting episode, and IMPROVED_EVIDENCE_COVERAGE tracks improvement directly.

THE THRESHOLD IS COUNTED OVER DISTINCT ANCHORS. The database enforces one episode per (catalogue
entry, anchor), and the evaluator counts anchors, so copies of one review cannot become three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

from app.research import butler_notebook_policy as np
from app.research import butler_policy as bp

OUTSIDE = "OUTSIDE_WORK_NOT_RECORDED"
SELECTED = "SELECTED_REVIEWS_ONLY"


@dataclass(frozen=True)
class EpisodeDraft:
    catalogue_id: str
    polarity: str
    observation_code: str
    source_category: str
    comparability_key: str
    isin: str
    occurred_on: date
    knowable_at: datetime
    anchor_kind: str
    anchor_id: str
    review_id: str | None = None
    thesis_id: str | None = None
    prior_thesis_id: str | None = None
    response_id: str | None = None
    evaluation_id: str | None = None
    limitations: tuple[str, ...] = (OUTSIDE,)


# =========================================================================================
# reviews
# =========================================================================================


@dataclass
class ReviewFacts:
    """One COMPLETED review as it stood when completed. ``latest`` maps question id to its
    latest response (dict with id, answer, not_applicable_reason, milestone_on, evidence_count)
    among responses recorded before completion -- 051 refuses later ones anyway."""

    review_id: str
    isin: str
    decision_kind: str
    question_ids: tuple[str, ...]
    completed_at: datetime
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)


def _answered(r: dict[str, Any] | None) -> bool:
    return r is not None and bool((r.get("answer") or "").strip())


def _na(r: dict[str, Any] | None) -> bool:
    return r is not None and bool(r.get("not_applicable_reason"))


def review_episodes(rev: ReviewFacts) -> list[EpisodeDraft]:
    out: list[EpisodeDraft] = []
    on = rev.completed_at.astimezone(UTC).date()
    base = {"isin": rev.isin, "occurred_on": on, "knowable_at": rev.completed_at,
            "anchor_kind": "REVIEW", "anchor_id": rev.review_id, "review_id": rev.review_id,
            "source_category": "REVIEW_RESPONSES"}
    qs = set(rev.question_ids)

    if {"EVIDENCE_FOR_BELIEF", "ALTERNATIVE_EXPLANATION"} <= qs:
        sup = rev.latest.get("EVIDENCE_FOR_BELIEF")
        alt = rev.latest.get("ALTERNATIVE_EXPLANATION")
        s_ev = int(sup["evidence_count"]) if sup else 0
        a_ev = int(alt["evidence_count"]) if alt else 0
        if _na(alt) or _na(sup):
            pol, obs = "UNDETERMINED", "MARKED_NOT_APPLICABLE"
        elif a_ev > 0:
            pol, obs = "COUNTERS", "CONTRARY_EVIDENCE_ATTACHED"
        elif s_ev > 0:
            pol, obs = "SUPPORTS", "SUPPORTING_EVIDENCE_ONLY"
        else:
            pol, obs = "UNDETERMINED", "NO_EVIDENCE_EITHER_SIDE"
        out.append(EpisodeDraft("EVIDENCE_SELECTION_ASYMMETRY", pol, obs,
                                comparability_key=f"REVIEW:{rev.decision_kind}",
                                limitations=(OUTSIDE, SELECTED), **base))

    if "WHAT_WOULD_CHANGE_YOUR_MIND" in qs:
        r = rev.latest.get("WHAT_WOULD_CHANGE_YOUR_MIND")
        if _na(r):
            pol, obs = "UNDETERMINED", "MARKED_NOT_APPLICABLE"
        elif _answered(r):
            # A fluent answer counts as ANSWERED -- that is all code can say. It is not checked
            # for quality or sincerity; the review's field state stays ANSWER_RECORDED.
            pol, obs = "COUNTERS", "FALSIFIER_ANSWERED"
        else:
            pol, obs = "SUPPORTS", "FALSIFIER_LEFT_OPEN"
        out.append(EpisodeDraft("REPEATED_UNANSWERED_FALSIFICATION_QUESTION", pol, obs,
                                comparability_key="REVIEW:QUESTION:WHAT_WOULD_CHANGE_YOUR_MIND",
                                limitations=(OUTSIDE, SELECTED), **base))

    expected = [q for q in rev.question_ids
                if q in bp.QUESTIONS and bp.QUESTIONS[q].evidence_expected]
    required = [q for q in expected if not _na(rev.latest.get(q))]
    if required:
        covered = all(rev.latest.get(q) and int(rev.latest[q]["evidence_count"]) > 0
                      for q in required)
        out.append(EpisodeDraft(
            "IMPROVED_EVIDENCE_COVERAGE",
            "SUPPORTS" if covered else "COUNTERS",
            "ALL_EVIDENCE_ATTACHED" if covered else "EVIDENCE_MISSING_FOR_SOME",
            comparability_key="REVIEW:EVIDENCE_EXPECTED",
            limitations=(OUTSIDE, SELECTED), **base))
    return out


@dataclass
class MilestoneFacts:
    """A dated next-evidence milestone from a completed review, plus the saved activity on the
    same company family between completion and the milestone date + grace."""

    response_id: str
    review_id: str
    isin: str
    completed_at: datetime
    milestone_on: date
    activity_count: int
    notes_consented: bool


def milestone_episode(m: MilestoneFacts, today: date) -> EpisodeDraft | None:
    """None until the milestone date plus grace has passed: before then, absence is not an
    observation at all. A delayed filing or a long horizon is covered by the grace period and
    by the always-listed alternatives, never by a guess."""
    due = m.milestone_on + np.FOLLOW_THROUGH_GRACE
    if today <= due:
        return None
    limitations: tuple[str, ...] = (OUTSIDE, "OPPORTUNITIES_INCOMPLETE")
    if m.activity_count > 0:
        pol, obs = "COUNTERS", "FOLLOW_UP_RECORDED"
    elif not m.notes_consented:
        # Notes are not an included source, so an absence of saved activity is not observable.
        pol, obs = "UNDETERMINED", "NOTES_NOT_INCLUDED"
    else:
        pol, obs = "SUPPORTS", "NO_SAVED_FOLLOW_UP"
    # The observation exists from the due date (milestone + grace), not from the promised date:
    # that is when "nothing saved yet" first became knowable.
    return EpisodeDraft(
        "OVERDUE_FOLLOW_THROUGH", pol, obs, "REVIEW_RESPONSES", f"MILESTONE:{m.isin}",
        m.isin, due, datetime.combine(due, time.min, tzinfo=UTC), "RESPONSE",
        m.response_id, review_id=m.review_id, response_id=m.response_id,
        limitations=limitations)


# =========================================================================================
# thesis versions
# =========================================================================================


@dataclass
class ThesisPair:
    """Consecutive versions of one company's thesis, with what was recorded between them."""

    isin: str
    prior_id: str
    next_id: str
    next_version_on: date
    next_recorded_at: datetime
    prior_criteria: Any
    next_criteria: Any
    prior_horizon: str
    next_horizon: str
    #: A TRIGGERED evaluation of the prior version recorded before the next version, if any.
    triggered_evaluation_id: str | None
    #: Evidence count on the EVIDENCE_FOR_BELIEF answer of a completed THESIS_CHANGE review
    #: linked to either version and completed before the next version; None when no such review.
    linked_review_evidence: int | None
    linked_review_id: str | None


_HORIZON = re.compile(r"^\s*(\d{1,3})\s*(years?|yrs?|months?|mos?|weeks?|wks?)\b", re.I)


def horizon_weeks(text: str) -> int | None:
    """Weeks for a plainly stated horizon ("3 years", "18 months"), else None. Deliberately
    narrow: anything else is free text code will not interpret."""
    m = _HORIZON.match(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("y"):
        return n * 52
    if unit.startswith("m"):
        return n * 52 // 12
    return n


def thesis_episodes(p: ThesisPair) -> list[EpisodeDraft]:
    out: list[EpisodeDraft] = []
    base = {"isin": p.isin, "occurred_on": p.next_version_on, "knowable_at": p.next_recorded_at,
            "anchor_kind": "THESIS", "anchor_id": p.next_id, "thesis_id": p.next_id,
            "prior_thesis_id": p.prior_id, "source_category": "DECISION_RECEIPTS",
            "comparability_key": f"THESIS:{p.isin}"}

    if p.triggered_evaluation_id is not None:
        changed = p.prior_criteria != p.next_criteria
        limitations = [OUTSIDE]
        if not changed:
            pol, obs = "COUNTERS", "CRITERION_KEPT"
        elif p.linked_review_evidence and p.linked_review_evidence > 0:
            # A change WITH attached evidence is the justified-revision reading, not the pattern.
            pol, obs = "COUNTERS", "CHANGED_WITH_EVIDENCE"
        else:
            pol, obs = "SUPPORTS", "CHANGED_WITHOUT_LINKED_EVIDENCE"
            if p.linked_review_id is None:
                limitations.append("NO_LINKED_REVIEW")
        out.append(EpisodeDraft("CRITERION_CHANGED_AFTER_COUNTEREVIDENCE", pol, obs,
                                evaluation_id=p.triggered_evaluation_id,
                                review_id=p.linked_review_id,
                                limitations=tuple(limitations), **base))

    if (p.prior_horizon or "").strip() or (p.next_horizon or "").strip():
        a, b = horizon_weeks(p.prior_horizon), horizon_weeks(p.next_horizon)
        if a is None or b is None:
            pol, obs, lim = "UNDETERMINED", "HORIZON_NOT_COMPARABLE", (
                OUTSIDE, "HORIZON_NOT_MACHINE_READABLE")
        elif b < a:
            pol, obs, lim = "SUPPORTS", "HORIZON_SHORTENED", (OUTSIDE,)
        else:
            pol, obs, lim = "COUNTERS", "HORIZON_KEPT_OR_LENGTHENED", (OUTSIDE,)
        out.append(EpisodeDraft("SHORTENED_STATED_HORIZON", pol, obs, limitations=lim, **base))
    return out


# =========================================================================================
# pattern evaluation -- counts and status are code's, always
# =========================================================================================


@dataclass(frozen=True)
class RevisionDraft:
    catalogue_id: str
    status: str
    reason_code: str
    supporting_ids: tuple[str, ...]
    counter_ids: tuple[str, ...]
    eligible: int
    undetermined: int
    distinct_dates: int
    denominator_known: bool
    window_start: date
    window_end: date
    alternatives: tuple[str, ...]
    questions: tuple[str, ...]
    limitations: tuple[str, ...]

    def signature(self) -> tuple:
        return (self.status, self.reason_code, tuple(sorted(self.supporting_ids)),
                tuple(sorted(self.counter_ids)), self.eligible, self.undetermined,
                self.distinct_dates, self.window_start, self.window_end,
                tuple(self.alternatives), tuple(self.questions), tuple(self.limitations))


def evaluate(catalogue_id: str, episodes: list[dict[str, Any]], today: date,
             existed_before: bool) -> RevisionDraft | None:
    """One pattern revision from the CURRENT, visible, uncorrected episodes of one catalogue
    entry within the evaluation window. None when there is nothing to say and no prior pattern.

    Status (provenance, not confidence):
      REPEATED_IN_RECORDS  >= 3 distinct supporting anchors over >= 2 dates, AND supporting
                           outnumber counterexamples, AND the newest episode is <= 90 days old
      STALE                had episodes, but none in the last 90 days
      RETIRED              a prior pattern now has no supporting episode at all
      TENTATIVE            anything else with at least one supporting episode
    """
    entry = np.CATALOGUE[catalogue_id]
    window_from = today - np.EVALUATION_WINDOW
    eps = [e for e in episodes
           if e["catalogue_id"] == catalogue_id and not e["corrected"]
           and e["occurred_on"] >= window_from]
    sup = [e for e in eps if e["polarity"] == "SUPPORTS"]
    cnt = [e for e in eps if e["polarity"] == "COUNTERS"]
    und = [e for e in eps if e["polarity"] == "UNDETERMINED"]
    if not sup and not existed_before:
        return None
    # Distinct anchors, never rows.
    sup_anchor = {str(e["anchor_id"]): e for e in sup}
    cnt_anchor = {str(e["anchor_id"]): e for e in cnt}
    sup_ids = tuple(sorted(str(e["id"]) for e in sup_anchor.values()))
    cnt_ids = tuple(sorted(str(e["id"]) for e in cnt_anchor.values()))
    sup_dates = {e["occurred_on"] for e in sup_anchor.values()}
    cited_dates = sup_dates | {e["occurred_on"] for e in cnt_anchor.values()}
    all_dates = [e["occurred_on"] for e in eps]
    newest = max(all_dates) if all_dates else today
    oldest = min(all_dates) if all_dates else today
    denominator_known = catalogue_id != "OVERDUE_FOLLOW_THROUGH"
    limitations = [OUTSIDE]
    if catalogue_id != "CRITERION_CHANGED_AFTER_COUNTEREVIDENCE" and \
            catalogue_id != "SHORTENED_STATED_HORIZON":
        limitations.append(SELECTED)
    if not denominator_known:
        limitations.append("OPPORTUNITIES_INCOMPLETE")
    if len(sup_anchor) < np.MIN_REPEATED_EPISODES:
        limitations.append("FEW_EPISODES")

    if not sup_anchor:
        status, reason = "RETIRED", "NO_SUPPORTING_EPISODES"
    elif today - newest > np.RELEVANCE_WINDOW:
        status, reason = "STALE", "NO_FRESH_EPISODE"
    elif (len(sup_anchor) >= np.MIN_REPEATED_EPISODES and len(sup_dates) >= np.MIN_REPEATED_DATES
          and len(sup_anchor) > len(cnt_anchor)):
        status, reason = "REPEATED_IN_RECORDS", "THRESHOLD_MET"
    elif len(sup_anchor) >= np.MIN_REPEATED_EPISODES and len(sup_anchor) <= len(cnt_anchor):
        status, reason = "TENTATIVE", "COUNTEREXAMPLES_NOT_OUTNUMBERED"
    else:
        status, reason = "TENTATIVE", "BELOW_THRESHOLD"
    return RevisionDraft(
        catalogue_id, status, reason, sup_ids, cnt_ids,
        eligible=len(eps), undetermined=len(und), distinct_dates=len(cited_dates),
        denominator_known=denominator_known, window_start=min(oldest, newest),
        window_end=newest, alternatives=entry.alternatives, questions=entry.questions,
        limitations=tuple(limitations))
