"""DTOs for POST/GET /investors/{id}/consent[/withdraw] -- DPDP consent capture.

Mirrors ``schemas/investors.py``'s convention: hand-written, field for field, rather
than a shared components/schemas section.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.db.consent import BASIS_CONSENT, PURPOSE_PORTFOLIO_TRACKING


class ConsentGrantIn(BaseModel):
    """POST /investors/{id}/consent request body.

    ``purpose``/``lawful_basis`` default to the only values Phase 1 has, per
    ``app.db.consent`` -- not hardcoded here twice, imported from the one place they're
    defined.
    """

    notice_version: str
    evidence_ref: str
    purpose: str = PURPOSE_PORTFOLIO_TRACKING
    lawful_basis: str = BASIS_CONSENT


class ConsentGrantOut(BaseModel):
    id: str
    investor_id: str
    notice_version: str
    purpose: str
    lawful_basis: str


class ConsentWithdrawOut(BaseModel):
    withdrawn_count: int


class ConsentStatusOut(BaseModel):
    has_live_consent: bool
