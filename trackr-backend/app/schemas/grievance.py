"""DTOs for the grievance router -- POST/GET /grievances and its sub-actions.

Mirrors ``app.db.grievance``'s field names directly, the same convention every other
``schemas/*.py`` file in this package follows. Not yet reflected in
``contracts/openapi.yaml`` -- the same gap that already exists for
``app/routers/auth.py`` and ``app/routers/privacy.py``, both shipped without a contract
entry. See docs/FULL_VISION_BACKEND_ROADMAP.md section A7.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel


class GrievanceIn(BaseModel):
    """POST /grievances request body. ``investor_id`` is optional on purpose: intake
    must accept complaints from people not yet matched to an investor record, per
    docs/COMPLIANCE.md."""

    subject: str
    channel: str
    detail: str | None = None
    investor_id: str | None = None


class GrievanceOut(BaseModel):
    """POST /grievances response. ``reference`` is what goes back to the complainant --
    never ``id``, per ``app.db.grievance.new_reference``'s docstring."""

    id: str
    reference: str
    status: str
    raised_at: datetime
    due_at: datetime


class GrievanceSummary(BaseModel):
    """One row of GET /grievances, /grievances/due-soon, /grievances/overdue."""

    id: str
    reference: str
    investor_id: str | None
    subject: str
    channel: str
    status: str
    raised_at: datetime
    due_at: datetime
    time_left: timedelta


class ResolveIn(BaseModel):
    """POST /grievances/{id}/resolve request body. ``resolution`` cannot be blank --
    enforced by ``app.db.grievance.resolve`` itself (and, below that, a database CHECK
    constraint), not duplicated here."""

    resolution: str


class ResolveOut(BaseModel):
    resolved_at: datetime
    due_at: datetime
    within_ceiling: bool


class EscalateIn(BaseModel):
    reason: str
