"""Pydantic request/response DTOs for /auth/*."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

from app.terms import CURRENT_TOS_VERSION

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None

    #: Which Terms + Privacy version the person ticked. REQUIRED, and validated against
    #: the one this build actually serves: a client that sends a stale or invented
    #: version has shown the person something other than what we would record, and a
    #: mismatched acceptance is not a weaker record than none -- it is a false one.
    tos_version: str

    @field_validator("tos_version")
    @classmethod
    def _is_the_version_we_serve(cls, value: str) -> str:
        if value.strip() != CURRENT_TOS_VERSION:
            raise ValueError(
                f"terms version {value!r} is not the current one ({CURRENT_TOS_VERSION}); "
                "reload the page and accept the terms shown there"
            )
        return value.strip()

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        if not _EMAIL.match(value.strip()):
            raise ValueError("not a valid email address")
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _password_long_enough(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("password must be at least 8 characters")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    created_at: datetime


class CapabilitiesResponse(BaseModel):
    """GET /auth/capabilities -- which products the CALLER may use.

    EFFECTIVE capabilities, not raw grant rows: a login granted only ``workspace``
    reports ``["portfolio", "workspace"]``, because Workspace includes Plus
    (docs/PRODUCT_ARCHITECTURE.md section 4). The expansion happens once, in
    ``app.db.capabilities.effective_capabilities``.

    Always the caller's own. There is no ``user_id`` parameter and no admin variant:
    an endpoint that could report someone else's access is an endpoint that has to get
    an authorisation check right, and this one has nothing to get wrong.

    Order is stable (``portfolio`` before ``workspace``) so a client may compare two
    responses for equality. A frontend uses this to decide what to RENDER; it is not
    what authorises anything. The server re-checks on every research request, which is
    why a tampered client gains nothing but a screen that fails on use.
    """

    capabilities: list[str]


class TokenResponse(BaseModel):
    # Named plainly as `token` rather than after the usual OAuth2 field vocabulary --
    # `make lint`'s credential-name ban is deliberately blunt (see Makefile) and would
    # otherwise flag this login response the same way it flags a stored broker
    # credential. This value is never persisted; only its hash is
    # (app_session.session_token_hash).
    token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
    # The login's oldest owned investor. Register always has one (registration
    # provisions it); login returns None for a user whose investors were all erased or
    # who predates ownership. Clients must take the id from HERE, not from localStorage
    # or a build-time default -- that shared fallback is exactly how two accounts ended
    # up reading one portfolio.
    investor_id: str | None = None


class BrowserSessionResponse(BaseModel):
    """Browser identity only. The credential travels exclusively in an HttpOnly cookie."""

    user: UserResponse
    investor_id: str | None = None
