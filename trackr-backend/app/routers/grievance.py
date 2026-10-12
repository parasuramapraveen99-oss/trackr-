"""POST /grievances, GET /grievances, GET /grievances/due-soon, GET /grievances/overdue,
POST /grievances/{id}/acknowledge, POST /grievances/{id}/resolve,
POST /grievances/{id}/escalate.

Thin HTTP layer over ``app.db.grievance``, which is already the whole implementation --
the 90-day DPDP clock, its trigger-maintained deadline, and the CHECK constraints that
refuse a backdated or contentless resolution. See
docs/FULL_VISION_BACKEND_ROADMAP.md section A7.

Intake (``POST /grievances``) is the one endpoint here without ``get_current_user``.
docs/COMPLIANCE.md is explicit that intake "accepts complaints from people not yet
matched to an investor record" -- and ``app_user`` (008_auth.sql) is an adviser login,
never an investor one, so there is no bearer token a complainant could hold even in
principle. Requiring auth on intake would not tighten anything; it would just make the
route unable to do the one thing docs/COMPLIANCE.md requires of it.

Every other endpoint here is staff work performed on a complaint already in the system
-- listing the open queue, acknowledging, resolving, escalating -- so each of them takes
``get_current_user``, the same as ``app.routers.privacy``'s export/erase and
``app.routers.ingest``'s promote/discard. This was decided endpoint by endpoint rather
than copied wholesale from privacy.py: intake is the one genuine exception, not a
reason to leave the rest open too.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.dependencies import get_current_user
from app.database import get_conn
from app.db import grievance
from app.db.grievance import AlreadyResolvedError
from app.schemas.grievance import (
    EscalateIn,
    GrievanceIn,
    GrievanceOut,
    GrievanceSummary,
    ResolveIn,
    ResolveOut,
)

router = APIRouter(prefix="/grievances", tags=["grievance"])


def _out_row(row: dict[str, Any]) -> dict[str, Any]:
    """One raised grievance as JSON. ``id`` is cast to str the same way
    ``app.db.repository.create_investor`` casts its own uuid columns --
    ``app.db.grievance`` hands back whatever psycopg returns for a uuid column, uncast,
    on purpose: it is not an HTTP module and has no wire format of its own to keep."""
    return {
        "id": str(row["id"]),
        "reference": row["reference"],
        "status": row["status"],
        "raised_at": row["raised_at"],
        "due_at": row["due_at"],
    }


def _summary_row(row: dict[str, Any]) -> dict[str, Any]:
    """One row of the open / due-soon / overdue listings as JSON."""
    return {
        "id": str(row["id"]),
        "reference": row["reference"],
        "investor_id": str(row["investor_id"]) if row["investor_id"] else None,
        "subject": row["subject"],
        "channel": row["channel"],
        "status": row["status"],
        "raised_at": row["raised_at"],
        "due_at": row["due_at"],
        "time_left": row["time_left"],
    }


@router.post("", response_model=GrievanceOut, status_code=status.HTTP_201_CREATED)
def file_grievance(body: GrievanceIn, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """File a complaint and start the 90-day clock. Deliberately unauthenticated --
    see module docstring."""
    row = grievance.raise_grievance(
        conn,
        subject=body.subject,
        channel=body.channel,
        detail=body.detail,
        investor_id=body.investor_id,
    )
    return _out_row(row)


@router.get("", response_model=list[GrievanceSummary])
def list_open_grievances(
    investor_id: str | None = None,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """The open queue, soonest deadline first. Staff reporting -- see module docstring
    for why this requires auth while intake does not."""
    return [_summary_row(r) for r in grievance.open_grievances(conn, investor_id=investor_id)]


@router.get("/due-soon", response_model=list[GrievanceSummary])
def list_due_soon(
    horizon_days: int = Query(default=grievance.WARNING_HORIZON_DAYS, ge=0),
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """What a weekly review should read: unresolved and inside the warning horizon,
    not only what has already breached."""
    return [_summary_row(r) for r in grievance.due_soon(conn, horizon_days=horizon_days)]


@router.get("/overdue", response_model=list[GrievanceSummary])
def list_overdue(
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Past the 90-day ceiling and still unresolved. Should always be empty."""
    return [_summary_row(r) for r in grievance.overdue(conn)]


@router.post("/{grievance_id}/acknowledge")
def acknowledge_grievance(
    grievance_id: str,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Mark it seen. Does not stop the clock -- only resolution does that."""
    grievance.acknowledge(conn, grievance_id, actor_uid=current_user["id"])
    return {"status": "acknowledged"}


@router.post("/{grievance_id}/resolve", response_model=ResolveOut)
def resolve_grievance(
    grievance_id: str,
    body: ResolveIn,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Close it, recording how. A resolved grievance cannot be reopened
    (``AlreadyResolvedError``, mapped to 409 in app.main -- re-raised here untouched)
    and cannot be closed with a blank resolution (a bare ``ValueError`` from
    ``app.db.grievance``, translated to 400 here since it is not a distinct type main.py
    can key a handler on)."""
    try:
        result = grievance.resolve(
            conn, grievance_id, body.resolution, actor_uid=current_user["id"]
        )
    except AlreadyResolvedError:
        raise
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return result


@router.post("/{grievance_id}/escalate")
def escalate_grievance(
    grievance_id: str,
    body: EscalateIn,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Escalate. Does NOT extend the deadline -- the ceiling is statutory, not internal."""
    grievance.escalate(conn, grievance_id, body.reason, actor_uid=current_user["id"])
    return {"status": "escalated"}
