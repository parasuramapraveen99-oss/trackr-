"""Agents, sealed tasks, attempts, results, human review and grounded chat (BE 19-25).

Repository for ``app/routers/tasks.py`` over the tables migrations 042/043 created and 044/045
guard. Same connection contract as ``app/db/workspace.py``: every function takes a connection
already scoped by ``enter_workspace``, and every write takes the tenant lock, checks its
idempotency receipt, then writes rows + audit + receipt in ONE transaction.

WHO WRITES WHAT -- the capability split is the design, not a detail (migration 045):

    HUMAN   agents, tasks + sealed context, chat threads and turns, acceptances, rejections
    RUNNER  attempts, results, failures, cancellations, assistant chat turns, lazy expiry
    READER  every GET, the context packet, the tools

The runner has no INSERT on ``note`` or ``task_review``, so the component that records model
output cannot accept it. Acceptance runs as the HUMAN capability with ``reviewer_uid`` taken from
the authenticated session; 044 re-checks ``ws_human()`` and ``ws_actor()`` in the database.

NOTHING HERE CALLS A MODEL, holds a provider key, or stores a request envelope, a stream chunk,
a provider error body or hidden reasoning. The browser calls the provider directly (BE 23) and
submits a strict proposal; this module validates and stores it.

STATE IS DERIVED, never stored (042's five-state projection, computed in the documented order):
done (ACCEPT review) > rejected (REJECT review) > review (a PROPOSED result, including a blocked
one) > running (latest attempt has no terminal result) > assigned.
"""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from app.db import research as rs
from app.db import research_skills as rsk
from app.db import workspace as ws
from app.research import canonical
from app.research import context as cx
from app.research import guardrails as gr
from app.research import skills as sk
from app.research import text as tx
from app.research.errors import (
    CONTEXT_DRIFT,
    VERSION_CONFLICT,
    WorkspaceError,
    conflict,
    invalid,
    not_found,
    ritual_required,
)
from app.research.source_policy import SourcePolicy

# --- operations (ws_receipt.operation) ---------------------------------------------------------
OP_AGENT_CREATE = "agent.create"
OP_AGENT_VERSION = "agent.version"
OP_TASK_CREATE = "task.create"
OP_ATTEMPT = "attempt.start"
OP_RESULT = "result.submit"
OP_FAILURE = "result.failure"
OP_CANCEL = "result.cancel"
OP_EXPIRE = "result.expire"
OP_ACCEPT = "task.accept"
OP_REJECT = "task.reject"
OP_THREAD = "chat.thread"
OP_TURN = "chat.turn"

# --- stable codes -----------------------------------------------------------------------------
ATTEMPT_ACTIVE = "ATTEMPT_ACTIVE"
TASK_CLOSED = "TASK_CLOSED"
LEASE_EXPIRED = "LEASE_EXPIRED"
RESULT_EXISTS = "RESULT_EXISTS"
RESULT_STALE = "RESULT_STALE"
REVIEW_EXISTS = "REVIEW_EXISTS"
BLOCKED_PROPOSAL = "BLOCKED_PROPOSAL"
NOT_REVIEWABLE = "NOT_REVIEWABLE"
SAMPLE_NOT_PERMITTED = "SAMPLE_NOT_PERMITTED"
SAMPLE_NOT_PROMOTABLE = "SAMPLE_NOT_PROMOTABLE"
AGENT_DISABLED = "AGENT_DISABLED"
SKILL_DISABLED = "SKILL_DISABLED"
SKILL_MISMATCH = "SKILL_MISMATCH"
METHOD_ROUTE = "METHOD_ROUTE"
PRECONDITION_UNMET = "PRECONDITION_UNMET"
PROPOSAL_TOO_LARGE = "PROPOSAL_TOO_LARGE"
ENDPOINT_NOT_ALLOWED = "ENDPOINT_NOT_ALLOWED"
OWN_ANSWER_FIRST = "OWN_ANSWER_FIRST"
EXECUTION_DISABLED = "EXECUTION_DISABLED"

MAX_PROPOSAL_BYTES = 256 * 1024
HOME_REVIEWS = 20

#: Named, attributed stored samples. Loaded by ``sample_ref`` only; a browser can never upload
#: text that becomes a trusted sample.
SAMPLES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "research_samples"

_rows = ws._rows
_one = ws._one
_uuid = ws._uuid

_KIND_COLUMN = {"note": "note_id", "thesis": "thesis_id", "answer": "answer_id",
                "source": "source_id", "fact": "fact_id", "evaluation": "evaluation_id"}


def _now() -> datetime:
    return datetime.now(UTC)


def _db_now(conn: Any) -> datetime:
    """Transaction start time from the database clock -- the same instant ``now()`` gives
    migration 042's ``context_cutoff`` default, so a server-derived cutoff never exceeds
    ``created_at`` (which 044 stamps with ``clock_timestamp()``)."""
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


# =========================================================================================
# BE 19 -- agent configurations
# =========================================================================================

_RESERVED_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".invalid", ".example",
                      ".lan", ".home", ".corp", ".intranet", ".onion", ".arpa")
_HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PATH = re.compile(r"^(/[A-Za-z0-9._~%-]*)*$")


def check_base_url(raw: str) -> str:
    """Normalise an endpoint and refuse anything that is not a public HTTPS destination.

    Checked on the NORMALISED, PERCENT-DECODED form, so an encoded ``@``, ``?``, ``#``, ``\\``
    or dot-segment cannot pass the pattern and then resolve elsewhere. The host must be a
    public-looking DNS name: IP literals, single labels, ``localhost`` and reserved suffixes are
    refused. This is a SYNTACTIC policy -- the server performs no DNS lookup and no request,
    because it has no business reaching the provider at all; the browser's own connection is
    what actually tests reachability.
    """
    text = raw.strip()
    if any(ch in text for ch in ("\\", " ", "\t", "\n", "\r")):
        raise invalid("base_url contains a forbidden character", {"base_url": ENDPOINT_NOT_ALLOWED},
                      code=ENDPOINT_NOT_ALLOWED)
    parts = urlsplit(text)
    refuse = invalid("base_url must be https://public-host[:port][/path] with no credentials, "
                     "query or fragment", {"base_url": ENDPOINT_NOT_ALLOWED},
                     code=ENDPOINT_NOT_ALLOWED)
    if parts.scheme.lower() != "https" or parts.query or parts.fragment or "?" in text \
            or "#" in text or parts.username is not None or parts.password is not None \
            or "@" in parts.netloc:
        raise refuse
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as exc:
        raise refuse from exc
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise refuse
    except ValueError:
        pass
    if not _HOST.match(host) or host == "localhost" or host.endswith(_RESERVED_SUFFIXES):
        raise refuse
    path = parts.path or ""
    decoded = path
    for _ in range(3):  # decode repeatedly: %2540 -> %40 -> @
        decoded = unquote(decoded)
    if any(ch in decoded for ch in ("@", "?", "#", "\\", "\x00")) or "/../" in f"{decoded}/" \
            or "/./" in f"{decoded}/" or not _PATH.match(path):
        raise refuse
    path = path.rstrip("/")
    port_text = f":{port}" if port is not None and port != 443 else ""
    return f"https://{host}{port_text}{path}"


_AGENT_COLUMNS = """
a.id, a.version, a.name, a.provider, a.model, a.base_url, a.role_description, a.skill_id,
a.skill_version, a.enabled, a.actor_uid, a.recorded_at
"""


def _agent_dto(row: dict[str, Any]) -> dict[str, Any]:
    # Enumerated: a future column cannot leak through this DTO.
    return {k: row[k] for k in ("id", "version", "name", "provider", "model", "base_url",
                                "role_description", "skill_id", "skill_version", "enabled",
                                "actor_uid", "recorded_at")}


def agent_version(conn: Any, investor_id: str, agent_id: str, version: int) -> dict[str, Any]:
    agent_id = _uuid(agent_id, "agent")
    row = _one(conn, f"SELECT {_AGENT_COLUMNS} FROM agent a "
                     "WHERE a.investor_id = %s AND a.id = %s AND a.version = %s",
               (investor_id, agent_id, version))
    if row is None:
        raise not_found("agent version")
    return _agent_dto(row)


def list_agents(conn: Any, investor_id: str, history: bool, cursor: str | None,
                limit: int | None) -> dict[str, Any]:
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "agents", {"history": history})
    after = canonical.decode_cursor(cursor, scope)
    latest_only = "" if history else """
      AND NOT EXISTS (SELECT 1 FROM agent n WHERE n.investor_id = a.investor_id
                       AND n.id = a.id AND n.version > a.version)"""
    rows = _rows(
        conn,
        f"""
        SELECT {_AGENT_COLUMNS} FROM agent a
         WHERE a.investor_id = %(inv)s {latest_only}
           AND (%(k0)s::uuid IS NULL OR (a.id, a.version) > (%(k0)s::uuid, %(k1)s::int))
         ORDER BY a.id, a.version LIMIT %(lim)s
        """,
        {"inv": investor_id, "k0": after[0] if after else None,
         "k1": after[1] if after else None, "lim": size + 1},
    )
    return ws._page(conn, rows, size, scope, lambda page: [_agent_dto(r) for r in page],
                    key=lambda r: [r["id"], r["version"]])


def _insert_agent(conn: Any, investor_id: str, actor_uid: str, agent_id: str, version: int,
                  body: dict[str, Any], dkey: str) -> None:
    for field_name, limit in (("name", tx.TITLE_MAX_BYTES), ("model", tx.SHORT_FIELD_MAX_BYTES),
                              ("role_description", tx.REASON_MAX_BYTES)):
        tx.check_text(body[field_name], field_name, limit)
    base_url = check_base_url(body["base_url"])
    # Exact method version, owned by this investor. Never "the latest" (404 if absent).
    rsk.skill_version(conn, investor_id, body["skill_id"], body["skill_version"])
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent (investor_id, id, version, name, provider, model, base_url,
                               role_description, skill_id, skill_version, enabled, actor_uid,
                               dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, agent_id, version, body["name"], body["provider"], body["model"],
             base_url, body["role_description"], _uuid(body["skill_id"], "skill"),
             body["skill_version"], body["enabled"], actor_uid, dkey),
        )


def create_agent(conn: Any, investor_id: str, actor_uid: str, body: dict[str, Any], key: str,
                 payload_digest: str) -> tuple[dict[str, Any], int]:
    """Version 1 of a new configuration. Endpoint metadata only; no connection check."""
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_AGENT_CREATE, "-", key, payload_digest)
    if receipt is not None:
        return agent_version(conn, investor_id, receipt.result["agent_id"], 1), 200
    agent_id = str(uuid.uuid4())
    _insert_agent(conn, investor_id, actor_uid, agent_id, 1, body,
                  ws.dedupe_key(OP_AGENT_CREATE, "-", key))
    ws.audit_write(conn, actor_uid, OP_AGENT_CREATE, "agent", agent_id, version=1)
    ws.record_receipt(conn, investor_id, OP_AGENT_CREATE, "-", key, payload_digest,
                      {"agent_id": agent_id}, actor_uid)
    return agent_version(conn, investor_id, agent_id, 1), 201


def append_agent_version(conn: Any, investor_id: str, actor_uid: str, agent_id: str,
                         body: dict[str, Any], key: str, payload_digest: str
                         ) -> tuple[dict[str, Any], int]:
    """Rename, model/endpoint change, method rebinding, disable and re-enable are all new
    versions. The receipt scope is investor-wide and the digest includes the agent id, so the
    same key reused for a different agent is a 409, not a second write."""
    agent_id = _uuid(agent_id, "agent")
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_AGENT_VERSION, "-", key, payload_digest)
    if receipt is not None:
        return agent_version(conn, investor_id, receipt.result["agent_id"],
                             int(receipt.result["version"])), 200
    current = _one(conn, "SELECT COALESCE(max(version), 0) AS v FROM agent "
                         "WHERE investor_id = %s AND id = %s", (investor_id, agent_id))
    if not current or current["v"] == 0:
        raise not_found("agent")
    if body["expected_version"] != current["v"]:
        raise conflict(VERSION_CONFLICT,
                       f"this agent is at version {current['v']}; reload before saving")
    version = current["v"] + 1
    _insert_agent(conn, investor_id, actor_uid, agent_id, version, body,
                  ws.dedupe_key(OP_AGENT_VERSION, agent_id, key))
    ws.audit_write(conn, actor_uid, OP_AGENT_VERSION, "agent", agent_id, version=version,
                   enabled=body["enabled"])
    ws.record_receipt(conn, investor_id, OP_AGENT_VERSION, "-", key, payload_digest,
                      {"agent_id": agent_id, "version": str(version)}, actor_uid)
    return agent_version(conn, investor_id, agent_id, version), 201


# =========================================================================================
# task rows and the five-state projection
# =========================================================================================

_TASK_COLUMNS = """
t.id, t.seq, t.isin, t.purpose, t.title, t.brief, t.skill_id, t.skill_version, t.agent_id,
t.agent_version, t.thesis_id, t.answer_id, t.chat_thread_id, t.parent_task_id, t.created_by,
t.created_at, t.context_cutoff, t.context_digest, t.context_manifest, t.house_rules_version
"""

_STATE_SQL = """
CASE
  WHEN rv.decision = 'ACCEPT' THEN 'done'
  WHEN rv.decision = 'REJECT' THEN 'rejected'
  WHEN EXISTS (SELECT 1 FROM task_result r WHERE r.investor_id = t.investor_id
                AND r.task_id = t.id AND r.outcome = 'PROPOSED') THEN 'review'
  WHEN la.id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task_result r
                WHERE r.investor_id = t.investor_id AND r.attempt_id = la.id) THEN 'running'
  ELSE 'assigned'
END
"""

_STATE_JOINS = """
LEFT JOIN task_review rv ON rv.investor_id = t.investor_id AND rv.task_id = t.id
LEFT JOIN LATERAL (SELECT a.id FROM task_attempt a
                    WHERE a.investor_id = t.investor_id AND a.task_id = t.id
                    ORDER BY a.attempt_no DESC LIMIT 1) la ON true
"""


def task_row(conn: Any, investor_id: str, task_id: str) -> dict[str, Any]:
    """Exact task, gated on its company. Foreign -> 404; locked -> 409."""
    task_id = _uuid(task_id, "task")
    row = _one(conn, f"SELECT {_TASK_COLUMNS} FROM task t WHERE t.investor_id = %s AND t.id = %s",
               (investor_id, task_id))
    if row is None:
        raise not_found("task")
    if row["isin"] not in ws.visible_isins(conn, investor_id):
        raise ritual_required()
    return row


def _attempts(conn: Any, investor_id: str, task_id: str) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT id, task_id, attempt_no, mode, sample_ref, started_at, lease_until
          FROM task_attempt WHERE investor_id = %s AND task_id = %s ORDER BY attempt_no
        """, (investor_id, task_id))


def _results(conn: Any, investor_id: str, task_id: str) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT id, task_id, attempt_id, outcome, output, structured_output, guardrail_status,
               error_code, finished_at, duration_ms
          FROM task_result WHERE investor_id = %s AND task_id = %s ORDER BY finished_at, id
        """, (investor_id, task_id))


def _review(conn: Any, investor_id: str, task_id: str) -> dict[str, Any] | None:
    return _one(conn, """
        SELECT id, task_id, result_id, decision, reason, accepted_note_id, reviewer_uid,
               reviewed_at
          FROM task_review WHERE investor_id = %s AND task_id = %s
        """, (investor_id, task_id))


def _citations(conn: Any, investor_id: str, result_id: str) -> list[dict[str, str]]:
    return _rows(conn, "SELECT context_id, claim_ref FROM result_citation "
                       "WHERE investor_id = %s AND result_id = %s ORDER BY claim_ref, context_id",
                 (investor_id, result_id))


def _attempt_dto(a: dict[str, Any], has_result: bool, now: datetime) -> dict[str, Any]:
    return {**{k: a[k] for k in ("id", "task_id", "attempt_no", "mode", "sample_ref",
                                  "started_at", "lease_until")},
            "lease_expired": (not has_result) and now >= a["lease_until"]}


def context_rows(conn: Any, investor_id: str, task_id: str) -> list[dict[str, Any]]:
    rows = _rows(conn, """
        SELECT id, ordinal, note_id, thesis_id, answer_id, source_id, fact_id, evaluation_id
          FROM task_context WHERE investor_id = %s AND task_id = %s ORDER BY ordinal
        """, (investor_id, task_id))
    for row in rows:
        kind = next(k for k, col in _KIND_COLUMN.items() if row[col] is not None)
        row["kind"] = kind
        row["ref_id"] = row[_KIND_COLUMN[kind]]
    return rows


def entries_for(conn: Any, investor_id: str, task_id: str,
                quotable: dict[str, tuple[str, ...]] | None = None
                ) -> dict[str, gr.ContextEntry]:
    """Guardrail/render view of a task's sealed context. Reads immutable rows only."""
    out: dict[str, gr.ContextEntry] = {}
    for row in context_rows(conn, investor_id, task_id):
        ref = cx.load_ref(conn, investor_id, row["kind"], row["ref_id"])
        out[row["id"]] = gr.ContextEntry(
            context_id=row["id"], kind=row["kind"], content=ref.content,
            quotable=(quotable or {}).get(row["id"], ref.texts))
    return out


def _result_dto(conn: Any, investor_id: str, task: dict[str, Any], r: dict[str, Any],
                attempts: dict[str, dict[str, Any]], detail: bool) -> dict[str, Any]:
    sample = attempts.get(r["attempt_id"], {}).get("mode") == "STORED_SAMPLE"
    structured = r["structured_output"] or {}
    proposal = structured.get("proposal") if r["outcome"] == "PROPOSED" else None
    rendered = None
    if detail and proposal is not None:
        entry = sk.entry_for(task["skill_id"])
        if entry is not None and entry.task_route == "TASKS":
            contract = sk.compile_contract(entry.output_contract)
            full = task if "context_manifest" in task else task_row(conn, investor_id,
                                                                    task["id"])
            rendered = gr.render(proposal, entries_for(conn, investor_id, task["id"]), contract,
                                 calculation_results(conn, investor_id, full))
            rendered["deterministic_blocks"] = method_blocks(conn, investor_id, full, proposal)
        else:
            rendered = structured.get("rendered")
    return {
        "id": r["id"], "task_id": r["task_id"], "attempt_id": r["attempt_id"],
        "outcome": r["outcome"], "guardrail_status": r["guardrail_status"],
        "guardrail_reasons": list((structured.get("guardrail") or {}).get("reasons", [])),
        "error_code": r["error_code"], "finished_at": r["finished_at"],
        "duration_ms": r["duration_ms"],
        "citations": _citations(conn, investor_id, r["id"]) if detail else [],
        "sample": sample,
        "invocation_evidence": "STORED_SAMPLE" if sample else "CLIENT_REPORTED",
        "proposal": proposal if detail else None,
        "rendered": rendered,
    }


def task_dto(conn: Any, investor_id: str, row: dict[str, Any], detail: bool = True
             ) -> dict[str, Any]:
    now = _now()
    attempts = _attempts(conn, investor_id, row["id"])
    results = _results(conn, investor_id, row["id"])
    review = _review(conn, investor_id, row["id"])
    by_attempt = {r["attempt_id"]: r for r in results}
    latest = attempts[-1] if attempts else None
    proposed = next((r for r in results if r["outcome"] == "PROPOSED"), None)
    if review is not None:
        state = "done" if review["decision"] == "ACCEPT" else "rejected"
    elif proposed is not None:
        state = "review"
    elif latest is not None and latest["id"] not in by_attempt:
        state = "running"
    else:
        state = "assigned"
    shown = proposed or (by_attempt.get(latest["id"]) if latest else None)
    amap = {a["id"]: a for a in attempts}
    entry = sk.entry_for(row["skill_id"])
    return {
        **{k: row[k] for k in ("id", "isin", "purpose", "title", "brief", "skill_id",
                               "skill_version", "agent_id", "agent_version", "thesis_id",
                               "answer_id", "chat_thread_id", "parent_task_id", "created_by",
                               "created_at", "context_cutoff", "context_digest",
                               "house_rules_version")},
        "method_key": entry.key if entry else None,
        "state": state,
        "latest_attempt": (_attempt_dto(latest, latest["id"] in by_attempt, now)
                           if latest else None),
        "result": _result_dto(conn, investor_id, row, shown, amap, detail) if shown else None,
        "review": review,
        "sample": bool(shown and amap.get(shown["attempt_id"], {}).get("mode")
                       == "STORED_SAMPLE"),
        "error_code": (shown["error_code"] if shown and state == "assigned" else None),
        "attempts": len(attempts),
    }


def task_by_id(conn: Any, investor_id: str, task_id: str) -> dict[str, Any]:
    return task_dto(conn, investor_id, task_row(conn, investor_id, task_id), detail=True)


def list_tasks(conn: Any, investor_id: str, state: str | None, isin: str | None,
               purpose: str | None, cursor: str | None, limit: int | None) -> dict[str, Any]:
    size = canonical.clamp_limit(limit)
    if state is not None and state not in ("assigned", "running", "review", "done", "rejected"):
        raise invalid("unknown state", {"state": "ENUM"})
    if purpose is not None and purpose not in ("RESEARCH", "ANSWER_REVIEW", "CHAT", "INLINE",
                                               "MONITOR"):
        raise invalid("unknown purpose", {"purpose": "ENUM"})
    filters = {"state": state, "isin": isin, "purpose": purpose}
    scope = canonical.scope_digest(investor_id, "tasks", filters)
    after = canonical.decode_cursor(cursor, scope)
    visible = ws.visible_isins(conn, investor_id)
    allowed = sorted(visible)
    if isin is not None:
        isin = ws.require_unlocked(conn, investor_id, isin)
        allowed = [i for i in ws.isin_family(conn, isin) if i in visible]
    rows = _rows(
        conn,
        f"""
        SELECT * FROM (
          SELECT t.id, t.seq, {_STATE_SQL} AS state
            FROM task t {_STATE_JOINS}
           WHERE t.investor_id = %(inv)s AND t.isin = ANY(%(allowed)s::text[])
             AND (%(purpose)s::text IS NULL OR t.purpose = %(purpose)s::text)
        ) x
        WHERE (%(state)s::text IS NULL OR x.state = %(state)s::text)
          AND (%(k0)s::bigint IS NULL OR x.seq < %(k0)s::bigint)
        ORDER BY x.seq DESC LIMIT %(lim)s
        """,
        {"inv": investor_id, "allowed": allowed, "purpose": purpose, "state": state,
         "k0": after[0] if after else None, "lim": size + 1},
    )

    def hydrate(page: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [task_dto(conn, investor_id, task_row(conn, investor_id, r["id"]), detail=False)
                for r in page]

    return ws._page(conn, rows, size, scope, hydrate, key=lambda r: [r["seq"]])


def pending_reviews(conn: Any, investor_id: str, investor_path: str) -> list[dict[str, Any]]:
    """Home's ``reviews``: tasks with a PROPOSED result and no review, newest first, bounded,
    each with a continuation URL. Butler selections are not note proposals and are excluded:
    they are shown in Butler, never as something to accept into the notebook."""
    visible = sorted(ws.visible_isins(conn, investor_id))
    rows = _rows(
        conn,
        """
        SELECT t.id AS task_id, t.isin, t.title, t.purpose, t.skill_id, r.id AS result_id,
               r.guardrail_status, r.finished_at, (a.mode = 'STORED_SAMPLE') AS sample
          FROM task t
          JOIN task_result r ON r.investor_id = t.investor_id AND r.task_id = t.id
                            AND r.outcome = 'PROPOSED'
          JOIN task_attempt a ON a.investor_id = r.investor_id AND a.id = r.attempt_id
         WHERE t.investor_id = %s AND t.isin = ANY(%s::text[]) AND t.skill_id <> %s
           AND NOT EXISTS (SELECT 1 FROM task_review rv
                            WHERE rv.investor_id = t.investor_id AND rv.task_id = t.id)
         ORDER BY r.finished_at DESC LIMIT %s
        """,
        (investor_id, visible, sk.BUTLER_SELECTOR.skill_id, HOME_REVIEWS + 1),
    )
    return [{**{k: r[k] for k in ("task_id", "isin", "title", "purpose", "result_id",
                                  "guardrail_status", "finished_at")},
             "sample": bool(r["sample"]),
             "continuation": f"{investor_path}/tasks/{r['task_id']}"} for r in rows]


# =========================================================================================
# BE 20 -- sealing a task
# =========================================================================================


def _selection(body: dict[str, Any]) -> cx.Selection:
    sel = body["context_selection"]
    pq = sel.get("include_position_query")
    return cx.Selection(
        note_ids=tuple(sel.get("note_ids", [])), source_ids=tuple(sel.get("source_ids", [])),
        fact_ids=tuple(sel.get("fact_ids", [])),
        evaluation_ids=tuple(sel.get("evaluation_ids", [])),
        calculation_specs=tuple(sel.get("calculation_specs", [])),
        position_query=pq, expected_thesis_id=sel.get("expected_thesis_id"),
    )


def _pinned_method(conn: Any, investor_id: str, agent_id: str, agent_version_no: int,
                   skill_id: str, skill_version_no: int, purpose: str, route: str
                   ) -> tuple[dict[str, Any], dict[str, Any], sk.MethodEntry]:
    agent = agent_version(conn, investor_id, agent_id, agent_version_no)
    if not agent["enabled"]:
        raise conflict(AGENT_DISABLED, "this agent configuration version is disabled")
    skill = rsk.skill_version(conn, investor_id, skill_id, skill_version_no)
    if (agent["skill_id"], agent["skill_version"]) != (skill["id"], skill["version"]):
        raise conflict(SKILL_MISMATCH, "the agent pins a different method version")
    if not skill["enabled"]:
        raise conflict(SKILL_DISABLED, "this method version is disabled")
    entry = sk.entry_for(skill["id"])
    if entry is None:
        raise not_found("method")
    if entry.task_route != route or purpose not in entry.purposes:
        raise conflict(METHOD_ROUTE, "this method cannot run for this purpose on this route")
    return agent, skill, entry


def _in_force(conn: Any, investor_id: str, isin: str, cutoff: datetime) -> str | None:
    selection, _ = rs.select_in_force(conn, investor_id, isin, cutoff)
    return selection.thesis.id if selection.state == "ACTIVE" else None


def _seal(
    conn: Any,
    fin_conn: Any,
    investor_id: str,
    actor_uid: str,
    *,
    isin: str,
    purpose: str,
    title: str,
    brief: str,
    agent: dict[str, Any],
    skill: dict[str, Any],
    thesis_id: str | None,
    answer_id: str | None,
    parent_task_id: str | None,
    chat_thread_id: str | None,
    selection: cx.Selection,
    prior_turn_ids: list[str],
    dedupe: str,
    registry_versions: dict[str, str] | None = None,
    digest_override: str | None = None,
    extra_refs: list[cx.Ref] | None = None,
) -> str:
    """Insert task + every task_context row with contiguous ordinals, in the caller's
    transaction, with a digest over exactly the sealed rows. Returns the task id.

    The caller holds the tenant lock and has checked its receipt. Any failure raises and the
    caller's transaction rolls back everything -- there is never a task with a half-written
    context (044 additionally refuses a context row outside the creating transaction).
    """
    tx.check_text(title, "title", tx.TITLE_MAX_BYTES)
    tx.check_text(brief, "brief", tx.NOTE_BODY_MAX_BYTES)
    cutoff = _db_now(conn)
    family = ws.isin_family(conn, isin)

    # expected_thesis_id is what the person saw as in force (null = "none in force"). It must
    # still be true at the cutoff; a stale, superseded or DRAFT id is 409, never re-pointed.
    in_force = _in_force(conn, investor_id, isin, cutoff)
    expected = (_uuid(selection.expected_thesis_id, "thesis")
                if selection.expected_thesis_id else None)
    if expected != in_force:
        raise conflict(cx.THESIS_NOT_IN_FORCE,
                       "expected_thesis_id is not the commitment in force now; reload")
    if thesis_id is not None:
        thesis_id = _uuid(thesis_id, "thesis")
        if thesis_id != in_force:
            raise conflict(cx.THESIS_NOT_IN_FORCE,
                           "a task may pin only the ACTIVE commitment in force at its cutoff")
    if answer_id is not None:
        answer_id = _uuid(answer_id, "answer")
    if parent_task_id is not None:
        parent = task_row(conn, investor_id, parent_task_id)
        if parent["isin"] not in family:
            raise conflict(cx.CROSS_COMPANY, "a linked child task must concern the same company")
        parent_task_id = parent["id"]

    refs = cx.select_refs(conn, investor_id, isin, selection, thesis_id=thesis_id,
                          answer_id=answer_id, cutoff=cutoff)
    refs += extra_refs or []
    calculations = []
    for spec in selection.calculation_specs:
        for fid in spec.get("fact_ids", []):
            ref = cx.load_ref(conn, investor_id, "fact", fid)
            if ref.isin not in ws.visible_isins(conn, investor_id):
                raise ritual_required()
        calculations.append(cx.calculation_ref(spec))
    position = None
    if selection.position_query is not None:
        as_of = selection.position_query["as_of"]
        if isinstance(as_of, str):
            as_of = date.fromisoformat(as_of)
        if as_of > cutoff.date():
            raise invalid("the position as_of cannot be after the cutoff",
                          {"include_position_query": "FUTURE"})
        position = cx.position_query(fin_conn, investor_id, family, as_of,
                                     bool(selection.position_query["advised_only"]))
    prior = _messages_by_ids(conn, investor_id, prior_turn_ids)
    cx.check_size(refs, prior)

    header = cx.Header(purpose=purpose, isin=isin, skill_id=skill["id"],
                       skill_version=skill["version"], agent_id=agent["id"],
                       agent_version=agent["version"], thesis_id=thesis_id,
                       answer_id=answer_id, chat_thread_id=chat_thread_id, cutoff=cutoff)
    digest = digest_override or cx.compute_digest(header, refs, prior, calculations, position)
    manifest = cx.build_manifest(prior_turn_ids, calculations, position, registry_versions)
    task_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task (investor_id, id, isin, purpose, title, brief, skill_id,
                              skill_version, agent_id, agent_version, thesis_id, answer_id,
                              chat_thread_id, parent_task_id, created_by, context_cutoff,
                              context_digest, context_manifest, house_rules_version,
                              dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s)
            """,
            (investor_id, task_id, isin, purpose, title, brief, skill["id"], skill["version"],
             agent["id"], agent["version"], thesis_id, answer_id, chat_thread_id,
             parent_task_id, actor_uid, cutoff, digest, json.dumps(manifest),
             sk.HOUSE_RULES_VERSION, dedupe),
        )
        for ordinal, ref in enumerate(refs):
            column = _KIND_COLUMN[ref.kind]
            cur.execute(
                f"INSERT INTO task_context (investor_id, task_id, ordinal, {column}) "
                "VALUES (%s, %s, %s, %s)",
                (investor_id, task_id, ordinal, ref.id),
            )
    ws.audit_write(conn, actor_uid, OP_TASK_CREATE, "task", task_id, purpose=purpose,
                   context=len(refs), calculations=len(calculations),
                   position=position is not None)
    return task_id


def create_task(conn: Any, fin_conn: Any, investor_id: str, actor_uid: str,
                body: dict[str, Any], key: str, payload_digest: str
                ) -> tuple[dict[str, Any], int]:
    """POST /tasks: RESEARCH, INLINE or ANSWER_REVIEW only (the DTO refuses CHAT/MONITOR)."""
    isin = ws.require_unlocked(conn, investor_id, body["isin"])
    purpose = body["purpose"]
    if (purpose == "ANSWER_REVIEW") != (body.get("answer_id") is not None):
        raise invalid("ANSWER_REVIEW requires answer_id, and only ANSWER_REVIEW may carry one",
                      {"answer_id": "PURPOSE"})
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_TASK_CREATE, isin, key, payload_digest)
    if receipt is not None:
        return task_by_id(conn, investor_id, receipt.result["task_id"]), 200
    agent, skill, entry = _pinned_method(conn, investor_id, body["agent_id"],
                                         body["agent_version"], body["skill_id"],
                                         body["skill_version"], purpose, "TASKS")
    if purpose == "ANSWER_REVIEW":
        _check_own_answer(conn, investor_id, isin, body["answer_id"])
    task_id = _seal(
        conn, fin_conn, investor_id, actor_uid, isin=isin, purpose=purpose,
        title=body["title"], brief=body["brief"], agent=agent, skill=skill,
        thesis_id=body.get("thesis_id"), answer_id=body.get("answer_id"),
        parent_task_id=body.get("parent_task_id"), chat_thread_id=None,
        selection=_selection(body), prior_turn_ids=[],
        dedupe=ws.dedupe_key(OP_TASK_CREATE, isin, key),
    )
    ws.record_receipt(conn, investor_id, OP_TASK_CREATE, isin, key, payload_digest,
                      {"task_id": task_id}, actor_uid)
    return task_by_id(conn, investor_id, task_id), 201


def _check_own_answer(conn: Any, investor_id: str, isin: str, answer_id: str) -> None:
    """BE 25: an existing, human-authored, nonempty answer on this company. Never a
    provisional answer generated so there is something to review."""
    ref = cx.load_ref(conn, investor_id, "answer", answer_id)
    if ref.isin not in ws.isin_family(conn, isin):
        raise conflict(cx.CROSS_COMPANY, "this answer belongs to a different company")
    if not ref.content.get("actor_uid") or not str(ref.content.get("answer", "")).strip():
        raise conflict(OWN_ANSWER_FIRST, "only your own recorded answer can be reviewed")


# =========================================================================================
# context recheck, packet and task scope
# =========================================================================================


def _messages_by_ids(conn: Any, investor_id: str, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = _rows(conn, """
        SELECT id, thread_id, role, body, result_id, recorded_at, seq
          FROM chat_message WHERE investor_id = %s AND id = ANY(%s::uuid[])
        """, (investor_id, ids))
    by_id = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise conflict(CONTEXT_DRIFT, "a pinned chat turn is no longer available")
    return [by_id[i] for i in ids]


def recheck(conn: Any, fin_conn: Any, investor_id: str, task: dict[str, Any]
            ) -> tuple[list[dict[str, Any]], list[cx.Ref], list[dict[str, Any]]]:
    """Rebuild the sealed context from its rows and compare digests. 409 CONTEXT_DRIFT on any
    mismatch; the old figure is never reconstructed. Returns (rows, refs, prior turns)."""
    rows = context_rows(conn, investor_id, task["id"])
    visible = ws.visible_isins(conn, investor_id)
    refs = []
    for row in rows:
        ref = cx.load_ref(conn, investor_id, row["kind"], row["ref_id"])
        if ref.isin not in visible:
            raise ritual_required()
        refs.append(ref)
    manifest = task["context_manifest"] or {}
    prior = _messages_by_ids(conn, investor_id, manifest.get("prior_chat_message_ids", []))
    position = manifest.get("position_query")
    if position is not None:
        now_mark = cx.ledger_watermark(fin_conn, investor_id,
                                       date.fromisoformat(position["as_of"]),
                                       bool(position["advised_only"]))
        if now_mark != position["ledger_watermark"]:
            raise conflict(CONTEXT_DRIFT, "the ledger changed after this task was sealed; the "
                           "position cannot be reproduced -- prepare a new task")
    if _is_butler(task):
        return rows, refs, prior  # Butler packets are rechecked by their own rebuild
    header = cx.Header(purpose=task["purpose"], isin=task["isin"], skill_id=task["skill_id"],
                       skill_version=task["skill_version"], agent_id=task["agent_id"],
                       agent_version=task["agent_version"], thesis_id=task["thesis_id"],
                       answer_id=task["answer_id"], chat_thread_id=task["chat_thread_id"],
                       cutoff=task["context_cutoff"],
                       house_rules_version=task["house_rules_version"])
    digest = cx.compute_digest(header, refs, prior, manifest.get("calculation_specs", []),
                               position)
    if digest != task["context_digest"]:
        raise conflict(CONTEXT_DRIFT, "the pinned context no longer reproduces its digest")
    return rows, refs, prior


def _is_butler(task: dict[str, Any]) -> bool:
    return str(task["skill_id"]) == sk.BUTLER_SELECTOR.skill_id


def _families(conn: Any, entries: dict[str, Any]) -> dict[str, frozenset[str]]:
    """VERIFIED alias family of every company appearing in the pinned context."""
    isins = {e.content["isin"] for e in entries.values() if "isin" in e.content}
    return {i: frozenset(ws.isin_family(conn, i)) for i in isins}


def precondition(conn: Any, investor_id: str, task: dict[str, Any],
                 registry: Any = None) -> dict[str, Any]:
    """The method's deterministic precondition over the PINNED context (BE 17, 30-35).
    UNKNOWN names every gap; a model run is refused until it is READY."""
    from app.research import method_checks as mc
    from app.research.registry import LIVE_REGISTRY

    entry = sk.entry_for(task["skill_id"])
    if entry is None or entry.precondition is None:
        return {"state": "READY", "missing": []}
    if entry.precondition == "ACTIVE_COMMITMENT_AT_CUTOFF":
        selection, _ = rs.select_in_force(conn, investor_id, task["isin"],
                                          task["context_cutoff"])
        return sk.commitment_precondition(
            selection.state, task["thesis_id"],
            selection.thesis.id if selection.state == "ACTIVE" else None)
    entries = entries_for(conn, investor_id, task["id"])
    return mc.precondition(entry.key, entries,
                           registry=registry if registry is not None else LIVE_REGISTRY,
                           subject_family=frozenset(ws.isin_family(conn, task["isin"])),
                           families=_families(conn, entries))


def method_blocks(conn: Any, investor_id: str, task: dict[str, Any],
                  proposal: dict[str, Any] | None, registry: Any = None) -> list[dict[str, Any]]:
    """Code-computed tables shown beside a method's proposal (BE 30-35). Never model-written."""
    from app.research import method_checks as mc
    from app.research.registry import LIVE_REGISTRY

    entry = sk.entry_for(task["skill_id"])
    if entry is None:
        return []
    registry = registry if registry is not None else LIVE_REGISTRY
    key = entry.key
    if key not in ("financial_statement_analysis", "comparable_companies", "due_diligence",
                   "catalyst_calendar"):
        return []
    entries = entries_for(conn, investor_id, task["id"])
    subject = frozenset(ws.isin_family(conn, task["isin"]))
    if key == "financial_statement_analysis":
        return [mc.statement_table(entries, registry)]
    if key == "comparable_companies":
        return [mc.peer_table(entries, subject, _families(conn, entries))]
    if key == "due_diligence":
        return [mc.requirement_states(entries, proposal)]
    from app.db import research_evidence as ev

    sources = {e.content["id"] for e in entries.values() if e.kind == "source"}
    if _one(conn, "SELECT to_regclass('research_event') IS NOT NULL AS ok", None)["ok"]:
        events = [e for e in ev.list_events(conn, investor_id, sorted(subject),
                                            task["context_cutoff"])
                  if e["source_id"] in sources]
    else:
        events = []
    return [mc.event_timeline(events)]


def _prior_turns_view(conn: Any, investor_id: str, prior: list[dict[str, Any]]
                      ) -> list[dict[str, Any]]:
    out = []
    for m in prior:
        content = m["body"]
        if m["role"] == "ASSISTANT" and m["result_id"]:
            res = _one(conn, "SELECT r.task_id, r.structured_output FROM task_result r "
                             "WHERE r.investor_id = %s AND r.id = %s",
                       (investor_id, m["result_id"]))
            content = _assistant_text(conn, investor_id, res)
        out.append({"message_id": m["id"], "role": m["role"], "content": content})
    return out


def _assistant_text(conn: Any, investor_id: str, res: dict[str, Any] | None) -> str:
    """Plain rendered text of an assistant result, for a later turn's context only."""
    if not res:
        return ""
    task = _one(conn, "SELECT id, skill_id FROM task WHERE investor_id = %s AND id = %s",
                (investor_id, res["task_id"]))
    proposal = (res["structured_output"] or {}).get("proposal")
    entry = sk.entry_for(task["skill_id"]) if task else None
    if not proposal or entry is None or entry.task_route != "TASKS":
        return ""
    rendered = gr.render(proposal, entries_for(conn, investor_id, task["id"]),
                         sk.compile_contract(entry.output_contract))
    lines = [rendered["summary"]]
    for section in rendered["sections"]:
        lines.append(section["heading"])
        lines += [p.get("text", "") for p in section["paragraphs"]]
    return "\n".join(line for line in lines if line)


def calculation_results(conn: Any, investor_id: str, task: dict[str, Any],
                        formulas: Any = None) -> dict[str, dict[str, Any]]:
    """Every pinned calculation spec, computed NOW from the facts recorded by the task's cutoff
    by the approved calculator (BE 27-28). Never stored, so it is always re-derivable; a spec
    whose formula is not approved stays UNKNOWN/FORMULA_NOT_APPROVED. Scenario assumptions were
    stated by the person who created the task, and are attributed to them."""
    from app.engine import research_metrics as rm
    from app.research import calculator as calc

    formulas = formulas if formulas is not None else rm.LIVE_FORMULAS
    family = ws.isin_family(conn, task["isin"])
    out: dict[str, dict[str, Any]] = {}
    for spec in (task.get("context_manifest") or {}).get("calculation_specs", []):
        assumptions = list(spec.get("assumptions", []))
        if assumptions and not all(set(a) == {"name", "value", "unit", "reason"}
                                   for a in assumptions):
            result = rm.CalcResult(status=rm.UNKNOWN, formula_id=spec["formula_id"],
                                   formula_version=spec["version"],
                                   reasons=[{"code": rm.ASSUMPTION_INVALID}])
        else:
            result = calc.run(conn, investor_id, family, formulas, spec["formula_id"],
                              spec["version"], list(spec.get("fact_ids", [])), assumptions,
                              str(task["created_by"]) if assumptions else None,
                              task["context_cutoff"])
        out[spec["calculation_ref"]] = calc.packet_result(spec["calculation_ref"], result)
    return out


def packet(conn: Any, fin_conn: Any, investor_id: str, task_id: str, policy: SourcePolicy,
           load_text: cx.TextLoader | None, butler_packet: dict[str, Any] | None = None,
           formulas: Any = None) -> dict[str, Any]:
    """GET /tasks/{id}/context. Rechecks scope, digest, licence and disclosure on every read.
    Starts nothing, sends nothing, needs no key."""
    task = task_row(conn, investor_id, task_id)
    rows, refs, prior = recheck(conn, fin_conn, investor_id, task)
    released = cx.release(rows, refs, policy, load_text)
    skill = rsk.skill_version(conn, investor_id, task["skill_id"], task["skill_version"])
    agent = agent_version(conn, investor_id, task["agent_id"], task["agent_version"])
    manifest = task["context_manifest"] or {}
    calcs = list(calculation_results(conn, investor_id, task, formulas).values())
    exclusions = list(released.exclusions) + [
        {"context_id": c["calculation_ref"], "code": c["code"],
         "message": "no approved formula exists for this calculation (BE-Q03)"
         if c["code"] in ("FORMULA_NOT_APPROVED", "OUTPUT_CONTRACT_NOT_APPROVED")
         else "the calculator could not produce a value from the pinned facts"}
        for c in calcs if c["status"] != "OK"]
    return {
        "task_id": task["id"],
        "context_digest": task["context_digest"],
        "house_rules_version": task["house_rules_version"],
        "skill": {k: skill[k] for k in ("id", "version", "name", "method", "citation_rules",
                                        "forbidden_conclusions", "output_contract",
                                        "permitted_tools", "method_key")},
        "agent": {k: agent[k] for k in ("id", "version", "provider", "model", "base_url")},
        "purpose": task["purpose"],
        "brief": task["brief"],
        "system_instructions": cx.system_instructions(skill, task["purpose"]),
        "ordered_context": released.ordered_context,
        "prior_chat_turns": _prior_turns_view(conn, investor_id, prior),
        "calculation_results": calcs,
        "position_query": ({k: v for k, v in manifest["position_query"].items()}
                           if manifest.get("position_query") else None),
        "allowed_tools": list(skill["permitted_tools"]),
        "exclusions": exclusions,
        "disclosure": {"destination_origin": cx.destination_origin(agent["base_url"]),
                       "source_verdict_refs": released.source_verdicts},
        "precondition": precondition(conn, investor_id, task),
        "butler_packet": butler_packet,
    }


def task_scope(conn: Any, investor_id: str, task_id: str) -> Any:
    """What a task-scoped tool call may see (``app/research/tools.py``)."""
    from app.research.tools import TaskScope

    task = task_row(conn, investor_id, task_id)
    pinned: dict[str, set[str]] = {}
    for row in context_rows(conn, investor_id, task["id"]):
        pinned.setdefault(row["kind"], set()).add(row["ref_id"])
    if task["thesis_id"]:
        pinned.setdefault("thesis", set()).add(task["thesis_id"])
    if task["answer_id"]:
        pinned.setdefault("answer", set()).add(task["answer_id"])
    skill = rsk.skill_version(conn, investor_id, task["skill_id"], task["skill_version"])
    return TaskScope(
        task_id=task["id"], isin=task["isin"], cutoff=task["context_cutoff"],
        pinned={k: frozenset(v) for k, v in pinned.items()}, thesis_id=task["thesis_id"],
        position_query=(task["context_manifest"] or {}).get("position_query"),
        permitted_tools=frozenset(skill["permitted_tools"]),
        calculation_specs=tuple((task["context_manifest"] or {}).get("calculation_specs", [])),
        created_by=str(task["created_by"]) if task.get("created_by") else None,
    )


# =========================================================================================
# BE 21 -- attempts, results, failures, cancellations, expiry (RUNNER capability)
# =========================================================================================


def _latest_attempt(conn: Any, investor_id: str, task_id: str) -> dict[str, Any] | None:
    return _one(conn, """
        SELECT a.id, a.task_id, a.attempt_no, a.mode, a.sample_ref, a.started_at,
               a.lease_until,
               EXISTS (SELECT 1 FROM task_result r WHERE r.investor_id = a.investor_id
                        AND r.attempt_id = a.id) AS has_result
          FROM task_attempt a WHERE a.investor_id = %s AND a.task_id = %s
         ORDER BY a.attempt_no DESC LIMIT 1
        """, (investor_id, task_id))


def expire_due(conn: Any, investor_id: str, task_id: str | None = None) -> int:
    """Append EXPIRED for every attempt whose server lease has passed with no terminal result.
    Runs in its own committed transaction before a start or submission, so an expiry is
    durable history even when the request that noticed it is then refused."""
    ws.lock_tenant(conn, investor_id)
    due = _rows(conn, """
        SELECT a.id, a.task_id FROM task_attempt a
         WHERE a.investor_id = %s AND (%s::uuid IS NULL OR a.task_id = %s::uuid)
           AND a.lease_until <= clock_timestamp()
           AND NOT EXISTS (SELECT 1 FROM task_result r WHERE r.investor_id = a.investor_id
                            AND r.attempt_id = a.id)
        """, (investor_id, task_id, task_id))
    for a in due:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_result (investor_id, task_id, attempt_id, outcome,
                                         guardrail_status, error_code, duration_ms, dedupe_key)
                VALUES (%s, %s, %s, 'EXPIRED', 'NOT_APPLICABLE', 'LEASE_EXPIRED', 0, %s)
                """,
                (investor_id, a["task_id"], a["id"], ws.dedupe_key(OP_EXPIRE, a["id"], "lease")),
            )
        ws.audit_write(conn, None, OP_EXPIRE, "task_attempt", a["id"])
    return len(due)


def _attempt_out(conn: Any, investor_id: str, attempt_id: str, digest: str, replayed: bool
                 ) -> dict[str, Any]:
    a = _one(conn, """
        SELECT a.id, a.task_id, a.attempt_no, a.mode, a.sample_ref, a.started_at, a.lease_until,
               EXISTS (SELECT 1 FROM task_result r WHERE r.investor_id = a.investor_id
                        AND r.attempt_id = a.id) AS has_result
          FROM task_attempt a WHERE a.investor_id = %s AND a.id = %s
        """, (investor_id, attempt_id))
    return {"attempt": _attempt_dto(a, a["has_result"], _now()), "context_digest": digest,
            "replayed": replayed}


def load_sample(sample_ref: str) -> dict[str, Any]:
    path = SAMPLES_DIR / f"{sample_ref}.json"
    if not re.fullmatch(r"[a-z0-9_]{3,64}", sample_ref) or not path.is_file():
        raise not_found("stored sample")
    return json.loads(path.read_text(encoding="utf-8"))


def start_attempt(
    conn: Any, fin_conn: Any, investor_id: str, requested_by: str, task_id: str,
    body: dict[str, Any], key: str, payload_digest: str, *, sample_allowed: bool,
    submit_sample: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], int]:
    """Recheck the digest, then append one attempt with the SERVER's ten-minute lease.

    One live attempt per task: a second start is 409 ATTEMPT_ACTIVE. A replay of the same
    start returns the same attempt with ``replayed: true`` -- never permission to send a
    second provider request. After a proposal or a review no attempt is allowed; a retry
    after FAILED/CANCELLED/EXPIRED is a new attempt only while the digest still matches.
    """
    task = task_row(conn, investor_id, task_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_ATTEMPT, task["id"], key, payload_digest)
    if receipt is not None:
        return _attempt_out(conn, investor_id, receipt.result["attempt_id"],
                            task["context_digest"], True), 200
    if _review(conn, investor_id, task["id"]) is not None or any(
            r["outcome"] == "PROPOSED" for r in _results(conn, investor_id, task["id"])):
        raise conflict(TASK_CLOSED, "this task already has a proposal; new evidence or a retry "
                       "is a new task linked by parent_task_id")
    latest = _latest_attempt(conn, investor_id, task["id"])
    if latest is not None and not latest["has_result"]:
        raise conflict(ATTEMPT_ACTIVE, "an attempt is already running for this task")
    pre = precondition(conn, investor_id, task)
    if pre["state"] != "READY":
        raise WorkspaceError(409, PRECONDITION_UNMET,
                             "this method's precondition is not met: "
                             + ", ".join(m["code"] for m in pre["missing"]),
                             {m["id"]: m["code"] for m in pre["missing"]})
    if not _is_butler(task):
        recheck(conn, fin_conn, investor_id, task)
    mode = body["mode"]
    if (mode == "STORED_SAMPLE") != (body.get("sample_ref") is not None):
        raise invalid("STORED_SAMPLE requires sample_ref, and only it may carry one",
                      {"sample_ref": "MODE"})
    sample = None
    if mode == "STORED_SAMPLE":
        if not sample_allowed:
            raise WorkspaceError(403, SAMPLE_NOT_PERMITTED,
                                 "stored samples are confined to isolated demo investors")
        sample = load_sample(body["sample_ref"])
    attempt_no = (latest["attempt_no"] if latest else 0) + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_attempt (investor_id, task_id, attempt_no, mode, sample_ref,
                                      lease_until, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + interval '10 minutes', %s)
            RETURNING id
            """,
            (investor_id, task["id"], attempt_no, mode, body.get("sample_ref"),
             ws.dedupe_key(OP_ATTEMPT, task["id"], key)),
        )
        attempt_id = str(cur.fetchone()[0])
    ws.audit_write(conn, requested_by, OP_ATTEMPT, "task_attempt", attempt_id,
                   task=task["id"], mode=mode)
    ws.record_receipt(conn, investor_id, OP_ATTEMPT, task["id"], key, payload_digest,
                      {"attempt_id": attempt_id}, requested_by)
    if sample is not None and submit_sample is not None:
        submit_sample(task["id"], attempt_id, _bind_sample(conn, investor_id, task["id"], sample))
    return _attempt_out(conn, investor_id, attempt_id, task["context_digest"], False), 201


def _bind_sample(conn: Any, investor_id: str, task_id: str, sample: dict[str, Any]
                 ) -> dict[str, Any]:
    """Replace ``@N`` ordinal placeholders in a stored sample with this task's context ids."""
    ids = [r["id"] for r in context_rows(conn, investor_id, task_id)]
    text = json.dumps(sample["proposal"])

    def sub(match: re.Match[str]) -> str:
        i = int(match.group(1))
        return json.dumps(ids[i]) if i < len(ids) else json.dumps("missing-context")

    return json.loads(re.sub(r'"@(\d{1,3})"', sub, text))


def _terminal_guard(conn: Any, investor_id: str, task: dict[str, Any], attempt_id: str
                    ) -> dict[str, Any]:
    attempt_id = _uuid(attempt_id, "attempt")
    a = _one(conn, """
        SELECT a.id, a.mode, a.lease_until,
               (SELECT r.id FROM task_result r WHERE r.investor_id = a.investor_id
                 AND r.attempt_id = a.id) AS result_id,
               (SELECT r.outcome FROM task_result r WHERE r.investor_id = a.investor_id
                 AND r.attempt_id = a.id) AS outcome
          FROM task_attempt a WHERE a.investor_id = %s AND a.task_id = %s AND a.id = %s
        """, (investor_id, task["id"], attempt_id))
    if a is None:
        raise not_found("attempt")
    if a["result_id"] is not None:
        if a["outcome"] == "EXPIRED":
            raise conflict(LEASE_EXPIRED, "this attempt's lease expired; start a new attempt")
        raise WorkspaceError(409, RESULT_EXISTS, "this attempt already has a result",
                             {"result_id": a["result_id"]})
    return a


def _insert_result(conn: Any, investor_id: str, task_id: str, attempt_id: str, outcome: str,
                   *, output: str = "", structured: dict[str, Any] | None = None,
                   guardrail: str = "NOT_APPLICABLE", error_code: str | None = None,
                   dedupe: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_result (investor_id, task_id, attempt_id, outcome, output,
                                     structured_output, guardrail_status, error_code,
                                     duration_ms, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s) RETURNING id
            """,
            (investor_id, task_id, attempt_id, outcome, output,
             json.dumps(structured or {}), guardrail, error_code, dedupe),
        )
        return str(cur.fetchone()[0])


def _secret_free(value: Any, path: str = "proposal") -> None:
    """422 SECRET_LIKE_CONTENT anywhere in a submission; nothing is stored."""
    if isinstance(value, str):
        for pattern in tx._SECRET_SHAPES:
            if pattern.search(value):
                raise WorkspaceError(422, tx.SECRET_LIKE_CONTENT,
                                     "the submission contains a credential-shaped string",
                                     {path: tx.SECRET_LIKE_CONTENT})
    elif isinstance(value, dict):
        for k, v in value.items():
            _secret_free(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _secret_free(v, f"{path}[{i}]")


#: Validates a Butler selector submission: (task, proposal) -> (status, reasons, structured).
ButlerValidator = Callable[[dict[str, Any], dict[str, Any]], tuple[str, list[str], dict]]


def submit_result(
    conn: Any, fin_conn: Any, investor_id: str, requested_by: str | None, task_id: str,
    attempt_id: str, body: dict[str, Any], key: str, payload_digest: str, *,
    policy: SourcePolicy, load_text: cx.TextLoader | None,
    butler_validator: ButlerValidator | None = None,
) -> tuple[dict[str, Any], int]:
    """Validate an untrusted proposal against the pinned method and sealed context and append
    exactly one PROPOSED result: PASS, or BLOCKED with reasons (retained, rejectable only)."""
    task = task_row(conn, investor_id, task_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_RESULT, _uuid(attempt_id, "attempt"), key,
                              payload_digest)
    if receipt is not None:
        return task_by_id(conn, investor_id, task["id"]), 200
    _terminal_guard(conn, investor_id, task, attempt_id)
    if body["context_digest"] != task["context_digest"]:
        raise conflict(CONTEXT_DRIFT, "the submission names a different context digest")
    raw = canonical.canonical_json(body["proposal"])
    if len(raw.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise WorkspaceError(413, PROPOSAL_TOO_LARGE,
                             f"a proposal is limited to {MAX_PROPOSAL_BYTES} bytes")
    _secret_free(body["proposal"])

    if _is_butler(task):
        if butler_validator is None:
            raise WorkspaceError(503, "BUTLER_UNAVAILABLE", "Butler validation is unavailable")
        status, reasons, structured = butler_validator(task, body["proposal"])
        citations: list[tuple[str, str]] = []
    else:
        from app.schemas.tasks import ProposalIn

        try:
            proposal = ProposalIn.model_validate(body["proposal"]).model_dump(mode="json")
        except ValidationError as exc:
            raise invalid("the proposal does not match the closed Proposal schema",
                          {".".join(str(p) for p in e["loc"]) or "proposal": e["type"]
                           for e in exc.errors()}) from exc
        rows, refs, _ = recheck(conn, fin_conn, investor_id, task)
        released = cx.release(rows, refs, policy, load_text)
        entries = {row["id"]: gr.ContextEntry(row["id"], ref.kind, ref.content,
                                              released.quotable.get(row["id"], ref.texts))
                   for row, ref in zip(rows, refs, strict=True)}
        entry = sk.entry_for(task["skill_id"])
        contract = sk.compile_contract(entry.output_contract)
        manifest = task["context_manifest"] or {}
        outcome = gr.validate(
            proposal, purpose=task["purpose"], contract=contract, context=entries,
            calc_refs={c["calculation_ref"] for c in manifest.get("calculation_specs", [])},
            has_position=manifest.get("position_query") is not None)
        from app.research import method_checks as mc

        extra = mc.proposal_reasons(entry.key, proposal, entries)
        if extra:
            outcome.reasons = list(dict.fromkeys(outcome.reasons + extra))
            outcome.status = gr.BLOCKED
        status, reasons, citations = outcome.status, outcome.reasons, outcome.citations
        structured = {"proposal": proposal,
                      "guardrail": {"version": gr.GUARDRAIL_VERSION, "reasons": reasons}}

    result_id = _insert_result(
        conn, investor_id, task["id"], _uuid(attempt_id, "attempt"), "PROPOSED",
        output=raw, structured=structured, guardrail=status,
        dedupe=ws.dedupe_key(OP_RESULT, attempt_id, key))
    with conn.cursor() as cur:
        for context_id, claim in citations:
            cur.execute("INSERT INTO result_citation (investor_id, task_id, result_id, "
                        "context_id, claim_ref) VALUES (%s, %s, %s, %s, %s)",
                        (investor_id, task["id"], result_id, context_id, claim[:200]))
        if task["purpose"] == "CHAT":
            # The assistant turn stores NO body; it is read through the result (043).
            cur.execute("INSERT INTO chat_message (investor_id, thread_id, role, result_id, "
                        "dedupe_key) VALUES (%s, %s, 'ASSISTANT', %s, %s)",
                        (investor_id, task["chat_thread_id"], result_id,
                         ws.dedupe_key(OP_RESULT, attempt_id, key, "assistant")))
    ws.audit_write(conn, requested_by, OP_RESULT, "task_result", result_id, task=task["id"],
                   guardrail=status, reasons=len(reasons))
    ws.record_receipt(conn, investor_id, OP_RESULT, _uuid(attempt_id, "attempt"), key,
                      payload_digest, {"result_id": result_id}, requested_by)
    return task_by_id(conn, investor_id, task["id"]), 201


def submit_terminal(conn: Any, investor_id: str, requested_by: str | None, task_id: str,
                    attempt_id: str, outcome: str, code: str, key: str, payload_digest: str
                    ) -> tuple[dict[str, Any], int]:
    """FAILED with a fixed redacted code, or CANCELLED. No message, body, URL, header or trace
    is accepted or stored. A cancellation records the local abort only: it does not claim
    the provider stopped generating or billing."""
    task = task_row(conn, investor_id, task_id)
    op = OP_FAILURE if outcome == "FAILED" else OP_CANCEL
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, op, _uuid(attempt_id, "attempt"), key,
                              payload_digest)
    if receipt is not None:
        return task_by_id(conn, investor_id, task["id"]), 200
    _terminal_guard(conn, investor_id, task, attempt_id)
    result_id = _insert_result(conn, investor_id, task["id"], _uuid(attempt_id, "attempt"),
                               outcome, error_code=code,
                               dedupe=ws.dedupe_key(op, attempt_id, key))
    ws.audit_write(conn, requested_by, op, "task_result", result_id, task=task["id"], code=code)
    ws.record_receipt(conn, investor_id, op, _uuid(attempt_id, "attempt"), key, payload_digest,
                      {"result_id": result_id}, requested_by)
    return task_by_id(conn, investor_id, task["id"]), 201


# =========================================================================================
# BE 22 -- human review (HUMAN capability)
# =========================================================================================


def _reviewable(conn: Any, investor_id: str, task: dict[str, Any], result_id: str
                ) -> dict[str, Any]:
    result_id = _uuid(result_id, "result")
    if _review(conn, investor_id, task["id"]) is not None:
        raise conflict(REVIEW_EXISTS, "this task has already been reviewed")
    results = _results(conn, investor_id, task["id"])
    current = next((r for r in results if r["outcome"] == "PROPOSED"), None)
    match = next((r for r in results if r["id"] == result_id), None)
    if match is None:
        raise conflict(RESULT_STALE, "this result does not belong to this task")
    if match["outcome"] != "PROPOSED":
        raise conflict(NOT_REVIEWABLE, "only a proposal can be accepted or rejected")
    if current is None or current["id"] != result_id:
        raise conflict(RESULT_STALE, "this is not the task's current proposal; reload")
    return match


def _result_is_sample(conn: Any, investor_id: str, result: dict[str, Any]) -> bool:
    row = _one(conn, "SELECT mode FROM task_attempt WHERE investor_id = %s AND id = %s",
               (investor_id, result["attempt_id"]))
    return bool(row and row["mode"] == "STORED_SAMPLE")


def accept(conn: Any, investor_id: str, reviewer_uid: str, task_id: str, body: dict[str, Any],
           key: str, payload_digest: str, *, sample_investor: bool
           ) -> tuple[dict[str, Any], int]:
    """Review + exactly one immutable note + its links + audit, one transaction.

    The note is the reviewer's own wording (they may edit the proposal), authorship
    ACCEPTED_AGENT so its machine origin is never erased; ``accepted_from`` then carries the
    task, result, agent version, method version and sample flag. Nothing else is created: no
    thesis, event, conviction, question closure, fact verification or transaction.
    """
    task = task_row(conn, investor_id, task_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_ACCEPT, task["id"], key, payload_digest)
    if receipt is not None:
        return _review_out(conn, investor_id, task["id"], receipt.result.get("note_id")), 200
    if _is_butler(task):
        raise conflict(NOT_REVIEWABLE, "a Butler selection is not a note proposal")
    result = _reviewable(conn, investor_id, task, body["result_id"])
    if result["guardrail_status"] != "PASS":
        raise conflict(BLOCKED_PROPOSAL, "a blocked proposal can only be rejected")
    if _result_is_sample(conn, investor_id, result) and not sample_investor:
        raise conflict(SAMPLE_NOT_PROMOTABLE, "a stored sample cannot become live research")
    tx.check_text(body["reason"], "reason", tx.REASON_MAX_BYTES)
    note = body["note"]
    tx.check_text(note["title"], "note.title", tx.TITLE_MAX_BYTES)
    tx.check_text(note["body"], "note.body", tx.NOTE_BODY_MAX_BYTES)
    if note["kind"] not in ("OBSERVATION", "INTERPRETATION"):
        raise invalid("an accepted proposal can only become an OBSERVATION or INTERPRETATION",
                      {"note.kind": "ENUM"})
    source_id = note.get("primary_source_id")
    if note["kind"] == "OBSERVATION":
        pinned = {r["ref_id"] for r in context_rows(conn, investor_id, task["id"])
                  if r["kind"] == "source"}
        if source_id is None or _uuid(source_id, "source") not in pinned:
            raise invalid("an OBSERVATION needs a real source pinned in this task; use "
                          "INTERPRETATION or reject", {"note.primary_source_id":
                                                       "SOURCE_REQUIRED"}, code="SOURCE_REQUIRED")
    if source_id is not None:
        ws._check_source(conn, investor_id, task["isin"], source_id)
        source_id = _uuid(source_id, "source")
    visible = ws.visible_isins(conn, investor_id)
    links = [ws._validate_link_target(conn, investor_id, None, link, visible)
             for link in note.get("links", [])]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO note (investor_id, isin, kind, title, body, written_on, actor_uid,
                              authorship, primary_source_id, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACCEPTED_AGENT', %s, %s) RETURNING id
            """,
            (investor_id, task["isin"], note["kind"], note["title"], note["body"],
             note["written_on"], reviewer_uid, source_id,
             ws.dedupe_key(OP_ACCEPT, task["id"], key, "note")),
        )
        note_id = str(cur.fetchone()[0])
    for i, link in enumerate(links):
        ws._insert_link(conn, investor_id, reviewer_uid, note_id, link,
                        ws.dedupe_key(OP_ACCEPT, task["id"], key, f"l{i}"))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_review (investor_id, task_id, result_id, decision, reason,
                                     accepted_note_id, reviewer_uid, dedupe_key)
            VALUES (%s, %s, %s, 'ACCEPT', %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, task["id"], result["id"], body["reason"], note_id, reviewer_uid,
             ws.dedupe_key(OP_ACCEPT, task["id"], key)),
        )
        review_id = str(cur.fetchone()[0])
    ws.audit_write(conn, reviewer_uid, OP_ACCEPT, "task_review", review_id, task=task["id"],
                   note=note_id, links=len(links))
    ws.record_receipt(conn, investor_id, OP_ACCEPT, task["id"], key, payload_digest,
                      {"review_id": review_id, "note_id": note_id}, reviewer_uid)
    return _review_out(conn, investor_id, task["id"], note_id), 201


def reject(conn: Any, investor_id: str, reviewer_uid: str, task_id: str, body: dict[str, Any],
           key: str, payload_digest: str) -> tuple[dict[str, Any], int]:
    """REJECT with a required reason. The output, citations and attribution are retained."""
    task = task_row(conn, investor_id, task_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_REJECT, task["id"], key, payload_digest)
    if receipt is not None:
        return _review_out(conn, investor_id, task["id"], None), 200
    result = _reviewable(conn, investor_id, task, body["result_id"])
    tx.check_text(body["reason"], "reason", tx.REASON_MAX_BYTES)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_review (investor_id, task_id, result_id, decision, reason,
                                     reviewer_uid, dedupe_key)
            VALUES (%s, %s, %s, 'REJECT', %s, %s, %s) RETURNING id
            """,
            (investor_id, task["id"], result["id"], body["reason"], reviewer_uid,
             ws.dedupe_key(OP_REJECT, task["id"], key)),
        )
        review_id = str(cur.fetchone()[0])
    ws.audit_write(conn, reviewer_uid, OP_REJECT, "task_review", review_id, task=task["id"])
    ws.record_receipt(conn, investor_id, OP_REJECT, task["id"], key, payload_digest,
                      {"review_id": review_id}, reviewer_uid)
    return _review_out(conn, investor_id, task["id"], None), 201


def _review_out(conn: Any, investor_id: str, task_id: str, note_id: str | None
                ) -> dict[str, Any]:
    task = task_by_id(conn, investor_id, task_id)
    return {"review": task["review"], "task": task,
            "note": ws.note_by_id(conn, investor_id, note_id) if note_id else None}


# =========================================================================================
# BE 24 -- grounded chat
# =========================================================================================

#: Conservative "is this turn trying to get the model to answer the person's own question?"
#: A WEAK SIGNAL, and documented as one: it declines early, it never permits anything. The
#: enforceable own-answer boundary is the typed answer route plus ANSWER_REVIEW's requirement
#: of an existing own answer. A turn that slips past this heuristic still cannot write an
#: answer -- no tool or route lets a model record one.
_ANSWER_INTENT = (
    re.compile(r"\b(answer|respond\s+to)\s+(my|this|the|that|these|those)\s+(own\s+)?"
               r"questions?\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(should|would|do)\s+(i|we)\s+(answer|say|write)\b", re.IGNORECASE),
    re.compile(r"\b(write|draft|fill\s+in|give\s+me)\s+(my|an|the)\s+answer\b", re.IGNORECASE),
    re.compile(r"\banswer\s+(it|them)\s+for\s+me\b", re.IGNORECASE),
)


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def answer_intent(text: str, open_questions: list[dict[str, Any]]) -> bool:
    if any(p.search(text) for p in _ANSWER_INTENT):
        return True
    body = _norm(text)
    return any(len(_norm(q["text"])) >= 12 and _norm(q["text"]) in body for q in open_questions)


def _thread_row(conn: Any, investor_id: str, thread_id: str) -> dict[str, Any]:
    thread_id = _uuid(thread_id, "thread")
    row = _one(conn, "SELECT id, isin, title, actor_uid, created_at FROM chat_thread "
                     "WHERE investor_id = %s AND id = %s", (investor_id, thread_id))
    if row is None:
        raise not_found("thread")
    if row["isin"] not in ws.visible_isins(conn, investor_id):
        raise ritual_required()
    return row


def _thread_dto(conn: Any, investor_id: str, row: dict[str, Any]) -> dict[str, Any]:
    count = _one(conn, "SELECT count(*) AS n FROM chat_message WHERE investor_id = %s "
                       "AND thread_id = %s", (investor_id, row["id"]))
    return {**row, "messages": int(count["n"])}


def list_threads(conn: Any, investor_id: str, isin: str | None, cursor: str | None,
                 limit: int | None) -> dict[str, Any]:
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "chats", {"isin": isin})
    after = canonical.decode_cursor(cursor, scope)
    visible = ws.visible_isins(conn, investor_id)
    allowed = sorted(visible)
    if isin is not None:
        isin = ws.require_unlocked(conn, investor_id, isin)
        allowed = [i for i in ws.isin_family(conn, isin) if i in visible]
    rows = _rows(conn, """
        SELECT id, isin, title, actor_uid, created_at FROM chat_thread
         WHERE investor_id = %(inv)s AND isin = ANY(%(allowed)s::text[])
           AND (%(k0)s::timestamptz IS NULL
                OR (created_at, id) < (%(k0)s::timestamptz, %(k1)s::uuid))
         ORDER BY created_at DESC, id DESC LIMIT %(lim)s
        """, {"inv": investor_id, "allowed": allowed, "k0": after[0] if after else None,
              "k1": after[1] if after else None, "lim": size + 1})
    return ws._page(conn, rows, size, scope,
                    lambda page: [_thread_dto(conn, investor_id, r) for r in page],
                    key=lambda r: [r["created_at"].isoformat(), r["id"]])


def create_thread(conn: Any, investor_id: str, actor_uid: str, isin: str, title: str, key: str,
                  payload_digest: str) -> tuple[dict[str, Any], int]:
    isin = ws.require_unlocked(conn, investor_id, isin)
    tx.check_text(title, "title", tx.TITLE_MAX_BYTES)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_THREAD, isin, key, payload_digest)
    if receipt is not None:
        return _thread_dto(conn, investor_id,
                           _thread_row(conn, investor_id, receipt.result["thread_id"])), 200
    with conn.cursor() as cur:
        cur.execute("INSERT INTO chat_thread (investor_id, isin, title, actor_uid, dedupe_key) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (investor_id, isin, title, actor_uid, ws.dedupe_key(OP_THREAD, isin, key)))
        thread_id = str(cur.fetchone()[0])
    ws.audit_write(conn, actor_uid, OP_THREAD, "chat_thread", thread_id)
    ws.record_receipt(conn, investor_id, OP_THREAD, isin, key, payload_digest,
                      {"thread_id": thread_id}, actor_uid)
    return _thread_dto(conn, investor_id, _thread_row(conn, investor_id, thread_id)), 201


def _message_dto(conn: Any, investor_id: str, m: dict[str, Any]) -> dict[str, Any]:
    rendered = guardrail = decision = task_id = None
    sample = False
    if m["role"] == "ASSISTANT" and m["result_id"]:
        res = _one(conn, """
            SELECT r.task_id, r.structured_output, r.guardrail_status, a.mode, rv.decision
              FROM task_result r
              JOIN task_attempt a ON a.investor_id = r.investor_id AND a.id = r.attempt_id
              LEFT JOIN task_review rv ON rv.investor_id = r.investor_id AND rv.result_id = r.id
             WHERE r.investor_id = %s AND r.id = %s
            """, (investor_id, m["result_id"]))
        if res:
            task_id, guardrail, decision = res["task_id"], res["guardrail_status"], res["decision"]
            sample = res["mode"] == "STORED_SAMPLE"
            task = _one(conn, "SELECT id, skill_id FROM task WHERE investor_id = %s AND id = %s",
                        (investor_id, res["task_id"]))
            entry = sk.entry_for(task["skill_id"]) if task else None
            proposal = (res["structured_output"] or {}).get("proposal")
            if proposal and entry is not None and entry.task_route == "TASKS":
                rendered = gr.render(proposal, entries_for(conn, investor_id, task["id"]),
                                     sk.compile_contract(entry.output_contract))
    elif m["role"] == "HUMAN":
        t = _one(conn, "SELECT id FROM task WHERE investor_id = %s AND purpose = 'CHAT' "
                       "AND context_manifest->'prior_chat_message_ids'->>-1 = %s",
                 (investor_id, m["id"]))
        task_id = t["id"] if t else None
    return {"id": m["id"], "thread_id": m["thread_id"], "role": m["role"], "body": m["body"],
            "actor_uid": m.get("actor_uid"), "result_id": m["result_id"], "task_id": task_id,
            "recorded_at": m["recorded_at"], "rendered": rendered,
            "guardrail_status": guardrail, "review_decision": decision, "sample": sample}


def list_messages(conn: Any, investor_id: str, thread_id: str, cursor: str | None,
                  limit: int | None) -> dict[str, Any]:
    thread = _thread_row(conn, investor_id, thread_id)
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "chat-messages", {"thread": thread["id"]})
    after = canonical.decode_cursor(cursor, scope)
    rows = _rows(conn, """
        SELECT id, thread_id, role, body, actor_uid, result_id, recorded_at, seq
          FROM chat_message WHERE investor_id = %s AND thread_id = %s
           AND (%s::bigint IS NULL OR seq > %s::bigint)
         ORDER BY seq LIMIT %s
        """, (investor_id, thread["id"], after[0] if after else None,
              after[0] if after else None, size + 1))
    return ws._page(conn, rows, size, scope,
                    lambda page: [_message_dto(conn, investor_id, r) for r in page],
                    key=lambda r: [r["seq"]])


def _insert_message(conn: Any, investor_id: str, thread_id: str, role: str, body: str,
                    actor_uid: str, dkey: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO chat_message (investor_id, thread_id, role, body, actor_uid, "
                    "dedupe_key) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (investor_id, thread_id, role, body, actor_uid, dkey))
        message_id = str(cur.fetchone()[0])
    return _one(conn, "SELECT id, thread_id, role, body, actor_uid, result_id, recorded_at, seq "
                      "FROM chat_message WHERE investor_id = %s AND id = %s",
                (investor_id, message_id))


REFUSAL_OWN_ANSWER = (
    "This looks like a request to answer one of your own open questions. Write your own "
    "answer first; you can then ask for a review of that exact answer."
)


def create_turn(conn: Any, fin_conn: Any, investor_id: str, actor_uid: str, thread_id: str,
                body: dict[str, Any], key: str, payload_digest: str
                ) -> tuple[dict[str, Any], int]:
    """A HUMAN turn plus its CHAT task in one transaction -- or the HUMAN turn plus a
    recorded REFUSAL and no task. There is no third outcome; any failure rolls back both.
    CHAT tasks are created nowhere else (POST /tasks refuses the purpose)."""
    thread = _thread_row(conn, investor_id, thread_id)
    tx.check_text(body["text"], "text", tx.NOTE_BODY_MAX_BYTES)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_TURN, thread["id"], key, payload_digest)
    if receipt is not None:
        return _turn_out(conn, investor_id, receipt.result), 200
    family = ws.isin_family(conn, thread["isin"])
    open_qs = ws.open_questions_for(conn, investor_id, family)
    human = _insert_message(conn, investor_id, thread["id"], "HUMAN", body["text"], actor_uid,
                            ws.dedupe_key(OP_TURN, thread["id"], key, "human"))
    if answer_intent(body["text"], open_qs):
        refusal = _insert_message(conn, investor_id, thread["id"], "REFUSAL",
                                  REFUSAL_OWN_ANSWER, actor_uid,
                                  ws.dedupe_key(OP_TURN, thread["id"], key, "refusal"))
        ws.audit_write(conn, actor_uid, OP_TURN, "chat_message", human["id"], refused=True)
        result = {"message_id": human["id"], "refusal_id": refusal["id"]}
        ws.record_receipt(conn, investor_id, OP_TURN, thread["id"], key, payload_digest, result,
                          actor_uid)
        return _turn_out(conn, investor_id, result), 201
    agent, skill, _ = _pinned_method(conn, investor_id, body["agent_id"], body["agent_version"],
                                     body["skill_id"], body["skill_version"], "CHAT", "TASKS")
    prior_ids = [r["id"] for r in _rows(conn, """
        SELECT id FROM chat_message WHERE investor_id = %s AND thread_id = %s
         ORDER BY seq""", (investor_id, thread["id"]))]
    title = body["text"].strip().splitlines()[0][:120] or thread["title"]
    task_id = _seal(
        conn, fin_conn, investor_id, actor_uid, isin=thread["isin"], purpose="CHAT",
        title=title, brief=body["text"], agent=agent, skill=skill, thesis_id=None,
        answer_id=None, parent_task_id=None, chat_thread_id=thread["id"],
        selection=_selection(body), prior_turn_ids=prior_ids,
        dedupe=ws.dedupe_key(OP_TURN, thread["id"], key, "task"),
    )
    ws.audit_write(conn, actor_uid, OP_TURN, "chat_message", human["id"], task=task_id)
    result = {"message_id": human["id"], "task_id": task_id}
    ws.record_receipt(conn, investor_id, OP_TURN, thread["id"], key, payload_digest, result,
                      actor_uid)
    return _turn_out(conn, investor_id, result), 201


def _turn_out(conn: Any, investor_id: str, result: dict[str, Any]) -> dict[str, Any]:
    m = _one(conn, "SELECT id, thread_id, role, body, actor_uid, result_id, recorded_at, seq "
                   "FROM chat_message WHERE investor_id = %s AND id = %s",
             (investor_id, result["message_id"]))
    refusal = None
    if result.get("refusal_id"):
        refusal = {"code": OWN_ANSWER_FIRST, "message": REFUSAL_OWN_ANSWER}
    task = task_by_id(conn, investor_id, result["task_id"]) if result.get("task_id") else None
    return {"message": _message_dto(conn, investor_id, m), "task": task, "refusal": refusal}

