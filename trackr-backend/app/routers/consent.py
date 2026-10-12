"""POST /investors/{id}/consent, POST /investors/{id}/consent/withdraw,
GET /investors/{id}/consent -- the DPDP consent-first gate app/db/consent.py exists to
support.

Thin HTTP layer over ``app.db.consent``, which is already the whole implementation
(grant/withdraw semantics, the audit trail). Before this router, ``client_consent``
existed only in principle: nothing under ``app/routers/`` could populate or read it,
and ``require_consent()`` -- now wired into POST /ingest/csv, POST /ingest/email and
POST /investors/{id}/transactions -- was unreachable dead code from the outside.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from app.auth.dependencies import get_current_user, require_owned_investor
from app.database import get_conn
from app.db import consent
from app.schemas.consent import (
    ConsentGrantIn,
    ConsentGrantOut,
    ConsentStatusOut,
    ConsentWithdrawOut,
)

router = APIRouter(prefix="/investors", tags=["consent"])


@router.post(
    "/{investor_id}/consent",
    response_model=ConsentGrantOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owned_investor)],
)
def grant_consent(
    investor_id: str,
    body: ConsentGrantIn,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Record a consent grant. Auth-gated like ``routers.privacy`` -- the actor
    recording this becomes part of the evidentiary trail (``audit_log.actor_uid``),
    which matters more here than almost anywhere else in the API."""
    consent_id = consent.record_consent(
        conn,
        investor_id,
        notice_version=body.notice_version,
        evidence_ref=body.evidence_ref,
        purpose=body.purpose,
        lawful_basis=body.lawful_basis,
        actor_uid=current_user["id"],
    )
    return {
        "id": consent_id,
        "investor_id": investor_id,
        "notice_version": body.notice_version,
        "purpose": body.purpose,
        "lawful_basis": body.lawful_basis,
    }


@router.post(
    "/{investor_id}/consent/withdraw",
    response_model=ConsentWithdrawOut,
    dependencies=[Depends(require_owned_investor)],
)
def withdraw_consent(
    investor_id: str,
    purpose: str = consent.PURPOSE_PORTFOLIO_TRACKING,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Withdrawal is a right, not a request -- takes effect immediately, per
    ``app.db.consent.withdraw_consent``'s own docstring."""
    count = consent.withdraw_consent(
        conn, investor_id, purpose=purpose, actor_uid=current_user["id"]
    )
    return {"withdrawn_count": count}


@router.get(
    "/{investor_id}/consent",
    response_model=ConsentStatusOut,
    dependencies=[Depends(require_owned_investor)],
)
def consent_status(
    investor_id: str,
    purpose: str = consent.PURPOSE_PORTFOLIO_TRACKING,
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Read-only status check, owner-gated like every other endpoint under
    ``/investors/{id}/...``. Lets the frontend show current status without the
    localStorage-cache workaround ``AccountOnboarding.tsx`` needs only because no
    equivalent GET exists for accounts."""
    return {"has_live_consent": consent.has_live_consent(conn, investor_id, purpose)}
