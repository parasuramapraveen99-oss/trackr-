"""Monitor schedules, due occurrences and their honest states (sessions 36-37, section 7.5).

Same contract as ``app/db/workspace.py``: every function takes a connection already scoped by
``enter_workspace`` to one investor. Raw SQL, append-only, no ledger, no financial table, no
model, no network. The arithmetic and matching rules live in ``app/research/monitor.py``; this
module reads rows, asks it, and records the answer.

WHO WRITES WHAT. A person (HUMAN capability) appends schedule versions -- create, pause,
resume, rebind and reschedule are all versions, and nothing is updated in place. The scheduler
(SCHEDULER capability, ``tick_schedule``) appends due occurrences. Neither can create a model
task here: while BE-Q01 is open the tick records ``BLOCKED_EXECUTION_POLICY``,
``SUPERSEDED_THESIS`` or ``BLOCKED_EVENT`` and nothing else, and 043's CHECK that ties QUEUED
to a task is honoured by never writing QUEUED rather than by inventing an empty task.

IDEMPOTENT BY CONSTRUCTION. The tick takes the tenant lock, reads what is recorded, and inserts
with ``ON CONFLICT DO NOTHING`` against 043's UNIQUE (investor, schedule, due_at) and 057's
UNIQUE (investor, schedule, event_identity). A repeated tick, two overlapping workers and a
retry after a crash each leave at most one occurrence per due instant and per event.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from app.db import research as rs
from app.db import workspace as ws
from app.research import canonical
from app.research import monitor as m
from app.research.errors import (
    VERSION_CONFLICT,
    WorkspaceError,
    conflict,
    invalid,
    not_found,
)
from app.research.source_policy import SourcePolicy

OP_CREATE = "monitor.create"
OP_VERSION = "monitor.version"
OP_TICK = "monitor.tick"
OP_EVENT = "monitor.event"

EXECUTION_POLICY_UNRESOLVED = m.EXECUTION_POLICY_STATUS

_rows = ws._rows
_one = ws._one
_uuid = ws._uuid

_SCHEDULE_COLUMNS = """
s.id, s.version, s.isin, s.thesis_id, s.skill_id, s.skill_version, s.agent_id,
s.agent_version, s.kind, s.timezone, s.starts_at, s.interval_days, s.enabled, s.actor_uid,
s.recorded_at
"""

_RUN_COLUMNS = """
r.id, r.isin, r.schedule_id, r.schedule_version, r.thesis_id, r.due_at, r.recorded_at,
r.task_id, r.disposition, r.reason, r.event_id, r.event_identity,
e.event_type, e.event_date, e.period_start, e.period_end, e.source_id AS event_source_id,
e.verification AS event_verification, e.status AS event_status
"""

_RUN_FROM = """
FROM monitor_run r
LEFT JOIN research_event e ON e.investor_id = r.investor_id AND e.id = r.event_id
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _ist_date(instant: datetime) -> date:
    return instant.astimezone(m.IST).date()


# =========================================================================================
# reads: schedule versions
# =========================================================================================


def _versions(conn: Any, investor_id: str, schedule_id: str) -> list[dict[str, Any]]:
    return _rows(conn, f"SELECT {_SCHEDULE_COLUMNS} FROM monitor_schedule s "
                       "WHERE s.investor_id = %s AND s.id = %s ORDER BY s.version",
                 (investor_id, schedule_id))


def latest_version(conn: Any, investor_id: str, schedule_id: str) -> dict[str, Any]:
    """The newest version of this schedule. Foreign or unknown -> 404; locked company -> 409."""
    schedule_id = _uuid(schedule_id, "monitor")
    versions = _versions(conn, investor_id, schedule_id)
    if not versions:
        raise not_found("monitor")
    latest = versions[-1]
    ws.require_unlocked(conn, investor_id, latest["isin"])
    return {**latest, "_created_at": versions[0]["recorded_at"], "_versions": len(versions)}


def _last_due(conn: Any, investor_id: str, schedule_id: str) -> datetime | None:
    row = _one(conn, "SELECT max(due_at) AS due FROM monitor_run "
                     "WHERE investor_id = %s AND schedule_id = %s", (investor_id, schedule_id))
    return row["due"] if row else None


def _run_counts(conn: Any, investor_id: str, schedule_id: str) -> dict[str, int]:
    rows = _rows(conn, "SELECT disposition, count(*) AS n FROM monitor_run "
                       "WHERE investor_id = %s AND schedule_id = %s GROUP BY disposition",
                 (investor_id, schedule_id))
    return {r["disposition"]: int(r["n"]) for r in rows}


def _commitment(conn: Any, investor_id: str, isin: str, cutoff: datetime
                ) -> tuple[str, str | None]:
    selection, _ = rs.select_in_force(conn, investor_id, isin, cutoff)
    return selection.state, (selection.thesis.id if selection.thesis else None)


def _event_rows(conn: Any, investor_id: str, family: list[str], event_type: str
                ) -> list[m.EventRow]:
    rows = _rows(conn, """
        SELECT e.id, e.isin, e.event_type, e.status, e.verification, e.date_precision,
               e.event_date, e.period_start, e.period_end, e.source_id, e.seq,
               s.source_type, s.publication_date, s.retrieved_at
          FROM research_event e
          JOIN research_source s ON s.investor_id = e.investor_id AND s.id = e.source_id
         WHERE e.investor_id = %s AND e.isin = ANY(%s::text[]) AND e.event_type = %s
         ORDER BY e.seq
        """, (investor_id, family, event_type))
    return [m.EventRow(id=r["id"], isin=r["isin"], event_type=r["event_type"],
                       status=r["status"], verification=r["verification"],
                       date_precision=r["date_precision"], event_date=r["event_date"],
                       period_start=r["period_start"], period_end=r["period_end"],
                       source_id=r["source_id"], source_type=r["source_type"],
                       publication_date=r["publication_date"], retrieved_at=r["retrieved_at"],
                       seq=int(r["seq"])) for r in rows]


def _recorded_identities(conn: Any, investor_id: str, schedule_id: str) -> frozenset[str]:
    rows = _rows(conn, "SELECT event_identity FROM monitor_run WHERE investor_id = %s "
                       "AND schedule_id = %s AND event_identity IS NOT NULL",
                 (investor_id, schedule_id))
    return frozenset(r["event_identity"] for r in rows)


def _effective_from(version: dict[str, Any]) -> date:
    """The Asia/Kolkata date this version began watching: never before it was saved."""
    return max(_ist_date(version["starts_at"]), _ist_date(version["recorded_at"]))


def _evaluate(conn: Any, investor_id: str, version: dict[str, Any], policy: SourcePolicy,
              now: datetime) -> tuple[list[m.EventDecision], list[m.PendingEvent]]:
    family = ws.isin_family(conn, version["isin"])
    rows = _event_rows(conn, investor_id, family, m.KIND_EVENT_TYPE[version["kind"]])
    return m.evaluate_events(
        version["kind"], rows, family=frozenset(family),
        allows=lambda source_type: policy.allows(source_type, m.MONITOR_SOURCE_USE),
        effective_from=_effective_from(version), today=_ist_date(now),
        recorded_identities=_recorded_identities(conn, investor_id, version["id"]),
    )


# =========================================================================================
# DTOs
# =========================================================================================


def _run_dto(conn: Any, investor_id: str, r: dict[str, Any], reports: dict[str, str],
             tasks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    is_event = r["event_id"] is not None
    task = tasks.get(r["task_id"]) if r["task_id"] else None
    report_id = reports.get(r["id"])
    state = m.run_display_state(r["disposition"], is_event,
                                task["state"] if task else None,
                                task["error_code"] if task else None, report_id is not None)
    late_by = int((r["recorded_at"] - r["due_at"]).total_seconds())
    return {
        "id": r["id"], "isin": r["isin"], "schedule_id": r["schedule_id"],
        "schedule_version": r["schedule_version"], "thesis_id": r["thesis_id"],
        "due_at": _iso(r["due_at"]), "recorded_at": _iso(r["recorded_at"]),
        "disposition": r["disposition"],
        "blocked_reason": r["reason"] if r["disposition"] != m.QUEUED else None,
        "task_id": r["task_id"], "task_state": task["state"] if task else None,
        "report_id": report_id,
        "display_state": state,
        # Recorded more than LATE_AFTER after it came due: the worker was late, which is a
        # different fact from blocked. For an event occurrence the due instant is derived from
        # the event's date, so a late verification is expected and not flagged.
        "recorded_late": (not is_event) and r["recorded_at"] - r["due_at"] > m.LATE_AFTER,
        "recorded_after_due_seconds": max(late_by, 0),
        # Nothing executed unless a task exists. Said explicitly so no client has to infer it.
        "executed": r["task_id"] is not None,
        "event": ({
            "event_id": r["event_id"], "identity": r["event_identity"],
            "event_type": r["event_type"], "event_date": _iso(r["event_date"]),
            "period_start": _iso(r["period_start"]), "period_end": _iso(r["period_end"]),
            "source_id": r["event_source_id"], "verification": r["event_verification"],
            "status": r["event_status"],
        } if is_event else None),
    }


def _hydrate_runs(conn: Any, investor_id: str, rows: list[dict[str, Any]]
                  ) -> list[dict[str, Any]]:
    ids = [r["id"] for r in rows]
    reports = {x["run_id"]: x["id"] for x in _rows(
        conn, "SELECT id, run_id FROM monitor_report WHERE investor_id = %s "
              "AND run_id = ANY(%s::uuid[])", (investor_id, ids))} if ids else {}
    tasks: dict[str, dict[str, Any]] = {}
    task_ids = [r["task_id"] for r in rows if r["task_id"]]
    if task_ids:
        from app.db import tasks as tk

        for tid in task_ids:
            tasks[tid] = tk.task_dto(conn, investor_id, tk.task_row(conn, investor_id, tid),
                                     detail=False)
    return [_run_dto(conn, investor_id, r, reports, tasks) for r in rows]


def _schedule_dto(conn: Any, investor_id: str, v: dict[str, Any], now: datetime,
                  policy: SourcePolicy, *, created_at: datetime | None = None,
                  versions: int | None = None) -> dict[str, Any]:
    state, in_force_id = _commitment(conn, investor_id, v["isin"], now)
    in_force_matches = in_force_id == v["thesis_id"]
    last_due = _last_due(conn, investor_id, v["id"])
    counts = _run_counts(conn, investor_id, v["id"])
    latest = _rows(conn, f"SELECT {_RUN_COLUMNS} {_RUN_FROM} WHERE r.investor_id = %s "
                         "AND r.schedule_id = %s ORDER BY r.due_at DESC, r.id DESC LIMIT 1",
                   (investor_id, v["id"]))
    next_due = None
    if v["enabled"] and v["kind"] == "SCHEDULED":
        next_due = _iso(m.next_due(v["starts_at"], v["timezone"], v["interval_days"],
                                   after=last_due, not_before=v["recorded_at"], now=now))
    watch = None
    if v["kind"] != "SCHEDULED":
        decisions, pending = _evaluate(conn, investor_id, v, policy, now)
        watch = {
            "event_type": m.KIND_EVENT_TYPE[v["kind"]],
            "state": pending[0].code if pending else m.NO_QUALIFYING_EVENT,
            "pending": [{"event_id": p.event_id, "identity": p.identity, "code": p.code,
                         "reason": p.reason} for p in pending[:20]],
            # Qualifying events the next tick will record. Not yet occurrences.
            "awaiting_tick": len(decisions),
            "watching_from": _effective_from(v).isoformat(),
        }
    return {
        "id": v["id"], "version": v["version"], "isin": v["isin"],
        "thesis_id": v["thesis_id"], "skill_id": v["skill_id"],
        "skill_version": v["skill_version"], "agent_id": v["agent_id"],
        "agent_version": v["agent_version"], "kind": v["kind"], "timezone": v["timezone"],
        "starts_at": _iso(v["starts_at"]), "interval_days": v["interval_days"],
        "enabled": v["enabled"], "actor_uid": v["actor_uid"],
        "recorded_at": _iso(v["recorded_at"]),
        "created_at": _iso(created_at or v["recorded_at"]),
        "versions": versions or v["version"],
        "execution_policy_status": EXECUTION_POLICY_UNRESOLVED,
        "execution_policy_reason": m.EXECUTION_POLICY_REASON,
        "commitment": {"state": state, "in_force_thesis_id": in_force_id,
                       "pinned_is_in_force": in_force_matches},
        "display_state": m.schedule_display_state(v["enabled"], state, in_force_matches),
        "next_due_at": next_due,
        "occurrences": {
            "total": sum(counts.values()),
            "executed": counts.get(m.QUEUED, 0),
            "blocked_execution_policy": counts.get(m.BLOCKED_EXECUTION_POLICY, 0),
            "blocked_event": counts.get(m.BLOCKED_EVENT, 0),
            "superseded_thesis": counts.get(m.SUPERSEDED_THESIS, 0),
        },
        "latest_run": _hydrate_runs(conn, investor_id, latest)[0] if latest else None,
        "event_watch": watch,
    }


def schedule_by_version(conn: Any, investor_id: str, schedule_id: str, version: int,
                        now: datetime, policy: SourcePolicy) -> dict[str, Any]:
    versions = _versions(conn, investor_id, schedule_id)
    row = next((v for v in versions if v["version"] == version), None)
    if row is None:
        raise not_found("monitor version")
    return _schedule_dto(conn, investor_id, row, now, policy,
                         created_at=versions[0]["recorded_at"], versions=len(versions))


# =========================================================================================
# writes: schedule versions (HUMAN)
# =========================================================================================


def _validate(conn: Any, investor_id: str, family: list[str], body: dict[str, Any],
              now: datetime) -> dict[str, Any]:
    """Validate a ScheduleCreate. Returns the pinned thesis row."""
    m.check_timezone(body["timezone"])
    kind = body["kind"]
    if kind not in m.KINDS:
        raise invalid("unknown monitor kind", {"kind": "UNKNOWN"})
    interval = body.get("interval_days")
    if kind == "SCHEDULED":
        if interval is None or not 1 <= interval <= m.MAX_INTERVAL_DAYS:
            raise invalid(f"a scheduled monitor needs interval_days between 1 and "
                          f"{m.MAX_INTERVAL_DAYS}", {"interval_days": "RANGE"})
    elif interval is not None:
        raise invalid("an event monitor fires on a verified event, not on a timer; "
                      "interval_days must be null", {"interval_days": "NOT_ALLOWED"})
    starts_at: datetime = body["starts_at"]
    if starts_at.tzinfo is None:
        raise invalid("starts_at must carry a UTC offset", {"starts_at": "FORMAT"})

    thesis = rs.thesis_row(conn, investor_id, body["thesis_id"])
    if thesis["isin"] not in family:
        raise invalid("the thesis belongs to a different company", {"thesis_id": "COMPANY"})
    agent = _one(conn, "SELECT skill_id, skill_version FROM agent WHERE investor_id = %s "
                       "AND id = %s AND version = %s",
                 (investor_id, _uuid(body["agent_id"], "agent"), body["agent_version"]))
    if agent is None:
        raise invalid("no such agent version", {"agent_id": "NOT_FOUND"})
    if (agent["skill_id"] != _uuid(body["skill_id"], "skill")
            or agent["skill_version"] != body["skill_version"]):
        raise invalid("the agent version runs a different method version",
                      {"skill_id": "AGENT_MISMATCH"})
    # An enabled monitor must watch the commitment actually in force now. A paused version
    # may keep an older binding -- it watches nothing until it is resumed, which re-checks.
    if body["enabled"]:
        try:
            rs.resolve_commitment(conn, investor_id, thesis["id"], now)
        except WorkspaceError as exc:
            raise WorkspaceError(exc.status, exc.code,
                                 "an enabled monitor must watch the commitment in force now; "
                                 "bind it to that thesis version or save it paused",
                                 {"thesis_id": exc.code}) from exc
    return thesis


def _insert_version(conn: Any, investor_id: str, actor_uid: str, schedule_id: str,
                    version: int, isin: str, body: dict[str, Any], dedupe: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO monitor_schedule (investor_id, id, version, isin, thesis_id, skill_id,
                skill_version, agent_id, agent_version, kind, timezone, starts_at,
                interval_days, enabled, actor_uid, dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (investor_id, schedule_id, version, isin, body["thesis_id"], body["skill_id"],
             body["skill_version"], body["agent_id"], body["agent_version"], body["kind"],
             body["timezone"], body["starts_at"], body.get("interval_days"), body["enabled"],
             actor_uid, dedupe),
        )


def create_schedule(conn: Any, investor_id: str, actor_uid: str, isin: str,
                    body: dict[str, Any], key: str, payload_digest: str, now: datetime,
                    policy: SourcePolicy) -> tuple[dict[str, Any], int]:
    """Version 1 of a new schedule. Saved with EXECUTION_POLICY_UNRESOLVED, never 'running'."""
    isin = ws.require_unlocked(conn, investor_id, isin)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_CREATE, isin, key, payload_digest)
    if receipt is not None:
        return schedule_by_version(conn, investor_id, receipt.result["schedule_id"],
                                   int(receipt.result["version"]), now, policy), 200
    thesis = _validate(conn, investor_id, ws.isin_family(conn, isin), body, now)
    schedule_id = str(uuid4())
    _insert_version(conn, investor_id, actor_uid, schedule_id, 1, thesis["isin"], body,
                    ws.dedupe_key(OP_CREATE, isin, key))
    ws.audit_write(conn, actor_uid, OP_CREATE, "monitor_schedule", schedule_id, version=1)
    ws.record_receipt(conn, investor_id, OP_CREATE, isin, key, payload_digest,
                      {"schedule_id": schedule_id, "version": "1"}, actor_uid)
    return schedule_by_version(conn, investor_id, schedule_id, 1, now, policy), 201


def append_version(conn: Any, investor_id: str, actor_uid: str, schedule_id: str,
                   body: dict[str, Any], expected_version: int, key: str, payload_digest: str,
                   now: datetime, policy: SourcePolicy) -> tuple[dict[str, Any], int]:
    """Pause, resume, rebind and reschedule: each is a full new version, under the tenant
    lock, against ``expected_version``. Concurrent appends yield one winner and one 409."""
    schedule_id = _uuid(schedule_id, "monitor")
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_VERSION, schedule_id, key, payload_digest)
    if receipt is not None:
        return schedule_by_version(conn, investor_id, receipt.result["schedule_id"],
                                   int(receipt.result["version"]), now, policy), 200
    latest = latest_version(conn, investor_id, schedule_id)
    if expected_version != latest["version"]:
        raise conflict(VERSION_CONFLICT,
                       f"expected version {expected_version}, but the latest is "
                       f"{latest['version']}; reload")
    thesis = _validate(conn, investor_id, ws.isin_family(conn, latest["isin"]), body, now)
    version = latest["version"] + 1
    _insert_version(conn, investor_id, actor_uid, schedule_id, version, thesis["isin"], body,
                    ws.dedupe_key(OP_VERSION, schedule_id, key))
    ws.audit_write(conn, actor_uid, OP_VERSION, "monitor_schedule", schedule_id,
                   version=version)
    ws.record_receipt(conn, investor_id, OP_VERSION, schedule_id, key, payload_digest,
                      {"schedule_id": schedule_id, "version": str(version)}, actor_uid)
    return schedule_by_version(conn, investor_id, schedule_id, version, now, policy), 201


def refuse_manual_run(conn: Any, investor_id: str, schedule_id: str,
                      schedule_version: int) -> None:
    """POST .../runs. Always 409 EXECUTION_POLICY_UNRESOLVED while BE-Q01 is open.

    The schedule is resolved first so a foreign or unknown id is still a 404 -- the gate does
    not become an existence oracle. Nothing is written: no receipt, no occurrence, no task.
    """
    latest = latest_version(conn, investor_id, schedule_id)
    if schedule_version != latest["version"]:
        raise conflict(VERSION_CONFLICT, "schedule_version is not the latest version")
    raise WorkspaceError(409, EXECUTION_POLICY_UNRESOLVED, m.MANUAL_RUN_REASON)


# =========================================================================================
# reads: listings
# =========================================================================================


def list_schedules(conn: Any, investor_id: str, isin: str | None, cursor: str | None,
                   limit: int | None, now: datetime, policy: SourcePolicy) -> dict[str, Any]:
    """Latest version of every schedule on a visible company, newest schedule first."""
    size = canonical.clamp_limit(limit)
    family: list[str] | None = None
    if isin:
        family = ws.isin_family(conn, ws.require_unlocked(conn, investor_id, isin))
    scope = canonical.scope_digest(investor_id, "monitors", {"isin": isin or None})
    after = canonical.decode_cursor(cursor, scope)
    visible = sorted(ws.visible_isins(conn, investor_id))
    rows = _rows(conn, f"""
        WITH latest AS (
          SELECT DISTINCT ON (s.id) {_SCHEDULE_COLUMNS} FROM monitor_schedule s
           WHERE s.investor_id = %s ORDER BY s.id, s.version DESC
        ), created AS (
          SELECT id, min(recorded_at) AS created_at, count(*) AS versions
            FROM monitor_schedule WHERE investor_id = %s GROUP BY id
        )
        SELECT l.*, c.created_at, c.versions FROM latest l JOIN created c ON c.id = l.id
         WHERE l.isin = ANY(%s::text[])
           AND (%s::text[] IS NULL OR l.isin = ANY(%s::text[]))
           AND (%s::timestamptz IS NULL OR (c.created_at, l.id) < (%s::timestamptz, %s::uuid))
         ORDER BY c.created_at DESC, l.id DESC
         LIMIT %s
        """, (investor_id, investor_id, visible, family, family,
              after[0] if after else None, after[0] if after else None,
              after[1] if after else None, size + 1))
    more = len(rows) > size
    rows = rows[:size]
    items = [_schedule_dto(conn, investor_id, r, now, policy, created_at=r["created_at"],
                           versions=int(r["versions"])) for r in rows]
    return {"items": items,
            "next_cursor": (canonical.encode_cursor(
                scope, [_iso(rows[-1]["created_at"]), rows[-1]["id"]]) if more and rows
                else None)}


def list_runs(conn: Any, investor_id: str, schedule_id: str, cursor: str | None,
              limit: int | None) -> dict[str, Any]:
    """Every recorded occurrence of one schedule, newest due first. History, not a feed."""
    latest = latest_version(conn, investor_id, schedule_id)
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "monitor-runs", {"schedule": latest["id"]})
    after = canonical.decode_cursor(cursor, scope)
    rows = _rows(conn, f"""
        SELECT {_RUN_COLUMNS} {_RUN_FROM}
         WHERE r.investor_id = %s AND r.schedule_id = %s
           AND (%s::timestamptz IS NULL OR (r.due_at, r.id) < (%s::timestamptz, %s::uuid))
         ORDER BY r.due_at DESC, r.id DESC LIMIT %s
        """, (investor_id, latest["id"], after[0] if after else None,
              after[0] if after else None, after[1] if after else None, size + 1))
    more = len(rows) > size
    rows = rows[:size]
    return {"items": _hydrate_runs(conn, investor_id, rows),
            "next_cursor": (canonical.encode_cursor(
                scope, [_iso(rows[-1]["due_at"]), rows[-1]["id"]]) if more and rows else None)}


def run_detail(conn: Any, investor_id: str, run_id: str) -> dict[str, Any]:
    """{run, task, report}. A blocked occurrence has no task and no report, and says so."""
    run_id = _uuid(run_id, "monitor run")
    row = _one(conn, f"SELECT {_RUN_COLUMNS} {_RUN_FROM} WHERE r.investor_id = %s AND r.id = %s",
               (investor_id, run_id))
    if row is None:
        raise not_found("monitor run")
    ws.require_unlocked(conn, investor_id, row["isin"])
    run = _hydrate_runs(conn, investor_id, [row])[0]
    task = None
    if row["task_id"]:
        from app.db import tasks as tk

        task = tk.task_by_id(conn, investor_id, row["task_id"])
    report = None
    if run["report_id"]:
        # BE 39 (report assembly and its read route) is blocked on BE-Q01; no report row can
        # exist yet. The identity is still returned if one ever does, never an invented body.
        rep = _one(conn, "SELECT id, title, lede, recorded_at FROM monitor_report "
                         "WHERE investor_id = %s AND id = %s", (investor_id, run["report_id"]))
        if rep is not None:
            report = {"id": rep["id"], "title": rep["title"], "lede": rep["lede"],
                      "recorded_at": _iso(rep["recorded_at"])}
    return {"run": run, "task": task, "report": report}


# =========================================================================================
# the tick (SCHEDULER)
# =========================================================================================


def schedule_ids(conn: Any, investor_id: str) -> list[str]:
    return [r["id"] for r in _rows(conn, "SELECT DISTINCT id FROM monitor_schedule "
                                         "WHERE investor_id = %s ORDER BY id", (investor_id,))]


def _insert_run(conn: Any, investor_id: str, v: dict[str, Any], due_at: datetime,
                disposition: str, reason: str, event_id: str | None, identity: str | None,
                dedupe: str) -> bool:
    if disposition == m.QUEUED:  # unreachable while BE-Q01 is open; refused, not trusted
        raise RuntimeError("the monitor tick never queues work while BE-Q01 is open")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO monitor_run (investor_id, isin, schedule_id, schedule_version, thesis_id,
                due_at, event_id, event_identity, disposition, reason, dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING id
            """,
            (investor_id, v["isin"], v["id"], v["version"], v["thesis_id"], due_at, event_id,
             identity, disposition, reason, dedupe),
        )
        return cur.fetchone() is not None


def tick_schedule(conn: Any, investor_id: str, schedule_id: str, now: datetime,
                  policy: SourcePolicy) -> dict[str, int]:
    """Record every occurrence of one schedule that is due at ``now`` and not yet recorded.

    The caller commits. The latest version decides; a paused latest version records nothing.
    Each occurrence pins the version and thesis it was bound to and checks, at its due cutoff,
    that the pinned thesis is still the commitment in force.
    """
    ws.lock_tenant(conn, investor_id)
    versions = _versions(conn, investor_id, schedule_id)
    counts = {"recorded": 0, "blocked_execution_policy": 0, "superseded_thesis": 0,
              "blocked_event": 0}
    if not versions or not versions[-1]["enabled"]:
        return counts
    v = versions[-1]

    def record(due_at: datetime, event: m.EventDecision | None) -> None:
        # The commitment in force when this occurrence became due for THIS version: the due
        # instant, but never before the version itself existed.
        cutoff = max(due_at, v["recorded_at"])
        if event is not None and not event.matched:
            disposition, reason = m.BLOCKED_EVENT, event.reason or ""
        else:
            state, in_force = _commitment(conn, investor_id, v["isin"], cutoff)
            disposition, reason = m.commitment_disposition(state, in_force, v["thesis_id"])
        if event is None:
            dedupe = f"{OP_TICK}:{schedule_id}:{due_at.isoformat()}"
            written = _insert_run(conn, investor_id, v, due_at, disposition, reason, None, None,
                                  dedupe)
        else:
            dedupe = f"{OP_EVENT}:{schedule_id}:{event.identity}"
            written = _insert_run(conn, investor_id, v, due_at, disposition, reason,
                                  event.event_id, event.identity, dedupe)
        if written:
            counts["recorded"] += 1
            counts[disposition.lower()] += 1

    if v["kind"] == "SCHEDULED":
        for due_at in m.due_instants(v["starts_at"], v["timezone"], v["interval_days"],
                                     after=_last_due(conn, investor_id, schedule_id),
                                     not_before=v["recorded_at"], until=now):
            record(due_at, None)
    else:
        decisions, _ = _evaluate(conn, investor_id, v, policy, now)
        for d in sorted(decisions, key=lambda d: d.due_at)[:m.MAX_DUE_PER_TICK]:
            record(d.due_at, d)
    return counts


def utc_now() -> datetime:
    return datetime.now(UTC)

