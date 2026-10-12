"""Agents, tasks, attempts, review and grounded chat (BE 19-25, BLUEPRINT_BACKEND.md 7.4).

Every route is under ``/investors/{investor_id}/workspace`` and requires, in order: a session
(401), ownership (403/404), the Workspace capability (403), an enabled and ready Workspace (503)
and migration 052 (503). WRITE routes also need write readiness and a UUID Idempotency-Key.

THE SERVER NEVER TALKS TO A MODEL. There is no key field, no key-test or reachability route, no
proxy and no relay here. The browser holds the person's provider key, calls the provider
directly, and reports back through the attempt routes; ``BROWSER_MODEL`` is therefore
CLIENT-REPORTED, never attested, and every response says so.

CAPABILITIES BY OPERATION (never by request): reads as READER; a person's configuration, task,
chat and review writes as HUMAN attributed to the session user; attempt starts, results,
failures, cancellations and lease expiry as RUNNER, which cannot write a note or a review.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import tasks as tk
from app.db.connection import WorkspaceCapability
from app.db.migrate import execution_schema_ready
from app.research import butler_selection as bsel
from app.research import canonical
from app.research import context as cx
from app.research.errors import CONTEXT_DRIFT, SOURCE_CONTENT_UNAVAILABLE, WorkspaceError
from app.research.source_policy import SourcePolicy
from app.routers.research import _verified_bytes
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_formula_registry,
    get_source_policy,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.tasks import (
    AcceptanceIn,
    Agent,
    AgentCreateIn,
    AgentVersionIn,
    AttemptIn,
    AttemptOut,
    CancellationIn,
    ChatMessage,
    ContextPacket,
    FailureIn,
    RejectionIn,
    ResultSubmissionIn,
    ReviewOut,
    Task,
    TaskCreateIn,
    Thread,
    ThreadCreateIn,
    TurnIn,
    TurnOut,
)
from app.schemas.workspace import Page
from app.storage.factory import get_store

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN
RUNNER = WorkspaceCapability.RUNNER

EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"

_execution_ready = False


def require_execution_ready(conn: Any = Depends(get_conn)) -> None:
    """503 until 052_research_execution.sql is applied: a runner receipt must never fail on a
    missing grant halfway through a submission."""
    global _execution_ready
    if _execution_ready:
        return
    if not execution_schema_ready(conn):
        raise WorkspaceError(503, EXECUTION_UNAVAILABLE,
                             "research execution storage is not installed on this server")
    _execution_ready = True


router = APIRouter(
    prefix="/investors/{investor_id}/workspace",
    tags=["tasks"],
    dependencies=[
        Depends(require_owned_investor),
        Depends(require_workspace_access),
        Depends(require_workspace_enabled),
        Depends(require_execution_ready),
    ],
)

WRITE = [Depends(require_research_writes_ready)]


def _reply(response: Response, result: tuple[Any, int]) -> Any:
    body, status = result
    response.status_code = status
    return body


def _actor(user: dict[str, Any]) -> str:
    return str(user["id"])


def _digest(operation: str, scope: str, body: Any) -> str:
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
    return canonical.digest({"operation": operation, "scope": scope, "body": payload})


def _sample_investor(settings: Settings, investor_id: str) -> bool:
    return investor_id.lower() in settings.sample_investor_ids


def text_loader(fin_conn: Any, settings: Settings, investor_id: str) -> cx.TextLoader:
    """Retained source text for release, rehashed against its pin on every read. Missing
    bytes -> SOURCE_CONTENT_UNAVAILABLE (content withheld); changed bytes -> CONTEXT_DRIFT.
    Called only after ``context.release`` has found model disclosure ALLOWED for the type."""
    store = get_store(settings)

    def load(text_document_id: str, expected_sha: str) -> str:
        if not text_document_id:
            raise WorkspaceError(409, SOURCE_CONTENT_UNAVAILABLE, "no retained text")
        _, data = _verified_bytes(fin_conn, store, investor_id, text_document_id)
        if hashlib.sha256(data).hexdigest() != expected_sha:
            raise WorkspaceError(409, CONTEXT_DRIFT, "retained text does not match its pin")
        return data.decode("utf-8")

    return load


# =========================================================================================
# BE 19 -- agents
# =========================================================================================


@router.get("/agents", response_model=Page[Agent])
def list_agents(
    investor_id: str,
    history: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Latest version per agent. No connection, readiness or key field: only the browser
    knows whether it holds a key for a configuration, and it joins that locally."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return tk.list_agents(conn, investor_id, history, cursor, limit)


@router.post("/agents", response_model=Agent, status_code=201, dependencies=WRITE)
def create_agent(
    investor_id: str,
    body: AgentCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.create_agent(
            conn, investor_id, _actor(user), body.model_dump(), key,
            _digest(tk.OP_AGENT_CREATE, "-", body)))


@router.get("/agents/{agent_id}/versions/{version}", response_model=Agent)
def get_agent_version(
    investor_id: str,
    agent_id: str,
    version: int,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Exact immutable version -- including disabled ones, which old tasks still cite."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return tk.agent_version(conn, investor_id, agent_id, version)


@router.post("/agents/{agent_id}/versions", response_model=Agent, status_code=201,
             dependencies=WRITE)
def append_agent_version(
    investor_id: str,
    agent_id: str,
    body: AgentVersionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.append_agent_version(
            conn, investor_id, _actor(user), agent_id, body.model_dump(), key,
            _digest(tk.OP_AGENT_VERSION, agent_id, body)))


# =========================================================================================
# BE 20 -- tasks and sealed context
# =========================================================================================


@router.get("/tasks", response_model=Page[Task])
def list_tasks(
    investor_id: str,
    state: str | None = None,
    isin: str | None = None,
    purpose: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return tk.list_tasks(conn, investor_id, state, isin, purpose, cursor, limit)


@router.post("/tasks", response_model=Task, status_code=201, dependencies=WRITE)
def create_task(
    investor_id: str,
    body: TaskCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> Any:
    """Atomically pin configuration, method, evidence and a server-derived cutoff. Starts
    no attempt, calls no model, needs no key and never sets ``sample``."""
    isin = body.isin.strip().upper()
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.create_task(
            conn, fin_conn, investor_id, _actor(user), body.model_dump(mode="json"), key,
            _digest(tk.OP_TASK_CREATE, isin, body)))


@router.get("/tasks/{task_id}", response_model=Task)
def get_task(
    investor_id: str,
    task_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return tk.task_by_id(conn, investor_id, task_id)


@router.get("/tasks/{task_id}/context", response_model=ContextPacket)
def get_context(
    investor_id: str,
    task_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
    policy: SourcePolicy = Depends(get_source_policy),
    settings: Settings = Depends(get_settings),
    formulas: Any = Depends(get_formula_registry),
) -> dict[str, Any]:
    """The checked packet. Every read rechecks ownership, ritual, licence and disclosure,
    rehashes pinned content and recomputes the digest. Pinned calculation specs are computed
    by the approved calculator at the task's cutoff (BE 27-28). Reading it starts nothing."""
    loader = text_loader(fin_conn, settings, investor_id)
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        task = tk.task_row(conn, investor_id, task_id)
        butler = bsel.packet_for(conn, investor_id, task) if tk._is_butler(task) else None
        out = tk.packet(conn, fin_conn, investor_id, task_id, policy, loader, butler,
                        formulas)
        if butler is not None:
            out["system_instructions"] = butler["system_prompt"]
        return out


# =========================================================================================
# BE 21 -- attempts and results (RUNNER)
# =========================================================================================


def _expire(sessions: WorkspaceSessions, investor_id: str, task_id: str) -> None:
    """Append EXPIRED for dead leases in its OWN committed transaction first, so the expiry is
    durable history even when the request that noticed it is refused."""
    with db_errors(), sessions.open(RUNNER, investor_id) as conn:
        tk.expire_due(conn, investor_id, tk.task_row(conn, investor_id, task_id)["id"])


def _check_switches(settings: Settings, task: dict[str, Any]) -> None:
    if not settings.research_model_execution_enabled:
        raise WorkspaceError(503, tk.EXECUTION_DISABLED,
                             "model execution is switched off on this server; stored research "
                             "and reviews remain available")
    if tk._is_butler(task) and not settings.butler_model_selection_enabled:
        raise WorkspaceError(503, tk.EXECUTION_DISABLED,
                             "Butler model selection is switched off; Butler's checked cards "
                             "remain available")


@router.post("/tasks/{task_id}/attempts", response_model=AttemptOut, status_code=201,
             dependencies=WRITE)
def start_attempt(
    investor_id: str,
    task_id: str,
    body: AttemptIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    policy: SourcePolicy = Depends(get_source_policy),
) -> Any:
    """A bounded, server-timed attempt. The browser may call its provider ONLY after this
    returns 201 with ``replayed: false``."""
    _expire(sessions, investor_id, task_id)
    digest = _digest(tk.OP_ATTEMPT, task_id, body)
    loader = text_loader(fin_conn, settings, investor_id)
    with db_errors(), sessions.open(RUNNER, investor_id) as conn:
        task = tk.task_row(conn, investor_id, task_id)
        _check_switches(settings, task)
        if tk._is_butler(task) and bsel.other_attempt_in_flight(conn, investor_id, task["id"]):
            raise WorkspaceError(409, bsel.BUTLER_ATTEMPT_ACTIVE,
                                 "another Butler review is already running for this investor")

        def submit_sample(tid: str, attempt_id: str, proposal: dict[str, Any]) -> None:
            sample_key = str(uuid.uuid5(uuid.UUID(key), "stored-sample"))
            submission = {"context_digest": task["context_digest"], "proposal": proposal}
            tk.submit_result(conn, fin_conn, investor_id, _actor(user), tid, attempt_id,
                             submission, sample_key,
                             _digest(tk.OP_RESULT, attempt_id, submission), policy=policy,
                             load_text=loader,
                             butler_validator=bsel.validator(conn, investor_id))

        return _reply(response, tk.start_attempt(
            conn, fin_conn, investor_id, _actor(user), task_id, body.model_dump(), key, digest,
            sample_allowed=_sample_investor(settings, investor_id),
            submit_sample=submit_sample))


@router.post("/tasks/{task_id}/attempts/{attempt_id}/results", response_model=Task,
             status_code=201, dependencies=WRITE)
def submit_result(
    investor_id: str,
    task_id: str,
    attempt_id: str,
    body: ResultSubmissionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
    policy: SourcePolicy = Depends(get_source_policy),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Validate the untrusted proposal. 422/413 on transport shape (not stored); otherwise
    stored PASS or BLOCKED with reasons, and the task moves to ``review`` -- never ``done``."""
    _expire(sessions, investor_id, task_id)
    loader = text_loader(fin_conn, settings, investor_id)
    with db_errors(), sessions.open(RUNNER, investor_id) as conn:
        return _reply(response, tk.submit_result(
            conn, fin_conn, investor_id, _actor(user), task_id, attempt_id,
            body.model_dump(mode="json"), key, _digest(tk.OP_RESULT, attempt_id, body),
            policy=policy, load_text=loader,
            butler_validator=bsel.validator(conn, investor_id)))


@router.post("/tasks/{task_id}/attempts/{attempt_id}/failures", response_model=Task,
             status_code=201, dependencies=WRITE)
def submit_failure(
    investor_id: str,
    task_id: str,
    attempt_id: str,
    body: FailureIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Fixed redacted code only. A message, body, URL, header or model-output field is an
    unknown property and 422."""
    _expire(sessions, investor_id, task_id)
    with db_errors(), sessions.open(RUNNER, investor_id) as conn:
        return _reply(response, tk.submit_terminal(
            conn, investor_id, _actor(user), task_id, attempt_id, "FAILED", body.code, key,
            _digest(tk.OP_FAILURE, attempt_id, body)))


@router.post("/tasks/{task_id}/attempts/{attempt_id}/cancellations", response_model=Task,
             status_code=201, dependencies=WRITE)
def submit_cancellation(
    investor_id: str,
    task_id: str,
    attempt_id: str,
    body: CancellationIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Records the browser's local abort. It does not claim the provider stopped generating
    or billing -- the server cannot know that."""
    _expire(sessions, investor_id, task_id)
    with db_errors(), sessions.open(RUNNER, investor_id) as conn:
        return _reply(response, tk.submit_terminal(
            conn, investor_id, _actor(user), task_id, attempt_id, "CANCELLED",
            "USER_CANCELLED", key, _digest(tk.OP_CANCEL, attempt_id, body)))


# =========================================================================================
# BE 22 -- human review (HUMAN)
# =========================================================================================


@router.post("/tasks/{task_id}/acceptances", response_model=ReviewOut, status_code=201,
             dependencies=WRITE)
def accept(
    investor_id: str,
    task_id: str,
    body: AcceptanceIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Review + one attributed note + links + audit, atomically. The reviewer is the session
    user; the body cannot name one. Creates no thesis, event, conviction, closure or fact."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.accept(
            conn, investor_id, _actor(user), task_id, body.model_dump(mode="json"), key,
            _digest(tk.OP_ACCEPT, task_id, body),
            sample_investor=_sample_investor(settings, investor_id)))


@router.post("/tasks/{task_id}/rejections", response_model=ReviewOut, status_code=201,
             dependencies=WRITE)
def reject(
    investor_id: str,
    task_id: str,
    body: RejectionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.reject(
            conn, investor_id, _actor(user), task_id, body.model_dump(mode="json"), key,
            _digest(tk.OP_REJECT, task_id, body)))


# =========================================================================================
# BE 24 -- grounded chat
# =========================================================================================


@router.get("/chats", response_model=Page[Thread])
def list_chats(
    investor_id: str,
    isin: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return tk.list_threads(conn, investor_id, isin, cursor, limit)


@router.post("/companies/{isin}/chats", response_model=Thread, status_code=201,
             dependencies=WRITE)
def create_chat(
    investor_id: str,
    isin: str,
    body: ThreadCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    scope = isin.strip().upper()
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.create_thread(
            conn, investor_id, _actor(user), isin, body.title, key,
            _digest(tk.OP_THREAD, scope, body)))


@router.get("/chats/{thread_id}/messages", response_model=Page[ChatMessage])
def list_messages(
    investor_id: str,
    thread_id: str,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Assistant turns are rendered from their result's template and pinned references on
    every read; the row itself holds no prose."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return tk.list_messages(conn, investor_id, thread_id, cursor, limit)


@router.post("/chats/{thread_id}/turns", response_model=TurnOut, status_code=201,
             dependencies=WRITE)
def create_turn(
    investor_id: str,
    thread_id: str,
    body: TurnIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> Any:
    """Human turn + CHAT task atomically, or the human turn + a recorded refusal and no task.
    Execution then uses the ordinary attempt routes."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, tk.create_turn(
            conn, fin_conn, investor_id, _actor(user), thread_id, body.model_dump(mode="json"),
            key, _digest(tk.OP_TURN, thread_id, body)))

