"""Reference status is investor-scoped; worker execution and errors are ops-only."""

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field

from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.db.reference_notice_outcome import record_outcome
from app.db.reference_sync import status
from app.reference_worker import run_once

router = APIRouter(tags=["reference-sync"])


@router.get(
    "/investors/{investor_id}/reference-status", dependencies=[Depends(require_owned_investor)]
)
def reference_status(
    investor_id: str, conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict:
    return {**status(conn, investor_id), "enabled": settings.reference_sync_enabled}


class NoticeOutcomeIn(BaseModel):
    """The investor's answer to one notice, identified as the reference-status list names
    it. ``note`` is free text for the investor's own reference; nothing reads it."""

    isin: str = Field(min_length=12, max_length=12)
    ex_date: str = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=500)
    outcome: Literal["NOT_PARTICIPATED", "PARTICIPATED", "UNSURE"]
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/investors/{investor_id}/reference-notices/outcome",
    dependencies=[Depends(require_owned_investor)],
)
def record_notice_outcome(
    investor_id: str,
    body: NoticeOutcomeIn,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Record what the investor did about a notice, then return the refreshed status.

    Writes no transaction. PARTICIPATED means the investor will record the trade through
    the normal entry path (the notice carries no price), so the notice leaves the list
    without a ledger row being invented for it."""
    record_outcome(
        conn,
        investor_id,
        isin=body.isin,
        ex_date=body.ex_date,
        subject=body.subject,
        outcome=body.outcome,
        note=body.note,
    )
    conn.commit()
    return {**status(conn, investor_id), "enabled": settings.reference_sync_enabled}


@router.post("/investors/{investor_id}/sync", dependencies=[Depends(require_owned_investor)])
def sync(
    investor_id: str, conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict:
    """One bounded catch-up cycle (prices, then corporate actions / ISIN links / replay)
    for the import screen to repeat while ``state`` is ``processing`` -- so a fresh
    import's analytics are complete in minutes rather than after days of daily crons
    (owner decision 2026-10-08; see app.post_import). Work is shared public data, not
    per-investor, and the worker's advisory lock turns a concurrent call into ``busy``.
    Returns this investor's status after the cycle, which is what the screen polls on."""
    from app.continuation import continue_if_needed
    from app.post_import import run_cycle

    cycle = run_cycle(conn, settings)
    if not cycle.get("busy"):
        continue_if_needed(
            settings, more_work=cycle["more_work"], progress=cycle["progress"], hop=0
        )
    return {"cycle": cycle, **status(conn, investor_id), "enabled": settings.reference_sync_enabled}


@router.post("/ops/reference-sync/run", dependencies=[Depends(require_ops_key)])
def run(conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)) -> dict:
    return run_once(conn, settings, limit=1)


@router.get("/ops/reference-sync/run-cron", dependencies=[Depends(require_cron_secret)])
def cron(conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)) -> dict:
    """Scheduled in vercel.json: the daily backstop for the whole catch-up pipeline.

    One bounded ``app.post_import`` cycle -- prices, F&O contract days, this worker's
    queue, snapshots -- and, when work is left, the self-continuing chain
    (``app.continuation``). Running only this worker here, as it once did, left a stalled
    snapshot or price backlog waiting for a page that might never be opened."""
    from app.continuation import continue_if_needed
    from app.post_import import run_cycle

    cycle = run_cycle(conn, settings)
    continued = not cycle.get("busy") and continue_if_needed(
        settings, more_work=cycle["more_work"], progress=cycle["progress"], hop=0
    )
    return {"cycle": cycle, "continued": continued}


@router.get("/ops/cycle/continue", status_code=202, dependencies=[Depends(require_cron_secret)])
def continue_cycle(
    background_tasks: BackgroundTasks,
    # 0 is a promote handing over its first cycle (app.continuation.start_cycle_elsewhere).
    hop: int = Query(default=1, ge=0),
    settings: Settings = Depends(get_settings),
) -> dict:
    """The next link of a catch-up chain (``app.continuation``). Answers 202 at once and
    runs the cycle as a background task, so the caller never waits for it; the cycle then
    decides for itself whether to start link ``hop + 1``."""
    from app.post_import import run_cycle_in_own_connection

    background_tasks.add_task(run_cycle_in_own_connection, settings, hop)
    return {"accepted": True, "hop": hop}


@router.get("/ops/reference-sync/status", dependencies=[Depends(require_ops_key)])
def operations(conn: Any = Depends(get_conn)) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind,state,count(*),max(updated_at) FROM reference_sync_task "
            "GROUP BY kind,state ORDER BY kind,state"
        )
        counts = [
            {"source": r[0], "state": r[1], "count": r[2], "updated_at": r[3]}
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT kind,task_key,last_error,available_at FROM reference_sync_task "
            "WHERE state='retry' ORDER BY updated_at DESC LIMIT 30"
        )
        failures = [
            {"source": r[0], "key": r[1], "error": r[2], "retry_at": r[3]} for r in cur.fetchall()
        ]
    return {"tasks": counts, "failures": failures}
