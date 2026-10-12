"""POST/GET /ops/security-alias/* -- the automated ISIN-change feed and DRAFT->VERIFIED
promotion. Mirrors app.routers.corporate_actions's refresh/refresh-cron pair exactly:
one X-Ops-Key POST for a human or a non-Vercel scheduler, one CRON_SECRET GET for
Vercel Cron specifically. See docs/DEPLOYMENT.md.

ONE JOB, NOT TWO, ON PURPOSE. ``refresh_isin_change_feed`` (app.db.isin_change_circulars)
writes DRAFT aliases; ``promote_draft_aliases`` (app.db.security_alias) is what can
actually apply one. Running them on independent schedules would leave a freshly-created
DRAFT alias sitting unpromoted until the next unrelated cron tick fires -- this endpoint
runs both, in that order, every time, so promotion follows ingestion by minutes, not by
whatever gap two independent schedules happen to leave.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends

from app.auth.ops import require_cron_secret, require_ops_key
from app.database import get_conn
from app.db import isin_change_circulars as icc_db
from app.db import security_alias as alias_db
from app.schemas.security_alias import SecurityAliasRefreshOut

ops_router = APIRouter(prefix="/ops/security-alias", tags=["ops"])

#: 10 days on a 7-day cadence, deliberately overlapping -- a late or failed previous
#: run must not create a gap a circular could fall through. Re-fetching an already-seen
#: circular is free: refresh_isin_change_feed dedupes on circular_number.
_POLL_WINDOW_DAYS = 10


def _refresh_and_promote(conn: Any) -> dict[str, Any]:
    today = date.today()
    feed_result = icc_db.refresh_isin_change_feed(
        conn, today - timedelta(days=_POLL_WINDOW_DAYS), today, actor_uid=None
    )
    promotion = alias_db.promote_draft_aliases(conn, as_of=today, actor_uid=None)
    return {
        "feed": feed_result,
        "promotion": {
            "verified": promotion.verified,
            "quarantined": promotion.quarantined,
            "inconclusive": promotion.inconclusive,
            "details": promotion.details,
        },
    }


@ops_router.post(
    "/refresh",
    response_model=SecurityAliasRefreshOut,
    dependencies=[Depends(require_ops_key)],
)
def refresh_security_aliases(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """``X-Ops-Key``-gated -- a human or a non-Vercel scheduler triggering the weekly
    job directly, same posture as ``app.routers.corporate_actions.refresh_corporate_actions``."""
    return _refresh_and_promote(conn)


@ops_router.get(
    "/refresh-cron",
    response_model=SecurityAliasRefreshOut,
    dependencies=[Depends(require_cron_secret)],
)
def refresh_security_aliases_cron(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """Same job, reachable by Vercel Cron specifically: GET, gated by ``CRON_SECRET``
    rather than ``X-Ops-Key``, since that is the only credential Vercel's scheduler
    sends and the only HTTP method it calls with."""
    return _refresh_and_promote(conn)
