"""Butler's consented investor notebook: consent, self-description, controls, reads (BT 12/15).

docs/BUTLER_INVESTOR_MEMORY.md, migration 053. The deterministic episode builder and pattern
evaluator are ``app/research/butler_episodes.py`` (BT 13); bounded reflection is
``app/research/butler_reflection.py`` (BT 14). This module is the person's side: what they
opted into, what they said about themselves, what they disputed, disabled, excluded or deleted,
and the read model Settings -> Butler -> Understanding you renders.

OFF BY DEFAULT, AND NOTHING ELSE CAN TURN IT ON. The only writer of ``butler_learning_consent``
is ``set_consent``, called only from ``POST /butler/notebook/consent`` as the human capability.
Signup, portfolio import, a company follow and a report request have no path here.

STATE IS DERIVED ON READ. A pattern whose cited episode was corrected or excluded, or whose
cited source was retracted, reads as SUSPENDED_PENDING_RECHECK immediately -- before the builder
writes a formal revision -- because the person's correction must take effect when they make it,
not at the next worker pass. A deleted pattern and everything under an ALL_BEFORE exclusion
disappear from every read, packet and coaching decision the moment the exclusion commits, and
the physical purge (eraser) follows.

NOT A REPORT INPUT. Nothing in ``app/research/report_*`` or ``app/jobs/hosted.py``'s report path
may import this module (``make lint``), and report packets refuse notebook keys.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.db import workspace as ws
from app.research import butler_notebook_policy as np
from app.research.errors import VERSION_CONFLICT, conflict, invalid, not_found

OP_CONSENT = "butler.notebook_consent"
OP_SELF = "butler.notebook_self"
OP_CONTROL = "butler.notebook_control"
OP_DELETE_ALL = "butler.notebook_delete_all"

_rows = ws._rows
_one = ws._one

NOTEBOOK_PURGE_ORDER = (
    # FK order: children first. butler_learning_consent and butler_notebook_exclusion are KEPT:
    # the consent history is the record of what was agreed, and the exclusion is the replay
    # fence -- identifiers only, no content.
    "butler_coaching_use", "butler_notebook_event", "butler_pattern_revision", "butler_pattern",
    "butler_episode", "butler_self_description",
)


def _now() -> datetime:
    return datetime.now(UTC)


# =========================================================================================
# consent
# =========================================================================================


def _consent_rows(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT id, version, action, disclosure_version, sources, include_from,
               reflection_enabled, recorded_at
          FROM butler_learning_consent WHERE investor_id = %s ORDER BY version
        """, (investor_id,))


def current_consent(conn: Any, investor_id: str) -> dict[str, Any]:
    """The consent in force. ``state`` is NOT_SET / ACTIVE / PAUSED / REVOKED; only ACTIVE
    permits capture or analysis. ``effective_include_from`` is the actual earliest date read --
    the opt-in date unless the person explicitly chose an earlier one."""
    rows = _consent_rows(conn, investor_id)
    if not rows:
        return {"state": "NOT_SET", "version": 0, "sources": [], "include_from": None,
                "effective_include_from": None, "reflection_enabled": False,
                "disclosure_version": None, "opted_in_at": None, "recorded_at": None,
                "history": []}
    last = rows[-1]
    state = {"OPT_IN": "ACTIVE", "UPDATE_SCOPE": "ACTIVE", "RESUME": "ACTIVE",
             "PAUSE": "PAUSED", "REVOKE": "REVOKED"}[last["action"]]
    opted = next((r for r in reversed(rows) if r["action"] == "OPT_IN"), None)
    effective = last["include_from"] or (opted["recorded_at"].astimezone(UTC).date()
                                         if opted else None)
    return {
        "state": state,
        "version": last["version"],
        "sources": list(last["sources"] or []),
        "include_from": last["include_from"],
        "effective_include_from": effective if state != "REVOKED" else None,
        "reflection_enabled": bool(last["reflection_enabled"]) and state == "ACTIVE",
        "disclosure_version": last["disclosure_version"],
        "opted_in_at": opted["recorded_at"] if opted else None,
        "recorded_at": last["recorded_at"],
        "history": [{"version": r["version"], "action": r["action"],
                     "sources": list(r["sources"] or []), "include_from": r["include_from"],
                     "reflection_enabled": r["reflection_enabled"],
                     "recorded_at": r["recorded_at"]} for r in rows],
    }


def consent_allows(consent: dict[str, Any], category: str | None = None,
                   on: date | None = None) -> bool:
    """The Python half of the capture/packet/read check (the database re-checks at persist)."""
    if consent["state"] != "ACTIVE":
        return False
    if category is not None and category not in consent["sources"]:
        return False
    return not (on is not None and consent["effective_include_from"] is not None
                and on < consent["effective_include_from"])


def set_consent(conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
                payload_digest: str) -> tuple[dict[str, Any], int]:
    action = body["action"]
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_CONSENT, "-", key, payload_digest)
    if receipt is not None:
        return current_consent(conn, investor_id), 200
    current = current_consent(conn, investor_id)
    if body["expected_version"] != current["version"]:
        raise conflict(VERSION_CONFLICT, "notebook consent changed; reload first")

    sources = list(current["sources"])
    include_from = current["include_from"]
    reflection = False
    disclosure = current["disclosure_version"] or np.DISCLOSURE_VERSION
    if action in ("OPT_IN", "UPDATE_SCOPE"):
        if body.get("disclosure_version") != np.DISCLOSURE_VERSION:
            raise invalid("the current disclosure must be shown and accepted",
                          {"disclosure_version": "NOT_CURRENT"})
        if not body.get("sources"):
            raise invalid("choose at least one source category", {"sources": "REQUIRED"})
        unknown = [s for s in body["sources"] if s not in np.SOURCE_CATEGORIES]
        if unknown:
            raise invalid("unknown source category", {"sources": "NOT_IN_CATALOGUE"})
        sources = sorted(set(body["sources"]))
        include_from = body.get("include_from")
        if include_from is not None and include_from > _now().date():
            raise invalid("the earliest included date cannot be in the future",
                          {"include_from": "FUTURE"})
        reflection = bool(body.get("reflection_enabled"))
        disclosure = np.DISCLOSURE_VERSION
    elif action == "RESUME":
        # Resume restores the reflection choice in force before the pause; it never widens.
        prior = [h for h in current["history"] if h["action"] in ("OPT_IN", "UPDATE_SCOPE",
                                                                   "RESUME")]
        reflection = bool(prior[-1]["reflection_enabled"]) if prior else False
    for field in ("sources", "include_from", "reflection_enabled", "disclosure_version"):
        if action in ("PAUSE", "RESUME", "REVOKE") and body.get(field) not in (None, [], False):
            raise invalid(f"{field} applies only to OPT_IN and UPDATE_SCOPE",
                          {field: "UNEXPECTED"})

    version = current["version"] + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_learning_consent (investor_id, version, action,
                disclosure_version, sources, include_from, reflection_enabled, actor_uid,
                dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, version, action, disclosure, sources, include_from, reflection,
             actor_uid, ws.dedupe_key(OP_CONSENT, "-", key)),
        )
    # Revocation stops queued/in-flight reflection NOW. The database fence (054) already makes a
    # late result unpersistable; this also records WHY the job stopped.
    # UPDATE_SCOPE seals a new consent version, so a job sealed under the old one can no longer
    # persist anything; cancelling it too makes that visible instead of a silent failure.
    if action in ("PAUSE", "REVOKE", "UPDATE_SCOPE"):
        _cancel_reflection(conn, investor_id, actor_uid,
                           "CONSENT_REVOKED" if action == "REVOKE" else "CONSENT_CHANGED")
    ws.audit_write(conn, actor_uid, OP_CONSENT, "butler_learning_consent", investor_id,
                   version=version, action=action)
    ws.record_receipt(conn, investor_id, OP_CONSENT, "-", key, payload_digest,
                      {"version": str(version)}, actor_uid)
    return current_consent(conn, investor_id), 201


def _cancel_reflection(conn: Any, investor_id: str, actor_uid: str, reason: str) -> None:
    """Append CANCEL_REQUESTED to any non-terminal reflection job, if 054 is installed. A
    consent change seals a new version, so an in-flight job's sealed version is no longer
    current and its output is refused regardless; this makes the stop visible."""
    if _one(conn, "SELECT to_regclass('hosted_job') IS NOT NULL AS ok")["ok"] is not True:
        return
    from app.db import hosted_jobs as hj

    for job in hj.running_jobs(conn, investor_id, "NOTEBOOK_REFLECTION"):
        hj.request_cancel_row(conn, investor_id, job["id"], actor_uid, reason)


# =========================================================================================
# self-description -- the person's own words
# =========================================================================================


def add_self_description(conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any],
                         key: str, payload_digest: str) -> tuple[dict[str, Any], int]:
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_SELF, "-", key, payload_digest)
    if receipt is not None:
        return _self_row(conn, investor_id, receipt.result["id"]), 200
    if current_consent(conn, investor_id)["state"] == "NOT_SET":
        # A self-description is only kept inside an opted-in notebook; before opt-in there is
        # no notebook to keep it in.
        raise invalid("turn on Butler's notebook before adding to it", {"consent": "NOT_SET"})
    isin = body.get("isin")
    if isin is not None:
        isin = ws.require_linked(conn, investor_id, isin)
    previous = body.get("previous_id")
    if previous is not None:
        previous = ws._uuid(previous, "self-description")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_self_description (investor_id, isin, statement, previous_id,
                                                 actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, isin, body["statement"], previous, actor_uid,
             ws.dedupe_key(OP_SELF, "-", key)),
        )
        new_id = str(cur.fetchone()[0])
    ws.audit_write(conn, actor_uid, OP_SELF, "butler_self_description", new_id)
    ws.record_receipt(conn, investor_id, OP_SELF, "-", key, payload_digest, {"id": new_id},
                      actor_uid)
    return _self_row(conn, investor_id, new_id), 201


def _self_row(conn: Any, investor_id: str, row_id: str) -> dict[str, Any]:
    row = _one(conn, """
        SELECT id, isin, statement, previous_id, recorded_at FROM butler_self_description
         WHERE investor_id = %s AND id = %s
        """, (investor_id, row_id))
    if row is None:
        raise not_found("self-description")
    return {**row, "origin": "USER", "retracted": False}


# =========================================================================================
# controls
# =========================================================================================

_PATTERN_ACTIONS = ("DISPUTE", "WITHDRAW_DISPUTE", "DISABLE", "ENABLE", "DELETE")
_EPISODE_ACTIONS = ("CORRECT_EPISODE", "EXCLUDE_EPISODE")


def pattern_user_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derived from the person's events on one pattern, oldest first."""
    disputed = disabled = deleted = False
    for e in events:
        a = e["action"]
        if a == "DISPUTE":
            disputed = True
        elif a == "WITHDRAW_DISPUTE":
            disputed = False
        elif a == "DISABLE":
            disabled = True
        elif a == "ENABLE":
            disabled = False
        elif a == "DELETE":
            deleted = True
    return {"disputed": disputed, "disabled": disabled, "deleted": deleted}


def record_control(conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
                   payload_digest: str) -> tuple[dict[str, Any], int]:
    """One control action. DELETE on a pattern and EXCLUDE/CORRECT on an episode also write the
    replay fence in the same transaction, so a rebuild cannot bring the material back."""
    action = body["action"]
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_CONTROL, "-", key, payload_digest)
    if receipt is not None:
        return {"event_id": receipt.result["event_id"], "action": action}, 200
    pattern_id = episode_id = self_id = None
    if action in _PATTERN_ACTIONS:
        pattern_id = ws._uuid(body.get("target_id") or "", "pattern")
        pattern = _one(conn, "SELECT id, catalogue_id, scope_key FROM butler_pattern "
                             "WHERE investor_id = %s AND id = %s", (investor_id, pattern_id))
        if pattern is None:
            raise not_found("pattern")
        events = _rows(conn, "SELECT action FROM butler_notebook_event WHERE investor_id = %s "
                             "AND pattern_id = %s ORDER BY seq", (investor_id, pattern_id))
        state = pattern_user_state(events)
        if state["deleted"]:
            raise not_found("pattern")
        redundant = {"DISPUTE": state["disputed"], "WITHDRAW_DISPUTE": not state["disputed"],
                     "DISABLE": state["disabled"], "ENABLE": not state["disabled"]}
        if redundant.get(action):
            raise conflict("INVALID_TRANSITION", f"pattern is already in that state ({action})")
    elif action in _EPISODE_ACTIONS:
        episode_id = ws._uuid(body.get("target_id") or "", "episode")
        episode = _one(conn, "SELECT id, anchor_id FROM butler_episode WHERE investor_id = %s "
                             "AND id = %s", (investor_id, episode_id))
        if episode is None:
            raise not_found("episode")
        if action == "CORRECT_EPISODE" and body.get("correction_code") not in np.CORRECTION_CODES:
            raise invalid("choose a correction reason", {"correction_code": "REQUIRED"})
    elif action == "RETRACT_SELF_DESCRIPTION":
        self_id = ws._uuid(body.get("target_id") or "", "self-description")
        if _one(conn, "SELECT 1 AS x FROM butler_self_description WHERE investor_id = %s "
                      "AND id = %s", (investor_id, self_id)) is None:
            raise not_found("self-description")
    else:  # pragma: no cover - schema refuses
        raise invalid("unknown control", {"action": "UNKNOWN"})
    if action != "CORRECT_EPISODE" and body.get("correction_code") is not None:
        raise invalid("correction_code applies to CORRECT_EPISODE", {"correction_code":
                                                                     "UNEXPECTED"})

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_notebook_event (investor_id, action, pattern_id, episode_id,
                self_description_id, correction_code, note, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, action, pattern_id, episode_id, self_id, body.get("correction_code"),
             body.get("note"), actor_uid, ws.dedupe_key(OP_CONTROL, "-", key)),
        )
        event_id = str(cur.fetchone()[0])
        if action == "DELETE":
            cur.execute(
                """
                INSERT INTO butler_notebook_exclusion (investor_id, target_kind, catalogue_id,
                    scope_key, reason_code, actor_uid, dedupe_key)
                VALUES (%s, 'PATTERN_SCOPE', %s, %s, 'USER_DELETED_PATTERN', %s, %s)
                """,
                (investor_id, pattern["catalogue_id"], pattern["scope_key"], actor_uid,
                 ws.dedupe_key(OP_CONTROL, "-", key, "fence")),
            )
        elif action in _EPISODE_ACTIONS:
            cur.execute(
                """
                INSERT INTO butler_notebook_exclusion (investor_id, target_kind, anchor_id,
                    reason_code, actor_uid, dedupe_key)
                VALUES (%s, 'ANCHOR', %s, %s, %s, %s)
                """,
                (investor_id, episode["anchor_id"],
                 "USER_EXCLUDED_EPISODE" if action == "EXCLUDE_EPISODE"
                 else "USER_CORRECTED_RECORD", actor_uid,
                 ws.dedupe_key(OP_CONTROL, "-", key, "fence")),
            )
    ws.audit_write(conn, actor_uid, OP_CONTROL, "butler_notebook_event", event_id,
                   action=action)
    ws.record_receipt(conn, investor_id, OP_CONTROL, "-", key, payload_digest,
                      {"event_id": event_id}, actor_uid)
    return {"event_id": event_id, "action": action}, 201


def delete_all_fence(conn: Any, investor_id: str, actor_uid: str, key: str,
                     payload_digest: str) -> tuple[dict[str, Any], int]:
    """Step 1 of "delete my notebook", as the person: revoke learning (if active) and write an
    ALL_BEFORE exclusion. From this commit, every read, packet and coaching decision ignores
    everything recorded before it, and no rebuild or later backfill can recreate it. Step 2 is
    ``purge_notebook`` on the eraser capability."""
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_DELETE_ALL, "-", key, payload_digest)
    if receipt is not None:
        return {"fence_id": receipt.result["fence_id"]}, 200
    current = current_consent(conn, investor_id)
    with conn.cursor() as cur:
        if current["state"] in ("ACTIVE", "PAUSED"):
            cur.execute(
                """
                INSERT INTO butler_learning_consent (investor_id, version, action,
                    disclosure_version, actor_uid, dedupe_key)
                VALUES (%s, %s, 'REVOKE', %s, %s, %s)
                """,
                (investor_id, current["version"] + 1,
                 current["disclosure_version"] or np.DISCLOSURE_VERSION, actor_uid,
                 ws.dedupe_key(OP_DELETE_ALL, "-", key, "revoke")),
            )
            _cancel_reflection(conn, investor_id, actor_uid, "CONSENT_REVOKED")
        cur.execute(
            """
            INSERT INTO butler_notebook_exclusion (investor_id, target_kind, before_at,
                reason_code, actor_uid, dedupe_key)
            VALUES (%s, 'ALL_BEFORE', clock_timestamp(), 'USER_DELETED_NOTEBOOK', %s, %s)
            RETURNING id
            """,
            (investor_id, actor_uid, ws.dedupe_key(OP_DELETE_ALL, "-", key)),
        )
        fence_id = str(cur.fetchone()[0])
    ws.audit_write(conn, actor_uid, OP_DELETE_ALL, "butler_notebook_exclusion", fence_id)
    ws.record_receipt(conn, investor_id, OP_DELETE_ALL, "-", key, payload_digest,
                      {"fence_id": fence_id}, actor_uid)
    return {"fence_id": fence_id}, 201


def purge_notebook(conn: Any, investor_id: str) -> dict[str, Any]:
    """Step 2, on the ERASER capability inside ``authorise_erasure``: physically delete the
    derived notebook rows. Consent history and exclusions stay (identifiers only). A legal hold
    makes the database refuse; that is reported as HELD, never as success -- the material is
    already invisible from step 1 either way."""
    import psycopg

    counts: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for table in NOTEBOOK_PURGE_ORDER:
                cur.execute(f"DELETE FROM {table} WHERE investor_id = %s", (investor_id,))
                counts[table] = cur.rowcount
    except psycopg.errors.RestrictViolation:
        return {"status": "HELD", "deleted": {}}
    return {"status": "PURGED", "deleted": counts}


# =========================================================================================
# reads -- the Understanding-you view
# =========================================================================================


def _fence(conn: Any, investor_id: str) -> datetime | None:
    row = _one(conn, "SELECT max(before_at) AS b FROM butler_notebook_exclusion "
                     "WHERE investor_id = %s AND target_kind = 'ALL_BEFORE'", (investor_id,))
    return row["b"] if row else None


def excluded_anchors(conn: Any, investor_id: str) -> set[str]:
    return {str(r["anchor_id"]) for r in _rows(conn, """
        SELECT anchor_id FROM butler_notebook_exclusion
         WHERE investor_id = %s AND target_kind = 'ANCHOR'
        """, (investor_id,))}


def excluded_scopes(conn: Any, investor_id: str) -> set[tuple[str, str]]:
    return {(r["catalogue_id"], r["scope_key"]) for r in _rows(conn, """
        SELECT catalogue_id, scope_key FROM butler_notebook_exclusion
         WHERE investor_id = %s AND target_kind = 'PATTERN_SCOPE'
        """, (investor_id,))}


def visible_episodes(conn: Any, investor_id: str, ids: list[str] | None = None
                     ) -> dict[str, dict[str, Any]]:
    """Current (unsuperseded), unexcluded, post-fence episodes, keyed by id. Corrected
    episodes are included with ``corrected`` set, so the person can see what they corrected."""
    fence = _fence(conn, investor_id)
    rows = _rows(conn, """
        SELECT e.id, e.catalogue_id, e.polarity, e.observation_code, e.source_category,
               e.comparability_key, e.isin, e.occurred_on, e.knowable_at, e.anchor_kind,
               e.anchor_id, e.review_id, e.thesis_id, e.prior_thesis_id, e.response_id,
               e.evaluation_id, e.limitations, e.builder_version, e.consent_version,
               e.recorded_at,
               (SELECT ne.correction_code FROM butler_notebook_event ne
                 WHERE ne.investor_id = e.investor_id AND ne.episode_id = e.id
                   AND ne.action = 'CORRECT_EPISODE' ORDER BY ne.seq DESC LIMIT 1)
                 AS correction_code
          FROM butler_episode e
         WHERE e.investor_id = %(inv)s
           AND (%(ids)s::uuid[] IS NULL OR e.id = ANY(%(ids)s::uuid[]))
           AND (%(fence)s::timestamptz IS NULL OR e.knowable_at > %(fence)s::timestamptz)
           AND NOT EXISTS (SELECT 1 FROM butler_episode s WHERE s.investor_id = e.investor_id
                           AND s.supersedes_id = e.id)
           AND NOT EXISTS (SELECT 1 FROM butler_notebook_event ne
                           WHERE ne.investor_id = e.investor_id AND ne.episode_id = e.id
                             AND ne.action = 'EXCLUDE_EPISODE')
         ORDER BY e.occurred_on, e.seq
        """, {"inv": investor_id, "ids": ids, "fence": fence})
    return {str(r["id"]): {**r, "corrected": r["correction_code"] is not None,
                           "origin": "CODE"} for r in rows}


def _retracted_sources_for(conn: Any, investor_id: str, review_ids: list[str]) -> set[str]:
    """Reviews whose attached evidence cites a retracted source."""
    if not review_ids:
        return set()
    return {str(r["review_id"]) for r in _rows(conn, """
        SELECT DISTINCT rr.review_id FROM butler_review_response rr
          JOIN butler_review_evidence ev ON ev.investor_id = rr.investor_id
                                        AND ev.response_id = rr.id
          JOIN butler_source_retraction x ON x.investor_id = ev.investor_id
                                         AND x.source_id = ev.source_id
         WHERE rr.investor_id = %s AND rr.review_id = ANY(%s::uuid[])
        """, (investor_id, review_ids))}


def effective_status(revision: dict[str, Any], user: dict[str, Any],
                     cited: dict[str, dict[str, Any] | None], retracted: set[str],
                     consent: dict[str, Any], now: datetime) -> tuple[str, str]:
    """(status, reason) as it must be shown and used NOW, from the stored revision plus
    everything the person or a correction did since. Never more assertive than stored."""
    if user["disputed"]:
        return "DISPUTED_BY_USER", "USER_DISPUTED"
    stored = revision["status"]
    for ep in cited.values():
        if ep is None or ep["corrected"]:
            return "SUSPENDED_PENDING_RECHECK", "CITED_EPISODE_CORRECTED_OR_EXCLUDED"
        if ep.get("review_id") and str(ep["review_id"]) in retracted:
            return "SUSPENDED_PENDING_RECHECK", "CITED_SOURCE_RETRACTED"
    if stored in ("TENTATIVE", "REPEATED_IN_RECORDS") and \
            now.date() - revision["window_end"] > np.RELEVANCE_WINDOW:
        return "STALE", "NO_FRESH_EPISODE"
    if consent["state"] != "ACTIVE" and stored == "REPEATED_IN_RECORDS":
        # Learning paused/revoked: shown, never used for coaching (see coaching_patterns).
        return stored, "LEARNING_NOT_ACTIVE"
    return stored, revision["reason_code"]


def _latest_revisions(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    fence = _fence(conn, investor_id)
    return _rows(conn, """
        SELECT DISTINCT ON (r.pattern_id)
               r.id, r.pattern_id, r.revision, r.status, r.origin, r.supporting_episode_ids,
               r.counter_episode_ids, r.eligible_count, r.supporting_count, r.counter_count,
               r.undetermined_count, r.distinct_dates, r.denominator_known, r.window_start,
               r.window_end, r.alternative_ids, r.question_ids, r.limitations, r.reason_code,
               r.consent_version, r.reflection_job_id, r.rule_version, r.evaluated_at,
               r.recorded_at, p.catalogue_id, p.scope_key
          FROM butler_pattern_revision r
          JOIN butler_pattern p ON p.investor_id = r.investor_id AND p.id = r.pattern_id
         WHERE r.investor_id = %s
           AND (%s::timestamptz IS NULL OR r.recorded_at > %s::timestamptz)
         ORDER BY r.pattern_id, r.revision DESC
        """, (investor_id, fence, fence))


def patterns_view(conn: Any, investor_id: str, now: datetime | None = None
                  ) -> list[dict[str, Any]]:
    """Every non-deleted pattern with its latest revision, exact cited episodes, alternatives,
    limitations, user state, derived status and influence history."""
    now = now or _now()
    consent = current_consent(conn, investor_id)
    revisions = _latest_revisions(conn, investor_id)
    if not revisions:
        return []
    pattern_ids = [str(r["pattern_id"]) for r in revisions]
    events: dict[str, list[dict]] = {}
    for e in _rows(conn, """
            SELECT pattern_id, action, note, recorded_at FROM butler_notebook_event
             WHERE investor_id = %s AND pattern_id = ANY(%s::uuid[]) ORDER BY seq
            """, (investor_id, pattern_ids)):
        events.setdefault(str(e["pattern_id"]), []).append(e)
    scopes = excluded_scopes(conn, investor_id)
    all_ids = sorted({str(i) for r in revisions
                      for i in list(r["supporting_episode_ids"]) + list(r["counter_episode_ids"])})
    episodes = visible_episodes(conn, investor_id, all_ids)
    retracted = _retracted_sources_for(conn, investor_id, sorted(
        {str(e["review_id"]) for e in episodes.values() if e["review_id"]}))
    uses = _rows(conn, """
        SELECT u.id, u.question_id, u.pattern_revision_id, u.review_id, u.recorded_at,
               r.pattern_id
          FROM butler_coaching_use u JOIN butler_pattern_revision r
            ON r.investor_id = u.investor_id AND r.id = u.pattern_revision_id
         WHERE u.investor_id = %s AND r.pattern_id = ANY(%s::uuid[]) ORDER BY u.seq DESC
        """, (investor_id, pattern_ids))
    out = []
    for r in revisions:
        pid = str(r["pattern_id"])
        user = pattern_user_state(events.get(pid, []))
        if user["deleted"] or (r["catalogue_id"], r["scope_key"]) in scopes:
            continue
        cited = {str(i): episodes.get(str(i)) for i in
                 list(r["supporting_episode_ids"]) + list(r["counter_episode_ids"])}
        status, reason = effective_status(r, user, cited, retracted, consent, now)
        entry = np.CATALOGUE.get(r["catalogue_id"])
        out.append({
            "pattern_id": pid,
            "catalogue_id": r["catalogue_id"],
            "catalogue_version": entry.version if entry else None,
            "text": entry.text if entry else "",
            "eligible": entry.eligible if entry else "",
            "comparison": entry.comparison if entry else "",
            "positive": bool(entry and entry.positive),
            "scope": r["scope_key"],
            "revision": r["revision"],
            "revision_id": str(r["id"]),
            "origin": r["origin"],
            "stored_status": r["status"],
            "status": status,
            "status_reason": reason,
            "supporting": [cited[str(i)] for i in r["supporting_episode_ids"]
                           if cited.get(str(i))],
            "counterexamples": [cited[str(i)] for i in r["counter_episode_ids"]
                                if cited.get(str(i))],
            "counts": {"eligible": r["eligible_count"], "supporting": r["supporting_count"],
                       "counter": r["counter_count"], "undetermined": r["undetermined_count"],
                       "distinct_dates": r["distinct_dates"],
                       "denominator_known": r["denominator_known"]},
            "window": {"start": r["window_start"], "end": r["window_end"]},
            "alternatives": [{"id": a, "text": np.ALTERNATIVES[a].text}
                             for a in r["alternative_ids"] if a in np.ALTERNATIVES],
            "questions": [{"id": q, "text": np.QUESTIONS[q].text}
                          for q in r["question_ids"] if q in np.QUESTIONS],
            "limitations": [{"id": code, "text": np.LIMITATIONS.get(code, code)}
                            for code in r["limitations"]],
            "user_state": {"disputed": user["disputed"], "disabled": user["disabled"]},
            "user_notes": [{"action": e["action"], "note": e["note"],
                            "recorded_at": e["recorded_at"]} for e in events.get(pid, [])],
            "consent_version": r["consent_version"],
            "reflection_job_id": str(r["reflection_job_id"]) if r["reflection_job_id"] else None,
            "rule_version": r["rule_version"],
            "last_evaluated_at": r["evaluated_at"],
            "influence": [{"question_id": u["question_id"], "review_id": str(u["review_id"]),
                           "revision_id": str(u["pattern_revision_id"]),
                           "recorded_at": u["recorded_at"]}
                          for u in uses if str(u["pattern_id"]) == pid],
            "used_for_coaching": (status in np.COACHING_STATUSES and not user["disabled"]
                                  and consent["state"] == "ACTIVE"),
        })
    return out


def coaching_patterns(conn: Any, investor_id: str, isin: str | None,
                      now: datetime | None = None) -> list[dict[str, Any]]:
    """The patterns that may shape a question RIGHT NOW: consent active, status repeated after
    every derived downgrade, not disputed, disabled, deleted, stale or suspended, and scoped to
    the investor or this company. Positive patterns included -- improvement is recognised."""
    consent = current_consent(conn, investor_id)
    if consent["state"] != "ACTIVE":
        return []
    return [p for p in patterns_view(conn, investor_id, now)
            if p["used_for_coaching"] and p["scope"] in ("INVESTOR", isin)]


def self_descriptions(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    fence = _fence(conn, investor_id)
    rows = _rows(conn, """
        SELECT d.id, d.isin, d.statement, d.previous_id, d.recorded_at,
               EXISTS (SELECT 1 FROM butler_notebook_event ne
                        WHERE ne.investor_id = d.investor_id AND ne.self_description_id = d.id
                          AND ne.action = 'RETRACT_SELF_DESCRIPTION') AS retracted,
               EXISTS (SELECT 1 FROM butler_self_description n
                        WHERE n.investor_id = d.investor_id AND n.previous_id = d.id)
                 AS superseded
          FROM butler_self_description d
         WHERE d.investor_id = %s AND (%s::timestamptz IS NULL OR d.recorded_at > %s::timestamptz)
         ORDER BY d.seq
        """, (investor_id, fence, fence))
    return [{**r, "origin": "USER"} for r in rows if not r["retracted"]]


def notebook_view(conn: Any, investor_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Everything Understanding-you shows. Never conceals that profiling occurs: when consent
    is active the view says so, and every stored record is listed."""
    now = now or _now()
    consent = current_consent(conn, investor_id)
    episodes = list(visible_episodes(conn, investor_id).values())
    return {
        "consent": consent,
        "profiling_active": consent["state"] == "ACTIVE",
        "self_descriptions": self_descriptions(conn, investor_id),
        "patterns": patterns_view(conn, investor_id, now),
        "episodes": episodes,
        "episode_counts": {
            "total": len(episodes),
            "supports": sum(1 for e in episodes if e["polarity"] == "SUPPORTS"),
            "counters": sum(1 for e in episodes if e["polarity"] == "COUNTERS"),
            "undetermined": sum(1 for e in episodes if e["polarity"] == "UNDETERMINED"),
        },
        "retention": {
            "category": "INVESTOR_NOTEBOOK",
            "policy": "UNMAPPED",
            "statement": "No retention period has been approved for these records yet. They "
                         "are kept until you delete them or your account data is erased.",
        },
        "generated_at": now,
    }


def export_notebook(conn: Any, investor_id: str) -> dict[str, Any]:
    """The notebook slice of the privacy export -- every stored row, including disputed,
    suspended, excluded and superseded material, read through the same allowlists."""
    from app.db import workspace_privacy as wp

    out: dict[str, Any] = {}
    for entry in wp.COVERAGE:
        if entry.category != "INVESTOR_NOTEBOOK" or entry.export_key is None:
            continue
        cols = ", ".join(entry.columns)
        out[entry.export_key] = _rows(
            conn, f"SELECT {cols} FROM {entry.table} WHERE investor_id = %s "
                  f"ORDER BY {entry.columns[0]}", (investor_id,))
    out["notebook_export_note"] = (
        "These are Butler's private notes about your recorded research process. They are "
        "observations about saved records, not facts about you, and they can be wrong.")
    return out


# =========================================================================================
# BT 14 -- personalised question selection and "Why this question?"
# =========================================================================================


def notebook_installed(conn: Any) -> bool:
    row = _one(conn, "SELECT to_regclass('butler_coaching_use') IS NOT NULL AS ok")
    return bool(row and row["ok"])


def select_personal_question(conn: Any, investor_id: str, isin: str, decision_kind: str,
                             already: tuple[str, ...], now: datetime | None = None
                             ) -> dict[str, Any] | None:
    """At most one explainable question for this review, or None. Deterministic: the current
    coaching-eligible pattern (consent active; not disputed, disabled, stale or suspended) that
    applies to this decision kind and has the most distinct supporting episodes; ties by
    catalogue order. The same factual checks apply whether or not one is chosen."""
    if not notebook_installed(conn):
        return None
    order = list(np.CATALOGUE)
    candidates = [p for p in coaching_patterns(conn, investor_id, isin, now)
                  if decision_kind in np.DECISION_KINDS.get(p["catalogue_id"], ())]
    candidates.sort(key=lambda p: (-p["counts"]["supporting"], order.index(p["catalogue_id"])))
    for p in candidates:
        for q in np.CATALOGUE[p["catalogue_id"]].questions:
            if q not in already:
                return {"question_id": q, "revision_id": p["revision_id"],
                        # The consent in force NOW (053 checks it), not the revision's.
                        "consent_version": current_consent(conn, investor_id)["version"],
                        "pattern_id": p["pattern_id"]}
    return None


def record_coaching_use(conn: Any, investor_id: str, review_id: str, pick: dict[str, Any],
                        consent_version: int) -> None:
    """Same transaction as the review. 053 refuses it if consent changed meanwhile, which
    rolls the whole review start back -- the caller retries without personalisation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_coaching_use (investor_id, question_id, pattern_revision_id,
                                             review_id, consent_version, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (investor_id, pick["question_id"], pick["revision_id"], review_id, consent_version,
             f"notebook.coaching:{review_id}:{pick['question_id']}"),
        )


def why_question(conn: Any, investor_id: str, review_id: str, question_id: str
                 ) -> dict[str, Any]:
    """The exact revision and episodes behind a personalised question -- shown in their
    CURRENT state, so a pattern disputed or deleted since says so (deleted: nothing shown)."""
    review_id = ws._uuid(review_id, "review")
    use = _one(conn, """
        SELECT u.pattern_revision_id, r.pattern_id FROM butler_coaching_use u
          JOIN butler_pattern_revision r ON r.investor_id = u.investor_id
                                        AND r.id = u.pattern_revision_id
         WHERE u.investor_id = %s AND u.review_id = %s AND u.question_id = %s
        """, (investor_id, review_id, question_id))
    if use is None:
        return {"review_id": review_id, "question_id": question_id, "personalised": False,
                "pattern": None,
                "statement": "This question is part of the standard checklist for this kind "
                             "of review. It was not chosen from your notebook."}
    pattern = next((p for p in patterns_view(conn, investor_id)
                    if p["pattern_id"] == str(use["pattern_id"])), None)
    if pattern is None:
        return {"review_id": review_id, "question_id": question_id, "personalised": True,
                "pattern": None,
                "statement": "This question was chosen from a notebook observation you have "
                             "since deleted. Nothing from it is kept for coaching."}
    return {"review_id": review_id, "question_id": question_id, "personalised": True,
            "pattern": pattern,
            "statement": "Butler offered this question because of the recorded episodes "
                         "below. They are observations about saved records, can be wrong, "
                         "and you can dispute, disable or delete them."}
