"""POST /auth/register, POST /auth/login, POST /auth/refresh, GET /auth/me.

Register provisions a personal investor in the same transaction it creates the user:
a new login must never land on the frontend's old fallback of "whatever investor id is
in localStorage". Login echoes back the caller's oldest owned investor, so every client
takes its investor identity from the token response, not from anywhere shared.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import repository, service
from app.auth.browser import clear_session, cookie_name, require_browser_write, set_session
from app.auth.dependencies import get_current_user, require_authentication
from app.auth.schemas import (
    BrowserSessionResponse,
    CapabilitiesResponse,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db.capabilities import effective_capabilities, grant_default_capabilities
from app.db.repository import create_personal_investor, first_investor_id_for_user
from app.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])


def _browser_identity(conn: Any, user: dict[str, Any]) -> BrowserSessionResponse:
    return BrowserSessionResponse(
        user=UserResponse(**user), investor_id=first_investor_id_for_user(conn, str(user["id"]))
    )


def _issue_browser_cookie(
    request: Request, response: Response, settings: Settings, conn: Any, user_id: str
) -> None:
    previous = request.cookies.get(cookie_name(settings))
    if previous:
        session = repository.get_active_session_by_token_hash(conn, service._hash_token(previous))
        if session:
            repository.revoke_session(conn, str(session["id"]))
    set_session(response, settings, service.issue_browser_session(settings, conn, user_id))


@router.post("/browser/register", response_model=BrowserSessionResponse, status_code=201)
@limiter.limit("5/minute")
def browser_register(
    request: Request,
    response: Response,
    body: RegisterRequest,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> BrowserSessionResponse:
    require_browser_write(request, settings)
    user = service.register(conn, body.email, body.password, body.display_name, body.tos_version)
    grant_default_capabilities(conn, user["id"])
    investor = create_personal_investor(
        conn, owner_user_id=user["id"], display_name=body.display_name
    )
    _issue_browser_cookie(request, response, settings, conn, user["id"])
    return BrowserSessionResponse(user=UserResponse(**user), investor_id=investor["id"])


@router.post("/browser/login", response_model=BrowserSessionResponse)
@limiter.limit("10/minute")
def browser_login(
    request: Request,
    response: Response,
    body: LoginRequest,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> BrowserSessionResponse:
    require_browser_write(request, settings)
    user = service.authenticate_credentials(conn, body.email, body.password)
    _issue_browser_cookie(request, response, settings, conn, user["id"])
    return _browser_identity(conn, user)


@router.get("/browser/session", response_model=BrowserSessionResponse)
def browser_session(
    response: Response,
    user: dict[str, Any] = Depends(get_current_user),
    conn: Any = Depends(get_conn),
) -> BrowserSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return _browser_identity(conn, user)


@router.post("/browser/refresh", response_model=BrowserSessionResponse,
             dependencies=[Depends(require_authentication)])
@limiter.limit("30/minute")
def browser_refresh(
    request: Request,
    response: Response,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> BrowserSessionResponse:
    require_browser_write(request, settings)
    token = request.cookies.get(cookie_name(settings))
    if not token:
        raise HTTPException(401, "Not signed in.")
    try:
        user = service.current_browser_user(conn, token)
    except service.InvalidTokenError:
        raise HTTPException(401, "Not signed in.") from None
    # Refresh does not rotate a credential shared between tabs. The database expiry
    # remains authoritative and an expired/revoked session can never be renewed here.
    response.headers["Cache-Control"] = "no-store"
    return _browser_identity(conn, user)


@router.post("/browser/logout", status_code=204)
def browser_logout(
    request: Request,
    response: Response,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> None:
    require_browser_write(request, settings)
    token = request.cookies.get(cookie_name(settings))
    if token:
        session = repository.get_active_session_by_token_hash(conn, service._hash_token(token))
        if session:
            repository.revoke_session(conn, str(session["id"]))
    clear_session(response, settings)


#: Same posture as dependencies.get_current_user's bearer -- but refresh cannot go
#: through ``get_current_user`` itself: its whole job is to accept a token whose exp has
#: passed, which current_user must keep refusing.
_bearer = HTTPBearer(auto_error=False)


@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("5/minute")
def register(
    request: Request,
    body: RegisterRequest,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = service.register(conn, body.email, body.password, body.display_name, body.tos_version)
    # BOTH capabilities, in the same transaction as the account itself -- an account must
    # never exist without its capability rows, even for one statement.
    #
    # Signing up on either origin lands here identically, and that is still the point:
    # which frontend sent the request is not permission (docs/PRODUCT_ARCHITECTURE.md
    # section 4). The 2026-09-27 reversal removed the operator gate on Workspace; it did
    # NOT introduce origin-based granting, because an Origin is set by the caller. Granting
    # both unconditionally means there is nothing here to forge.
    grant_default_capabilities(conn, user["id"])
    investor = create_personal_investor(
        conn, owner_user_id=user["id"], display_name=body.display_name
    )
    token, expires_in = service.issue_token(settings, conn, user["id"])
    return TokenResponse(
        token=token,
        expires_in=expires_in,
        user=UserResponse(**user),
        investor_id=investor["id"],
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    body: LoginRequest,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    token, expires_in, user = service.login(conn, settings, body.email, body.password)
    return TokenResponse(
        token=token,
        expires_in=expires_in,
        user=UserResponse(**user),
        investor_id=first_investor_id_for_user(conn, str(user["id"])),
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("30/minute")
def refresh(
    request: Request,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> TokenResponse:
    """A fresh token for a token the session table still recognises. The response is a
    full ``TokenResponse`` -- same shape as login -- so clients apply it through the
    exact path they already have, investor_id included."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        token, expires_in, user = service.refresh(conn, settings, credentials.credentials)
    except service.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return TokenResponse(
        token=token,
        expires_in=expires_in,
        user=UserResponse(**user),
        investor_id=first_investor_id_for_user(conn, str(user["id"])),
    )


@router.get("/me", response_model=UserResponse)
def me(current: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    return UserResponse(**current)


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities(
    conn: Any = Depends(get_conn),
    current: dict[str, Any] = Depends(get_current_user),
) -> CapabilitiesResponse:
    """Which products the caller may use: ``["portfolio"]`` or
    ``["portfolio", "workspace"]``.

    Read from ``user_capability`` on this request's own connection, deliberately not
    from a token claim. Capabilities are absent from the JWT payload precisely so that
    a revocation bites on the next request instead of whenever the caller's current
    token happens to expire -- see ``app/db/capabilities.py``.

    Anonymous requests get 401 from ``get_current_user``, like every other authenticated
    route. There is no way to ask about another account: the answer is always about
    ``current``.

    ``TokenResponse`` is unchanged. Capabilities are deliberately NOT bolted onto
    login/refresh: clients that predate this endpoint keep working untouched, and a
    client that cares reads the live value here rather than a snapshot taken at login.
    """
    return CapabilitiesResponse(capabilities=effective_capabilities(conn, str(current["id"])))
