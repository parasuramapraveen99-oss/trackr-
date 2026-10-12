"""HttpOnly browser sessions with explicit protection against cross-site writes."""

from __future__ import annotations

from fastapi import HTTPException, Request, Response

from app.config import Settings


def cookie_name(settings: Settings) -> str:
    return "trackr_session" if settings.debug else "__Host-trackr_session"


def require_browser_write(request: Request, settings: Settings) -> None:
    # SameSite=Lax is a second defence. An exact trusted Origin and a custom header
    # prevent simple forms and requests from sibling/untrusted sites authorizing writes.
    if (
        request.headers.get("origin") not in settings.cors_origin_list
        or request.headers.get("x-trackr-request") != "browser"
    ):
        raise HTTPException(403, "Browser request could not be verified.")


def set_session(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=settings.session_ttl_seconds,
        secure=not settings.debug,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def clear_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings), secure=not settings.debug, httponly=True, samesite="lax", path="/"
    )
    response.headers["Cache-Control"] = "no-store"
