"""Butler's private notebook: Settings -> Butler -> Understanding you (BT 12-15).

Under ``/investors/{investor_id}/butler/notebook``. Same chain as the Butler routes, plus an
applied ``053_butler_notebook.sql`` (503 NOTEBOOK_UNAVAILABLE otherwise). Writes need write
readiness and a UUID ``Idempotency-Key``.

VISIBLE TO THE PERSON IT DESCRIBES, AND TO NO ONE ELSE. Every route requires ownership of the
investor; there is no operator/support lookup, no share route and no report route here. Reads
run as READER, the person's writes as HUMAN; the deterministic builder and hosted reflection
write as ENGINE elsewhere. Workspace only -- Plus has no client for these paths.

OFF UNTIL THE PERSON TURNS IT ON. Nothing but ``POST /consent`` can opt anyone in; opt-out never
removes access to their notes, theses, reviews or portfolio.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import butler_notebook as bn
from app.db.connection import WorkspaceCapability
from app.db.migrate import hosted_jobs_schema_ready, notebook_schema_ready
from app.db.workspace_context import authorise_erasure
from app.research import butler_notebook_policy as np
from app.research import canonical
from app.research.errors import WorkspaceError
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.butler_notebook import (
    Consent,
    ConsentIn,
    ControlIn,
    ControlOut,
    DeleteAllIn,
    DeleteAllOut,
    Notebook,
    SelfDescription,
    SelfDescriptionIn,
    WhyQuestion,
)

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN
ERASER = WorkspaceCapability.ERASER

_ready = False


def require_notebook_ready(conn: Any = Depends(get_conn)) -> None:
    global _ready
    if _ready:
        return
    if not notebook_schema_ready(conn):
        raise WorkspaceError(503, "NOTEBOOK_UNAVAILABLE",
                             "the Butler notebook is not installed on this server")
    _ready = True


router = APIRouter(
    prefix="/investors/{investor_id}/butler/notebook",
    tags=["butler-notebook"],
    dependencies=[
        Depends(require_owned_investor),
        Depends(require_workspace_access),
        Depends(require_workspace_enabled),
        Depends(require_notebook_ready),
    ],
)
WRITE = [Depends(require_research_writes_ready)]


def _digest(op: str, body: Any) -> str:
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
    return canonical.digest({"operation": op, "body": payload})


def _reply(response: Response, result: tuple[Any, int]) -> Any:
    body, status = result
    response.status_code = status
    return body


@router.get("/catalogue")
def catalogue(investor_id: str) -> dict[str, Any]:
    """The reviewed notebook catalogue, disclosure text and stated heuristics."""
    return np.manifest()


@router.get("", response_model=Notebook)
def notebook(
    investor_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Everything stored, with origin, status, exact episodes, limits and influence history."""
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        view = bn.notebook_view(conn, investor_id)
        if hosted_jobs_schema_ready(fin_conn):
            from app.research import butler_reflection as br

            view["reflection"] = br.latest_reflection(conn, investor_id)
        return view


@router.post("/consent", response_model=Consent, status_code=201, dependencies=WRITE)
def set_consent(
    investor_id: str, body: ConsentIn, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Opt in, change scope, pause, resume or revoke. Versioned; the latest row is in force."""
    actor = str(user["id"])
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        return _reply(response, bn.set_consent(conn, investor_id, actor, body.model_dump(),
                                               key, _digest(bn.OP_CONSENT, body)))


@router.post("/self-descriptions", response_model=SelfDescription, status_code=201,
             dependencies=WRITE)
def add_self_description(
    investor_id: str, body: SelfDescriptionIn, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    actor = str(user["id"])
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        return _reply(response, bn.add_self_description(
            conn, investor_id, actor, body.model_dump(), key, _digest(bn.OP_SELF, body)))


@router.post("/controls", response_model=ControlOut, status_code=201, dependencies=WRITE)
def control(
    investor_id: str, body: ControlIn, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Dispute / withdraw dispute / disable / enable / delete a pattern; correct or exclude an
    episode; retract a self-description. Takes effect on the next read -- no worker pass."""
    actor = str(user["id"])
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        return _reply(response, bn.record_control(conn, investor_id, actor, body.model_dump(),
                                                  key, _digest(bn.OP_CONTROL, body)))


@router.post("/delete", response_model=DeleteAllOut, status_code=201, dependencies=WRITE)
def delete_notebook(
    investor_id: str, body: DeleteAllIn, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Revoke learning, fence everything recorded so far (it disappears from every read at
    once), then purge the derived rows on the eraser capability. Research and portfolio access
    are untouched. A legal hold blocks the purge and is reported, never hidden."""
    actor = str(user["id"])
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        fence, status = bn.delete_all_fence(conn, investor_id, actor, key,
                                            _digest(bn.OP_DELETE_ALL, body))
    with db_errors(), sessions.open(ERASER, investor_id) as conn, \
            authorise_erasure(conn, investor_id):
        purge = bn.purge_notebook(conn, investor_id)
    response.status_code = status
    return {"fence_id": fence["fence_id"], "purge": purge,
            "kept": ["butler_learning_consent", "butler_notebook_exclusion"],
            "note": "Consent history and a content-free exclusion list are kept so deleted "
                    "observations cannot be rebuilt. Anything already sent to a model provider "
                    "during an earlier reflection cannot be recalled."}


@router.get("/export")
def export_notebook(
    investor_id: str, sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Every stored notebook row, including disputed, suspended and excluded material."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bn.export_notebook(conn, investor_id)


@router.get("/why", response_model=WhyQuestion)
def why(
    investor_id: str,
    review_id: str = Query(min_length=36, max_length=36),
    question_id: str = Query(pattern=r"^[A-Z][A-Z0-9_]{2,63}$"),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """The "Why this question?" link on every personalised review question."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bn.why_question(conn, investor_id, review_id, question_id)


@router.post("/refresh", status_code=201, dependencies=WRITE)
def refresh(
    investor_id: str, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    settings: Settings = Depends(get_settings),
    fin_conn: Any = Depends(get_conn),
) -> Any:
    """Explicit "Refresh understanding": one hosted reflection job within today's quota.
    Enqueues and returns; nothing runs in this request."""
    from app.db import hosted_jobs as hj
    from app.research import butler_reflection as br
    from app.research import platform_inference as pi

    if not hosted_jobs_schema_ready(fin_conn):
        raise hj.not_configured("HOSTED_JOBS_NOT_INSTALLED")
    actor = str(user["id"])
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        try:
            job_id, status = br.admit_refresh(conn, investor_id, actor, settings, key,
                                              canonical.digest({"operation": "refresh"}))
        except pi.PlatformNotConfigured as exc:
            raise hj.not_configured(exc.reason) from exc
        response.status_code = status
        return br.latest_reflection(conn, investor_id) or {"id": job_id}
