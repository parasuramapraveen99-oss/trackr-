"""Consent capture and the gate that enforces it.

DPDP is consent-first. That is only real if something refuses to proceed without it,
which is what ``require_consent`` is for -- a ``client_consent`` table nobody checks is
documentation, not compliance.

The consent language that carries the legal weight, from ``docs/COMPLIANCE.md``:

    "You will provide your own account statements (CAS, contract notes, broker exports)
    to me for portfolio tracking. I do not access your broker, depository or registrar
    accounts directly, and I do not hold your login credentials."

That sentence is the difference between a document-processing service and a regulated
data intermediary. ``notice_version`` records which wording a principal actually agreed
to, because the wording will change and consent to an old version is not consent to a new
one.
"""

from __future__ import annotations

from typing import Any

from app.db import audit

#: The lawful basis this platform operates on. Recorded per grant rather than assumed,
#: because DPDP requires the basis to be stated and it may differ per purpose.
BASIS_CONSENT = "CONSENT"

PURPOSE_PORTFOLIO_TRACKING = "PORTFOLIO_TRACKING"

#: The wording currently in force -- the exact sentence from ``docs/COMPLIANCE.md``
#: §3, "The consent language that carries the legal weight." Bump this whenever that
#: sentence changes; a consent recorded under an old version is not consent to a new
#: one, which is the entire reason ``notice_version`` exists as its own column instead
#: of being inferred from ``consent_given_at``. The frontend's consent-capture screen
#: hardcodes this same literal.
CURRENT_NOTICE_VERSION = "2026-08-consent-v1"


class ConsentMissingError(PermissionError):
    """No live consent for this investor. Refuse the operation.

    A PermissionError rather than a ValueError on purpose: this is an authorisation
    failure, not a malformed input, and it should never be caught by a generic
    input-validation handler and turned into a warning.
    """

    def __init__(self, investor_id: str) -> None:
        self.investor_id = investor_id
        super().__init__(
            f"No live consent for investor {investor_id}. Personal data must not be "
            "processed without it, and a withdrawn consent is not a live one."
        )


def record_consent(
    conn: Any,
    investor_id: str,
    notice_version: str,
    evidence_ref: str,
    purpose: str = PURPOSE_PORTFOLIO_TRACKING,
    lawful_basis: str = BASIS_CONSENT,
    actor_uid: str | None = None,
) -> str:
    """Capture a consent grant. Returns its id.

    Never updates an existing row. A re-grant after withdrawal is a NEW row, so the
    history of what was agreed, when, and under which notice version survives intact --
    which is exactly what a regulator or a dispute will ask for.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO client_consent
                (investor_id, notice_version, purpose, lawful_basis, evidence_ref)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (investor_id, notice_version, purpose, lawful_basis, evidence_ref),
        )
        consent_id = str(cur.fetchone()[0])

    audit.record(
        conn,
        action=audit.ACTION_CONSENT_GIVEN,
        entity_type="client_consent",
        entity_id=consent_id,
        actor_uid=actor_uid,
        diff={"investor_id": investor_id, "notice_version": notice_version, "purpose": purpose},
    )
    return consent_id


def withdraw_consent(
    conn: Any,
    investor_id: str,
    purpose: str = PURPOSE_PORTFOLIO_TRACKING,
    actor_uid: str | None = None,
) -> int:
    """Stamp ``withdrawn_at`` on every live consent for this purpose. Returns the count.

    Withdrawal is a right, not a request: it takes effect immediately and does not need
    anyone's approval. Erasure is a separate act -- see ``db/privacy.py`` -- because a
    principal may withdraw consent while records are still under a legal hold.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE client_consent SET withdrawn_at = now()
            WHERE investor_id = %s AND purpose = %s AND withdrawn_at IS NULL
            """,
            (investor_id, purpose),
        )
        count = cur.rowcount

    if count:
        audit.record(
            conn,
            action=audit.ACTION_CONSENT_WITHDRAWN,
            entity_type="investor",
            entity_id=investor_id,
            actor_uid=actor_uid,
            diff={"purpose": purpose, "consents_withdrawn": count},
        )
    return count


def has_live_consent(
    conn: Any, investor_id: str, purpose: str = PURPOSE_PORTFOLIO_TRACKING
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM client_consent
            WHERE investor_id = %s AND purpose = %s AND withdrawn_at IS NULL
            LIMIT 1
            """,
            (investor_id, purpose),
        )
        return cur.fetchone() is not None


def require_consent(conn: Any, investor_id: str, purpose: str = PURPOSE_PORTFOLIO_TRACKING) -> None:
    """Raise unless live consent exists. Call before processing any personal data.

    Raises:
        ConsentMissingError
    """
    if not has_live_consent(conn, investor_id, purpose):
        raise ConsentMissingError(investor_id)
