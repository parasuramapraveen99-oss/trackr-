"""Monitor routes: versioned schedules and their due history (sessions 36-37, section 7.5).

Same guards as ``app/routers/research.py``: session, ownership, Workspace capability, enabled and
ready Workspace; WRITE routes also need write readiness and an Idempotency-Key. Hiding the
Monitors rail item is not the control -- these dependencies are.

**BE-Q01 IS OPEN.** Saving a schedule is allowed and says so: every listing carries
``execution_policy_status: EXECUTION_POLICY_UNRESOLVED``. The manual-run route answers 409 with
the reason. The scheduler tick is an internal job (``app/jobs/monitors.py``), not an endpoint,
and records blocked occurrences only. ``GET /monitor-reports/{id}`` (BE 39) is not registered:
report assembly needs real execution, which BE 38 cannot provide until the decision exists.

NOTHING HERE CALLS A MODEL, FETCHES A URL, READS A PROVIDER KEY OR TOUCHES THE LEDGER.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.database import get_conn
from app.db import monitors as mon
from app.db.connection import WorkspaceCapability
from app.research import canonical
from app.research.errors import WorkspaceError
from app.research.source_policy import SourcePolicy
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_source_policy,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.monitors import (
    ManualRunIn,
    Run,
    RunDetail,
    Schedule,
    ScheduleCreateIn,
    ScheduleVersionIn,
)
from app.schemas.workspace import Page

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN


def require_monitor_schema(conn: Any = Depends(get_conn)) -> None:
    """503 MONITORS_UNAVAILABLE until 055 and 057 are applied -- never a 500 from a missing
    column or an event type the database would refuse."""
    from app.db.migrate import monitor_schema_ready

    if not monitor_schema_ready(conn):
        raise WorkspaceError(503, "MONITORS_UNAVAILABLE",
                             "monitor storage (migrations 055 and 057) is not installed")


router = APIRouter(
    prefix="/investors/{investor_id}/workspace",
    tags=["monitors"],
    dependencies=[
        Depends(require_owned_investor),
        Depends(require_workspace_access),
        Depends(require_workspace_enabled),
        Depends(require_monitor_schema),
    ],
)

WRITE = [Depends(require_research_writes_ready)]


def _reply(response: Response, result: tuple[Any, int]) -> Any:
    body, status = result
    response.status_code = status
    return body


def _actor(user: dict[str, Any]) -> str:
    return str(user["id"])


def _digest(operation: str, body: Any) -> str:
    return canonical.digest({"operation": operation, "body": body.model_dump(mode="json")})


@router.get("/monitors", response_model=Page[Schedule])
def list_monitors(
    investor_id: str,
    isin: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
) -> Any:
    """Latest version of each schedule, its execution-policy status and latest occurrence.
    A schedule that has never executed never displays as active or healthy."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return mon.list_schedules(conn, investor_id, isin, cursor, limit, mon.utc_now(),
                                  policy)


@router.post("/companies/{isin}/monitors", response_model=Schedule, status_code=201,
             dependencies=WRITE)
def create_monitor(
    investor_id: str,
    isin: str,
    body: ScheduleCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
) -> Any:
    """Version 1. Saved with EXECUTION_POLICY_UNRESOLVED; nothing will run while BE-Q01 is open."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, mon.create_schedule(
            conn, investor_id, _actor(user), isin, body.model_dump(), key,
            _digest(mon.OP_CREATE, body), mon.utc_now(), policy))


@router.post("/monitors/{schedule_id}/versions", response_model=Schedule, status_code=201,
             dependencies=WRITE)
def append_monitor_version(
    investor_id: str,
    schedule_id: str,
    body: ScheduleVersionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
) -> Any:
    """Pause, resume, rebind or reschedule: a full new version against expected_version."""
    fields = body.model_dump()
    expected = fields.pop("expected_version")
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, mon.append_version(
            conn, investor_id, _actor(user), schedule_id, fields, expected, key,
            _digest(mon.OP_VERSION, body), mon.utc_now(), policy))


@router.get("/monitors/{schedule_id}/runs", response_model=Page[Run])
def list_monitor_runs(
    investor_id: str,
    schedule_id: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return mon.list_runs(conn, investor_id, schedule_id, cursor, limit)


@router.post("/monitors/{schedule_id}/runs", response_model=Run, status_code=201,
             dependencies=WRITE)
def request_monitor_run(
    investor_id: str,
    schedule_id: str,
    body: ManualRunIn,
    _key: str = Depends(idempotency_key),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """409 EXECUTION_POLICY_UNRESOLVED while BE-Q01 is open. Writes nothing."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        mon.refuse_manual_run(conn, investor_id, schedule_id, body.schedule_version)
    raise AssertionError("unreachable: refuse_manual_run always raises")  # pragma: no cover


@router.get("/monitor-runs/{run_id}", response_model=RunDetail)
def get_monitor_run(
    investor_id: str,
    run_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """The occurrence, its task (if any) and its report (if any). Blocked is not 'no changes'."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return mon.run_detail(conn, investor_id, run_id)
