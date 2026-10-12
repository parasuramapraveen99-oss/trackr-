"""Butler repository: explicit follows, memory references, derived activity, the inbox,
decision reviews, source retractions and the worker's outbox SQL (BT 02, 05, 06).

docs/BUTLER_ARCHITECTURE.md sections 3, 6, 7 and 9; migration 051_butler.sql.

SAME CONTRACT AS ``app/db/workspace.py``, and built on its plumbing rather than beside it.
Every function takes a connection already scoped by ``enter_workspace`` to one investor;
RLS (051) confines every statement to that tenant and the explicit ``investor_id = %s``
predicates keep each query readable alone. Neither is the ownership check -- that happened in
``require_owned_investor`` before any of this ran. Writes take the tenant lock, check the
idempotency receipt, then write rows + audit + receipt in one transaction.

WHAT THIS MODULE NEVER DOES. It never writes a note, answer, thesis, source, fact or
transaction; it never stores prose written by anything other than the person (review answers
are the person's own words, verbatim); it never infers an interest, a mood, time spent or a
sincerity score; and it never reads the financial ledger. State (paused, snoozed, forgotten,
documented) is DERIVED from appended events on every read.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from app.db.workspace import (
    _one,
    _rows,
    _uuid,
    _valid_isin,
    audit_write,
    check_replay,
    dedupe_key,
    identities,
    isin_family,
    lock_tenant,
    record_receipt,
    require_linked,
    require_unlocked,
    visible_isins,
)
from app.research import butler_policy as bp
from app.research import canonical
from app.research.errors import (
    EVIDENCE_NOT_AVAILABLE,
    INVALID_TRANSITION,
    ISIN_REQUIRED,
    REVIEW_CLOSED,
    REVIEW_STALE,
    STALE_PREDECESSOR,
    UNRESOLVED_NOT_ACKNOWLEDGED,
    VERSION_CONFLICT,
    WorkspaceError,
    conflict,
    invalid,
    not_found,
)

# --- operation names (ws_receipt.operation) ------------------------------------------------
OP_PREFERENCE = "butler.preference"
OP_FOLLOW = "butler.follow"
OP_SUBSCRIPTION_EVENT = "butler.subscription_event"
OP_REMEMBER = "butler.remember"
OP_MEMORY_EVENT = "butler.memory_event"
OP_INBOX_EVENT = "butler.inbox_event"
OP_REVIEW_START = "butler.review_start"
OP_REVIEW_RESPONSE = "butler.review_response"
OP_REVIEW_COMPLETE = "butler.review_complete"
OP_RETRACTION = "butler.source_retraction"

PREVIEW_CHARS = 280


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preview(text: str | None) -> str:
    value = (text or "").strip()
    return value if len(value) <= PREVIEW_CHARS else value[:PREVIEW_CHARS] + "…"


# =========================================================================================
# preferences
# =========================================================================================

_PREF_COLUMNS = (
    "id, version, timezone, quiet_start, quiet_end, digest_limit, actor_uid, recorded_at"
)


def current_preference(conn: Any, investor_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        f"SELECT {_PREF_COLUMNS} FROM butler_preference WHERE investor_id = %s "
        "ORDER BY version DESC LIMIT 1",
        (investor_id,),
    )


def preference_dto(row: dict[str, Any] | None) -> dict[str, Any]:
    """The saved preference, or an explicit NOT_SET state carrying the offered defaults."""
    if row is None:
        return {
            "state": "NOT_SET",
            "version": 0,
            "timezone": None,
            "quiet_start": bp.DEFAULT_QUIET_START.isoformat(timespec="minutes"),
            "quiet_end": bp.DEFAULT_QUIET_END.isoformat(timespec="minutes"),
            "digest_limit": bp.DEFAULT_DIGEST_LIMIT,
            "recorded_at": None,
        }
    return {
        "state": "SET",
        "version": row["version"],
        "timezone": row["timezone"],
        "quiet_start": row["quiet_start"].isoformat(timespec="minutes"),
        "quiet_end": row["quiet_end"].isoformat(timespec="minutes"),
        "digest_limit": row["digest_limit"],
        "recorded_at": row["recorded_at"],
    }


def set_preference(
    conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    try:
        bp.zone(body["timezone"])
    except ValueError as exc:
        raise invalid("timezone must be an IANA zone name", {"timezone": "UNKNOWN"}) from exc
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_PREFERENCE, "-", key, payload_digest)
    if receipt is not None:
        return preference_dto(current_preference(conn, investor_id)), 200
    current = current_preference(conn, investor_id)
    version = current["version"] if current else 0
    if body["expected_version"] != version:
        raise conflict(VERSION_CONFLICT, "preferences changed since you loaded them; reload")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_preference (investor_id, version, timezone, quiet_start, quiet_end,
                                           digest_limit, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, version + 1, body["timezone"], body["quiet_start"], body["quiet_end"],
             body["digest_limit"], actor_uid, dedupe_key(OP_PREFERENCE, "-", key)),
        )
        pref_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_PREFERENCE, "butler_preference", pref_id, version=version + 1)
    record_receipt(conn, investor_id, OP_PREFERENCE, "-", key, payload_digest,
                   {"preference_id": pref_id}, actor_uid)
    return preference_dto(current_preference(conn, investor_id)), 201


# =========================================================================================
# subscriptions -- explicit follow / pause / resume / unfollow / checkpoint
# =========================================================================================


def _subscription_rows(conn: Any, investor_id: str, ids: list[str] | None) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT s.id, s.subject_kind, s.isin, s.topic, s.topic_aliases, s.actor_uid,
               s.recorded_at
          FROM butler_subscription s
         WHERE s.investor_id = %(inv)s AND (%(ids)s::uuid[] IS NULL OR s.id = ANY(%(ids)s::uuid[]))
         ORDER BY s.recorded_at, s.id
        """,
        {"inv": investor_id, "ids": ids},
    )


def _subscription_events(conn: Any, investor_id: str, ids: list[str]) -> dict[str, list[dict]]:
    if not ids:
        return {}
    rows = _rows(
        conn,
        """
        SELECT subscription_id, version, action, checkpoint_days, recorded_at
          FROM butler_subscription_event
         WHERE investor_id = %s AND subscription_id = ANY(%s::uuid[])
         ORDER BY subscription_id, version
        """,
        (investor_id, ids),
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        out[row["subscription_id"]].append(row)
    return out


def derive_subscription(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Current state from the event history. Pure: the latest version decides."""
    state = "ACTIVE"
    checkpoint_days = None
    checkpoint_set_at = None
    for event in events:
        action = event["action"]
        if action == "PAUSE":
            state = "PAUSED"
        elif action == "RESUME":
            state = "ACTIVE"
        elif action == "UNFOLLOW":
            state = "ENDED"
        if action in ("FOLLOW", "SET_CHECKPOINT"):
            checkpoint_days = event["checkpoint_days"]
            checkpoint_set_at = event["recorded_at"] if checkpoint_days else None
    return {
        "state": state,
        "version": events[-1]["version"] if events else 0,
        "checkpoint_days": checkpoint_days,
        "checkpoint_set_at": checkpoint_set_at,
    }


def _checkpoint_view(sub: dict[str, Any], pref: dict[str, Any] | None, now: datetime) -> dict:
    """Checkpoint status. NOT_SET and TIMEZONE_REQUIRED are honest states, never defaults."""
    if not sub["checkpoint_days"]:
        return {"state": "NOT_SET", "every_days": None, "next_due_on": None,
                "offered_days": bp.CHECKPOINT_DAYS_OFFERED}
    if pref is None:
        return {"state": "TIMEZONE_REQUIRED", "every_days": sub["checkpoint_days"],
                "next_due_on": None, "offered_days": None}
    tz = bp.zone(pref["timezone"])
    anchor = sub["checkpoint_set_at"].astimezone(tz).date()
    today = now.astimezone(tz).date()
    due = bp.checkpoint_due_on(anchor, sub["checkpoint_days"], today)
    base = due or anchor
    return {
        "state": "SCHEDULED" if sub["state"] == "ACTIVE" else sub["state"],
        "every_days": sub["checkpoint_days"],
        "next_due_on": base + timedelta(days=sub["checkpoint_days"]),
        "offered_days": None,
    }


def _hydrate_subscriptions(
    conn: Any, investor_id: str, rows: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    events = _subscription_events(conn, investor_id, [r["id"] for r in rows])
    pref = current_preference(conn, investor_id)
    ids = identities(conn, [r["isin"] for r in rows if r["isin"]])
    out = []
    for row in rows:
        history = events.get(row["id"], [])
        derived = derive_subscription(history)
        out.append({
            "id": row["id"],
            "subject_kind": row["subject_kind"],
            "isin": row["isin"],
            "identity": ids.get(row["isin"]) if row["isin"] else None,
            "topic": row["topic"],
            "topic_aliases": list(row["topic_aliases"] or []),
            "state": derived["state"],
            "version": derived["version"],
            "checkpoint": _checkpoint_view(derived, pref, now),
            "recorded_at": row["recorded_at"],
            "history": [
                {"version": e["version"], "action": e["action"],
                 "checkpoint_days": e["checkpoint_days"], "recorded_at": e["recorded_at"]}
                for e in history
            ],
            # Stated per subscription so an empty inbox is never read as an all-clear.
            "coverage": {
                "saved_workspace_records": "CHECKED",
                "external_sources": "NOT_CHECKED",
                "topic_matching": "NOT_CHECKED" if row["subject_kind"] == "TOPIC" else None,
            },
        })
    return out


def subscription_by_id(conn: Any, investor_id: str, subscription_id: str) -> dict[str, Any]:
    sid = _uuid(subscription_id, "subscription")
    rows = _subscription_rows(conn, investor_id, [sid])
    if not rows:
        raise not_found("subscription")
    return _hydrate_subscriptions(conn, investor_id, rows, _now())[0]


def list_subscriptions(
    conn: Any, investor_id: str, include_ended: bool, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "butler.subscriptions", {"ended": include_ended})
    after = canonical.decode_cursor(cursor, scope)
    rows = _rows(
        conn,
        """
        SELECT s.id, s.subject_kind, s.isin, s.topic, s.topic_aliases, s.actor_uid,
               s.recorded_at, e.action AS last_action
          FROM butler_subscription s
          JOIN LATERAL (SELECT action FROM butler_subscription_event x
                         WHERE x.investor_id = s.investor_id AND x.subscription_id = s.id
                         ORDER BY version DESC LIMIT 1) e ON true
         WHERE s.investor_id = %(inv)s
           AND (%(ended)s OR e.action <> 'UNFOLLOW')
           AND (%(k0)s::timestamptz IS NULL
                OR (s.recorded_at, s.id) > (%(k0)s::timestamptz, %(k1)s::uuid))
         ORDER BY s.recorded_at, s.id
         LIMIT %(lim)s
        """,
        {"inv": investor_id, "ended": include_ended,
         "k0": after[0] if after else None, "k1": after[1] if after else None,
         "lim": size + 1},
    )
    more = len(rows) > size
    rows = rows[:size]
    items = _hydrate_subscriptions(conn, investor_id, rows, _now())
    next_cursor = (canonical.encode_cursor(scope, [rows[-1]["recorded_at"].isoformat(),
                                                   rows[-1]["id"]])
                   if more and rows else None)
    return {"items": items, "next_cursor": next_cursor}


def _active_company_subscription(conn: Any, investor_id: str, family: list[str]) -> str | None:
    row = _one(
        conn,
        """
        SELECT s.id FROM butler_subscription s
         WHERE s.investor_id = %s AND s.subject_kind = 'COMPANY' AND s.isin = ANY(%s::text[])
           AND (SELECT action FROM butler_subscription_event x
                 WHERE x.investor_id = s.investor_id AND x.subscription_id = s.id
                 ORDER BY version DESC LIMIT 1) <> 'UNFOLLOW'
         ORDER BY s.recorded_at LIMIT 1
        """,
        (investor_id, family),
    )
    return row["id"] if row else None


def _active_topic_subscription(conn: Any, investor_id: str, topic: str) -> str | None:
    row = _one(
        conn,
        """
        SELECT s.id FROM butler_subscription s
         WHERE s.investor_id = %s AND s.subject_kind = 'TOPIC' AND lower(s.topic) = lower(%s)
           AND (SELECT action FROM butler_subscription_event x
                 WHERE x.investor_id = s.investor_id AND x.subscription_id = s.id
                 ORDER BY version DESC LIMIT 1) <> 'UNFOLLOW'
         LIMIT 1
        """,
        (investor_id, topic),
    )
    return row["id"] if row else None


def follow(
    conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Explicit follow. COMPANY needs an exact ISIN of an already-linked company; a ticker or
    name is refused, because resolving one is a guess (ISINs are unambiguous; tickers are
    renamed and reused). An existing active follow of the same identity family is returned,
    not duplicated."""
    kind = body["subject_kind"]
    if kind == "COMPANY":
        raw = body.get("isin")
        if not raw:
            raise invalid("following a company needs its ISIN; search securities first",
                          {"isin": "REQUIRED"}, code=ISIN_REQUIRED)
        isin = require_linked(conn, investor_id, _valid_isin(raw))
        family = isin_family(conn, isin)
        subject_scope = isin
    else:
        topic = (body.get("topic") or "").strip()
        if not topic:
            raise invalid("a topic needs a name", {"topic": "REQUIRED"})
        isin = None
        family = []
        subject_scope = "topic"
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_FOLLOW, subject_scope, key, payload_digest)
    if receipt is not None:
        return subscription_by_id(conn, investor_id, receipt.result["subscription_id"]), 200

    existing = (_active_company_subscription(conn, investor_id, family) if kind == "COMPANY"
                else _active_topic_subscription(conn, investor_id, topic))
    if existing is not None:
        record_receipt(conn, investor_id, OP_FOLLOW, subject_scope, key, payload_digest,
                       {"subscription_id": existing}, actor_uid, status_code=200)
        return subscription_by_id(conn, investor_id, existing), 200

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_subscription (investor_id, subject_kind, isin, topic,
                                             topic_aliases, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, kind, isin, topic if kind == "TOPIC" else None,
             list(body.get("topic_aliases") or []) if kind == "TOPIC" else [],
             actor_uid, dedupe_key(OP_FOLLOW, subject_scope, key)),
        )
        sub_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO butler_subscription_event (investor_id, subscription_id, version, action,
                                                   checkpoint_days, actor_uid, dedupe_key)
            VALUES (%s, %s, 1, 'FOLLOW', %s, %s, %s)
            """,
            (investor_id, sub_id, body.get("checkpoint_days"), actor_uid,
             dedupe_key(OP_FOLLOW, subject_scope, key, "event")),
        )
    audit_write(conn, actor_uid, OP_FOLLOW, "butler_subscription", sub_id)
    record_receipt(conn, investor_id, OP_FOLLOW, subject_scope, key, payload_digest,
                   {"subscription_id": sub_id}, actor_uid)
    return subscription_by_id(conn, investor_id, sub_id), 201


def subscription_event(
    conn: Any, investor_id: str, actor_uid: str, subscription_id: str, body: dict[str, Any],
    key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Version-checked pause/resume/unfollow/checkpoint. History is preserved; nothing is
    deleted, and unfollowing never blocks privacy rights over what was recorded."""
    sub = subscription_by_id(conn, investor_id, subscription_id)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_SUBSCRIPTION_EVENT, sub["id"], key,
                           payload_digest)
    if receipt is not None:
        return subscription_by_id(conn, investor_id, sub["id"]), 200
    current = subscription_by_id(conn, investor_id, sub["id"])
    if body["expected_version"] != current["version"]:
        raise conflict(VERSION_CONFLICT, "this follow changed since you loaded it; reload")
    action = body["action"]
    if current["state"] == "ENDED":
        raise conflict(INVALID_TRANSITION, "this follow has ended; follow again to restart")
    if (action == "PAUSE" and current["state"] == "PAUSED") or (
            action == "RESUME" and current["state"] != "PAUSED"):
        raise conflict(INVALID_TRANSITION, f"cannot {action.lower()} a {current['state'].lower()} "
                       "follow")
    if action == "SET_CHECKPOINT" and current["subject_kind"] != "COMPANY":
        raise invalid("checkpoints apply to followed companies", {"action": "SUBJECT"})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_subscription_event (investor_id, subscription_id, version, action,
                                                   checkpoint_days, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, sub["id"], current["version"] + 1, action,
             body.get("checkpoint_days") if action == "SET_CHECKPOINT" else None, actor_uid,
             dedupe_key(OP_SUBSCRIPTION_EVENT, sub["id"], key)),
        )
        event_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_SUBSCRIPTION_EVENT, "butler_subscription", sub["id"],
                action=action)
    record_receipt(conn, investor_id, OP_SUBSCRIPTION_EVENT, sub["id"], key, payload_digest,
                   {"event_id": event_id}, actor_uid)
    return subscription_by_id(conn, investor_id, sub["id"]), 201


# =========================================================================================
# memory -- explicit references to saved, attributed records
# =========================================================================================

_REF_COLUMN = {"NOTE": "note_id", "ANSWER": "answer_id", "THESIS": "thesis_id",
               "CHAT_MESSAGE": "chat_message_id"}

_NOTE_ATTRIBUTION = {
    "INTERPRETATION": "USER_HYPOTHESIS",
    "THESIS_NOTE": "USER_HYPOTHESIS",
    "DECISION": "USER_DECISION",
    "QUESTION": "USER_QUESTION",
    "OBSERVATION": "USER_STATEMENT",
    "LITERATURE": "USER_STATEMENT",
}


def _referenced_records(
    conn: Any, investor_id: str, refs: list[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """(kind, id) -> {isin, attribution, title, text, recorded_at, is_latest}. Attribution is
    READ from the record (authorship, role), never stored beside it."""
    by_kind: dict[str, list[str]] = defaultdict(list)
    for kind, rid in refs:
        by_kind[kind].append(rid)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if by_kind.get("NOTE"):
        for r in _rows(
            conn,
            """
            SELECT n.id, n.isin, n.kind, n.title, n.body, n.authorship, n.recorded_at,
                   NOT EXISTS (SELECT 1 FROM note s WHERE s.investor_id = n.investor_id
                                AND s.previous_id = n.id) AS is_latest
              FROM note n WHERE n.investor_id = %s AND n.id = ANY(%s::uuid[])
            """,
            (investor_id, by_kind["NOTE"]),
        ):
            attribution = ("ACCEPTED_ASSISTANT_TEXT" if r["authorship"] == "ACCEPTED_AGENT"
                           else _NOTE_ATTRIBUTION.get(r["kind"], "USER_STATEMENT"))
            out[("NOTE", r["id"])] = {
                "isin": r["isin"], "attribution": attribution, "title": r["title"],
                "text": r["body"] or r["title"], "recorded_at": r["recorded_at"],
                "is_latest": r["is_latest"],
            }
    if by_kind.get("ANSWER"):
        for r in _rows(
            conn,
            """
            SELECT a.id, q.isin, a.answer, a.recorded_at, n.title,
                   NOT EXISTS (SELECT 1 FROM question_answer s WHERE s.investor_id = a.investor_id
                                AND s.previous_id = a.id) AS is_latest
              FROM question_answer a
              JOIN question q ON q.investor_id = a.investor_id AND q.id = a.question_id
              JOIN note n ON n.investor_id = q.investor_id AND n.id = q.note_id
             WHERE a.investor_id = %s AND a.id = ANY(%s::uuid[])
            """,
            (investor_id, by_kind["ANSWER"]),
        ):
            out[("ANSWER", r["id"])] = {
                "isin": r["isin"], "attribution": "USER_ANSWER", "title": r["title"],
                "text": r["answer"], "recorded_at": r["recorded_at"], "is_latest": r["is_latest"],
            }
    if by_kind.get("THESIS"):
        for r in _rows(
            conn,
            """
            SELECT t.id, t.isin, t.version, t.argument, t.recorded_at,
                   NOT EXISTS (SELECT 1 FROM thesis s WHERE s.investor_id = t.investor_id
                                AND s.isin = t.isin AND s.version > t.version) AS is_latest
              FROM thesis t WHERE t.investor_id = %s AND t.id = ANY(%s::uuid[])
            """,
            (investor_id, by_kind["THESIS"]),
        ):
            out[("THESIS", r["id"])] = {
                "isin": r["isin"], "attribution": "USER_BELIEF",
                "title": f"Thesis version {r['version']}", "text": r["argument"],
                "recorded_at": r["recorded_at"], "is_latest": r["is_latest"],
            }
    if by_kind.get("CHAT_MESSAGE"):
        for r in _rows(
            conn,
            """
            SELECT m.id, t.isin, m.role, m.body, m.recorded_at, t.title, res.output
              FROM chat_message m
              JOIN chat_thread t ON t.investor_id = m.investor_id AND t.id = m.thread_id
              LEFT JOIN task_result res ON res.investor_id = m.investor_id AND res.id = m.result_id
             WHERE m.investor_id = %s AND m.id = ANY(%s::uuid[])
            """,
            (investor_id, by_kind["CHAT_MESSAGE"]),
        ):
            assistant = r["role"] == "ASSISTANT"
            out[("CHAT_MESSAGE", r["id"])] = {
                "isin": r["isin"],
                "attribution": "ASSISTANT_SUGGESTION" if assistant else "USER_STATEMENT",
                "title": r["title"], "text": (r["output"] if assistant else r["body"]) or "",
                "recorded_at": r["recorded_at"], "is_latest": True,
            }
    return out


def _memory_rows(conn: Any, investor_id: str, where: str, params: dict) -> list[dict]:
    return _rows(
        conn,
        f"""
        SELECT m.id, m.isin, m.ref_kind, m.note_id, m.answer_id, m.thesis_id, m.chat_message_id,
               m.recorded_at, m.actor_uid, e.action AS end_action, e.superseded_by,
               e.recorded_at AS ended_at
          FROM butler_memory m
          LEFT JOIN butler_memory_event e ON e.investor_id = m.investor_id AND e.memory_id = m.id
         WHERE m.investor_id = %(inv)s AND {where}
         ORDER BY m.recorded_at DESC, m.id DESC
        """,
        {"inv": investor_id, **params},
    )


def _ref_id(row: dict[str, Any]) -> str:
    return row[_REF_COLUMN[row["ref_kind"]]]


def _hydrate_memories(conn: Any, investor_id: str, rows: list[dict]) -> list[dict[str, Any]]:
    records = _referenced_records(conn, investor_id, [(r["ref_kind"], _ref_id(r)) for r in rows])
    visible = visible_isins(conn, investor_id)
    out = []
    for row in rows:
        record = records.get((row["ref_kind"], _ref_id(row)))
        shown = record is not None and record["isin"] in visible
        status = {"FORGET": "FORGOTTEN", "SUPERSEDE": "SUPERSEDED"}.get(row["end_action"] or "",
                                                                       "ACTIVE")
        out.append({
            "id": row["id"],
            "isin": row["isin"],
            "ref_kind": row["ref_kind"],
            "ref_id": _ref_id(row),
            "attribution": record["attribution"] if record else "UNAVAILABLE",
            "status": status,
            "superseded_by": row["superseded_by"],
            "recorded_at": row["recorded_at"],
            "ended_at": row["ended_at"],
            "record": {
                "available": shown,
                "title": record["title"] if shown else None,
                "preview": _preview(record["text"]) if shown else None,
                "recorded_at": record["recorded_at"] if shown else None,
                "is_latest_version": record["is_latest"] if shown else None,
            },
        })
    return out


def memory_by_id(conn: Any, investor_id: str, memory_id: str) -> dict[str, Any]:
    mid = _uuid(memory_id, "memory")
    rows = _memory_rows(conn, investor_id, "m.id = %(mid)s", {"mid": mid})
    if not rows:
        raise not_found("memory")
    return _hydrate_memories(conn, investor_id, rows)[0]


def list_memories(
    conn: Any, investor_id: str, isin: str | None, include_ended: bool, cursor: str | None,
    limit: int | None,
) -> dict[str, Any]:
    size = canonical.clamp_limit(limit)
    family = isin_family(conn, _valid_isin(isin)) if isin else None
    scope = canonical.scope_digest(investor_id, "butler.memories",
                                   {"isin": isin, "ended": include_ended})
    after = canonical.decode_cursor(cursor, scope)
    rows = _memory_rows(
        conn, investor_id,
        """(%(fam)s::text[] IS NULL OR m.isin = ANY(%(fam)s::text[]))
           AND (%(ended)s OR e.action IS NULL)
           AND (%(k0)s::timestamptz IS NULL
                OR (m.recorded_at, m.id) < (%(k0)s::timestamptz, %(k1)s::uuid))""",
        {"fam": family, "ended": include_ended,
         "k0": after[0] if after else None, "k1": after[1] if after else None},
    )
    more = len(rows) > size
    rows = rows[:size]
    return {
        "items": _hydrate_memories(conn, investor_id, rows),
        "next_cursor": (canonical.encode_cursor(scope, [rows[-1]["recorded_at"].isoformat(),
                                                        rows[-1]["id"]])
                        if more and rows else None),
    }


def forgotten_refs(conn: Any, investor_id: str) -> set[tuple[str, str]]:
    """Every record the investor asked Butler to forget. The tombstone set retrieval, the
    renderer and the worker all consult, so a late event cannot resurrect one."""
    rows = _rows(
        conn,
        """
        SELECT m.ref_kind, m.note_id, m.answer_id, m.thesis_id, m.chat_message_id
          FROM butler_memory m
          JOIN butler_memory_event e ON e.investor_id = m.investor_id AND e.memory_id = m.id
         WHERE m.investor_id = %s AND e.action = 'FORGET'
        """,
        (investor_id,),
    )
    return {(r["ref_kind"], _ref_id(r)) for r in rows}


def remember(
    conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Explicit remember of ONE saved record version. No free text is accepted, so an unsaved
    draft or an arbitrary chat sentence cannot become memory."""
    kind = body["ref_kind"]
    ref_id = _uuid(body["ref_id"], "record")
    record = _referenced_records(conn, investor_id, [(kind, ref_id)]).get((kind, ref_id))
    if record is None:
        raise not_found("record")
    if record["isin"] not in visible_isins(conn, investor_id):
        require_unlocked(conn, investor_id, record["isin"])
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_REMEMBER, ref_id, key, payload_digest)
    if receipt is not None:
        return memory_by_id(conn, investor_id, receipt.result["memory_id"]), 200
    existing = _one(
        conn,
        f"""
        SELECT m.id FROM butler_memory m
         WHERE m.investor_id = %s AND m.{_REF_COLUMN[kind]} = %s
           AND NOT EXISTS (SELECT 1 FROM butler_memory_event e
                            WHERE e.investor_id = m.investor_id AND e.memory_id = m.id)
         LIMIT 1
        """,
        (investor_id, ref_id),
    )
    if existing is not None:
        record_receipt(conn, investor_id, OP_REMEMBER, ref_id, key, payload_digest,
                       {"memory_id": existing["id"]}, actor_uid, status_code=200)
        return memory_by_id(conn, investor_id, existing["id"]), 200
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO butler_memory (investor_id, isin, ref_kind, {_REF_COLUMN[kind]},
                                       actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, record["isin"], kind, ref_id, actor_uid,
             dedupe_key(OP_REMEMBER, ref_id, key)),
        )
        memory_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_REMEMBER, "butler_memory", memory_id, ref_kind=kind)
    record_receipt(conn, investor_id, OP_REMEMBER, ref_id, key, payload_digest,
                   {"memory_id": memory_id}, actor_uid)
    return memory_by_id(conn, investor_id, memory_id), 201


def _chain_successor(conn: Any, investor_id: str, kind: str, old_id: str, new_id: str) -> bool:
    """Is ``new_id`` a later version of ``old_id`` in the same record chain?"""
    if kind in ("NOTE", "ANSWER"):
        table = "note" if kind == "NOTE" else "question_answer"
        row = _one(
            conn,
            f"""
            WITH RECURSIVE chain(id) AS (
              SELECT %(new)s::uuid
              UNION
              SELECT p.previous_id FROM chain c JOIN {table} p
                ON p.investor_id = %(inv)s AND p.id = c.id WHERE p.previous_id IS NOT NULL
            ) SELECT 1 AS x FROM chain WHERE id = %(old)s::uuid
            """,
            {"inv": investor_id, "new": new_id, "old": old_id},
        )
        return row is not None and old_id != new_id
    if kind == "THESIS":
        row = _one(
            conn,
            """
            SELECT 1 AS x FROM thesis o JOIN thesis n
              ON n.investor_id = o.investor_id AND ws_same_security(n.isin, o.isin)
             WHERE o.investor_id = %s AND o.id = %s AND n.id = %s AND n.version > o.version
            """,
            (investor_id, old_id, new_id),
        )
        return row is not None
    return False


def memory_event(
    conn: Any, investor_id: str, actor_uid: str, memory_id: str, body: dict[str, Any],
    key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """FORGET (tombstone) or SUPERSEDE (by a memory of a later version of the same record).

    Forgetting removes the record from Butler retrieval and invalidates cards that quoted it;
    it never deletes the note, answer or thesis -- that is the editor's and privacy erasure's
    job, and forgetting a reminder must not silently destroy source evidence."""
    memory = memory_by_id(conn, investor_id, memory_id)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_MEMORY_EVENT, memory["id"], key, payload_digest)
    if receipt is not None:
        return memory_by_id(conn, investor_id, memory["id"]), 200
    memory = memory_by_id(conn, investor_id, memory["id"])
    if memory["status"] != "ACTIVE":
        raise conflict(INVALID_TRANSITION, "this memory was already forgotten or superseded")
    successor = None
    if body["action"] == "SUPERSEDE":
        successor = memory_by_id(conn, investor_id, body["superseded_by"] or "")
        if (successor["status"] != "ACTIVE" or successor["ref_kind"] != memory["ref_kind"]
                or not _chain_successor(conn, investor_id, memory["ref_kind"], memory["ref_id"],
                                        successor["ref_id"])):
            raise invalid("a memory can only be superseded by a memory of a later version of "
                          "the same record", {"superseded_by": "NOT_A_LATER_VERSION"})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_memory_event (investor_id, memory_id, action, superseded_by,
                                             actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, memory["id"], body["action"], successor["id"] if successor else None,
             actor_uid, dedupe_key(OP_MEMORY_EVENT, memory["id"], key)),
        )
        event_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_MEMORY_EVENT, "butler_memory", memory["id"],
                action=body["action"])
    record_receipt(conn, investor_id, OP_MEMORY_EVENT, memory["id"], key, payload_digest,
                   {"event_id": event_id}, actor_uid)
    return memory_by_id(conn, investor_id, memory["id"]), 201


# =========================================================================================
# activity -- DERIVED from the rows that already are the receipts
# =========================================================================================


def activity_counts(
    conn: Any, investor_id: str, family: list[str], start: datetime, end: datetime
) -> dict[str, int]:
    """What was RECORDED HERE in [start, end). Not attention, not time, not knowledge.

    Saves, linked evidence and answers are counted from their own immutable rows. A saved
    QUESTION note is counted as a question, not a note.
    """
    row = _one(
        conn,
        """
        SELECT
          (SELECT count(*) FROM note n WHERE n.investor_id = %(inv)s
              AND n.isin = ANY(%(fam)s::text[]) AND n.kind <> 'QUESTION'
              AND n.recorded_at >= %(s)s AND n.recorded_at < %(e)s) AS notes_saved,
          (SELECT count(*) FROM note_link l JOIN note n
              ON n.investor_id = l.investor_id AND n.id = l.from_note_id
            WHERE l.investor_id = %(inv)s AND n.isin = ANY(%(fam)s::text[])
              AND (l.source_id IS NOT NULL OR l.relation IN ('SUPPORTS','CONTRADICTS'))
              AND l.recorded_at >= %(s)s AND l.recorded_at < %(e)s) AS evidence_linked,
          (SELECT count(*) FROM question_answer a JOIN question q
              ON q.investor_id = a.investor_id AND q.id = a.question_id
            WHERE a.investor_id = %(inv)s AND q.isin = ANY(%(fam)s::text[])
              AND a.recorded_at >= %(s)s AND a.recorded_at < %(e)s) AS answers_recorded,
          (SELECT count(*) FROM question q WHERE q.investor_id = %(inv)s
              AND q.isin = ANY(%(fam)s::text[]) AND q.recorded_at < %(e)s
              AND NOT EXISTS (SELECT 1 FROM question_answer a WHERE a.investor_id = q.investor_id
                               AND a.question_id = q.id AND a.recorded_at < %(e)s))
              AS open_questions
        """,
        {"inv": investor_id, "fam": family, "s": start, "e": end},
    )
    return {k: int(v) for k, v in (row or {}).items()}


# =========================================================================================
# source retraction
# =========================================================================================


def retraction_for(conn: Any, investor_id: str, source_ids: list[str]) -> dict[str, dict]:
    if not source_ids:
        return {}
    rows = _rows(
        conn,
        "SELECT id, source_id, reason_code, replacement_source_id, recorded_at FROM "
        "butler_source_retraction WHERE investor_id = %s AND source_id = ANY(%s::uuid[])",
        (investor_id, source_ids),
    )
    return {r["source_id"]: r for r in rows}


def retract_source(
    conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    source_id = _uuid(body["source_id"], "source")
    source = _one(conn, "SELECT id, isin FROM research_source WHERE investor_id = %s AND id = %s",
                  (investor_id, source_id))
    if source is None:
        raise not_found("source")
    require_unlocked(conn, investor_id, source["isin"])
    replacement = body.get("replacement_source_id")
    if replacement is not None:
        replacement = _uuid(replacement, "source")
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_RETRACTION, source_id, key, payload_digest)
    if receipt is not None:
        return retraction_for(conn, investor_id, [source_id])[source_id], 200
    if retraction_for(conn, investor_id, [source_id]):
        raise conflict(INVALID_TRANSITION, "this source is already marked")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_source_retraction (investor_id, isin, source_id, reason_code,
                                                  replacement_source_id, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, source["isin"], source_id, body["reason_code"], replacement,
             actor_uid, dedupe_key(OP_RETRACTION, source_id, key)),
        )
        retraction_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_RETRACTION, "research_source", source_id,
                reason=body["reason_code"])
    record_receipt(conn, investor_id, OP_RETRACTION, source_id, key, payload_digest,
                   {"retraction_id": retraction_id}, actor_uid)
    return retraction_for(conn, investor_id, [source_id])[source_id], 201


# =========================================================================================
# inbox
# =========================================================================================

_ITEM_COLUMNS = """
i.id, i.seq, i.card_kind, i.priority, i.isin, i.subscription_id, i.template_id,
i.template_version, i.rule_version, i.issue_key, i.evidence_version, i.args, i.source_id,
i.thesis_id, i.evaluation_id, i.supersedes_item_id, i.deliver_after, i.recorded_at
"""


def item_status(events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Seen/snoozed/dismissed/resolved from the item's event history. Pure."""
    seen = dismissed = resolved = False
    snoozed_until = None
    for event in events:
        action = event["action"]
        if action == "SEEN":
            seen = True
        elif action == "SNOOZE":
            snoozed_until = event["snooze_until"]
            seen = True
        elif action == "DISMISS":
            dismissed = True
        elif action == "RESOLVE":
            resolved = True
        elif action == "RESTORE":
            dismissed = resolved = False
            snoozed_until = None
    snoozed = snoozed_until is not None and snoozed_until > now
    return {
        "version": events[-1]["version"] if events else 0,
        "seen": seen,
        "snoozed_until": snoozed_until if snoozed else None,
        "dismissed": dismissed,
        "resolved": resolved,
    }


def _item_events(conn: Any, investor_id: str, ids: list[str]) -> dict[str, list[dict]]:
    if not ids:
        return {}
    rows = _rows(
        conn,
        "SELECT item_id, version, action, snooze_until, recorded_at FROM butler_inbox_event "
        "WHERE investor_id = %s AND item_id = ANY(%s::uuid[]) ORDER BY item_id, version",
        (investor_id, ids),
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        out[row["item_id"]].append(row)
    return out


def inbox_rows(
    conn: Any, investor_id: str, ids: list[str] | None = None, after_seq: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        f"""
        SELECT {_ITEM_COLUMNS},
               (SELECT n.id FROM butler_inbox_item n WHERE n.investor_id = i.investor_id
                 AND n.supersedes_item_id = i.id) AS superseded_by_item_id
          FROM butler_inbox_item i
         WHERE i.investor_id = %(inv)s
           AND (%(ids)s::uuid[] IS NULL OR i.id = ANY(%(ids)s::uuid[]))
           AND (%(after)s::bigint IS NULL OR i.seq < %(after)s::bigint)
         ORDER BY i.seq DESC
         LIMIT %(lim)s
        """,
        {"inv": investor_id, "ids": ids, "after": after_seq, "lim": limit},
    )
    events = _item_events(conn, investor_id, [r["id"] for r in rows])
    for row in rows:
        row["status_events"] = events.get(row["id"], [])
    return rows


def worker_status(conn: Any, investor_id: str, now: datetime) -> dict[str, Any]:
    """Truthful last success and lag. No tick yet is NEVER, not a green light."""
    last_ok = _one(
        conn,
        "SELECT finished_at FROM butler_tick WHERE investor_id = %s AND outcome = 'OK' "
        "ORDER BY finished_at DESC LIMIT 1",
        (investor_id,),
    )
    last_any = _one(
        conn,
        "SELECT finished_at, outcome FROM butler_tick WHERE investor_id = %s "
        "ORDER BY finished_at DESC LIMIT 1",
        (investor_id,),
    )
    pending = _one(
        conn,
        """
        SELECT count(*) AS n, min(o.recorded_at) AS oldest FROM butler_outbox o
         WHERE o.investor_id = %s AND NOT EXISTS (SELECT 1 FROM butler_outbox_effect f
                WHERE f.investor_id = o.investor_id AND f.outbox_id = o.id)
        """,
        (investor_id,),
    )
    oldest = pending["oldest"] if pending else None
    return {
        "last_success_at": last_ok["finished_at"] if last_ok else None,
        "last_run_at": last_any["finished_at"] if last_any else None,
        "last_outcome": last_any["outcome"] if last_any else "NEVER",
        "pending_events": int(pending["n"]) if pending else 0,
        "oldest_pending_at": oldest,
        "lag_seconds": int((now - oldest).total_seconds()) if oldest else 0,
        "delivery": "IN_APP_ONLY",
        "model_execution": "NOT_CONNECTED",
    }


def inbox_event(
    conn: Any, investor_id: str, actor_uid: str, item_id: str, body: dict[str, Any], key: str,
    payload_digest: str, now: datetime,
) -> dict[str, Any]:
    """Seen/snooze/dismiss/resolve/restore with an expected version. Returns the new status.
    Resolving a reminder records nothing about the company."""
    iid = _uuid(item_id, "inbox item")
    rows = inbox_rows(conn, investor_id, [iid])
    if not rows:
        raise not_found("inbox item")
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_INBOX_EVENT, iid, key, payload_digest)
    if receipt is not None:
        return {"item_id": iid, "status": item_status(
            _item_events(conn, investor_id, [iid]).get(iid, []), now), "replayed": True}
    status = item_status(_item_events(conn, investor_id, [iid]).get(iid, []), now)
    if body["expected_version"] != status["version"]:
        raise conflict(VERSION_CONFLICT, "this item changed since you loaded it; reload")
    snooze_until = body.get("snooze_until")
    if body["action"] == "SNOOZE":
        if snooze_until is None:
            raise invalid("snooze needs snooze_until", {"snooze_until": "REQUIRED"})
        if snooze_until <= now or snooze_until > now + bp.MAX_SNOOZE:
            raise invalid("snooze_until must be in the next 90 days",
                          {"snooze_until": "RANGE"})
    elif snooze_until is not None:
        raise invalid("snooze_until applies only to SNOOZE", {"snooze_until": "UNEXPECTED"})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_inbox_event (investor_id, item_id, version, action, snooze_until,
                                            actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, iid, status["version"] + 1, body["action"], snooze_until, actor_uid,
             dedupe_key(OP_INBOX_EVENT, iid, key)),
        )
        event_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_INBOX_EVENT, "butler_inbox_item", iid, action=body["action"])
    record_receipt(conn, investor_id, OP_INBOX_EVENT, iid, key, payload_digest,
                   {"event_id": event_id}, actor_uid)
    return {"item_id": iid, "status": item_status(
        _item_events(conn, investor_id, [iid]).get(iid, []), now), "replayed": False}


# =========================================================================================
# render context -- everything a card cites, re-read under current scope
# =========================================================================================


def render_inputs(conn: Any, investor_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Current rows for every record the given items cite. Read at render time, every time:
    a card is never served from what was true when it was created."""
    source_ids: set[str] = set()
    thesis_ids: set[str] = set()
    evaluation_ids: set[str] = set()
    for item in items:
        args = item["args"] or {}
        for k in ("source_id",):
            if args.get(k):
                source_ids.add(args[k])
        for k in ("old_thesis_id", "new_thesis_id", "thesis_id"):
            if args.get(k):
                thesis_ids.add(args[k])
        if args.get("evaluation_id"):
            evaluation_ids.add(args["evaluation_id"])
    sources = {r["id"]: r for r in _rows(
        conn,
        """
        SELECT s.id, s.isin, s.source, s.source_type, s.source_url, s.publication_date,
               s.period, s.retrieved_at, s.recorded_at, s.content_sha256,
               e.status AS extraction_status, e.unavailable_reason
          FROM research_source s
          LEFT JOIN research_source_extraction e
            ON e.investor_id = s.investor_id AND e.source_id = s.id
         WHERE s.investor_id = %s AND s.id = ANY(%s::uuid[])
        """,
        (investor_id, sorted(source_ids)),
    )} if source_ids else {}
    theses = {r["id"]: r for r in _rows(
        conn,
        "SELECT id, isin, version, version_on, argument, intended_holding_period, recorded_at "
        "FROM thesis WHERE investor_id = %s AND id = ANY(%s::uuid[])",
        (investor_id, sorted(thesis_ids)),
    )} if thesis_ids else {}
    evaluations = {r["id"]: r for r in _rows(
        conn,
        "SELECT id, thesis_id, state, evidence_cutoff, input_digest FROM trigger_evaluation "
        "WHERE investor_id = %s AND id = ANY(%s::uuid[])",
        (investor_id, sorted(evaluation_ids)),
    )} if evaluation_ids else {}
    isins = sorted({i["isin"] for i in items})
    return {
        "sources": sources,
        "retractions": retraction_for(conn, investor_id, sorted(source_ids)),
        "theses": theses,
        "evaluations": evaluations,
        "identities": identities(conn, isins),
        "visible": visible_isins(conn, investor_id),
        "forgotten": forgotten_refs(conn, investor_id),
    }


# =========================================================================================
# decision reviews
# =========================================================================================


def _latest_thesis(conn: Any, investor_id: str, family: list[str]) -> dict[str, Any] | None:
    return _one(
        conn,
        """
        SELECT id, isin, version, status, argument, anti_thesis, intended_holding_period,
               version_on, recorded_at
          FROM thesis WHERE investor_id = %s AND isin = ANY(%s::text[])
         ORDER BY version DESC, recorded_at DESC LIMIT 1
        """,
        (investor_id, family),
    )


def review_premises(thesis: dict[str, Any] | None) -> set[str]:
    """What a review can truthfully quote. External coverage is always a recorded gap in v1:
    no permitted source feed exists, so SOURCE_COVERAGE_GAP's premise always holds."""
    available = {bp.REQUIRES_COVERAGE_GAP}
    if thesis is not None and (thesis["argument"] or "").strip():
        available.add(bp.REQUIRES_BELIEF)
    if thesis is not None and (thesis["intended_holding_period"] or "").strip():
        available.add(bp.REQUIRES_HORIZON)
    return available


def _review_row(conn: Any, investor_id: str, review_id: str) -> dict[str, Any]:
    rid = _uuid(review_id, "review")
    row = _one(
        conn,
        """
        SELECT r.id, r.isin, r.decision_kind, r.thesis_id, r.checklist_id, r.checklist_version,
               r.question_ids, r.trigger_item_id, r.supersedes_review_id, r.actor_uid,
               r.recorded_at,
               (SELECT n.id FROM butler_review n WHERE n.investor_id = r.investor_id
                 AND n.supersedes_review_id = r.id) AS superseded_by_review_id
          FROM butler_review r WHERE r.investor_id = %s AND r.id = %s
        """,
        (investor_id, rid),
    )
    if row is None:
        raise not_found("review")
    return row


def start_review(
    conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[str, int]:
    """Open a review with the current thesis version and checklist PINNED. Returns its id."""
    isin = require_unlocked(conn, investor_id, body["isin"])
    family = isin_family(conn, isin)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_REVIEW_START, isin, key, payload_digest)
    if receipt is not None:
        return receipt.result["review_id"], 200
    supersedes = body.get("supersedes_review_id")
    if supersedes is not None:
        prior = _review_row(conn, investor_id, supersedes)
        if prior["isin"] not in family:
            raise invalid("a superseding review must be about the same company",
                          {"supersedes_review_id": "OTHER_COMPANY"})
        if prior["superseded_by_review_id"] is not None:
            raise conflict(STALE_PREDECESSOR, "that review was already superseded; reload")
        supersedes = prior["id"]
    trigger = body.get("trigger_item_id")
    if trigger is not None:
        trigger = _uuid(trigger, "inbox item")
        if not inbox_rows(conn, investor_id, [trigger]):
            raise not_found("inbox item")
    thesis = _latest_thesis(conn, investor_id, family)
    questions = bp.checklist_for(body["decision_kind"], review_premises(thesis))
    if not questions:
        raise invalid("no checklist question applies to this review", {"decision_kind": "EMPTY"})
    # BT 14: at most one personalised question from an active, current notebook pattern. The
    # checklist is unchanged; the extra field is answerable, dismissible (not applicable, with
    # a reason) and explained by GET /butler/notebook/why.
    from app.db import butler_notebook as bn

    personal = bn.select_personal_question(conn, investor_id, isin, body["decision_kind"],
                                           tuple(questions))
    if personal is not None and len(questions) < 7:
        questions = (*questions, personal["question_id"])
    else:
        personal = None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_review (investor_id, isin, decision_kind, thesis_id, checklist_id,
                                       checklist_version, question_ids, trigger_item_id,
                                       supersedes_review_id, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, thesis["isin"] if thesis else isin, body["decision_kind"],
             thesis["id"] if thesis else None, body["decision_kind"], bp.CHECKLIST_VERSION,
             list(questions), trigger, supersedes, actor_uid,
             dedupe_key(OP_REVIEW_START, isin, key)),
        )
        review_id = str(cur.fetchone()[0])
    if personal is not None:
        bn.record_coaching_use(conn, investor_id, review_id, personal,
                               personal["consent_version"])
    audit_write(conn, actor_uid, OP_REVIEW_START, "butler_review", review_id,
                questions=len(questions))
    record_receipt(conn, investor_id, OP_REVIEW_START, isin, key, payload_digest,
                   {"review_id": review_id}, actor_uid)
    return review_id, 201


def _responses(conn: Any, investor_id: str, review_id: str) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        """
        SELECT id, question_id, answer, attested, not_applicable_reason, milestone_on,
               previous_id, actor_uid, recorded_at, seq
          FROM butler_review_response WHERE investor_id = %s AND review_id = %s ORDER BY seq
        """,
        (investor_id, review_id),
    )
    evidence = _rows(
        conn,
        """
        SELECT e.response_id, e.ordinal, e.source_id, e.note_id
          FROM butler_review_evidence e
          JOIN butler_review_response r ON r.investor_id = e.investor_id AND r.id = e.response_id
         WHERE e.investor_id = %s AND r.review_id = %s ORDER BY e.response_id, e.ordinal
        """,
        (investor_id, review_id),
    )
    by_response: dict[str, list[dict]] = defaultdict(list)
    for row in evidence:
        by_response[row["response_id"]].append(row)
    for row in rows:
        row["evidence"] = by_response.get(row["id"], [])
    return rows


def _evidence_availability(
    conn: Any, investor_id: str, evidence: list[dict[str, Any]], visible: set[str],
    forgotten: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Each attached item with whether it is still available and why not. Availability is NOT
    sufficiency: an available source is shown, never scored."""
    source_ids = [e["source_id"] for e in evidence if e["source_id"]]
    note_ids = [e["note_id"] for e in evidence if e["note_id"]]
    sources = {r["id"]: r for r in _rows(
        conn, "SELECT id, isin, source, source_type, publication_date FROM research_source "
              "WHERE investor_id = %s AND id = ANY(%s::uuid[])", (investor_id, source_ids),
    )} if source_ids else {}
    notes = {r["id"]: r for r in _rows(
        conn, "SELECT id, isin, title, recorded_at FROM note WHERE investor_id = %s "
              "AND id = ANY(%s::uuid[])", (investor_id, note_ids),
    )} if note_ids else {}
    retracted = retraction_for(conn, investor_id, source_ids)
    out = []
    for e in evidence:
        if e["source_id"]:
            s = sources.get(e["source_id"])
            reason = (None if s and s["isin"] in visible else "EVIDENCE_UNAVAILABLE")
            if reason is None and e["source_id"] in retracted:
                reason = "SOURCE_RETRACTED"
            out.append({"kind": "SOURCE", "id": e["source_id"], "available": reason is None,
                        "reason": reason,
                        "label": s["source"] if s and reason is None else None,
                        "published_on": s["publication_date"] if s and reason is None else None})
        else:
            n = notes.get(e["note_id"])
            reason = None if n and n["isin"] in visible else "EVIDENCE_UNAVAILABLE"
            if reason is None and ("NOTE", e["note_id"]) in forgotten:
                reason = "MEMORY_FORGOTTEN"
            out.append({"kind": "NOTE", "id": e["note_id"], "available": reason is None,
                        "reason": reason, "label": n["title"] if n and reason is None else None,
                        "published_on": None})
    return out


def review_view(conn: Any, investor_id: str, review_id: str, now: datetime) -> dict[str, Any]:
    """The review, each field's derived state, what remains unresolved and what changed.

    Nothing here guesses an answer. The next question is the first open field in the pinned
    priority order; a follow-up is a fixed prompt, never a judgment of the answer.
    """
    review = _review_row(conn, investor_id, review_id)
    visible = visible_isins(conn, investor_id)
    if review["isin"] not in visible:
        require_unlocked(conn, investor_id, review["isin"])
    forgotten = forgotten_refs(conn, investor_id)
    responses = _responses(conn, investor_id, review["id"])
    completion = _one(
        conn,
        "SELECT id, choice, reason, unresolved, actor_uid, recorded_at FROM "
        "butler_review_completion WHERE investor_id = %s AND review_id = %s",
        (investor_id, review["id"]),
    )
    pinned = None
    if review["thesis_id"]:
        pinned = _one(conn, "SELECT id, isin, version, argument, intended_holding_period, "
                            "version_on, recorded_at FROM thesis "
                            "WHERE investor_id = %s AND id = %s",
                      (investor_id, review["thesis_id"]))
    latest = _latest_thesis(conn, investor_id, isin_family(conn, review["isin"]))
    review_on = review["recorded_at"].date()

    # Latest response per question (the chain tip), with its revision history.
    chains: dict[str, list[dict]] = defaultdict(list)
    for r in responses:
        chains[r["question_id"]].append(r)
    fields = []
    differences: list[dict[str, Any]] = []
    for qid in review["question_ids"]:
        chain = chains.get(qid, [])
        named = {r["previous_id"] for r in chain if r["previous_id"]}
        tips = [r for r in chain if r["id"] not in named]
        tip = max(tips, key=lambda r: r["seq"]) if tips else None
        evidence = (_evidence_availability(conn, investor_id, tip["evidence"], visible, forgotten)
                    if tip else [])
        available = (None if not evidence else all(e["available"] for e in evidence))
        template = bp.question_template(qid)
        check = bp.run_code_check(template.code_check, tip, review_on)
        state = bp.field_state(qid, tip, available, check)
        for e in evidence:
            if not e["available"]:
                differences.append({"code": e["reason"], "question_id": qid, "record_id": e["id"]})
        fields.append({
            "question_id": qid,
            "question_text": template.text,
            "question_version": template.version,
            # A personalised (notebook) question always carries its "Why this question?"
            # route; the answer is the person's, and dismissing it is always allowed.
            "personalised": qid in bp.PERSONAL_QUESTIONS,
            "state": state,
            "resolved": state in bp.RESOLVED_STATES,
            "response": ({
                "id": tip["id"], "answer": tip["answer"], "attested": tip["attested"],
                "not_applicable_reason": tip["not_applicable_reason"],
                "milestone_on": tip["milestone_on"], "recorded_at": tip["recorded_at"],
                "actor_uid": tip["actor_uid"], "previous_id": tip["previous_id"],
                "revision_ids": [r["id"] for r in chain],
            } if tip else None),
            "evidence": evidence,
            "code_check": ({"id": template.code_check,
                            "result": "NOT_RUN" if check is None else
                            ("PASSED" if check else "FAILED")}
                           if template.code_check else None),
            "follow_up": bp.follow_up_for(qid, state, tip),
        })
    if pinned and latest and latest["id"] != pinned["id"]:
        differences.append({"code": "THESIS_CHANGED", "question_id": None,
                            "record_id": latest["id"],
                            "from_version": pinned["version"], "to_version": latest["version"]})
    if pinned is None and latest is not None and review["recorded_at"] < latest["recorded_at"]:
        differences.append({"code": "THESIS_ADDED", "question_id": None,
                            "record_id": latest["id"], "from_version": None,
                            "to_version": latest["version"]})
    unresolved = [f["question_id"] for f in fields if not f["resolved"]]
    if review["superseded_by_review_id"]:
        status = "SUPERSEDED"
    elif completion:
        status = "DOCUMENTED"
    elif any(f["state"] != bp.MISSING for f in fields):
        status = "AWAITING_INPUT"
    else:
        status = "DRAFT"
    next_field = next((f for f in fields if f["state"] == bp.MISSING), None) or next(
        (f for f in fields if f["follow_up"]), None)
    ids = identities(conn, [review["isin"]])
    return {
        "id": review["id"],
        "isin": review["isin"],
        "identity": ids.get(review["isin"]),
        "decision_kind": review["decision_kind"],
        "status": status,
        "checklist": {"id": review["checklist_id"], "version": review["checklist_version"]},
        "pinned_thesis": ({"id": pinned["id"], "version": pinned["version"],
                           "version_on": pinned["version_on"],
                           "intended_holding_period": pinned["intended_holding_period"],
                           "argument_excerpt": _preview(pinned["argument"])}
                          if pinned else None),
        "trigger_item_id": review["trigger_item_id"],
        "supersedes_review_id": review["supersedes_review_id"],
        "superseded_by_review_id": review["superseded_by_review_id"],
        "fields": fields,
        "next": ({"question_id": next_field["question_id"],
                  "follow_up": next_field["follow_up"],
                  "follow_up_text": bp.FOLLOW_UPS.get(next_field["follow_up"] or "")}
                 if next_field and not completion else None),
        "unresolved": unresolved,
        "differences": differences,
        "stale": bool(differences) and not completion,
        "completion": ({
            "choice": completion["choice"], "reason": completion["reason"],
            "unresolved": list(completion["unresolved"]), "recorded_at": completion["recorded_at"],
            "actor_uid": completion["actor_uid"],
            # Fixed wording (character section 5). Never "approved".
            "statement": "Review documented; these checks remain unresolved."
                         if completion["unresolved"] else "Review documented.",
        } if completion else None),
        "recorded_at": review["recorded_at"],
        "actor_uid": review["actor_uid"],
    }


def respond(
    conn: Any, investor_id: str, actor_uid: str, review_id: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Append the person's own response (or a revision naming its predecessor), exactly as
    written, with the evidence they chose. The model has no path to this function."""
    review = _review_row(conn, investor_id, review_id)
    require_unlocked(conn, investor_id, review["isin"])
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_REVIEW_RESPONSE, review["id"], key,
                           payload_digest)
    if receipt is not None:
        return review_view(conn, investor_id, review["id"], _now()), 200
    review = _review_row(conn, investor_id, review["id"])
    if review["superseded_by_review_id"] or _one(
            conn, "SELECT 1 AS x FROM butler_review_completion WHERE investor_id = %s "
                  "AND review_id = %s", (investor_id, review["id"])):
        raise conflict(REVIEW_CLOSED, "this review is closed; start a new one to revise it")
    qid = body["question_id"]
    if qid not in review["question_ids"]:
        raise invalid("this question is not part of the review", {"question_id": "NOT_IN_REVIEW"})
    chain = [r for r in _responses(conn, investor_id, review["id"]) if r["question_id"] == qid]
    named = {r["previous_id"] for r in chain if r["previous_id"]}
    tips = [r for r in chain if r["id"] not in named]
    current = max(tips, key=lambda r: r["seq"])["id"] if tips else None
    expected = body.get("expected_latest_response_id")
    expected = _uuid(expected, "response") if expected else None
    if expected != current:
        raise conflict(STALE_PREDECESSOR, "a newer response was saved; reload before revising")

    visible = visible_isins(conn, investor_id)
    evidence = body.get("evidence") or []
    for i, item in enumerate(evidence):
        if item.get("source_id"):
            sid = _uuid(item["source_id"], "source")
            row = _one(conn, "SELECT isin FROM research_source WHERE investor_id = %s AND id = %s",
                       (investor_id, sid))
            if row is None or row["isin"] not in visible:
                raise WorkspaceError(422, EVIDENCE_NOT_AVAILABLE,
                                     "attached source is not available",
                                     {f"evidence[{i}]": "UNAVAILABLE"})
            if retraction_for(conn, investor_id, [sid]):
                raise WorkspaceError(422, EVIDENCE_NOT_AVAILABLE, "attached source was retracted",
                                     {f"evidence[{i}]": "RETRACTED"})
            item["source_id"] = sid
        else:
            nid = _uuid(item.get("note_id") or "", "note")
            row = _one(conn, "SELECT isin FROM note WHERE investor_id = %s AND id = %s",
                       (investor_id, nid))
            if row is None or row["isin"] not in visible:
                raise WorkspaceError(422, EVIDENCE_NOT_AVAILABLE,
                                     "attached note is not available",
                                     {f"evidence[{i}]": "UNAVAILABLE"})
            item["note_id"] = nid
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_review_response (investor_id, review_id, question_id, answer,
                                                attested, not_applicable_reason, milestone_on,
                                                previous_id, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, review["id"], qid, body.get("answer") or "", body.get("attested", False),
             body.get("not_applicable_reason"), body.get("milestone_on"), current, actor_uid,
             dedupe_key(OP_REVIEW_RESPONSE, review["id"], key)),
        )
        response_id = str(cur.fetchone()[0])
        for i, item in enumerate(evidence):
            cur.execute(
                "INSERT INTO butler_review_evidence (investor_id, response_id, ordinal, "
                "source_id, note_id) VALUES (%s, %s, %s, %s, %s)",
                (investor_id, response_id, i, item.get("source_id"), item.get("note_id")),
            )
    audit_write(conn, actor_uid, OP_REVIEW_RESPONSE, "butler_review", review["id"],
                question=qid, evidence=len(evidence), revision=current is not None)
    record_receipt(conn, investor_id, OP_REVIEW_RESPONSE, review["id"], key, payload_digest,
                   {"response_id": response_id}, actor_uid)
    return review_view(conn, investor_id, review["id"], _now()), 201


def complete_review(
    conn: Any, investor_id: str, actor_uid: str, review_id: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Record the person's explicit process choice. Every unresolved check must be named back,
    so "documented with these open" is a statement they made, not something hidden. A review
    whose pinned thesis or evidence changed is STALE and cannot be completed: the person starts
    a superseding review that shows the differences."""
    review = _review_row(conn, investor_id, review_id)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_REVIEW_COMPLETE, review["id"], key,
                           payload_digest)
    if receipt is not None:
        return review_view(conn, investor_id, review["id"], _now()), 200
    view = review_view(conn, investor_id, review["id"], _now())
    if view["status"] in ("DOCUMENTED", "SUPERSEDED"):
        raise conflict(REVIEW_CLOSED, "this review is already closed")
    if view["stale"]:
        raise WorkspaceError(409, REVIEW_STALE,
                             "the thesis or attached evidence changed; start a superseding review",
                             {"differences": [d["code"] for d in view["differences"]]})
    acknowledged = sorted(set(body["acknowledged_unresolved"]))
    if acknowledged != sorted(view["unresolved"]):
        raise WorkspaceError(422, UNRESOLVED_NOT_ACKNOWLEDGED,
                             "name every unresolved check to document the review with them open",
                             {"unresolved": view["unresolved"]})
    reason = body.get("reason") or ""
    if view["unresolved"] and not reason.strip():
        raise invalid("give a reason for continuing with unresolved checks", {"reason": "REQUIRED"})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_review_completion (investor_id, review_id, choice, reason,
                                                  unresolved, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, review["id"], body["choice"], reason, view["unresolved"], actor_uid,
             dedupe_key(OP_REVIEW_COMPLETE, review["id"], key)),
        )
    audit_write(conn, actor_uid, OP_REVIEW_COMPLETE, "butler_review", review["id"],
                choice=body["choice"], unresolved=len(view["unresolved"]))
    record_receipt(conn, investor_id, OP_REVIEW_COMPLETE, review["id"], key, payload_digest,
                   {"review_id": review["id"]}, actor_uid)
    return review_view(conn, investor_id, review["id"], _now()), 201


def list_reviews(conn: Any, investor_id: str, isin: str | None, limit: int | None) -> list[dict]:
    size = canonical.clamp_limit(limit)
    family = isin_family(conn, _valid_isin(isin)) if isin else None
    rows = _rows(
        conn,
        """
        SELECT r.id FROM butler_review r
         WHERE r.investor_id = %s AND (%s::text[] IS NULL OR r.isin = ANY(%s::text[]))
         ORDER BY r.seq DESC LIMIT %s
        """,
        (investor_id, family, family, size),
    )
    visible = visible_isins(conn, investor_id)
    now = _now()
    out = []
    for row in rows:
        view = review_view(conn, investor_id, row["id"], now)
        if view["isin"] in visible:
            out.append({k: view[k] for k in ("id", "isin", "identity", "decision_kind", "status",
                                              "unresolved", "recorded_at", "stale")})
    return out


# =========================================================================================
# worker SQL (ENGINE capability) -- see app/jobs/butler.py
# =========================================================================================


def pending_events(conn: Any, investor_id: str, now: datetime, limit: int) -> list[dict]:
    """Outbox rows with no effect whose latest lease (if any) has expired, oldest first."""
    return _rows(
        conn,
        """
        SELECT o.id, o.seq, o.event_kind, o.isin, o.record_id, o.recorded_at,
               COALESCE(c.attempt_no, 0) AS attempts
          FROM butler_outbox o
          LEFT JOIN LATERAL (SELECT attempt_no, lease_until FROM butler_outbox_claim x
                              WHERE x.investor_id = o.investor_id AND x.outbox_id = o.id
                              ORDER BY attempt_no DESC LIMIT 1) c ON true
         WHERE o.investor_id = %s
           AND NOT EXISTS (SELECT 1 FROM butler_outbox_effect f
                            WHERE f.investor_id = o.investor_id AND f.outbox_id = o.id)
           AND (c.lease_until IS NULL OR c.lease_until < %s)
         ORDER BY o.seq LIMIT %s
        """,
        (investor_id, now, limit),
    )


def claim_event(conn: Any, investor_id: str, outbox_id: str, attempt_no: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO butler_outbox_claim (investor_id, outbox_id, attempt_no) "
            "VALUES (%s, %s, %s) RETURNING id",
            (investor_id, outbox_id, attempt_no),
        )
        return str(cur.fetchone()[0])


def record_effect(
    conn: Any, investor_id: str, outbox_id: str, claim_id: str, outcome: str, reason: str,
    item_id: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_outbox_effect (investor_id, outbox_id, claim_id, outcome,
                                              reason_code, inbox_item_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (investor_id, outbox_id, claim_id, outcome, reason, item_id),
        )


def item_by_dedupe(conn: Any, investor_id: str, key: str) -> str | None:
    row = _one(conn, "SELECT id FROM butler_inbox_item WHERE investor_id = %s AND dedupe_key = %s",
               (investor_id, key))
    return row["id"] if row else None


def latest_issue_item(conn: Any, investor_id: str, issue_key: str) -> dict[str, Any] | None:
    return _one(
        conn,
        """
        SELECT i.id, i.evidence_version, i.recorded_at,
               EXISTS (SELECT 1 FROM butler_inbox_item n WHERE n.investor_id = i.investor_id
                        AND n.supersedes_item_id = i.id) AS superseded
          FROM butler_inbox_item i WHERE i.investor_id = %s AND i.issue_key = %s
         ORDER BY i.seq DESC LIMIT 1
        """,
        (investor_id, issue_key),
    )


def items_presented_between(
    conn: Any, investor_id: str, start: datetime, end: datetime
) -> int:
    row = _one(
        conn,
        "SELECT count(*) AS n FROM butler_inbox_item WHERE investor_id = %s AND priority = "
        "'INFORMATIONAL' AND deliver_after >= %s AND deliver_after < %s",
        (investor_id, start, end),
    )
    return int(row["n"]) if row else 0


def insert_item(conn: Any, investor_id: str, item: dict[str, Any]) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_inbox_item (investor_id, card_kind, priority, isin, subscription_id,
                template_id, template_version, rule_version, issue_key, evidence_version, args,
                source_id, thesis_id, evaluation_id, supersedes_item_id, deliver_after,
                dedupe_key)
            VALUES (%(inv)s, %(card_kind)s, %(priority)s, %(isin)s, %(subscription_id)s,
                %(template_id)s, %(template_version)s, %(rule_version)s, %(issue_key)s,
                %(evidence_version)s, %(args)s, %(source_id)s, %(thesis_id)s, %(evaluation_id)s,
                %(supersedes_item_id)s, %(deliver_after)s, %(dedupe_key)s)
            RETURNING id
            """,
            {"inv": investor_id, **{k: item.get(k) for k in (
                "card_kind", "priority", "isin", "subscription_id", "template_id",
                "template_version", "rule_version", "issue_key", "evidence_version",
                "source_id", "thesis_id", "evaluation_id", "supersedes_item_id",
                "deliver_after", "dedupe_key")},
             "args": canonical.canonical_json(item["args"])},
        )
        return str(cur.fetchone()[0])


def record_tick(
    conn: Any, investor_id: str, started_at: datetime, outcome: str, counts: dict[str, int],
    oldest_pending_at: datetime | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_tick (investor_id, started_at, outcome, processed, cards,
                                     suppressed, failed, oldest_pending_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, started_at, outcome, counts.get("processed", 0),
             counts.get("cards", 0), counts.get("suppressed", 0), counts.get("failed", 0),
             oldest_pending_at),
        )


def company_subscriptions(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    """Every company subscription with its derived state, for the worker."""
    rows = _subscription_rows(conn, investor_id, None)
    events = _subscription_events(conn, investor_id, [r["id"] for r in rows])
    out = []
    for row in rows:
        if row["subject_kind"] != "COMPANY":
            continue
        derived = derive_subscription(events.get(row["id"], []))
        out.append({**row, **derived, "family": isin_family(conn, row["isin"])})
    return out


def source_row(conn: Any, investor_id: str, source_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        """
        SELECT s.id, s.isin, s.source, s.source_type, s.source_url, s.content_sha256,
               s.publication_date, s.recorded_at, e.status AS extraction_status,
               e.unavailable_reason
          FROM research_source s
          LEFT JOIN research_source_extraction e
            ON e.investor_id = s.investor_id AND e.source_id = s.id
         WHERE s.investor_id = %s AND s.id = %s
        """,
        (investor_id, source_id),
    )


def prior_origin_item(
    conn: Any, investor_id: str, subscription_id: str, origin: str
) -> str | None:
    """An earlier NEW_MATERIAL item for the same ORIGIN (content hash or canonical URL). Several
    redistributed copies of one filing are one piece of evidence, not several."""
    row = _one(
        conn,
        "SELECT id FROM butler_inbox_item WHERE investor_id = %s AND subscription_id = %s "
        "AND card_kind = 'NEW_MATERIAL' AND args->>'origin' = %s LIMIT 1",
        (investor_id, subscription_id, origin),
    )
    return row["id"] if row else None


def thesis_pair(conn: Any, investor_id: str, thesis_id: str) -> tuple[dict | None, dict | None]:
    new = _one(conn, "SELECT id, isin, version, version_on FROM thesis WHERE investor_id = %s "
                     "AND id = %s", (investor_id, thesis_id))
    if new is None or new["version"] <= 1:
        return None, new
    old = _one(conn, "SELECT id, isin, version, version_on FROM thesis WHERE investor_id = %s "
                     "AND isin = %s AND version = %s",
               (investor_id, new["isin"], new["version"] - 1))
    return old, new


def evaluation_pair(
    conn: Any, investor_id: str, evaluation_id: str
) -> tuple[dict | None, dict | None]:
    new = _one(
        conn,
        """
        SELECT e.id, e.thesis_id, e.state, e.evidence_cutoff, e.evaluated_at, t.version, t.isin
          FROM trigger_evaluation e
          JOIN thesis t ON t.investor_id = e.investor_id AND t.id = e.thesis_id
         WHERE e.investor_id = %s AND e.id = %s
        """,
        (investor_id, evaluation_id),
    )
    if new is None:
        return None, None
    old = _one(
        conn,
        "SELECT id, state FROM trigger_evaluation WHERE investor_id = %s AND thesis_id = %s "
        "AND evaluated_at < %s ORDER BY evaluated_at DESC LIMIT 1",
        (investor_id, new["thesis_id"], new["evaluated_at"]),
    )
    return old, new


def checkpoint_window(
    sub: dict[str, Any], tz_name: str, due_on: date
) -> tuple[datetime, datetime]:
    """[previous due date, this due date) in the investor's zone, as UTC instants."""
    tz = bp.zone(tz_name)
    start_day = max(sub["checkpoint_set_at"].astimezone(tz).date(),
                    due_on - timedelta(days=sub["checkpoint_days"]))
    start = datetime.combine(start_day, time(0, 0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(due_on, time(0, 0), tzinfo=tz).astimezone(UTC)
    return start, end


def parse_uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def content_hash(text: str) -> str:
    return _sha(text)

