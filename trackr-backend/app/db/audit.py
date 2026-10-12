"""Audit logging -- the DPDP access log and the SEBI advisory record, in one table.

The rule this module exists to make true: **every mutation leaves an audit row, in the
same database transaction as the mutation itself.** Not "should"; cannot-not. If the
audit insert fails, the caller's transaction rolls back and the mutation is lost with it.

An audit log that is written on a best-effort basis is worth very little in a dispute,
because the interesting cases are exactly the ones where something went wrong.
"""

from __future__ import annotations

import json
from typing import Any

#: Actions worth a row. Reads are audited too: DPDP treats access to personal data as an
#: event a principal is entitled to know about, so decrypting a PAN is logged like a write.
ACTION_INSERT = "INSERT"
ACTION_UPDATE = "UPDATE"
ACTION_DELETE = "DELETE"
ACTION_IMPORT = "IMPORT"
ACTION_CONSENT_GIVEN = "CONSENT_GIVEN"
ACTION_CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
ACTION_SECRET_WRITTEN = "SECRET_WRITTEN"
ACTION_SECRET_READ = "SECRET_READ"
ACTION_EXPORT = "EXPORT"
ACTION_ERASURE = "ERASURE"
ACTION_LEGAL_HOLD_PLACED = "LEGAL_HOLD_PLACED"
ACTION_LEGAL_HOLD_RELEASED = "LEGAL_HOLD_RELEASED"
ACTION_DOCUMENT_STORED = "DOCUMENT_STORED"
#: Issuing a signed URL, not following one. The follow happens at the storage provider,
#: which this system may never hear about, so issuance is the only honest moment to log.
ACTION_DOCUMENT_ACCESSED = "DOCUMENT_ACCESSED"
ACTION_DOCUMENT_DELETED = "DOCUMENT_DELETED"
ACTION_CORPORATE_ACTION_ADDED = "CORPORATE_ACTION_ADDED"
ACTION_CORPORATE_ACTION_AUTO_APPLIED = "CORPORATE_ACTION_AUTO_APPLIED"
ACTION_CORPORATE_ACTION_VERIFIED = "CORPORATE_ACTION_VERIFIED"
ACTION_CORPORATE_ACTION_DISPUTED = "CORPORATE_ACTION_DISPUTED"
ACTION_CORPORATE_ACTION_PRICE_WAIVED = "CORPORATE_ACTION_PRICE_WAIVED"
ACTION_DIVIDEND_EVENT_ADDED = "DIVIDEND_EVENT_ADDED"
ACTION_DIVIDEND_EVENT_CORROBORATED = "DIVIDEND_EVENT_CORROBORATED"
ACTION_DIVIDEND_EVENT_VERIFIED = "DIVIDEND_EVENT_VERIFIED"
ACTION_DIVIDEND_EVENT_DISPUTED = "DIVIDEND_EVENT_DISPUTED"
ACTION_SECURITY_ALIAS_ADDED = "SECURITY_ALIAS_ADDED"
ACTION_SECURITY_ALIAS_VERIFIED = "SECURITY_ALIAS_VERIFIED"
ACTION_SECURITY_ALIAS_QUARANTINED = "SECURITY_ALIAS_QUARANTINED"


def record(
    conn: Any,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_uid: str | None = None,
    diff: dict[str, Any] | None = None,
) -> None:
    """Write one audit row on the caller's connection.

    Takes ``conn`` rather than opening its own, and that is the whole design: sharing the
    transaction is what makes the mutation and its record atomic. A version of this that
    opened a second connection would be able to succeed while the mutation rolled back,
    producing an audit trail that describes things which never happened.

    ``diff`` must never contain a PAN, a demat number or ciphertext. Pass identifiers and
    counts; the audit log is widely readable by design, and it is not a place to leak the
    things ``investor_secret`` exists to protect.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (actor_uid, action, entity_type, entity_id, diff_json)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (actor_uid, action, entity_type, entity_id, json.dumps(diff) if diff else None),
        )


def entries_for(conn: Any, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    """Audit history for one entity, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, actor_uid, action, entity_type, entity_id, ts, diff_json
            FROM audit_log
            WHERE entity_type = %s AND entity_id = %s
            ORDER BY ts, id
            """,
            (entity_type, entity_id),
        )
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
