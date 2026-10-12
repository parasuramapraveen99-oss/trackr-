"""Make an import usable in minutes, not after tomorrow's cron.

WHY. Every pipeline that turns a fresh import into correct analytics -- price history for
newly held ISINs, the NSE corporate-action sweep, ISIN-change links, account replay -- ran
only from once-a-day crons (Vercel Hobby cannot schedule more often). A user who imported
contract notes saw unpriced holdings and 409s until the next day. Owner decision
2026-10-08: the user imports and analytics work, with no further step.

HOW. One bounded cycle of the SAME jobs the crons run -- nothing here is a second
implementation of either:

  1. ``app.prices.history.backfill_price_history`` -- newest unsettled trading days first,
     so today's valuation lands before deep history; resumable per (security, date).
  2. ``app.db.imports.heal_fno_awaiting_master`` -- F&O rows rejected only because the
     F&O contract master lacked their day: backfill exactly those days from the exchange
     F&O bhavcopy and re-resolve the rows (owner amendment, 8 Oct 2026). A no-op when
     nothing is waiting.
  3. ``app.reference_worker.run_once`` with the remaining time budget -- NSDL identity,
     NSE notices month by month, price-verified BONUS/SPLIT, alias promotion, replay.

Run from two places: a background task straight after a batch is promoted, and
``POST /investors/{id}/sync``, which the import screen repeats while reference status
reads ``processing``. Both are bounded under Vercel's 300 s function limit, so a deep
history simply takes a few cycles; every cycle is idempotent and the worker's advisory
lock makes a concurrent cycle return ``busy`` rather than double-process.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

#: Whole-cycle budget, under Vercel's 300 s function limit with room for the response.
CYCLE_BUDGET_SECONDS = 240.0
#: Prices go first but may not starve the corporate-action sweep of the whole cycle.
PRICE_SHARE_SECONDS = 120.0
#: Below this, starting a reference task risks overrunning the cycle.
MIN_REFERENCE_SECONDS = 15.0


def run_cycle(conn: Any, settings: Settings, budget_seconds: float = CYCLE_BUDGET_SECONDS) -> dict:
    from app.prices.alerts import get_notifier
    from app.prices.history import backfill_price_history
    from app.reference_worker import run_once

    began = time.monotonic()
    out: dict[str, Any] = {}
    try:
        out["prices"] = backfill_price_history(
            conn,
            notifier=get_notifier(settings),
            time_budget_seconds=min(PRICE_SHARE_SECONDS, budget_seconds),
        )
        conn.commit()
    except Exception as exc:  # a price-source outage must not stop the sweep below
        conn.rollback()
        logger.warning("post_import_prices_failed", exc_info=True)
        out["prices"] = {"error": type(exc).__name__}

    # Right after an import the investor's value history should not wait for the next
    # scheduled run: build any missing days now from whatever is settled.
    try:
        from app.routers.analytical import catch_up_snapshots

        out["snapshots"] = catch_up_snapshots(conn, get_notifier(settings))
        conn.commit()
    except Exception as exc:  # a snapshot failure must not stop the sweep below
        conn.rollback()
        logger.warning("post_import_snapshots_failed", exc_info=True)
        out["snapshots"] = {"error": type(exc).__name__}

    try:
        from app.db.imports import heal_fno_awaiting_master

        out["fno"] = heal_fno_awaiting_master(conn, notifier=get_notifier(settings))
    except Exception as exc:  # flagged rows stay flagged; the next cycle retries them
        conn.rollback()
        logger.warning("post_import_fno_heal_failed", exc_info=True)
        out["fno"] = {"error": type(exc).__name__}

    remaining = budget_seconds - (time.monotonic() - began)
    if remaining < MIN_REFERENCE_SECONDS:
        out["reference"] = {"deferred": True}
        return out
    try:
        out["reference"] = run_once(
            conn, settings, limit=500, time_budget_seconds=remaining - MIN_REFERENCE_SECONDS
        )
    except Exception as exc:  # failed tasks stay queued with backoff; report, don't raise
        logger.warning("post_import_reference_failed", exc_info=True)
        out["reference"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return out


def run_cycle_in_own_connection(settings: Settings) -> None:
    """Background-task entry point: the request's connection is gone by the time this
    runs, so it opens its own against the same configured database."""
    from app.db.connection import connect

    try:
        with connect(settings.database_url) as conn:
            result = run_cycle(conn, settings)
        logger.info("post_import_cycle", extra={"result": str(result)[:500]})
    except Exception:
        logger.warning("post_import_cycle_failed", exc_info=True)
