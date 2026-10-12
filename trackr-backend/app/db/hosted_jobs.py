"""Scoped hosted jobs: admission, state events, leases, checkpoints and budget (RP 01, 054).

docs/BUTLER_REPORTS.md section 3. Two purposes only (COMPANY_REPORT, NOTEBOOK_REFLECTION).
The API ENQUEUES AND RETURNS: nothing here runs a stage. ``app/jobs/hosted.py`` is the worker.

EVERYTHING IS APPEND-ONLY AND RE-CHECKED BY 054. Python checks first so errors are sentences;
the triggers check again under the tenant lock so a race cannot slip through: one running job
per purpose, sequential state versions, terminal states final, only the latest unexpired lease
may land a checkpoint, nothing lands after CANCEL_REQUESTED or the deadline, reflection output
needs the sealed consent to still be current, and a reservation that would take worst-case
exposure over the sealed budget is refused.

THIS MODULE NEVER SEES A CREDENTIAL. Pricing/model identifiers are sealed; the key is read only
inside ``app/research/platform_inference.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.db import workspace as ws
from app.research import canonical
from app.research.errors import WorkspaceError, conflict, not_found

OP_JOB = "hosted.job_create"
OP_CANCEL = "hosted.job_cancel"
TERMINAL = frozenset({"READY_WITH_LIMITATIONS", "READY_CHECKED_SCOPE", "FAILED", "CANCELLED",
                      "EXPIRED"})
LEASE_SECONDS = 300

_rows = ws._rows
_one = ws._one


def _now() -> datetime:
    return datetime.now(UTC)


# =========================================================================================
# delegation -- short-lived, purpose-bound, signed
# =========================================================================================


@dataclass(frozen=True)
class Delegation:
    """What one worker lease is allowed to do: this job, this purpose, this stage, until then.
    Signed with the operator's delegation key (not a provider credential). Only its SHA-256 is
    stored (hosted_job_lease.delegation_digest); the token itself lives in worker memory."""

    investor_id: str
    job_id: str
    purpose: str
    stage: str
    attempt_no: int
    expires_at: datetime

    def payload(self) -> bytes:
        return json.dumps({"inv": self.investor_id, "job": self.job_id, "purpose": self.purpose,
                           "stage": self.stage, "attempt": self.attempt_no,
                           "exp": self.expires_at.isoformat()}, sort_keys=True).encode()


def sign(d: Delegation, signing_key: str) -> str:
    return hmac.new(signing_key.encode(), d.payload(), hashlib.sha256).hexdigest()


def verify(d: Delegation, signature: str, signing_key: str, now: datetime) -> bool:
    return (now < d.expires_at
            and hmac.compare_digest(sign(d, signing_key), signature))


def delegation_digest(signature: str) -> str:
    return hashlib.sha256(signature.encode()).hexdigest()


# =========================================================================================
# admission
# =========================================================================================


@dataclass(frozen=True)
class Sealed:
    purpose: str
    trigger_kind: str
    isin: str | None
    request: dict[str, Any]
    consent_version: int | None
    provider: str
    model: str
    price_version: str
    currency: str
    budget_limit: Decimal
    max_input_tokens: int
    max_output_tokens: int
    max_stage_attempts: int
    deadline: timedelta
    local_day: Any = None
    revision_of: str | None = None
    contract_version: str = "report-contract-1"


def _state_rows(conn: Any, investor_id: str, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not job_ids:
        return {}
    return {str(r["job_id"]): r for r in _rows(conn, """
        SELECT DISTINCT ON (job_id) job_id, version, state, stage, reason_code, recorded_at
          FROM hosted_job_event WHERE investor_id = %s AND job_id = ANY(%s::uuid[])
         ORDER BY job_id, version DESC
        """, (investor_id, job_ids))}


def running_jobs(conn: Any, investor_id: str, purpose: str) -> list[dict[str, Any]]:
    jobs = _rows(conn, "SELECT id FROM hosted_job WHERE investor_id = %s AND purpose = %s",
                 (investor_id, purpose))
    states = _state_rows(conn, investor_id, [str(j["id"]) for j in jobs])
    return [{"id": str(j["id"]), **states[str(j["id"])]} for j in jobs
            if str(j["id"]) in states and states[str(j["id"])]["state"] not in TERMINAL]


def admit(conn: Any, investor_id: str, actor_uid: str | None, sealed: Sealed, key: str,
          payload_digest: str, reason: str = "ADMITTED") -> tuple[str, int]:
    """Create the job and its QUEUED event in one transaction. Returns (job_id, status)."""
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_JOB, sealed.purpose.lower().replace(
        "_", "-"), key, payload_digest)
    if receipt is not None:
        return receipt.result["job_id"], 200
    if running_jobs(conn, investor_id, sealed.purpose):
        raise conflict("JOB_ALREADY_RUNNING",
                       "one job of this kind is already running for this investor")
    request_digest = canonical.digest(sealed.request)
    deadline_at = _now() + sealed.deadline
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO hosted_job (investor_id, purpose, trigger_kind, isin, request,
                request_digest, contract_version, revision_of, consent_version, provider, model,
                price_version, currency, budget_limit, max_input_tokens, max_output_tokens,
                max_stage_attempts, deadline_at, local_day, actor_uid, dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (investor_id, sealed.purpose, sealed.trigger_kind, sealed.isin,
             json.dumps(sealed.request), request_digest, sealed.contract_version,
             sealed.revision_of, sealed.consent_version, sealed.provider, sealed.model,
             sealed.price_version, sealed.currency, sealed.budget_limit,
             sealed.max_input_tokens, sealed.max_output_tokens, sealed.max_stage_attempts,
             deadline_at, sealed.local_day, actor_uid,
             ws.dedupe_key(OP_JOB, sealed.purpose.lower(), key)),
        )
        job_id = str(cur.fetchone()[0])
    append_event(conn, investor_id, job_id, "QUEUED", None, reason, {}, actor_uid)
    ws.audit_write(conn, actor_uid, OP_JOB, "hosted_job", job_id, purpose=sealed.purpose)
    ws.record_receipt(conn, investor_id, OP_JOB, sealed.purpose.lower().replace("_", "-"), key,
                      payload_digest, {"job_id": job_id}, actor_uid)
    return job_id, 201


def append_event(conn: Any, investor_id: str, job_id: str, state: str, stage: str | None,
                 reason: str, detail: dict[str, Any], actor_uid: str | None = None) -> int:
    row = _one(conn, "SELECT COALESCE(max(version), 0) AS v FROM hosted_job_event "
                     "WHERE investor_id = %s AND job_id = %s", (investor_id, job_id))
    version = int(row["v"]) + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO hosted_job_event (investor_id, job_id, version, state, stage,
                                          reason_code, detail, actor_uid)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, job_id, version, state, stage, reason, json.dumps(detail),
             actor_uid),
        )
    return version


def request_cancel_row(conn: Any, investor_id: str, job_id: str, actor_uid: str | None,
                       reason: str = "USER_CANCELLED") -> bool:
    """Append CANCEL_REQUESTED unless already terminal or requested. From this commit no
    lease, checkpoint or reservation can land (054)."""
    state = current_state(conn, investor_id, job_id)
    if state is None:
        raise not_found("job")
    if state["state"] in TERMINAL or state["state"] == "CANCEL_REQUESTED":
        return False
    append_event(conn, investor_id, job_id, "CANCEL_REQUESTED", state.get("stage"), reason, {},
                 actor_uid)
    return True


def cancel(conn: Any, investor_id: str, actor_uid: str, job_id: str, key: str,
           payload_digest: str) -> tuple[dict[str, Any], int]:
    job_id = ws._uuid(job_id, "job")
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_CANCEL, job_id, key, payload_digest)
    if receipt is not None:
        return job_view(conn, investor_id, job_id), 200
    request_cancel_row(conn, investor_id, job_id, actor_uid)
    ws.audit_write(conn, actor_uid, OP_CANCEL, "hosted_job", job_id)
    ws.record_receipt(conn, investor_id, OP_CANCEL, job_id, key, payload_digest,
                      {"job_id": job_id}, actor_uid)
    return job_view(conn, investor_id, job_id), 201


# =========================================================================================
# worker primitives
# =========================================================================================


def current_state(conn: Any, investor_id: str, job_id: str) -> dict[str, Any] | None:
    return _state_rows(conn, investor_id, [job_id]).get(job_id)


def job_row(conn: Any, investor_id: str, job_id: str) -> dict[str, Any]:
    row = _one(conn, "SELECT * FROM hosted_job WHERE investor_id = %s AND id = %s",
               (investor_id, job_id))
    if row is None:
        raise not_found("job")
    return row


def runnable_jobs(conn: Any, investor_id: str) -> list[str]:
    """Non-terminal jobs whose latest lease (if any) has expired -- work a worker may take."""
    jobs = _rows(conn, "SELECT id FROM hosted_job WHERE investor_id = %s ORDER BY seq",
                 (investor_id,))
    states = _state_rows(conn, investor_id, [str(j["id"]) for j in jobs])
    out = []
    for j in jobs:
        jid = str(j["id"])
        st = states.get(jid)
        if st is None or st["state"] in TERMINAL:
            continue
        lease = _one(conn, """
            SELECT l.lease_until,
                   EXISTS (SELECT 1 FROM hosted_job_checkpoint c WHERE c.investor_id = l.investor_id
                           AND c.lease_id = l.id) AS landed
              FROM hosted_job_lease l WHERE l.investor_id = %s AND l.job_id = %s
             ORDER BY l.attempt_no DESC LIMIT 1""", (investor_id, jid))
        # A cancellation is honoured at once, without waiting for an in-flight lease: its
        # output is already fenced by 054, and any open reservation is charged in full.
        if st["state"] == "CANCEL_REQUESTED" or lease is None or lease["landed"] \
                or lease["lease_until"] <= _now():
            out.append(jid)
    return out


def take_lease(conn: Any, investor_id: str, job_id: str, stage: str, signing_key: str,
               lease_seconds: int = LEASE_SECONDS) -> tuple[str, Delegation, str]:
    """Commit a lease row (caller commits) and return (lease_id, delegation, signature)."""
    last = _one(conn, "SELECT COALESCE(max(attempt_no), 0) AS n FROM hosted_job_lease "
                      "WHERE investor_id = %s AND job_id = %s", (investor_id, job_id))
    attempt = int(last["n"]) + 1
    d = Delegation(investor_id, job_id, job_row(conn, investor_id, job_id)["purpose"], stage,
                   attempt, _now() + timedelta(seconds=lease_seconds))
    signature = sign(d, signing_key)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO hosted_job_lease (investor_id, job_id, attempt_no, stage, lease_seconds,
                                          delegation_digest)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, job_id, attempt, stage, lease_seconds, delegation_digest(signature)),
        )
        lease_id = str(cur.fetchone()[0])
    return lease_id, d, signature


def checkpoints(conn: Any, investor_id: str, job_id: str) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT id, stage, stage_version, outcome, input_digest, output_digest, output,
               recorded_at
          FROM hosted_job_checkpoint WHERE investor_id = %s AND job_id = %s
         ORDER BY recorded_at
        """, (investor_id, job_id))


def write_checkpoint(conn: Any, investor_id: str, job_id: str, lease_id: str, stage: str,
                     outcome: str, input_digest: str, output: dict[str, Any],
                     stage_version: int = 1) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO hosted_job_checkpoint (investor_id, job_id, lease_id, stage,
                stage_version, outcome, input_digest, output_digest, output)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, job_id, lease_id, stage, stage_version, outcome, input_digest,
             canonical.digest(output), json.dumps(output)),
        )
        return str(cur.fetchone()[0])


def reserve(conn: Any, investor_id: str, job_id: str, lease_id: str, amount: Decimal) -> str:
    """Refused by 054 ('hosted job budget exhausted') when worst-case exposure would exceed
    the sealed limit. The caller commits BEFORE invoking the provider."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hosted_job_charge (investor_id, job_id, lease_id, kind, amount) "
            "VALUES (%s, %s, %s, 'RESERVE', %s) RETURNING id",
            (investor_id, job_id, lease_id, amount))
        return str(cur.fetchone()[0])


def settle(conn: Any, investor_id: str, job_id: str, lease_id: str, reserve_id: str,
           kind: str, amount: Decimal, input_tokens: int | None,
           output_tokens: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO hosted_job_charge (investor_id, job_id, lease_id, kind, reserve_id,
                                           amount, input_tokens, output_tokens)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, job_id, lease_id, kind, reserve_id, amount, input_tokens,
             output_tokens),
        )


def open_reservations(conn: Any, investor_id: str, job_id: str) -> list[dict[str, Any]]:
    """Reservations with no settlement -- left by a crash between reserve and settle. The
    worker settles them UNKNOWN (full amount) on recovery: it cannot know whether the call
    went out, so it never re-sends blindly and never under-counts."""
    return _rows(conn, """
        SELECT r.id, r.lease_id, r.amount FROM hosted_job_charge r
         WHERE r.investor_id = %s AND r.job_id = %s AND r.kind = 'RESERVE'
           AND NOT EXISTS (SELECT 1 FROM hosted_job_charge s WHERE s.investor_id = r.investor_id
                           AND s.reserve_id = r.id)
        """, (investor_id, job_id))


def exposure(conn: Any, investor_id: str, job_id: str) -> Decimal:
    row = _one(conn, "SELECT hosted_job_exposure(%s, %s) AS e", (investor_id, job_id))
    return Decimal(row["e"])


# =========================================================================================
# reads
# =========================================================================================


def job_view(conn: Any, investor_id: str, job_id: str) -> dict[str, Any]:
    job = job_row(conn, investor_id, job_id)
    state = current_state(conn, investor_id, job_id) or {}
    cps = checkpoints(conn, investor_id, job_id)
    coverage = next((c["output"].get("coverage") for c in reversed(cps)
                     if c["stage"] == "PLAN" and isinstance(c["output"], dict)), None)
    return {
        "id": str(job["id"]),
        "purpose": job["purpose"],
        "isin": job["isin"],
        "request": job["request"],
        "state": state.get("state", "QUEUED"),
        "state_reason": state.get("reason_code", ""),
        "stage": state.get("stage"),
        "revision_of": str(job["revision_of"]) if job["revision_of"] else None,
        "provider": job["provider"],
        "model": job["model"],
        "currency": job["currency"],
        "budget_limit": str(job["budget_limit"]),
        "spent_worst_case": str(exposure(conn, investor_id, str(job["id"]))),
        "deadline_at": job["deadline_at"],
        "recorded_at": job["recorded_at"],
        # Completed stages, never a percentage (section 4: no fictitious progress).
        "completed_stages": [c["stage"] for c in cps if c["outcome"] == "ACCEPTED"],
        "coverage": coverage,
    }


def list_jobs(conn: Any, investor_id: str, purpose: str, limit: int = 20,
              before_seq: int | None = None) -> list[dict[str, Any]]:
    rows = _rows(conn, """
        SELECT id, seq FROM hosted_job WHERE investor_id = %s AND purpose = %s
           AND (%s::bigint IS NULL OR seq < %s::bigint)
         ORDER BY seq DESC LIMIT %s
        """, (investor_id, purpose, before_seq, before_seq, min(max(limit, 1), 50)))
    return [job_view(conn, investor_id, str(r["id"])) for r in rows]


def events(conn: Any, investor_id: str, job_id: str, after_version: int) -> dict[str, Any]:
    """Progress events after a reconnect cursor. Codes and counts only -- no raw model output
    or private text is ever in an event's detail."""
    job_row(conn, investor_id, job_id)
    items = _rows(conn, """
        SELECT version, state, stage, reason_code, detail, recorded_at FROM hosted_job_event
         WHERE investor_id = %s AND job_id = %s AND version > %s ORDER BY version LIMIT 200
        """, (investor_id, job_id, after_version))
    return {"items": items,
            "next_cursor": items[-1]["version"] if items else after_version}


def not_configured(reason: str) -> WorkspaceError:
    return WorkspaceError(503, "PLATFORM_NOT_CONFIGURED",
                          f"hosted jobs are not available on this server ({reason})")
