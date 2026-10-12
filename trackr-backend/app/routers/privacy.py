"""GET /privacy/export, POST /privacy/erase -- the DPDP data-principal rights.

Thin HTTP layer over ``app/db/privacy.py``, which is already the whole implementation
(export and erasure semantics, legal-hold enforcement, the audit trail). Both endpoints
require auth: this is the most sensitive personal data in the system, and erasure is
irreversible by design (the ledger's append-only trigger has exactly one escape hatch
for it -- see 001_core.sql).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_current_user, require_owned_investor
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import privacy
from app.storage.factory import get_store

router = APIRouter(prefix="/privacy", tags=["privacy"])


@router.get("/export", dependencies=[Depends(require_owned_investor)])
def export_investor(
    investor_id: str = Query(...),
    include_identifiers: bool = Query(default=False),
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Everything held about one investor -- the DPDP portability right. Decrypting PAN
    and demat is off by default; turning it on is its own audited access event."""
    return privacy.export_investor(
        conn, investor_id, include_identifiers=include_identifiers, actor_uid=current_user["id"]
    )


@router.post("/erase", dependencies=[Depends(require_owned_investor)])
def erase_investor(
    investor_id: str = Query(...),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, int]:
    """Delete everything about one investor. Refuses while a legal hold is open --
    see ``db.privacy.LegalHoldError``, mapped to 409 in app/main.py."""
    store = get_store(settings)
    return privacy.erase_investor(conn, investor_id, actor_uid=current_user["id"], store=store)
