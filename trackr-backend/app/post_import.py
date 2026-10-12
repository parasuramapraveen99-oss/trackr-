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
  3. ``app.reference_worker.run_once`` -- NSDL identity, NSE notices month by month
     (corporate actions AND dividends), price-verified BONUS/SPLIT, alias promotion,
     replay, gap resolution.
  4. ``app.routers.analytical.catch_up_snapshots`` -- daily value history, one day at a
     time, oldest first, each day committed.

EVERY STEP GETS A SLICE OF ONE BUDGET, in that order. Before 12 Oct 2026 the snapshot
catch-up ran unbounded between steps 2 and 3: on a fresh account it downloaded ~450 days
of bhavcopies before saving anything, Vercel killed every cycle at 300 s, and step 3 --
the only path to corporate actions, dividend history and gap closing -- never ran once.
Corporate actions now run before snapshots, so snapshots value the corrected holdings.

Run from: a background task straight after a batch is promoted; ``POST
/investors/{id}/sync``, which the import screen repeats while reference status reads
``processing``; and ``GET /ops/cycle/continue``, which a cycle that leaves work behind
calls on itself (``app.continuation``) so catch-up finishes with no page open. One cycle
at a time (``CYCLE_LOCK_KEY``): a concurrent call returns ``busy``. Every step is
idempotent and resumable, so a deep history simply takes more cycles.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import monotonic
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

#: Whole-cycle budget under Vercel's 300 s function limit. Budgets are checked between
#: items, so one slow item (a bhavcopy day, an NSE month, the worker's scheduling pass)
#: can overrun a slice: measured 12 Oct 2026, a cycle with a 240 s budget ran 286 s. The
#: ~90 s left over is that margin.
CYCLE_BUDGET_SECONDS = 210.0
#: Per-step ceilings. Prices go first but may not starve the steps after them; whatever
#: is left after the reference step goes to snapshots.
PRICE_SHARE_SECONDS = 60.0
FNO_SHARE_SECONDS = 25.0
FNO_EXPIRY_SHARE_SECONDS = 15.0
REFERENCE_SHARE_SECONDS = 60.0
#: Below this, starting another step risks overrunning the cycle.
MIN_STEP_SECONDS = 15.0
#: Session advisory lock: one cycle at a time across every caller.
CYCLE_LOCK_KEY = 7_401_064_002


def run_cycle(
    conn: Any,
    settings: Settings | None,
    budget_seconds: float = CYCLE_BUDGET_SECONDS,
    clock: Callable[[], float] = monotonic,
    include_sectors: bool = False,
) -> dict:
    """One bounded catch-up cycle. ``more_work`` says whether anything is still pending
    after it; ``progress`` whether it moved anything forward -- together they decide
    whether ``app.continuation`` starts the next cycle."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (CYCLE_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    conn.commit()
    if not acquired:
        return {"busy": True, "more_work": False, "progress": False}
    try:
        return _run_cycle_locked(conn, settings, budget_seconds, clock, include_sectors)
    finally:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (CYCLE_LOCK_KEY,))
            conn.commit()
        except Exception:  # a dead connection releases a session lock by itself
            logger.warning("post_import_cycle_unlock_failed", exc_info=True)


def _run_cycle_locked(
    conn: Any,
    settings: Settings | None,
    budget_seconds: float,
    clock: Callable[[], float],
    include_sectors: bool = False,
) -> dict:
    from app.db.imports import heal_fno_awaiting_master
    from app.prices.alerts import get_notifier
    from app.prices.history import backfill_price_history
    from app.reference_worker import run_once
    from app.routers.analytical import catch_up_snapshots

    began = clock()

    def left() -> float:
        return budget_seconds - (clock() - began)

    out: dict[str, Any] = {}
    try:
        out["prices"] = backfill_price_history(
            conn,
            notifier=get_notifier(settings),
            time_budget_seconds=min(PRICE_SHARE_SECONDS, left()),
        )
        conn.commit()
    except Exception as exc:  # a price-source outage must not stop the steps below
        conn.rollback()
        logger.warning("post_import_prices_failed", exc_info=True)
        out["prices"] = {"error": type(exc).__name__}

    if include_sectors:
        # Sectors otherwise arrive only with the next daily fundamentals cron, so a fresh
        # import showed "Unclassified" for up to a day. Once per import (hop 0), not per
        # follow-up cycle: a security with no published sector (an ETF, a retired ISIN)
        # would be refetched every few minutes for nothing.
        try:
            from app.db.sectors import refresh_sectors

            missing = _isins_missing_sector(conn)
            if missing:
                out["sectors"] = refresh_sectors(conn, missing)
                conn.commit()
        except Exception as exc:  # the allocation chart can wait; the steps below cannot
            conn.rollback()
            logger.warning("post_import_sectors_failed", exc_info=True)
            out["sectors"] = {"error": type(exc).__name__}

    try:
        out["fno"] = heal_fno_awaiting_master(
            conn,
            notifier=get_notifier(settings),
            time_budget_seconds=min(FNO_SHARE_SECONDS, max(left() - MIN_STEP_SECONDS, 0)),
        )
    except Exception as exc:  # flagged rows stay flagged; the next cycle retries them
        conn.rollback()
        logger.warning("post_import_fno_heal_failed", exc_info=True)
        out["fno"] = {"error": type(exc).__name__}

    try:
        from app.db.fno_reference import backfill_expiry_settlements

        # Expired contracts still need their expiry-day settlement price, which neither
        # the heal (trade days) nor the daily cron (7-day lookback) ever fetches.
        out["fno_expiry"] = backfill_expiry_settlements(
            conn,
            notifier=get_notifier(settings),
            time_budget_seconds=min(FNO_EXPIRY_SHARE_SECONDS, max(left() - MIN_STEP_SECONDS, 0)),
        )
    except Exception as exc:  # the expired position stays left out and named; retried
        conn.rollback()
        logger.warning("post_import_fno_expiry_failed", exc_info=True)
        out["fno_expiry"] = {"error": type(exc).__name__}

    if left() < MIN_STEP_SECONDS:
        out["reference"] = {"deferred": True}
    else:
        try:
            out["reference"] = run_once(
                conn, settings, limit=500,
                time_budget_seconds=min(REFERENCE_SHARE_SECONDS, left() - MIN_STEP_SECONDS),
            )
        except Exception as exc:  # failed tasks stay queued with backoff; report, don't raise
            conn.rollback()
            logger.warning("post_import_reference_failed", exc_info=True)
            out["reference"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    if left() < MIN_STEP_SECONDS:
        out["snapshots"] = {"deferred": True, "complete": False}
    else:
        try:
            out["snapshots"] = catch_up_snapshots(
                conn, get_notifier(settings), time_budget_seconds=left() - MIN_STEP_SECONDS
            )
        except Exception as exc:  # a snapshot failure must not hide the steps above
            conn.rollback()
            logger.warning("post_import_snapshots_failed", exc_info=True)
            out["snapshots"] = {"error": type(exc).__name__, "complete": False}

    out["more_work"] = _more_work(conn, out, settings)
    out["progress"] = _progress(out)
    return out


def _more_work(conn: Any, out: dict, settings: Settings | None = None) -> bool:
    prices = out.get("prices", {})
    prices_left = "error" in prices or (
        prices.get("pending", 0)
        > prices.get("fetched_days", 0) + prices.get("closed_days", 0)
    )
    fno_left = bool(out.get("fno", {}).get("deferred")) or bool(
        out.get("fno_expiry", {}).get("deferred")
    )
    snapshots_left = not out.get("snapshots", {}).get("complete", False)
    try:
        reference_left = _reference_work_left(conn, settings)
    except Exception:
        conn.rollback()
        logger.warning("post_import_reference_check_failed", exc_info=True)
        reference_left = False
    return prices_left or fno_left or snapshots_left or reference_left


def _progress(out: dict) -> bool:
    prices = out.get("prices", {})
    fno = out.get("fno", {})
    snapshots = out.get("snapshots", {})
    return any(
        (
            prices.get("fetched_days", 0),
            prices.get("closed_days", 0),
            fno.get("healed", 0),
            fno.get("still_rejected", 0),
            out.get("fno_expiry", {}).get("fetched_days", 0),
            out.get("reference", {}).get("processed", 0),
            snapshots.get("snapshots_written", 0),
            snapshots.get("price_dates_fetched", 0),
        )
    )


def _isins_missing_sector(conn: Any) -> list[str]:
    """Held securities with no sector on file."""
    from app.db.prices import held_isins

    held = held_isins(conn)
    if not held:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin FROM security WHERE isin = ANY(%s) AND sector IS NULL", (held,)
        )
        missing = [r[0] for r in cur.fetchall()]
    conn.commit()
    return missing


def _reference_work_left(conn: Any, settings: Settings | None = None) -> bool:
    """Reference work THIS deployment can run now, or an import not yet turned into
    tasks. NSE-API tasks do not count where ``nse_api_enabled`` is false: they wait for
    ``app.nse_worker``, and counting them would keep a chain alive that cannot move them."""
    from app.reference_worker import NSE_TASK_KINDS

    nse_enabled = getattr(settings, "nse_api_enabled", True)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS (SELECT 1 FROM reference_sync_task
                   WHERE state IN ('pending','retry','running') AND available_at <= now()
                     AND (%s OR kind <> ALL(%s)))
               OR EXISTS (SELECT 1 FROM reference_sync_import
                   WHERE scheduled_at IS NULL OR requested_at > scheduled_at)""",
            (nse_enabled, list(NSE_TASK_KINDS)),
        )
        found = bool(cur.fetchone()[0])
    conn.commit()
    return found


def run_cycle_in_own_connection(settings: Settings, hop: int = 0) -> None:
    """Background-task entry point: the request's connection is gone by the time this
    runs, so it opens its own against the same configured database. When work is left
    and this cycle moved something forward, it starts the next cycle itself."""
    from app.continuation import continue_if_needed
    from app.db.connection import connect

    try:
        with connect(settings.database_url) as conn:
            result = run_cycle(conn, settings, include_sectors=hop == 0)
        logger.info("post_import_cycle hop=%s result=%s", hop, str(result)[:500])
    except Exception:
        logger.warning("post_import_cycle_failed", exc_info=True)
        return
    if not result.get("busy"):
        continue_if_needed(
            settings, more_work=result["more_work"], progress=result["progress"], hop=hop
        )
