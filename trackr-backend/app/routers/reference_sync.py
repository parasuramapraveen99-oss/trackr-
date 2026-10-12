"""Reference status is investor-scoped; worker execution and errors are ops-only."""

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.db.reference_notice_outcome import record_outcome
from app.db.reference_sync import status
from app.reference_worker import run_once

router = APIRouter(tags=["reference-sync"])

#: Leaves headroom inside the 300-second Vercel function limit (Hobby and Pro alike) for
#: the task already running when the budget runs out.
REFERENCE_CRON_BUDGET_SECONDS = 240.0
REFERENCE_CRON_MAX_TASKS = 500


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
    from app.post_import import run_cycle

    cycle = run_cycle(conn, settings)
    return {"cycle": cycle, **status(conn, investor_id), "enabled": settings.reference_sync_enabled}


@router.post("/ops/reference-sync/run", dependencies=[Depends(require_ops_key)])
def run(conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)) -> dict:
    return run_once(conn, settings, limit=1)


@router.get("/ops/reference-sync/run-cron", dependencies=[Depends(require_cron_secret)])
def cron(conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)) -> dict:
    """Scheduled in vercel.json. A full batch per call, bounded by time rather than by a
    task count of one -- at one task per call the queue (one NSDL task per held ISIN
    plus one NSE task per month of history) never drained in production."""
    return run_once(
        conn, settings, limit=REFERENCE_CRON_MAX_TASKS,
        time_budget_seconds=REFERENCE_CRON_BUDGET_SECONDS,
    )


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
