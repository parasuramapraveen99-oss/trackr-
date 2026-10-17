"""Owner-scoped forwarding settings, processing outcomes, and retry operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.dependencies import get_current_user, require_owned_investor
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import email_ingestion as db

router = APIRouter(
    prefix="/investors/{investor_id}/email",
    tags=["email"],
    dependencies=[Depends(require_owned_investor)],
)


class AutomationIn(BaseModel):
    enabled: bool


@router.get("/settings")
def settings_view(
    investor_id: str, conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT token FROM email_inbox WHERE investor_id = %s", (investor_id,))
        row = cur.fetchone()
    active = db.mandate(conn, investor_id)
    available = settings.email_forwarding_enabled and settings.ingest_webhook_provider == "mailgun"
    return {
        "available": available,
        "address": (
            f"mail-{str(row[0]).replace('-', '')}@{settings.forwarding_domain}"
            if available and row
            else None
        ),
        "automation_enabled": bool(active),
        "policy_version": db.POLICY_VERSION,
        "mandate_id": str(active["id"]) if active else None,
        "gmail_requires_attachment": True,
    }


@router.put("/settings")
def update_settings(
    investor_id: str,
    body: AutomationIn,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict = Depends(get_current_user),
) -> dict:
    if body.enabled and not (
        settings.email_forwarding_enabled and settings.ingest_webhook_provider == "mailgun"
    ):
        raise HTTPException(409, "Mailgun forwarding is not enabled on this deployment")
    db.set_automation(conn, investor_id, body.enabled, str(current_user["id"]))
    conn.commit()
    return settings_view(investor_id, conn, settings)


@router.get("/activity")
def activity(
    investor_id: str, limit: int = Query(50, ge=1, le=200), conn: Any = Depends(get_conn)
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id, a.delivery_id, a.document_id, a.batch_id, a.status, a.detail,
                      a.updated_at, a.extraction_attempt, d.filename
               FROM email_attachment a LEFT JOIN document d ON d.id = a.document_id
               WHERE a.investor_id = %s ORDER BY a.updated_at DESC LIMIT %s""",
            (investor_id, limit),
        )
        attachments = [db.row_dict(cur, r) for r in cur.fetchall()]
        cur.execute(
            "SELECT id, status, last_error, received_at FROM email_delivery "
            "WHERE investor_id = %s ORDER BY received_at DESC LIMIT %s",
            (investor_id, limit),
        )
        deliveries = [db.row_dict(cur, r) for r in cur.fetchall()]
        cur.execute(
            "SELECT id, attachment_id, outcome, detail, created_at FROM email_import_event "
            "WHERE investor_id = %s ORDER BY created_at DESC LIMIT %s",
            (investor_id, limit),
        )
        notifications = [db.row_dict(cur, r) for r in cur.fetchall()]
        cur.execute(
            "SELECT requested_revision, completed_revision FROM email_refresh_state "
            "WHERE investor_id = %s",
            (investor_id,),
        )
        refresh = db.row_dict(cur, cur.fetchone())
    return {
        "deliveries": deliveries,
        "attachments": attachments,
        "notifications": notifications,
        "portfolio_refresh": refresh,
    }


@router.post("/retry")
def retry(investor_id: str, document_id: UUID | None = None, conn: Any = Depends(get_conn)) -> dict:
    count = db.retry_blocked(conn, investor_id, str(document_id) if document_id else None)
    conn.commit()
    return {"queued": count}


@router.get("/notifications")
def notifications(investor_id: str, conn: Any = Depends(get_conn)) -> dict:
    """Small polling surface; no document bytes, processing payloads, or ledger reads."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, outcome, detail, created_at FROM email_import_event "
            "WHERE investor_id = %s AND outcome NOT IN ('IGNORED', 'DUPLICATE') "
            "ORDER BY created_at DESC LIMIT 5",
            (investor_id,),
        )
        items = [db.row_dict(cur, r) for r in cur.fetchall()]
        cur.execute(
            "SELECT requested_revision, completed_revision FROM email_refresh_state "
            "WHERE investor_id = %s",
            (investor_id,),
        )
        revision = cur.fetchone()
    return {
        "items": items,
        "portfolio_revision": revision[1] if revision else 0,
        "pending_refresh": bool(revision and revision[0] > revision[1]),
    }


@router.post("/deliveries/{delivery_id}/retry")
def retry_delivery(investor_id: str, delivery_id: UUID, conn: Any = Depends(get_conn)) -> dict:
    if not db.retry_delivery(conn, investor_id, str(delivery_id)):
        raise HTTPException(404, "No such delivery")
    conn.commit()
    return {"queued": True}
