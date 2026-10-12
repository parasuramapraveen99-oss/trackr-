"""GET /brokers -- the one registry three screens used to each invent for themselves.

The onboarding wizard's broker picker, the Import screen's format labels and the
"where do I find these files" help all need the same answer: which brokers exist, what
can we actually read from each, and does email forwarding work for them here. Only the
backend knows -- the adapters live here -- so only the backend answers.

AUTHENTICATED, though it carries no personal data. Nothing in the response belongs to an
investor, but ``forwarding`` reflects this deployment's mail configuration, and there is
no reason for that to be readable by anyone who is not signed in.

See app/brokers.py for how each capability is derived, and why nothing here may be
declared optimistically.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.brokers import registry
from app.config import Settings, get_settings
from app.schemas.investors import BrokerOut

router = APIRouter(tags=["onboarding"])


@router.get("/brokers", response_model=list[BrokerOut])
def list_brokers(
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Every broker the picker may offer, with honest capability flags."""
    return registry(settings.forwarding_domain)  # type: ignore[return-value]
