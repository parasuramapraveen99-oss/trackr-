"""POST/GET /ops/demerger-watch/* -- the daily notification poll.

Gated TWICE, deliberately: the same ``X-Ops-Key``/``CRON_SECRET`` credential every other
ops route uses, AND ``Settings.demerger_watch_enabled``. The credential gate stops an
outsider from triggering it; the feature flag is what a deployment that does not want
the warnings turns off. That flag defaulted to false until 2026-09-22, gating the feature
on the ``_DISQUALIFYING_TERMS`` coverage audit (see app.ingest.corporate_action_feed) and
on a first look at real output; both are done and the default is now true -- see
``Settings.demerger_watch_enabled`` for the 90-day measurement behind that. A disabled
poll still returns ``skipped: true`` rather than a 404 or an error, which is what let the
cron schedule be declared in ``vercel.json`` ahead of the flip.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.ingest.demerger_watch import check_demerger_watch

ops_router = APIRouter(prefix="/ops/demerger-watch", tags=["ops"])


class DemergerWatchOut(BaseModel):
    """See ``app.ingest.demerger_watch.check_demerger_watch``. ``skipped`` is true only
    when ``Settings.demerger_watch_enabled`` is false; ``logged`` is always 0 in that
    case, not a meaningful "found nothing"."""

    skipped: bool
    logged: int


def _run(conn: Any, settings: Settings) -> dict[str, Any]:
    # Also skipped where NSE blocks this deployment: the watch reads NSE's notices API.
    if not settings.demerger_watch_enabled or not settings.nse_api_enabled:
        return {"skipped": True, "logged": 0}
    return {"skipped": False, "logged": check_demerger_watch(conn)}


@ops_router.post(
    "/check",
    response_model=DemergerWatchOut,
    dependencies=[Depends(require_ops_key)],
)
def check_demerger_watch_now(
    conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """``X-Ops-Key``-gated -- a human or a non-Vercel scheduler triggering the daily
    check directly."""
    return _run(conn, settings)


@ops_router.get(
    "/check-cron",
    response_model=DemergerWatchOut,
    dependencies=[Depends(require_cron_secret)],
)
def check_demerger_watch_cron(
    conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Same check, reachable by Vercel Cron specifically."""
    return _run(conn, settings)
