"""Durable Mailgun receipts, standing import mandates, and user-visible outcomes.

All mutations use the caller's transaction. Queue payloads contain ids only; delivery
bytes are encrypted in the temporary Postgres spool, then archived by the worker.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db import audit, consent, crypto
from app.jobs import queue

POLICY_VERSION = "email-deterministic-v1"
SERVICE_ACTOR = "9f35fb4b-7526-51a8-84db-0c02a518304e"
KIND_DELIVERY = "email_delivery"
KIND_DOCUMENT = "email_document"
KIND_REFRESH = "email_portfolio_refresh"


class SpoolCapacityError(RuntimeError):
    """No durable space for a new delivery; the provider must retry later."""


def row_dict(cur: Any, row: Any) -> dict[str, Any] | None:
    return dict(zip([c.name for c in cur.description], row, strict=True)) if row else None


def inbox(conn: Any, investor_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO email_inbox(investor_id) VALUES (%s) "
            "ON CONFLICT (investor_id) DO UPDATE SET investor_id = EXCLUDED.investor_id "
            "RETURNING token",
            (investor_id,),
        )
        return str(cur.fetchone()[0]).replace("-", "")


def mandate(conn: Any, investor_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, policy_version, authorized_by, granted_at FROM email_import_mandate "
            "WHERE investor_id = %s AND revoked_at IS NULL" + (" FOR SHARE" if lock else ""),
            (investor_id,),
        )
        return row_dict(cur, cur.fetchone())


def set_automation(conn: Any, investor_id: str, enabled: bool, actor_uid: str) -> None:
    # Serialize grants/revocations, including the first grant when no mandate exists.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM investor WHERE id = %s FOR UPDATE", (investor_id,))
        if cur.fetchone() is None:
            raise LookupError("No such investor")
        if enabled:
            consent.require_consent(conn, investor_id)
        active = mandate(conn, investor_id)
        if enabled and not active:
            cur.execute(
                "INSERT INTO email_import_mandate(investor_id, policy_version, authorized_by) "
                "VALUES (%s, %s, %s)",
                (investor_id, POLICY_VERSION, actor_uid),
            )
        elif not enabled and active:
            cur.execute(
                "UPDATE email_import_mandate SET revoked_at = now() WHERE id = %s", (active["id"],)
            )
    inbox(conn, investor_id)
    if enabled:
        retry_blocked(conn, investor_id)
    audit.record(
        conn,
        "EMAIL_AUTOMATION_CHANGED",
        "investor",
        investor_id,
        actor_uid,
        {"enabled": enabled, "policy_version": POLICY_VERSION},
    )


def receive(
    conn: Any,
    investor_id: str,
    recipient: str,
    snapshot: dict[str, Any],
    max_pending_bytes: int = 67_108_864,
) -> dict:
    consent.require_consent(conn, investor_id)
    plaintext = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"email-intake:{investor_id}",),
        )
        cur.execute(
            "SELECT id, status FROM email_delivery WHERE investor_id = %s AND sha256 = %s",
            (investor_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            delivery_id, status = existing
            cur.execute(
                "SELECT id FROM job WHERE work_key = %s AND status IN ('PENDING', 'RUNNING')",
                (f"email-delivery:{delivery_id}",),
            )
            active = cur.fetchone()
            # A retry must not wait behind extraction/object-storage work holding the
            # receipt row. These bytes are already durable, so no write is necessary.
            if status == "ARCHIVED" or active:
                return {"delivery_id": str(delivery_id), "sha256": digest, "status": status}
            cur.execute(
                "UPDATE email_delivery SET status = 'PENDING', last_error = NULL WHERE id = %s",
                (delivery_id,),
            )
            queue.enqueue(
                conn,
                KIND_DELIVERY,
                {"delivery_id": str(delivery_id)},
                investor_id=investor_id,
                max_attempts=8,
                work_key=f"email-delivery:{delivery_id}",
            )
            return {"delivery_id": str(delivery_id), "sha256": digest, "status": "PENDING"}
        encrypted = crypto.encrypt(plaintext)
        cur.execute(
            "SELECT COALESCE(sum(octet_length(payload_enc)), 0) "
            "FROM email_delivery WHERE investor_id = %s",
            (investor_id,),
        )
        if cur.fetchone()[0] + len(encrypted) > max_pending_bytes:
            raise SpoolCapacityError("Email processing backlog is full; retry delivery later")
        cur.execute(
            """INSERT INTO email_delivery(investor_id, sha256, recipient, payload_enc)
               VALUES (%s, %s, %s, %s)
               RETURNING id, status""",
            (investor_id, digest, recipient, encrypted),
        )
        delivery_id, status = cur.fetchone()
        if status != "ARCHIVED":
            cur.execute(
                "UPDATE email_delivery SET status = 'PENDING', last_error = NULL WHERE id = %s",
                (delivery_id,),
            )
            queue.enqueue(
                conn,
                KIND_DELIVERY,
                {"delivery_id": str(delivery_id)},
                investor_id=investor_id,
                max_attempts=8,
                work_key=f"email-delivery:{delivery_id}",
            )
    return {"delivery_id": str(delivery_id), "sha256": digest, "status": status}


def outcome(
    conn: Any,
    attachment_id: str,
    status: str,
    detail: str,
    batch_id: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE email_attachment SET status = %s, detail = %s, "
            "batch_id = COALESCE(%s, batch_id), updated_at = now() WHERE id = %s "
            "RETURNING investor_id",
            (status, detail[:1000], batch_id, attachment_id),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "INSERT INTO email_import_event(investor_id, attachment_id, outcome, detail) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (attachment_id, outcome) DO NOTHING",
                (row[0], attachment_id, status, detail[:1000]),
            )


def retry_delivery(conn: Any, investor_id: str, delivery_id: str) -> bool:
    consent.require_consent(conn, investor_id)
    # Avoid updating a receipt that a live extraction job already owns.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM email_delivery WHERE id = %s AND investor_id = %s",
            (delivery_id, investor_id),
        )
        if not cur.fetchone():
            return False
        cur.execute(
            "SELECT id FROM job WHERE work_key = %s AND status IN ('PENDING', 'RUNNING')",
            (f"email-delivery:{delivery_id}",),
        )
        if cur.fetchone():
            return True
        cur.execute(
            "UPDATE email_delivery SET status = 'PENDING', last_error = NULL "
            "WHERE id = %s AND investor_id = %s",
            (delivery_id, investor_id),
        )
    queue.enqueue(
        conn,
        KIND_DELIVERY,
        {"delivery_id": delivery_id},
        investor_id=investor_id,
        max_attempts=8,
        work_key=f"email-delivery:{delivery_id}",
    )
    return True


def request_refresh(conn: Any, investor_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO email_refresh_state(investor_id) VALUES (%s) "
            "ON CONFLICT (investor_id) DO UPDATE "
            "SET requested_revision = email_refresh_state.requested_revision + 1",
            (investor_id,),
        )
    queue.enqueue(
        conn,
        KIND_REFRESH,
        {"investor_id": investor_id},
        investor_id=investor_id,
        work_key=f"email-refresh:{investor_id}",
        max_attempts=8,
    )


def overlap_problem(conn: Any, batch_id: str, account_id: str) -> str:
    """Revised/conflicting same-source fills need correction, not a duplicate skip."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM import_row WHERE batch_id = %s AND status = 'STAGED' "
            "GROUP BY segment, dedupe_key HAVING count(*) > 1 LIMIT 1",
            (batch_id,),
        )
        if cur.fetchone():
            return "Repeated trade identifiers need review before automatic import"
        for segment, table, key, day, side, instrument in (
            ("EQ", "transaction", "transaction", "txn_date", "txn_type", "isin"),
            ("FNO", "fno_trade", "fno_trade", "trade_date", "side", "contract_id"),
        ):
            # Identifiers are closed constants, never caller/model strings.
            cur.execute(
                f"""SELECT 1 FROM import_row r JOIN {table} t
                    ON t.account_id = %s
                   AND t.source_ref = r.payload->'{key}'->>'source_ref'
                   AND t.{day} = (r.payload->'{key}'->>'{day}')::date
                   AND t.{side}::text = r.payload->'{key}'->>'{side}'
                   AND t.source::text = r.payload->'{key}'->>'source'
                  WHERE r.batch_id = %s AND r.status = 'STAGED' AND r.segment = %s
                    AND t.reverses_id IS NULL
                    AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.reverses_id = t.id)
                    AND (t.{instrument}::text, t.quantity, t.price, t.charges, t.stt)
                        IS DISTINCT FROM
                        (r.payload->'{key}'->>'{instrument}',
                         (r.payload->'{key}'->>'quantity')::numeric,
                         (r.payload->'{key}'->>'price')::numeric,
                         (r.payload->'{key}'->>'charges')::numeric,
                         (r.payload->'{key}'->>'stt')::numeric)
                  LIMIT 1""",
                (account_id, batch_id, segment),
            )
            if cur.fetchone():
                return "This statement changes an existing trade; a linked correction needs review"
    return ""


def retry_blocked(conn: Any, investor_id: str, document_id: str | None = None) -> int:
    """Explicit replay after a parser/password change; original bytes are reused."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id FROM email_attachment a
               WHERE a.investor_id = %s AND a.document_id IS NOT NULL
                 AND a.status IN ('WAITING_PASSWORD', 'WAITING_REFERENCE',
                                  'NEEDS_ATTENTION', 'UNSUPPORTED', 'FAILED')
                 AND (%s::uuid IS NULL OR a.document_id = %s)
               ORDER BY a.updated_at FOR UPDATE""",
            (investor_id, document_id, document_id),
        )
        ids = [str(r[0]) for r in cur.fetchall()]
        if ids:
            consent.require_consent(conn, investor_id)
        for attachment_id in ids:
            queue.enqueue(
                conn,
                KIND_DOCUMENT,
                {"attachment_id": attachment_id},
                investor_id=investor_id,
                max_attempts=8,
                work_key=f"email-attachment:{attachment_id}",
            )
            cur.execute(
                "UPDATE email_attachment SET status = 'PENDING', updated_at = now() WHERE id = %s",
                (attachment_id,),
            )
    return len(ids)
