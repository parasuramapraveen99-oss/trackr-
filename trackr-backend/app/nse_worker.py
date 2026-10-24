"""Run every nseindia.com/api job from a machine NSE does not block.

    python -m app.nse_worker --env-file .env.vercel            # from trackr-backend/
    python -m app.nse_worker --env-file .env.vercel --days 10  # after a longer gap

WHY. NSE answers 403 to cloud data-centre IPs; Vercel's are among them (measured 12 Oct
2026: every NSE month task failed there, while the same request from an Indian home
connection succeeded). A deployment with ``NSE_API_ENABLED=false`` therefore leaves its
NSE work pending -- the month-by-month history sweep (corporate actions AND dividend
history), gap resolution, and the daily notices -- and this command does it, writing to
the same database. Once a day is enough: NSE publishes notices daily, and a new user's
history simply waits for the next run (excluded and named, never guessed).

WHAT ONE RUN DOES, in order:
  1. Re-opens what the blocked deployment shut: the ISIN-change feed's circuit breaker
     (tripped by 403s, not by an NSE outage) and the backoff on NSE/GAP tasks.
  2. The daily feeds for the last ``--days`` days: corporate actions, dividend notices,
     ISIN-change circulars + alias promotion, the demerger watch, dividend reconcile,
     and stock sectors (NSE list, BSE for the rest).
  3. The reference worker until its queue is empty or ``--max-minutes`` is spent: NSE
     months, GAP resolution, and whatever replays/verifications they unlock.
Everything is the same code the crons and the worker run; nothing here is a second
implementation. Safe to stop at any time and run again: every step is resumable.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from app.config import Settings

logger = logging.getLogger("nse_worker")

#: The ISIN-change feed's breaker key (app.db.isin_change_circulars.FEED_NAME).
_BREAKER_FEED = "nse_isin_change_circulars"
#: Pause before retrying while the deployment's own cycle holds the worker lock.
_BUSY_WAIT_SECONDS = 20
#: Consecutive lost connections tolerated before giving up (the run can be repeated).
_MAX_RECONNECTS = 5


def load_settings(env_file: str) -> Settings:
    """The deployment's settings, with the NSE API always enabled -- this machine is the
    one that can reach it, whatever the deployment's own file says."""
    return Settings(_env_file=env_file, nse_api_enabled=True)


def reopen(conn: Any) -> None:
    """Undo what the blocked deployment's 403s did: close the ISIN-change breaker and
    make NSE/GAP tasks in backoff runnable now."""
    from app.reference_worker import NSE_TASK_KINDS

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE feed_poll_state SET consecutive_failures = 0, breaker_open_until = NULL "
            "WHERE feed = %s",
            (_BREAKER_FEED,),
        )
        cur.execute(
            "UPDATE reference_sync_task SET available_at = now() "
            "WHERE kind = ANY(%s) AND state IN ('pending','retry')",
            (list(NSE_TASK_KINDS),),
        )
    conn.commit()


def daily_feeds(conn: Any, settings: Settings, days: int) -> dict[str, Any]:
    from app.db import exchange_financials
    from app.db.dividend_reconcile import reconcile_pending_dividends
    from app.db.prices import held_isins
    from app.db.sectors import refresh_sectors
    from app.db.universe import current_universe_isins
    from app.routers import corporate_actions, demerger_watch, dividends_ops, security_alias

    end = date.today()
    start = end - timedelta(days=days)
    steps = {
        "corporate_actions": lambda: corporate_actions._refresh_corporate_actions(
            conn, start, end, settings
        ),
        "dividend_notices": lambda: dividends_ops._refresh_dividends(conn, start, end, settings),
        "isin_changes": lambda: security_alias._refresh_and_promote(conn, nse_api_enabled=True),
        "demerger_watch": lambda: demerger_watch._run(conn, settings),
        "dividend_reconcile": lambda: reconcile_pending_dividends(conn),
        # Same job as the fundamentals cron's sector step. Its BSE fallback is an
        # api.bseindia.com call a blocked deployment may not reach either.
        "sectors": lambda: refresh_sectors(conn, held_isins(conn)),
        # Year-end results (XBRL) for the health rubric: held stocks and ranking peers.
        "exchange_financials": lambda: exchange_financials.refresh(
            conn,
            exchange_financials.stale_symbols(
                conn, sorted({*held_isins(conn), *current_universe_isins(conn)})
            ),
        ),
    }
    out: dict[str, Any] = {}
    for name, step in steps.items():
        try:
            out[name] = step()
            conn.commit()
        except Exception as exc:  # one feed's failure must not stop the others
            conn.rollback()
            logger.warning("nse_worker %s failed", name, exc_info=True)
            out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        print(f"  {name}: {out[name]}", flush=True)
    return out


def drain(connect: Callable[[], Any], settings: Settings, max_minutes: float) -> dict[str, int]:
    """Run the reference queue, one fresh connection per batch. A long NSE task (one
    month: up to 10 circular PDFs plus two notice feeds) can outlive a hosted database's
    connection -- measured 12 Oct 2026, Neon closed one mid-run -- so a lost connection
    is reconnected and the batch retried, up to ``_MAX_RECONNECTS`` in a row. A task
    caught mid-run stays 'running' and is reset to pending by the next batch."""
    import psycopg

    from app.reference_worker import run_once

    deadline = time.monotonic() + max_minutes * 60
    totals = {"processed": 0, "failed": 0, "waiting": 0}
    reconnects = 0
    while time.monotonic() < deadline:
        left = deadline - time.monotonic()
        try:
            with connect() as conn:
                # Every batch, not once per run: a deployment still calling NSE (its
                # NSE_API_ENABLED not yet false) re-trips the breaker within minutes.
                reopen(conn)
                result = run_once(conn, settings, limit=500, time_budget_seconds=min(240.0, left))
        except psycopg.OperationalError as exc:
            reconnects += 1
            print(f"  connection lost ({str(exc).splitlines()[0]}); reconnecting", flush=True)
            if reconnects > _MAX_RECONNECTS:
                raise
            time.sleep(_BUSY_WAIT_SECONDS)
            continue
        reconnects = 0
        if result.get("busy"):
            print("  worker busy (the deployment's cycle holds it); waiting", flush=True)
            time.sleep(_BUSY_WAIT_SECONDS)
            continue
        for key in totals:
            totals[key] += result.get(key, 0)
        print(f"  batch: {result}", flush=True)
        if not result.get("processed"):
            break  # nothing runnable now (done, or only tasks in backoff remain)
    return totals


def summary(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, state, count(*) FROM reference_sync_task GROUP BY 1,2 ORDER BY 1,2"
        )
        tasks = cur.fetchall()
        cur.execute("SELECT count(*) FROM corporate_action")
        actions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dividend_event")
        dividends = cur.fetchone()[0]
    conn.commit()
    print(f"corporate actions on file: {actions}; dividend events on file: {dividends}")
    for kind, state, count in tasks:
        print(f"  tasks {kind:<9} {state:<8} {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.nse_worker", description=__doc__)
    parser.add_argument("--env-file", default=".env.vercel")
    parser.add_argument("--days", type=int, default=7, help="daily-feed window (default 7)")
    parser.add_argument("--max-minutes", type=float, default=60.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    from app.db.connection import connect

    settings = load_settings(args.env_file)
    with connect(settings.database_url) as conn:
        print("1/3 re-opening NSE work shut by the blocked deployment", flush=True)
        reopen(conn)
        print(f"2/3 daily NSE feeds, last {args.days} days", flush=True)
        daily_feeds(conn, settings, args.days)
    print(f"3/3 reference queue (up to {args.max_minutes:g} min)", flush=True)
    totals = drain(lambda: connect(settings.database_url), settings, args.max_minutes)
    print(f"done: {totals}")
    with connect(settings.database_url) as conn:
        summary(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
