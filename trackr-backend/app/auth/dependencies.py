"""FastAPI wiring for JWT auth. Kept separate from ``service.py`` so that module stays
framework-free -- the same separation ``app/database.py`` keeps from ``app/db/connection.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import service
from app.auth.browser import cookie_name, require_browser_write
from app.auth.service import InvalidTokenError
from app.config import Settings, get_settings
from app.database import get_conn
from app.db.capabilities import (
    CAPABILITY_WORKSPACE,
    WorkspaceCapabilityRequiredError,
    has_capability,
)
from app.db.repository import get_investor_owner, investor_exists

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Accept native bearer or HttpOnly browser authentication; verify its live session.
    Browser writes also require the trusted Origin and CSRF header."""
    token = credentials.credentials if credentials else request.cookies.get(cookie_name(settings))
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    if credentials is None and request.method not in ("GET", "HEAD", "OPTIONS"):
        require_browser_write(request, settings)
    try:
        return (service.current_user(conn, settings, token) if credentials
                else service.current_browser_user(conn, token))
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def _authorise(conn: Any, current_user: dict[str, Any], investor_id: str | None) -> None:
    """The one ownership decision every scoped endpoint shares. 404 for an unknown
    investor (it reveals nothing), 403 when the row exists but belongs to someone else
    or to nobody yet -- never 404 for the not-yours case, which would let a caller
    probe which investor ids exist."""
    if investor_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such investor")
    if not investor_exists(conn, investor_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such investor: {investor_id}")
    owner = get_investor_owner(conn, investor_id)
    if owner != str(current_user["id"]):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this investor belongs to another account"
            if owner
            else "this investor is unclaimed; run app.db.assign_owner to claim it",
        )


def assert_owned_investor(conn: Any, current_user: dict[str, Any], investor_id: str) -> None:
    """Programmatic twin of ``require_owned_investor`` -- for handlers whose scope
    arrives outside the request path, e.g. POST /ingest/csv's ``investor_id`` form
    field."""
    _authorise(conn, current_user, investor_id)


def require_owned_investor(
    investor_id: str,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Route-level guard: the authenticated user must own ``{investor_id}``. Resolves
    ``investor_id`` from the path -- or the query string, for endpoints like
    /privacy/export that scope by query parameter instead."""
    _authorise(conn, current_user, investor_id)


def require_workspace_access(
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Route-level guard: the authenticated user must hold the ``workspace`` capability.

    **This is a product gate, not an ownership gate, and it replaces neither.** Holding
    Workspace says which application a login may use; it says nothing whatever about
    whose ledger, notes or theses it may touch. Every investor-scoped research route
    declares BOTH this and ``require_owned_investor``:

        @router.get(
            "/{investor_id}/research/...",
            dependencies=[Depends(require_workspace_access), Depends(require_owned_investor)],
        )

    Two dependencies rather than one combined helper, deliberately. A single
    ``require_workspace_investor`` would read like it covered both and would be one
    careless edit away from letting a Workspace subscriber open a stranger's portfolio.
    Ownership semantics and their existing 403/404 split are untouched by this function.

    Resolved from the database on every request, never from a token claim -- see
    ``app/db/capabilities.py``. Revocation therefore takes effect on the caller's very
    next request, not whenever their current token happens to expire.

    Missing or invalid credentials raise 401 from ``get_current_user`` before this runs.
    A valid login without the capability raises ``WorkspaceCapabilityRequiredError``,
    which ``app/main.py`` renders as 403 with the stable
    ``workspace_capability_required`` code.

    Returns the user dict, so a handler that needs the caller can depend on this
    directly instead of also depending on ``get_current_user``.
    """
    if not has_capability(conn, str(current_user["id"]), CAPABILITY_WORKSPACE):
        raise WorkspaceCapabilityRequiredError(str(current_user["id"]))
    return current_user


def require_owned_batch(
    batch_id: str,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Route-level guard for /ingest/stage/{batch_id}/*: authorises against the batch's
    investor, not the batch itself -- a batch id guessed right but owned by someone else
    must read exactly like one that does not exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT investor_id FROM import_batch WHERE id = %s", (batch_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(batch_id))
    _authorise(conn, current_user, str(row[0]))
