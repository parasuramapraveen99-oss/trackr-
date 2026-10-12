"""Gather, write and evaluate notebook episodes (BT 13), as the ENGINE capability.

``app/research/butler_episodes.py`` decides; this module reads the investor's saved records
under the CURRENT consent, writes new episodes idempotently and appends a pattern revision only
when the deterministic evaluation actually changed. Called from the existing Butler worker pass
(``app/jobs/butler.py``) -- not a second scheduler -- and from the reflection job before it
seals a packet.

CONSENT IS APPLIED BEFORE READING, NOT AFTER. A category the person did not include is never
queried; records before the effective include-from date, before an ALL_BEFORE fence, or whose
anchor was excluded are filtered in SQL. The database re-checks consent at insert (053), so a
revocation that lands mid-pass makes the remaining writes fail rather than land.

NOTHING HIDDEN IS REPLAYED. An excluded anchor never becomes an episode again; a deleted
pattern's catalogue/scope is never revised again; a new builder version supersedes an episode
under the same anchor rather than adding a countable copy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import psycopg

from app.db import butler_notebook as bn
from app.db import workspace as ws
from app.research import butler_episodes as be
from app.research import butler_notebook_policy as np

_rows = ws._rows
_one = ws._one


def _reviews(conn: Any, inv: str, since: date, fence: datetime | None) -> list[be.ReviewFacts]:
    rows = _rows(conn, """
        SELECT r.id, r.isin, r.decision_kind, r.question_ids, c.recorded_at AS completed_at
          FROM butler_review r
          JOIN butler_review_completion c ON c.investor_id = r.investor_id AND c.review_id = r.id
         WHERE r.investor_id = %(inv)s
           AND (c.recorded_at AT TIME ZONE 'UTC')::date >= %(since)s
           AND (%(fence)s::timestamptz IS NULL OR c.recorded_at > %(fence)s::timestamptz)
           AND NOT EXISTS (SELECT 1 FROM butler_review s WHERE s.investor_id = r.investor_id
                           AND s.supersedes_review_id = r.id)
         ORDER BY c.recorded_at
        """, {"inv": inv, "since": since, "fence": fence})
    if not rows:
        return []
    ids = [str(r["id"]) for r in rows]
    latest: dict[str, dict[str, dict[str, Any]]] = {}
    for r in _rows(conn, """
            SELECT DISTINCT ON (rr.review_id, rr.question_id)
                   rr.id, rr.review_id, rr.question_id, rr.answer, rr.not_applicable_reason,
                   rr.milestone_on, rr.recorded_at,
                   (SELECT count(*) FROM butler_review_evidence ev
                     WHERE ev.investor_id = rr.investor_id AND ev.response_id = rr.id)
                     AS evidence_count
              FROM butler_review_response rr
             WHERE rr.investor_id = %s AND rr.review_id = ANY(%s::uuid[])
             ORDER BY rr.review_id, rr.question_id, rr.seq DESC
            """, (inv, ids)):
        latest.setdefault(str(r["review_id"]), {})[r["question_id"]] = r
    return [be.ReviewFacts(str(r["id"]), r["isin"], r["decision_kind"],
                           tuple(r["question_ids"]), r["completed_at"],
                           latest.get(str(r["id"]), {})) for r in rows]


def _activity(conn: Any, inv: str, isin: str, start: datetime, end: date,
              notes: bool) -> int:
    """Saved records on the company family between completion and the due date. Existence and
    timestamps only -- no content is read. Notes count only if notes are an included source."""
    family = ws.isin_family(conn, isin)
    end_ts = datetime.combine(end, datetime.max.time(), tzinfo=UTC)
    row = _one(conn, """
        SELECT (SELECT count(*) FROM research_source WHERE investor_id = %(inv)s
                  AND isin = ANY(%(fam)s) AND recorded_at > %(s)s AND recorded_at <= %(e)s)
             + (SELECT count(*) FROM butler_review WHERE investor_id = %(inv)s
                  AND isin = ANY(%(fam)s) AND recorded_at > %(s)s AND recorded_at <= %(e)s)
             + CASE WHEN %(notes)s THEN (SELECT count(*) FROM note WHERE investor_id = %(inv)s
                  AND isin = ANY(%(fam)s) AND recorded_at > %(s)s AND recorded_at <= %(e)s)
               ELSE 0 END AS n
        """, {"inv": inv, "fam": family, "s": start, "e": end_ts, "notes": notes})
    return int(row["n"]) if row else 0


def _thesis_pairs(conn: Any, inv: str, since: date, fence: datetime | None
                  ) -> list[be.ThesisPair]:
    rows = _rows(conn, """
        SELECT id, isin, version, version_on, recorded_at, variables, exits,
               intended_holding_period
          FROM thesis WHERE investor_id = %s ORDER BY isin, version
        """, (inv,))
    pairs: list[be.ThesisPair] = []
    for prev, nxt in zip(rows, rows[1:], strict=False):
        if prev["isin"] != nxt["isin"] or nxt["version_on"] < since:
            continue
        if fence is not None and nxt["recorded_at"] <= fence:
            continue
        ev = _one(conn, """
            SELECT id FROM trigger_evaluation
             WHERE investor_id = %s AND thesis_id = %s AND state = 'TRIGGERED'
               AND evaluated_at < %s
             ORDER BY evaluated_at DESC LIMIT 1
            """, (inv, prev["id"], nxt["recorded_at"]))
        review = _one(conn, """
            SELECT r.id,
                   (SELECT count(*) FROM butler_review_response rr
                      JOIN butler_review_evidence e ON e.investor_id = rr.investor_id
                                                   AND e.response_id = rr.id
                     WHERE rr.investor_id = r.investor_id AND rr.review_id = r.id
                       AND rr.question_id = 'EVIDENCE_FOR_BELIEF') AS evidence
              FROM butler_review r
              JOIN butler_review_completion c ON c.investor_id = r.investor_id
                                             AND c.review_id = r.id
             WHERE r.investor_id = %s AND r.decision_kind = 'THESIS_CHANGE'
               AND r.thesis_id = ANY(%s::uuid[]) AND c.recorded_at <= %s
             ORDER BY c.recorded_at DESC LIMIT 1
            """, (inv, [prev["id"], nxt["id"]], nxt["recorded_at"]))
        pairs.append(be.ThesisPair(
            nxt["isin"], str(prev["id"]), str(nxt["id"]), nxt["version_on"],
            nxt["recorded_at"], [prev["variables"], prev["exits"]],
            [nxt["variables"], nxt["exits"]], prev["intended_holding_period"],
            nxt["intended_holding_period"], str(ev["id"]) if ev else None,
            int(review["evidence"]) if review else None,
            str(review["id"]) if review else None))
    return pairs


def draft_episodes(conn: Any, inv: str, consent: dict[str, Any], today: date
                   ) -> list[be.EpisodeDraft]:
    """Every episode the current consent permits, before de-duplication against stored ones."""
    since = consent["effective_include_from"]
    fence = bn._fence(conn, inv)
    drafts: list[be.EpisodeDraft] = []
    if "REVIEW_RESPONSES" in consent["sources"]:
        notes = "RESEARCH_NOTES" in consent["sources"]
        for rev in _reviews(conn, inv, since, fence):
            drafts.extend(be.review_episodes(rev))
            m = rev.latest.get("NEXT_EVIDENCE_MILESTONE")
            if m and m.get("milestone_on") and not m.get("not_applicable_reason"):
                due = m["milestone_on"] + np.FOLLOW_THROUGH_GRACE
                if today > due:
                    facts = be.MilestoneFacts(
                        str(m["id"]), rev.review_id, rev.isin, rev.completed_at,
                        m["milestone_on"],
                        _activity(conn, inv, rev.isin, rev.completed_at, due, notes), notes)
                    ep = be.milestone_episode(facts, today)
                    if ep is not None:
                        drafts.append(ep)
    if "DECISION_RECEIPTS" in consent["sources"]:
        for pair in _thesis_pairs(conn, inv, since, fence):
            drafts.extend(be.thesis_episodes(pair))
    excluded = bn.excluded_anchors(conn, inv)
    return [d for d in drafts if d.anchor_id not in excluded
            and consent_allows_draft(consent, d)]


def consent_allows_draft(consent: dict[str, Any], d: be.EpisodeDraft) -> bool:
    return bn.consent_allows(consent, d.source_category, d.occurred_on)


def write_episodes(conn: Any, inv: str, consent: dict[str, Any], drafts: list[be.EpisodeDraft]
                   ) -> int:
    """Insert episodes whose (catalogue, anchor) is not yet stored. Idempotent: a rerun, a
    duplicate tick or a second worker finds them stored (or loses the UNIQUE race)."""
    stored = {(r["catalogue_id"], str(r["anchor_id"])) for r in _rows(
        conn, "SELECT catalogue_id, anchor_id FROM butler_episode WHERE investor_id = %s",
        (inv,))}
    written = 0
    for d in drafts:
        if (d.catalogue_id, d.anchor_id) in stored:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO butler_episode (investor_id, catalogue_id, polarity,
                    observation_code, source_category, comparability_key, isin, occurred_on,
                    knowable_at, anchor_kind, anchor_id, review_id, thesis_id, prior_thesis_id,
                    response_id, evaluation_id, limitations, builder_version, consent_version,
                    dedupe_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (inv, d.catalogue_id, d.polarity, d.observation_code, d.source_category,
                 d.comparability_key, d.isin, d.occurred_on, d.knowable_at, d.anchor_kind,
                 d.anchor_id, d.review_id, d.thesis_id, d.prior_thesis_id, d.response_id,
                 d.evaluation_id, list(d.limitations), np.BUILDER_VERSION,
                 consent["version"],
                 f"notebook.episode:{d.catalogue_id}:{d.anchor_id}:{np.BUILDER_VERSION}"),
            )
        stored.add((d.catalogue_id, d.anchor_id))
        written += 1
    return written


def _pattern_id(conn: Any, inv: str, catalogue_id: str, scope: str) -> str | None:
    row = _one(conn, "SELECT id FROM butler_pattern WHERE investor_id = %s AND catalogue_id = %s"
                     " AND scope_key = %s", (inv, catalogue_id, scope))
    return str(row["id"]) if row else None


def _ensure_pattern(conn: Any, inv: str, catalogue_id: str, scope: str) -> str:
    existing = _pattern_id(conn, inv, catalogue_id, scope)
    if existing:
        return existing
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO butler_pattern (investor_id, catalogue_id, scope_key, dedupe_key) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (inv, catalogue_id, scope, f"notebook.pattern:{catalogue_id}:{scope}"))
        return str(cur.fetchone()[0])


def latest_revision(conn: Any, inv: str, pattern_id: str) -> dict[str, Any] | None:
    return _one(conn, """
        SELECT * FROM butler_pattern_revision WHERE investor_id = %s AND pattern_id = %s
         ORDER BY revision DESC LIMIT 1
        """, (inv, pattern_id))


def _signature_of(row: dict[str, Any]) -> tuple:
    return (row["status"], row["reason_code"],
            tuple(sorted(str(i) for i in row["supporting_episode_ids"])),
            tuple(sorted(str(i) for i in row["counter_episode_ids"])), row["eligible_count"],
            row["undetermined_count"], row["distinct_dates"], row["window_start"],
            row["window_end"], tuple(row["alternative_ids"]), tuple(row["question_ids"]),
            tuple(row["limitations"]))


def append_revision(conn: Any, inv: str, pattern_id: str, draft: be.RevisionDraft,
                    consent_version: int, origin: str = "CODE",
                    reflection_job_id: str | None = None) -> str | None:
    """Append a revision when it differs from the latest (by signature) or when the origin
    differs. Returns the new revision id or None when nothing changed."""
    prior = latest_revision(conn, inv, pattern_id)
    if prior is not None and prior["origin"] == origin and \
            _signature_of(prior) == draft.signature():
        return None
    revision = (prior["revision"] if prior else 0) + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_pattern_revision (investor_id, pattern_id, revision, status,
                origin, supporting_episode_ids, counter_episode_ids, eligible_count,
                supporting_count, counter_count, undetermined_count, distinct_dates,
                denominator_known, window_start, window_end, alternative_ids, question_ids,
                limitations, reason_code, consent_version, reflection_job_id, rule_version,
                dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s::uuid[],%s::uuid[],%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s) RETURNING id
            """,
            (inv, pattern_id, revision, draft.status, origin, list(draft.supporting_ids),
             list(draft.counter_ids), draft.eligible, len(draft.supporting_ids),
             len(draft.counter_ids), draft.undetermined, draft.distinct_dates,
             draft.denominator_known, draft.window_start, draft.window_end,
             list(draft.alternatives), list(draft.questions), list(draft.limitations),
             draft.reason_code, consent_version, reflection_job_id, np.RULE_VERSION,
             f"notebook.revision:{pattern_id}:{revision}"),
        )
        return str(cur.fetchone()[0])


def evaluate_patterns(conn: Any, inv: str, consent: dict[str, Any], today: date) -> int:
    """Deterministic revisions for every catalogue entry, investor scope. Deleted/excluded
    scopes are skipped here AND refused by 053."""
    episodes = list(bn.visible_episodes(conn, inv).values())
    scopes = bn.excluded_scopes(conn, inv)
    deleted = {str(r["pattern_id"]) for r in _rows(
        conn, "SELECT pattern_id FROM butler_notebook_event WHERE investor_id = %s "
              "AND action = 'DELETE'", (inv,))}
    appended = 0
    for catalogue_id in np.CATALOGUE:
        if (catalogue_id, "INVESTOR") in scopes:
            continue
        pid = _pattern_id(conn, inv, catalogue_id, "INVESTOR")
        if pid in deleted:
            continue
        draft = be.evaluate(catalogue_id, episodes, today, existed_before=pid is not None)
        if draft is None:
            continue
        pid = pid or _ensure_pattern(conn, inv, catalogue_id, "INVESTOR")
        if append_revision(conn, inv, pid, draft, consent["version"]) is not None:
            appended += 1
    return appended


def notebook_pass(conn: Any, inv: str, now: datetime) -> dict[str, Any]:
    """One idempotent pass inside the caller's ENGINE transaction. A revocation racing this
    pass surfaces as the 053 consent error; the caller rolls back and nothing lands."""
    consent = bn.current_consent(conn, inv)
    if consent["state"] != "ACTIVE":
        return {"state": consent["state"], "episodes": 0, "revisions": 0}
    today = now.astimezone(UTC).date()
    try:
        written = write_episodes(conn, inv, consent, draft_episodes(conn, inv, consent, today))
        revised = evaluate_patterns(conn, inv, consent, today)
    except psycopg.errors.RaiseException as exc:
        if "consent not current" in str(exc) or "record excluded" in str(exc):
            raise ConsentChanged(str(exc)) from exc
        raise
    return {"state": "ACTIVE", "episodes": written, "revisions": revised}


class ConsentChanged(RuntimeError):
    """Consent or an exclusion changed under the pass; the transaction must be rolled back."""
