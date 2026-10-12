"""The job queue: enqueue, claim, complete, fail.

Raw SQL over the ``job`` table (migration 038). Four functions, on purpose -- see
``app/jobs/__init__.py``.

THE ONE RULE ABOUT PAYLOADS. A payload carries ids and nothing else. Never document
bytes, never a PAN, never a password. This table is queried by hand, included in
backups, and printed into error logs; the worker re-reads bytes from object storage and
re-derives the PAN from ``investor_secret`` when it actually parses. ``enqueue`` enforces
this rather than trusting callers, because the convenient implementation is the one that
stuffs the bytes in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: The one job kind so far: open a stored document and stage its rows. Lives here rather
#: than in drain.py so an enqueuing caller never has to import the worker.
KIND_PARSE_DOCUMENT = "parse_document"

#: How long a claimed job may stay RUNNING before another worker treats it as abandoned.
#: Must exceed the slowest single document a worker can legitimately take -- a 40-page CAS
#: is seconds, so minutes is generous -- and must be short enough that a killed worker's
#: row is recovered within one drain cycle rather than at the next deploy.
DEFAULT_LEASE_SECONDS = 900

#: Payload keys that must never appear. Checked by name because the mistake is always a
#: well-meaning one ("just pass the bytes, it is right there") and it is cheap to refuse.
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "content",
        "content_b64",
        "bytes",
        "body",
        "pan",
        "password",
        "pdf_password",
        "secret",
        "token",
    }
)


class PayloadNotAllowedError(ValueError):
    """A payload carried bytes or a credential. See this module's docstring."""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    investor_id: str | None
    attempts: int
    max_attempts: int


def _check_payload(payload: dict[str, Any]) -> None:
    offending = sorted(k for k in payload if k.lower() in _FORBIDDEN_PAYLOAD_KEYS)
    if offending:
        raise PayloadNotAllowedError(
            f"job payloads carry ids only, never bytes or credentials; "
            f"refused key(s): {', '.join(offending)}"
        )


def enqueue(
    conn: Any,
    kind: str,
    payload: dict[str, Any],
    *,
    investor_id: str | None = None,
    max_attempts: int = 3,
) -> str:
    """Record one unit of deferred work. Returns the job id.

    Does NOT commit: the caller's transaction owns it. That is the whole point -- the job
    row and whatever else the caller writes (an ``import_batch``, a ``document``) either
    both land or neither does.

    Raises:
        PayloadNotAllowedError: the payload carried bytes or a credential.
    """
    _check_payload(payload)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job (kind, payload, investor_id, max_attempts)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (kind, json.dumps(payload), investor_id, max_attempts),
        )
        return str(cur.fetchone()[0])


def claim(
    conn: Any,
    limit: int = 1,
    *,
    kind: str | None = None,
    investor_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[Job]:
    """Take up to ``limit`` claimable jobs and mark them RUNNING.

    ``FOR UPDATE SKIP LOCKED`` is what makes concurrent drains safe without a broker: two
    workers running the same query take disjoint sets rather than fighting or double-doing.
    It is also why a duplicated cron invocation is a no-op rather than a problem.

    Claimable means PENDING, **or** RUNNING with an expired lease -- the second half is
    what recovers a worker that died mid-document. Either way ``attempts`` is incremented
    here, at claim time, not at failure: a worker killed without ever reaching ``fail``
    still burns an attempt, so a document that reliably crashes the process cannot be
    retried forever.

    Does not commit. The caller commits after doing (or failing) the work, so a crash
    between claim and commit releases the row by rolling back rather than stranding it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH claimable AS (
                SELECT id FROM job
                 WHERE (status = 'PENDING'
                        OR (status = 'RUNNING'
                            AND claimed_at < now() - make_interval(secs => %(lease)s)))
                   AND attempts < max_attempts
                   AND (%(kind)s::text IS NULL OR kind = %(kind)s)
                   AND (%(investor)s::uuid IS NULL OR investor_id = %(investor)s)
                 ORDER BY created_at
                 LIMIT %(limit)s
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE job j
               SET status = 'RUNNING',
                   claimed_at = now(),
                   attempts = j.attempts + 1
              FROM claimable c
             WHERE j.id = c.id
            RETURNING j.id, j.kind, j.payload, j.investor_id, j.attempts, j.max_attempts
            """,
            {
                "lease": lease_seconds,
                "kind": kind,
                "investor": investor_id,
                "limit": limit,
            },
        )
        rows = cur.fetchall()
    return [
        Job(
            id=str(r[0]),
            kind=r[1],
            # psycopg returns jsonb already decoded; tolerate a str for a raw-SQL caller.
            payload=r[2] if isinstance(r[2], dict) else json.loads(r[2]),
            investor_id=str(r[3]) if r[3] else None,
            attempts=r[4],
            max_attempts=r[5],
        )
        for r in rows
    ]


def complete(conn: Any, job_id: str) -> None:
    """Mark a job done. Clears the lease so nothing can re-claim it."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job
               SET status = 'DONE', finished_at = now(), claimed_at = NULL, last_error = NULL
             WHERE id = %s
            """,
            (job_id,),
        )


def fail(conn: Any, job_id: str, error: str, *, permanent: bool = False) -> None:
    """Record a failure, and decide whether it may be retried.

    ``permanent=True`` means no retry can ever succeed -- an unparseable file, a document
    belonging to another investor, an adapter that does not exist. Retrying those wastes
    the queue and, worse, hides the real reason behind an attempts counter.

    A transient failure returns to PENDING and is retried until ``max_attempts``, after
    which it becomes FAILED with the error text kept. It is never silently dropped: a
    FAILED row with its reason is the only thing that makes "why is this document not in
    my ledger" answerable months later. ``error`` is truncated because a driver traceback
    can be enormous and this column is read by humans.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job
               SET status = CASE
                       WHEN %(permanent)s THEN 'FAILED'
                       WHEN attempts >= max_attempts THEN 'FAILED'
                       ELSE 'PENDING'
                   END,
                   last_error = %(error)s,
                   claimed_at = NULL,
                   finished_at = CASE
                       WHEN %(permanent)s OR attempts >= max_attempts THEN now()
                       ELSE NULL
                   END
             WHERE id = %(id)s
            """,
            {"id": job_id, "error": error[:2000], "permanent": permanent},
        )


def pending_count(conn: Any, *, kind: str | None = None) -> int:
    """How much work is outstanding. Used by the drain to decide whether to keep going
    and by ops to answer "is anything stuck" without reading platform logs -- which on
    Vercel's Hobby plan are retained for one hour."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM job
             WHERE status IN ('PENDING', 'RUNNING')
               AND attempts < max_attempts
               AND (%(kind)s::text IS NULL OR kind = %(kind)s)
            """,
            {"kind": kind},
        )
        return int(cur.fetchone()[0])
