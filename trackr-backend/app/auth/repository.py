"""Raw SQL against ``app_user`` and ``app_session``. Nothing else.

Same discipline as ``app/db/repository.py``: this module reads and writes rows, and the
service layer above it is where password hashing and JWT issuance live. No ORM, per
docs/STACK_DECISION.md -- these are two more raw-SQL tables, not a reason to introduce one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: psycopg3 maps a `uuid` column to `uuid.UUID`, not `str`. Every row dict this module
#: returns crosses into a Pydantic schema typed `id: str` (app/auth/schemas.py), so `id`
#: is stringified here at the repository boundary -- the same discipline
#: app/db/repository.py uses for `Transaction.id` -- rather than leaking a driver type
#: upward for every caller to convert itself.


def _row_dict(cur: Any, row: Any) -> dict[str, Any]:
    result = dict(zip([c.name for c in cur.description], row, strict=True))
    if "id" in result and result["id"] is not None:
        result["id"] = str(result["id"])
    return result


def create_user(
    conn: Any,
    email: str,
    password_hash: str,
    display_name: str | None = None,
    tos_version: str | None = None,
) -> dict[str, Any]:
    """Insert a new user. Raises ``psycopg.errors.UniqueViolation`` if the email is
    already registered -- the UNIQUE constraint is the enforcement, not a prior SELECT,
    for the same race-condition reason ``transaction.dedupe_key`` is enforced that way.

    ``tos_version`` is the Terms + Privacy version this person ticked at registration
    (app.terms.CURRENT_TOS_VERSION). Stamped in the same INSERT as the account itself,
    never in a follow-up UPDATE: an account that exists without an acceptance beside it,
    even for one statement, is an account we cannot prove was shown anything.

    ``tos_accepted_at`` is set from the same value rather than taken as an argument --
    the caller cannot backdate an acceptance, and NULL version means NULL timestamp.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_user (email, password_hash, display_name, tos_version, tos_accepted_at)
            VALUES (%s, %s, %s, %s::text, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END)
            RETURNING id, email, display_name, is_active, created_at
            """,
            (email, password_hash, display_name, tos_version, tos_version),
        )
        row = cur.fetchone()
        return _row_dict(cur, row)


def get_user_by_email(conn: Any, email: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash, display_name, is_active, created_at "
            "FROM app_user WHERE email = %s",
            (email,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_dict(cur, row)


def get_user_by_id(conn: Any, user_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, display_name, is_active, created_at FROM app_user WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_dict(cur, row)


def create_session(conn: Any, user_id: str, token_hash: str, expires_at: datetime) -> str:
    """Record one issued token. Returns the session id.

    ``current_user`` looks a session up by the hash of its token (see
    ``get_active_session_by_token_hash``), not by this id -- the id is returned only so
    a caller could log or reference this specific row if it ever needs to.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_session (user_id, session_token_hash, expires_at)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (user_id, token_hash, expires_at),
        )
        return str(cur.fetchone()[0])


def get_active_session_by_token_hash(conn: Any, token_hash: str) -> dict[str, Any] | None:
    """The session for this token hash, if it exists, is not revoked, and has not
    expired. None otherwise -- the caller cannot tell from this alone whether the hash
    matched nothing, the session was revoked, or it simply expired, which is deliberate:
    the response to all three is the same 401."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, expires_at, revoked_at
            FROM app_session
            WHERE session_token_hash = %s AND revoked_at IS NULL AND expires_at > now()
            """,
            (token_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c.name for c in cur.description], row, strict=True))


def revoke_session(conn: Any, session_id: str) -> None:
    """Mark one session revoked. Idempotent by construction -- setting a timestamp that
    is already set changes nothing -- because refresh rotation and logout can race."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_session SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
            (session_id,),
        )
