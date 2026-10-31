"""The full-market close store: every bhavcopy row kept, `price` filled from it in SQL.

WHY. An import must never wait on a download (owner decision 2026-10-31,
docs/INSTANT_IMPORT_PLAN.md). ``price`` keeps only ledger + ranking-universe ISINs, so a
stock nobody had held before used to need every past bhavcopy fetched again. Here each
stored day keeps the whole merged file (``app.prices.pipe.merge``: NSE over BSE) in
``market_close``, and ``hydrate_prices`` copies what the ledger needs into ``price`` and
``price_absent`` with two statements.

THE RULES ARE ``app.prices.history.store_backfilled_days``' RULES, in SQL:

- only exchange-routed ISINs (a mutual fund is never in a bhavcopy);
- an absence only from a day on which BOTH exchanges answered and rows came back
  (``market_close_day.complete``), and only from the ISIN's first transaction on;
- an existing ``price`` row is never overwritten here. Corrections arrive through the
  normal store path, which updates both tables.

Collection (``backfill_market_history`` and the CLI below) is the only part that touches
the network, and only inside ``archive_collection`` or the laptop collector's context.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from app.prices.alerts import Notifier
from app.prices.pipe import NSE_BHAVCOPY_EARLIEST, PipeResult, backfill_days, trading_days

logger = logging.getLogger(__name__)

#: The ``security`` / ``market_close`` ISIN check. A row the database would reject is
#: skipped and logged rather than failing the whole day.
_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")
DEFAULT_HISTORY_START = date(2016, 1, 1)
#: Calendar days of recent closes copied for a just-imported ISIN before the background
#: cycle runs, so Holdings can value it on the next screen.
RECENT_DAYS = 10


def record_market_day(conn: Any, result: PipeResult) -> int:
    """Keep a fetched day's whole merged file. Returns rows written.

    Nothing is recorded for a day with no rows (a holiday or a failed fetch: absence of
    data is not data). A complete day is never replaced by an incomplete re-fetch, whose
    merge could put a BSE close where NSE's belongs. An incomplete day adds only rows it
    does not already have; a complete day overwrites, carrying a republished correction.
    """
    rows = [p for p in result.prices if _ISIN.match(p.isin) and p.close > 0]
    skipped = len(result.prices) - len(rows)
    if skipped:
        logger.warning(
            "market_close_rows_skipped",
            extra={"price_date": str(result.price_date), "skipped": skipped},
        )
    if not rows:
        return 0
    complete = bool(result.fetched) and result.succeeded
    with conn.cursor() as cur:
        cur.execute(
            "SELECT complete FROM market_close_day WHERE price_date = %s", (result.price_date,)
        )
        existing = cur.fetchone()
        if existing is not None and existing[0] and not complete:
            return 0
        cur.execute(
            """INSERT INTO market_close_day (price_date, fetched, failures, not_published, complete)
               VALUES (%s, %s::jsonb, %s::jsonb, %s, %s)
               ON CONFLICT (price_date) DO UPDATE SET fetched = EXCLUDED.fetched,
                 failures = EXCLUDED.failures, not_published = EXCLUDED.not_published,
                 complete = EXCLUDED.complete, recorded_at = now()""",
            (
                result.price_date,
                json.dumps(result.fetched),
                json.dumps(result.failures),
                list(result.not_published),
                complete,
            ),
        )
        conflict = (
            "DO UPDATE SET close = EXCLUDED.close, source = EXCLUDED.source"
            if complete
            else "DO NOTHING"
        )
        cur.executemany(
            "INSERT INTO market_close (isin, price_date, close, source) VALUES (%s, %s, %s, %s) "
            f"ON CONFLICT (isin, price_date) {conflict}",
            [(p.isin, p.price_date, p.close, p.source) for p in rows],
        )
    return len(rows)


def complete_market_days(conn: Any, start: date, end: date) -> set[date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT price_date FROM market_close_day WHERE complete "
            "AND price_date BETWEEN %s AND %s",
            (start, end),
        )
        return {row[0] for row in cur.fetchall()}


def hydrate_prices(conn: Any, days: Iterable[date]) -> dict[str, Any]:
    """Answer ``days`` for every exchange-routed ledger ISIN from ``market_close``.

    Returns ``settled_days``: the given days that are complete in ``market_close_day``.
    After this call every wanted ISIN has a price or a recorded absence on each of them,
    so a backfill need not fetch them. Other days get any stored prices but stay
    unsettled.
    """
    from app.db.fund_reference import non_exchange_isins

    days = sorted(set(days))
    if not days:
        return {"prices": 0, "absences": 0, "settled_days": set()}
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT isin FROM transaction")
        off_exchange = non_exchange_isins(conn, [row[0] for row in cur.fetchall()])
        wanted = """wanted AS (SELECT isin, min(txn_date) AS first_held FROM transaction
                    WHERE isin <> ALL(%(off)s) GROUP BY isin)"""
        cur.execute(
            f"""WITH {wanted}
                INSERT INTO price (isin, price_date, close, source)
                SELECT m.isin, m.price_date, m.close, m.source
                FROM market_close m JOIN wanted w ON w.isin = m.isin
                WHERE m.price_date = ANY(%(days)s) AND m.price_date >= w.first_held
                ON CONFLICT (isin, price_date) DO NOTHING""",
            {"off": off_exchange, "days": days},
        )
        prices = cur.rowcount
        cur.execute(
            f"""WITH {wanted}
                INSERT INTO price_absent (isin, price_date)
                SELECT w.isin, d.price_date
                FROM market_close_day d JOIN wanted w ON d.price_date >= w.first_held
                WHERE d.complete AND d.price_date = ANY(%(days)s)
                  AND NOT EXISTS (SELECT 1 FROM market_close m
                                  WHERE m.isin = w.isin AND m.price_date = d.price_date)
                  AND NOT EXISTS (SELECT 1 FROM price p
                                  WHERE p.isin = w.isin AND p.price_date = d.price_date)
                ON CONFLICT (isin, price_date) DO NOTHING""",
            {"off": off_exchange, "days": days},
        )
        absences = cur.rowcount
        cur.execute(
            "SELECT price_date FROM market_close_day WHERE complete AND price_date = ANY(%s)",
            (days,),
        )
        settled = {row[0] for row in cur.fetchall()}
    return {"prices": prices, "absences": absences, "settled_days": settled}


def hydrate_isins(conn: Any, isins: Iterable[str], start: date, end: date) -> int:
    """Copy stored closes for ``isins`` over ``[start, end]`` into ``price``. Prices only:
    no absence is recorded, so this never settles a day. For a just-imported batch and
    for the ranking universe."""
    isins = sorted({i for i in isins if _ISIN.match(i)})
    if not isins:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO price (isin, price_date, close, source)
               SELECT m.isin, m.price_date, m.close, m.source FROM market_close m
               JOIN security s ON s.isin = m.isin
               WHERE m.isin = ANY(%s) AND m.price_date BETWEEN %s AND %s
               ON CONFLICT (isin, price_date) DO NOTHING""",
            (isins, start, end),
        )
        return cur.rowcount


def hydrate_batch(conn: Any, batch_id: str, today: date | None = None) -> int:
    """Recent closes for the ISINs a just-promoted batch carries, from the database.

    Runs in the promote request so the next screen can value a stock nobody held before.
    Database only. The background cycle then answers the full history."""
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT COALESCE(payload->'transaction'->>'isin', payload->>'isin') "
            "FROM import_row WHERE batch_id = %s",
            (batch_id,),
        )
        isins = [row[0] for row in cur.fetchall() if row[0]]
    return hydrate_isins(conn, isins, today - timedelta(days=RECENT_DAYS), today)


def backfill_market_history(
    conn: Any,
    start: date,
    end: date,
    *,
    time_budget_seconds: float,
    notifier: Notifier | None = None,
    sources: list[Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Fetch bhavcopy days missing from ``market_close``, newest first, bounded.

    Each fetched day goes through ``store_backfilled_days``, so ``price`` /
    ``price_absent`` for ledger ISINs and ``market_close`` are written by the one store
    path. Known closed days are skipped and newly proven ones recorded, under
    ``app.prices.history``'s rules. Must run inside a collection context.
    """
    from app.db.index_levels import weekend_sessions
    from app.prices.history import _closed_days, _is_closed, store_backfilled_days

    today = today or date.today()
    start = max(start, NSE_BHAVCOPY_EARLIEST)
    if start > end:
        return {"pending": 0, "fetched_days": 0, "closed_days": 0, "failed_days": 0}
    done = complete_market_days(conn, start, end)
    closed = _closed_days(conn, start, end)
    sessions = weekend_sessions(conn, start, end)
    pending = [
        d
        for d in reversed(trading_days(start, end, sessions))
        if d not in done and d not in (closed or set())
    ]
    fetched = closed_days = failed = attempted = 0
    began = time.monotonic()
    for on in pending:
        if time.monotonic() - began > time_budget_seconds:
            break
        attempted += 1
        result = backfill_days(on, on, sources=sources, notifier=notifier, sessions=sessions)[0]
        try:
            if result.prices:
                store_backfilled_days(conn, [result])
                fetched += 1
            elif closed is not None and _is_closed(result, today):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO price_day_closed (price_date) VALUES (%s) "
                        "ON CONFLICT DO NOTHING",
                        (on,),
                    )
                closed_days += 1
            if result.failures:
                failed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            failed += 1
            logger.warning("market history backfill failed for %s", on, exc_info=True)
    return {
        "pending": len(pending),
        "attempted": attempted,
        "fetched_days": fetched,
        "closed_days": closed_days,
        "failed_days": failed,
        "complete": attempted == len(pending) and not failed,
    }


def main(argv: list[str] | None = None) -> int:
    """One-time (resumable) history backfill from a machine that can reach the archives."""
    parser = argparse.ArgumentParser(
        prog="python -m app.prices.market", description=backfill_market_history.__doc__
    )
    parser.add_argument("--env-file", default=".env.vercel")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--max-minutes", type=float, default=120.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    from app.db.connection import connect
    from app.exchange_reference import archive_collection, reference_transaction
    from app.nse_worker import load_settings
    from app.prices.alerts import get_notifier

    settings = load_settings(args.env_file)
    end = args.end or date.today() - timedelta(days=1)
    with connect(settings.database_url) as conn, reference_transaction(conn):
        with archive_collection(conn):
            result = backfill_market_history(
                conn,
                args.start,
                end,
                time_budget_seconds=args.max_minutes * 60,
                notifier=get_notifier(settings),
            )
    print(json.dumps(result, default=str))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
