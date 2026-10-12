"""Butler teaching preferences and item feedback (BT 09, migration 052).

EXPLICIT CHOICES ONLY. A person picks concise or detailed lesson text, how often an applicable
lesson is offered, and whether reviewed follow-up questions are asked. They may mark a reviewed
question, lesson or card template useful or not useful. Every choice is a new appended row; the
latest is current; history is kept.

WHAT THIS IS NOT. Not a profile, not model training, not engagement optimisation. Silence and
snoozes are not agreement and are never recorded here. Nothing in this module is read by
validation: the factual and contradiction gates (``butler_validation.py``) are identical however
confident, agreeable or emotional anyone sounds and whatever these rows say. The only effect on a
packet is ``lessons_enabled_at`` -- which removes lessons, never adds evidence or relaxes a check.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.db import workspace as ws
from app.research import butler_policy as bp
from app.research import skills as sk
from app.research.errors import VERSION_CONFLICT, conflict, invalid, not_found

OP_TEACHING = "butler.teaching"
OP_FEEDBACK = "butler.feedback"

DEFAULTS = {"detail": "CONCISE", "lesson_cadence": "EVERY_REVIEW", "follow_ups": "ASK"}
WEEKLY = timedelta(days=7)

_rows = ws._rows
_one = ws._one


def current_teaching(conn: Any, investor_id: str, at: datetime | None = None
                     ) -> dict[str, Any]:
    row = _one(conn, """
        SELECT id, version, detail, lesson_cadence, follow_ups, recorded_at
          FROM butler_teaching_preference
         WHERE investor_id = %s AND (%s::timestamptz IS NULL OR recorded_at <= %s::timestamptz)
         ORDER BY version DESC LIMIT 1
        """, (investor_id, at, at))
    if row is None:
        return {"state": "NOT_SET", "version": 0, **DEFAULTS, "recorded_at": None}
    return {"state": "SET", **{k: row[k] for k in ("version", "detail", "lesson_cadence",
                                                   "follow_ups", "recorded_at")}}


def set_teaching(conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
                 payload_digest: str) -> tuple[dict[str, Any], int]:
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_TEACHING, "-", key, payload_digest)
    if receipt is not None:
        return current_teaching(conn, investor_id), 200
    current = current_teaching(conn, investor_id)
    if body["expected_version"] != current["version"]:
        raise conflict(VERSION_CONFLICT, "teaching preferences changed; reload first")
    version = current["version"] + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_teaching_preference (investor_id, version, detail,
                                                    lesson_cadence, follow_ups, actor_uid,
                                                    dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, version, body["detail"], body["lesson_cadence"], body["follow_ups"],
             actor_uid, ws.dedupe_key(OP_TEACHING, "-", key)),
        )
    ws.audit_write(conn, actor_uid, OP_TEACHING, "butler_teaching_preference", investor_id,
                   version=version)
    ws.record_receipt(conn, investor_id, OP_TEACHING, "-", key, payload_digest,
                      {"version": str(version)}, actor_uid)
    return current_teaching(conn, investor_id), 201


def _subject_version(kind: str, subject_id: str) -> str | None:
    if kind == "QUESTION":
        t = bp.QUESTIONS.get(subject_id)
    elif kind == "LESSON":
        t = bp.LESSONS.get(subject_id)
    else:
        t = bp.CARDS.get(subject_id)
    return t.version if t is not None else None


def record_feedback(conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any],
                    key: str, payload_digest: str) -> tuple[dict[str, Any], int]:
    """One explicit rating on one reviewed catalogue item. Unknown ids or versions are 422: a
    rating cannot name, and so cannot introduce, anything outside the reviewed catalogue."""
    version = _subject_version(body["subject_kind"], body["subject_id"])
    if version is None or version != body["subject_version"]:
        raise invalid("feedback must name a reviewed catalogue item and its current version",
                      {"subject_id": "NOT_IN_CATALOGUE"})
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_FEEDBACK, "-", key, payload_digest)
    if receipt is not None:
        return _feedback_row(conn, investor_id, receipt.result["feedback_id"]), 200
    item = body.get("inbox_item_id")
    if item is not None:
        item = ws._uuid(item, "inbox item")
        if _one(conn, "SELECT 1 AS x FROM butler_inbox_item WHERE investor_id = %s AND id = %s",
                (investor_id, item)) is None:
            raise not_found("inbox item")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO butler_feedback (investor_id, subject_kind, subject_id, subject_version,
                                         inbox_item_id, rating, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, body["subject_kind"], body["subject_id"], body["subject_version"],
             item, body["rating"], actor_uid, ws.dedupe_key(OP_FEEDBACK, "-", key)),
        )
        feedback_id = str(cur.fetchone()[0])
    ws.audit_write(conn, actor_uid, OP_FEEDBACK, "butler_feedback", feedback_id,
                   subject=body["subject_id"], rating=body["rating"])
    ws.record_receipt(conn, investor_id, OP_FEEDBACK, "-", key, payload_digest,
                      {"feedback_id": feedback_id}, actor_uid)
    return _feedback_row(conn, investor_id, feedback_id), 201


def _feedback_row(conn: Any, investor_id: str, feedback_id: str) -> dict[str, Any]:
    row = _one(conn, """
        SELECT id, subject_kind, subject_id, subject_version, inbox_item_id, rating, recorded_at
          FROM butler_feedback WHERE investor_id = %s AND id = %s
        """, (investor_id, feedback_id))
    if row is None:
        raise not_found("feedback")
    return row


def latest_feedback(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    """The current rating per catalogue subject (latest row wins; history kept)."""
    return _rows(conn, """
        SELECT DISTINCT ON (subject_kind, subject_id)
               id, subject_kind, subject_id, subject_version, inbox_item_id, rating, recorded_at
          FROM butler_feedback WHERE investor_id = %s
         ORDER BY subject_kind, subject_id, seq DESC
        """, (investor_id,))


def lessons_enabled_at(conn: Any, investor_id: str, cutoff: datetime) -> bool:
    """Whether a packet sealed at ``cutoff`` may offer lessons, from the preference in force
    then. OFF -> never. WEEKLY -> not if a validated Butler selection already offered a lesson
    in the preceding seven days. Deterministic from stored rows, so a rebuild at the same cutoff
    reproduces the same packet."""
    pref = current_teaching(conn, investor_id, cutoff)
    if pref["lesson_cadence"] == "OFF":
        return False
    if pref["lesson_cadence"] == "EVERY_REVIEW":
        return True
    recent = _one(conn, """
        SELECT 1 AS x FROM task_result r JOIN task t
            ON t.investor_id = r.investor_id AND t.id = r.task_id
         WHERE r.investor_id = %s AND t.skill_id = %s AND r.outcome = 'PROPOSED'
           AND r.guardrail_status = 'PASS'
           AND r.finished_at > %s AND r.finished_at <= %s
           AND jsonb_array_length(COALESCE(r.structured_output->'proposal'->'lesson_ids',
                                           '[]'::jsonb)) > 0
         LIMIT 1
        """, (investor_id, sk.BUTLER_SELECTOR.skill_id, cutoff - WEEKLY, cutoff))
    return recent is None
