"""Start the next catch-up cycle without anyone waiting for it.

WHY. A fresh account needs tens of bounded cycles (``app.post_import``) to catch up:
price history, F&O contract days, the NSE month-by-month sweep that carries corporate
actions and dividend history, gap resolution, daily snapshots. On Vercel Hobby a cron can
fire only once a day and the import screen calls ``/sync`` only while it is open, so
without this a new user's analytics took days to settle -- or never, when nobody kept the
page open.

HOW. When a cycle ends with work left AND it moved something forward, it calls
``GET /ops/cycle/continue?hop=N+1`` on this same deployment, authenticated with
``CRON_SECRET``. That route answers 202 at once and runs the next cycle as a background
task (the same mechanism a promote already uses), so this request never waits for it.

The chain stops on its own: no work left, no progress (an exchange outage would otherwise
loop), or ``MAX_HOPS``. The daily crons remain the backstop and restart a stopped chain.
A failed send is logged and never raised: the import that triggered it has already
succeeded, and the next cron or ``/sync`` call resumes the work.
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: ~4 minutes per cycle, so about four hours of continuous catch-up before the chain
#: hands back to the daily crons.
MAX_HOPS = 60
#: The continue route answers 202 before doing any work; this only bounds a hung network.
SEND_TIMEOUT_SECONDS = 10.0


def continue_if_needed(settings: Any, *, more_work: bool, progress: bool, hop: int) -> bool:
    """Start cycle ``hop + 1`` when warranted. Returns whether a request was sent."""
    if not (more_work and progress):
        return False
    if hop >= MAX_HOPS:
        logger.info("cycle_chain_stopped_at_hop_cap hop=%s", hop)
        return False
    base = _base_url(settings)
    secret = getattr(settings, "cron_secret", None)
    if not base or not secret:
        logger.info("cycle_chain_not_configured")
        return False
    url = f"{base}/ops/cycle/continue?hop={hop + 1}"
    try:
        _send(url, secret)
    except Exception:
        logger.warning("cycle_chain_send_failed hop=%s", hop + 1, exc_info=True)
        return False
    return True


def follow_up(settings: Any, path_and_query: str) -> bool:
    """Call ``path_and_query`` on this deployment with ``CRON_SECRET`` -- for a cron job
    that stopped at its time budget and continues in a fresh call (the weekly Yahoo
    dividend sweep). Returns whether a request was sent; never raises."""
    base = _base_url(settings)
    secret = getattr(settings, "cron_secret", None)
    if not base or not secret:
        logger.info("follow_up_not_configured %s", path_and_query)
        return False
    try:
        _send(f"{base}{path_and_query}", secret)
    except Exception:
        logger.warning("follow_up_send_failed %s", path_and_query, exc_info=True)
        return False
    return True


def _base_url(settings: Any) -> str | None:
    explicit = getattr(settings, "self_base_url", None)
    if explicit:
        return explicit.rstrip("/")
    # Set by Vercel on every deployment: the project's production domain, no scheme.
    host = getattr(settings, "vercel_project_production_url", None)
    return f"https://{host.strip('/')}" if host else None


def _send(url: str, secret: str) -> None:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret}"})
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:  # noqa: S310
        response.read()
