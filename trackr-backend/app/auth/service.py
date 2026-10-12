"""Password hashing and JWT issue/verify.

The only place either happens. A router must never call ``bcrypt`` or ``jwt`` directly --
routing every login and every token check through here is what keeps the two consistent
and keeps a future change (rotating ``JWT_SECRET``, tightening password rules) to one file.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.auth import repository
from app.config import Settings


class EmailAlreadyRegisteredError(ValueError):
    """This email already has an account."""

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(f"{email} is already registered")


class InvalidCredentialsError(ValueError):
    """Wrong email or wrong password. Deliberately one error for both: telling an
    attacker WHICH was wrong is a free account-enumeration oracle."""


class InvalidTokenError(ValueError):
    """Missing, malformed, expired, or revoked. Same posture as above -- one error, so
    a caller learns only "not authenticated", never which specific check failed."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def _hash_token(token: str) -> str:
    """SHA-256 of the issued JWT. Stored, never the token itself -- session_token_hash
    exists precisely so a stolen database is not also a set of stolen live sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register(
    conn: Any,
    email: str,
    password: str,
    display_name: str | None = None,
    tos_version: str | None = None,
) -> dict[str, Any]:
    """Create a new user. Raises EmailAlreadyRegisteredError if the email is taken.

    ``tos_version`` records which Terms + Privacy version was accepted. The route layer
    is what insists on its presence (app.auth.schemas.RegisterRequest); it stays optional
    here so the existing tests and any internal caller that provisions a user by another
    route are not forced to fabricate an acceptance.
    """
    import psycopg

    normalised = email.strip().lower()
    try:
        return repository.create_user(
            conn, normalised, hash_password(password), display_name, tos_version
        )
    except psycopg.errors.UniqueViolation as exc:
        raise EmailAlreadyRegisteredError(normalised) from exc


def issue_token(settings: Settings, conn: Any, user_id: str) -> tuple[str, int]:
    """Mint an access token for ``user_id`` and record its session row.

    Returns ``(token, expires_in_seconds)``. The token is signed first -- its own ``exp``
    is authoritative -- then hashed and recorded, so a later request can look the session
    up by that hash (see ``current_user``) rather than trusting the signature alone. That
    lookup is what makes revocation possible for something that would otherwise stay
    valid, unrevocably, until it simply expires.

    The SESSION outlives the TOKEN: it lives ``session_ttl_seconds``, not
    ``jwt_expiry_seconds``. The short JWT is what travels on every request; the session
    row is what ``refresh`` renews. With both at one hour there is nothing to renew --
    an expired token meant logging in again, every hour.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.jwt_expiry_seconds)
    session_expires_at = now + timedelta(seconds=settings.session_ttl_seconds)

    # jti makes every issued token unique even for two logins inside the same wall-clock
    # second: without it, iat/exp round to seconds and two rapid logins hash to the SAME
    # session_token_hash -- the UNIQUE index then refuses the second login outright.
    payload = {"sub": str(user_id), "exp": expires_at, "iat": now, "jti": uuid.uuid4().hex}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    repository.create_session(conn, user_id, _hash_token(token), session_expires_at)
    return token, settings.jwt_expiry_seconds


def login(
    conn: Any, settings: Settings, email: str, password: str
) -> tuple[str, int, dict[str, Any]]:
    """Verify credentials and issue a token. Returns ``(token, expires_in, user)``.

    Raises:
        InvalidCredentialsError: no such user, wrong password, or a deactivated account.
            All three collapse to the same error and the same 401 -- see the class
            docstring.
    """
    user = authenticate_credentials(conn, email, password)
    token, expires_in = issue_token(settings, conn, str(user["id"]))
    return token, expires_in, user


def authenticate_credentials(conn: Any, email: str, password: str) -> dict[str, Any]:
    """One password check shared by native bearer and HttpOnly browser sign-in."""
    user = repository.get_user_by_email(conn, email.strip().lower())
    if user is None or not user["is_active"]:
        # Hash something anyway. Returning immediately on "no such user" makes a login
        # attempt against a real email measurably slower than one against a fake one --
        # a timing oracle for account enumeration.
        bcrypt.checkpw(b"", bcrypt.gensalt())
        raise InvalidCredentialsError("invalid email or password")
    if not verify_password(password, user["password_hash"]):
        raise InvalidCredentialsError("invalid email or password")

    return user


def issue_browser_session(settings: Settings, conn: Any, user_id: str) -> str:
    # An opaque 256-bit credential, independently revocable and hashed in the database.
    # Unlike access JWTs it follows the session horizon, so tabs/reloads do not race
    # to rotate an expired hourly token. It is never returned in browser JSON.
    token = secrets.token_urlsafe(32)
    repository.create_session(conn, user_id, _hash_token(token),
                              datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds))
    return token


def current_browser_user(conn: Any, token: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        # A native JWT must not gain the longer cookie-session lifetime.
        raise InvalidTokenError("Not signed in.")
    session = repository.get_active_session_by_token_hash(conn, _hash_token(token))
    if session is None:
        raise InvalidTokenError("Not signed in.")
    user = repository.get_user_by_id(conn, str(session["user_id"]))
    if user is None or not user["is_active"]:
        raise InvalidTokenError("Not signed in.")
    return user


def refresh(conn: Any, settings: Settings, token: str) -> tuple[str, int, dict[str, Any]]:
    """Exchange a token for a fresh one, renewing the session it was issued under.

    Returns ``(token, expires_in, user)``, shaped like ``login``'s return so callers
    treat both identically.

    The signature is always verified; only ``exp`` is relaxed. That is safe here because
    the JWT alone authorises nothing -- the SESSION row (looked up by this exact token's
    hash) is what authorises the exchange, and its own ``expires_at`` and ``revoked_at``
    are enforced untouched. An expired token whose session is still live is precisely the
    case refresh exists for; an expired one whose session is gone gets the same
    collapsed error as everywhere else.

    The old session is revoked as part of the exchange: one live session per chain of
    refreshes, so a stolen-and-refreshed token invalidates the copy it was stolen from.

    Raises:
        InvalidTokenError: malformed or wrongly signed, no matching live session,
            or the user has vanished or been deactivated.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("invalid or expired token") from exc

    session = repository.get_active_session_by_token_hash(conn, _hash_token(token))
    if session is None:
        raise InvalidTokenError("session revoked or expired")

    user = repository.get_user_by_id(conn, payload["sub"])
    if user is None or not user["is_active"]:
        raise InvalidTokenError("no such user")

    repository.revoke_session(conn, str(session["id"]))
    new_token, expires_in = issue_token(settings, conn, str(user["id"]))
    return new_token, expires_in, user


def current_user(conn: Any, settings: Settings, token: str) -> dict[str, Any]:
    """Verify a bearer token and return the user it names.

    Checks the JWT signature and expiry AND the session row: a token whose session was
    revoked must stop working before its own ``exp``, which the signature check alone
    cannot express.

    Raises:
        InvalidTokenError: malformed, expired, revoked, or the user no longer exists.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("invalid or expired token") from exc

    session = repository.get_active_session_by_token_hash(conn, _hash_token(token))
    if session is None:
        raise InvalidTokenError("session revoked or expired")

    user = repository.get_user_by_id(conn, payload["sub"])
    if user is None or not user["is_active"]:
        raise InvalidTokenError("no such user")
    return user
