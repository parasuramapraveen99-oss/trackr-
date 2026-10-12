"""POST /ops/jobs/drain, GET /ops/cron/jobs-drain, GET /ops/jobs/pending.

A THIN WRAPPER, AND DELIBERATELY NOTHING MORE. The worker is
``app/jobs/drain.py::drain_once``, which runs identically under ``python -m app.jobs.drain``
with no HTTP involved. That inversion -- module first, route second -- is the single
decision that keeps where the worker runs a deployment choice:

  * today, with no users, the drain runs on the developer's own machine, where CPU is
    unmetered and no production secret is shared with any third party;
  * on Vercel's Hobby plan, parsing consumes Active CPU against a 4 CPU-hour monthly
    allowance that *pauses the feature for thirty days* when exhausted rather than billing
    for the overage -- so being able to move the same work elsewhere is a reliability
    property, not a cost optimisation;
  * a cron tick is the FLOOR that guarantees nothing is ever stranded, not the mechanism.

Gated by the same shared secrets as every other ``/ops/*`` route (never a user login):
``require_ops_key`` for a human curling the box, ``require_cron_secret`` for the scheduled
tick, matching ``app.routers.analytical``'s prices/snapshots pair. These drain work for
EVERY investor at once, not just a caller's own, which is exactly why a session login was
never the intended caller.

See docs/BACKFILL_PIPELINE.md section 4.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.jobs import drain, queue
from app.storage.factory import get_store

logger = logging.getLogger(__name__)

ops_router = APIRouter(prefix="/ops", tags=["ops"])

#: Wall-clock budget for one HTTP-triggered drain, in seconds.
#:
#: Vercel's maximum function duration is 300 s on Hobby and Pro alike (Pro can be raised to
#: 800 s). 240 leaves room for request overhead plus ``drain_once``'s own headroom, so the
#: last job claimed finishes inside the platform limit instead of being cut off mid-parse --
#: which would strand it in RUNNING until its lease expired.
DEFAULT_HTTP_BUDGET_SECONDS = 240.0

#: Cap on documents per HTTP drain. A bound, not a target: the cron tick is a floor and is
#: allowed to take several passes. Draining a whole backfill in one request is what
#: ``python -m app.jobs.drain`` is for.
DEFAULT_HTTP_LIMIT = 400


def _drain(
    conn: Any,
    settings: Settings,
    *,
    budget_seconds: float,
    limit: int,
    investor_id: str | None = None,
) -> dict[str, Any]:
    """The one implementation both routes call.

    ``get_store`` is allowed to raise: with ``STORAGE_BACKEND`` unset or misconfigured
    there is no archive to read the bytes back from, and answering 200 having drained
    nothing would be a lie that a cron would then repeat silently every day.
    """
    store = get_store(settings)
    summary = drain.drain_once(
        conn,
        store,
        limit=limit,
        budget_seconds=budget_seconds,
        investor_id=investor_id,
    )
    logger.info("ops drain: %s", summary)
    return summary


@ops_router.post(
    "/jobs/drain",
    summary="Work the job queue now (ops key)",
    dependencies=[Depends(require_ops_key)],
)
def drain_jobs(
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    budget_seconds: float = Query(DEFAULT_HTTP_BUDGET_SECONDS, gt=0, le=290),
    limit: int = Query(DEFAULT_HTTP_LIMIT, gt=0, le=5000),
    investor_id: str | None = Query(None, description="drain one investor's work only"),
) -> dict[str, Any]:
    """Claim and process queued documents until the queue empties or the budget is spent.

    Safe to call concurrently with itself and with the cron tick: ``queue.claim`` uses
    ``FOR UPDATE SKIP LOCKED``, so two drains take disjoint work rather than colliding or
    double-importing. That is also why a duplicated invocation needs no guard.
    """
    return _drain(
        conn, settings, budget_seconds=budget_seconds, limit=limit, investor_id=investor_id
    )


@ops_router.get(
    "/cron/jobs-drain",
    summary="Scheduled drain (CRON_SECRET)",
    dependencies=[Depends(require_cron_secret)],
)
def drain_jobs_cron(
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The safety net. GET, and gated by ``CRON_SECRET``, per Vercel Cron's own convention
    -- the same shape ``/ops/cron/prices-refresh`` uses.

    Vercel documents cron invocation as best effort and possibly duplicated; both are
    already harmless here because claiming is ``SKIP LOCKED`` and every write downstream is
    idempotent on a content hash. On the Hobby plan this fires once per day, which is
    sufficient for a backfill nobody is waiting on -- responsiveness, when it is wanted, is
    a free external pinger against ``POST /ops/jobs/drain``, not a paid plan.
    """
    return _drain(
        conn,
        settings,
        budget_seconds=DEFAULT_HTTP_BUDGET_SECONDS,
        limit=DEFAULT_HTTP_LIMIT,
    )


@ops_router.get(
    "/jobs/pending",
    summary="How much work is outstanding (ops key)",
    dependencies=[Depends(require_ops_key)],
)
def pending_jobs(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """Queue depth and the most recent failures.

    Exists because Vercel's Hobby plan retains one hour of runtime logs: a drain that died
    overnight leaves no trace in the platform by morning. The ``job`` table is where
    retention is ours, so "is anything stuck, and why" is answered from rows rather than
    from logs that have already aged out.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, count(*) FROM job
             WHERE status <> 'DONE'
             GROUP BY status
            """
        )
        by_status = {row[0]: int(row[1]) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT id, kind, attempts, max_attempts, left(last_error, 300), finished_at
              FROM job
             WHERE status = 'FAILED'
             ORDER BY finished_at DESC NULLS LAST
             LIMIT 20
            """
        )
        failures = [
            {
                "job_id": str(r[0]),
                "kind": r[1],
                "attempts": r[2],
                "max_attempts": r[3],
                "last_error": r[4],
                "finished_at": r[5].isoformat() if r[5] else None,
            }
            for r in cur.fetchall()
        ]
    return {
        "claimable": queue.pending_count(conn),
        "by_status": by_status,
        "recent_failures": failures,
    }
