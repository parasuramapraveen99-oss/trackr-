"""Price HISTORY: store backfilled bhavcopy days, and fill the ledger's gaps automatically.

WHY THIS EXISTS. The daily cron stores TODAY's bhavcopy and nothing else; history only
ever arrived when someone called ``POST /ops/snapshots/run`` with a date range by hand.
In production that never happened, so on 2026-09-29 the price archive began on
2026-08-31 for every security -- and every screen that values a holding at a PAST date
(contribution's period start, trailing XIRR/TWRR windows, the history chart) raised
MissingPriceError for anyone who had imported trades older than a month. That is every
real user, not one portfolio.

``backfill_price_history`` closes the gap with no human step: each run finds the trading
days on which some ledger security (from its first transaction on) has neither a price
nor a recorded absence, and fetches the NEWEST of them first -- recent periods are what
screens ask about -- bounded by a day count and a time budget so one invocation always
fits a serverless timeout. The backlog drains across runs; nothing is lost if a run
stops early, because "settled" is re-derived from the database every time.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

from app.db.fund_reference import sync_route_observations
from app.db.index_levels import weekend_sessions
from app.db.prices import (
    held_isins,
    price_dates_settled,
    record_absent_prices,
    upsert_prices,
)
from app.db.quote_mapping import upsert_listings
from app.db.universe import current_universe_isins
from app.prices.alerts import Notifier
from app.prices.pipe import NSE_BHAVCOPY_EARLIEST, PipeResult, backfill_days, trading_days

logger = logging.getLogger(__name__)

#: A weekday is recorded closed only once it is this old: a file published late, or an
#: archive briefly serving 404s, must never settle a real trading day.
CLOSED_DAY_MIN_AGE_DAYS = 7
DEFAULT_MAX_DAYS = 120
DEFAULT_TIME_BUDGET_SECONDS = 240.0


def store_backfilled_days(connection: Any, price_results: list[PipeResult]) -> int:
    """Upsert each fetched day and record what the archive answered without.

    Returns how many dates stored at least one price. Shared by ``_run_snapshots`` and
    ``backfill_price_history`` so the absence rules below exist exactly once.
    """
    held = set(held_isins(connection))
    # Stored for held AND universe ISINs, so the ranking universe accumulates the history
    # factor scores need. ABSENCE RECORDING STAYS SCOPED TO `held`, deliberately: a
    # universe member is reference data, and letting one decide whether a date is
    # "settled" would let a security nobody owns govern whether a real holding's price
    # is ever fetched again.
    wanted = held | set(current_universe_isins(connection))
    price_dates_fetched = 0
    for result in price_results:
        stored = upsert_prices(connection, [p for p in result.prices if p.isin in wanted])
        if stored:
            price_dates_fetched += 1
        sync_route_observations(
            connection,
            {p.isin for p in result.prices if p.isin in held and p.isin.startswith("INF")},
        )
        upsert_listings(
            connection,
            {isin: lst for isin, lst in result.listings.items() if isin in wanted},
            result.price_date,
        )
        # Whatever the archive answered with but did not mention is absent, not pending.
        # BOTH CONDITIONS ARE LOAD-BEARING:
        #   `result.fetched`   -- some data actually came back. Empty on a holiday or a
        #                         wholesale failure, where calling every security absent
        #                         would settle the date forever and lose its prices.
        #   `result.succeeded` -- and NO exchange errored. With BSE down, every BSE-only
        #                         security is missing from a healthy-looking NSE file;
        #                         recording those absent would be a permanent, silent lie.
        # Exchange-routed holdings only (record_absent_prices enforces it too): a mutual
        # fund missing from a bhavcopy is not evidence of anything.
        if result.fetched and result.succeeded:
            record_absent_prices(
                connection, result.price_date, sorted(held - {p.isin for p in result.prices})
            )
    return price_dates_fetched


def _closed_days(connection: Any, start: date, end: date) -> set[date] | None:
    """Known exchange-closed weekdays, or None when migration 059 is not applied yet --
    the backfill still runs then, it just cannot skip holidays it has already seen."""
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('price_day_closed') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute(
            "SELECT price_date FROM price_day_closed WHERE price_date BETWEEN %s AND %s",
            (start, end),
        )
        return {row[0] for row in cur.fetchall()}


def _is_closed(result: PipeResult, today: date) -> bool:
    """Every exchange said "nothing published", none failed, and the date is old enough."""
    return (
        not result.fetched
        and not result.failures
        and {"NSE", "BSE"} <= set(result.not_published)
        and result.price_date <= today - timedelta(days=CLOSED_DAY_MIN_AGE_DAYS)
    )


def backfill_price_history(
    connection: Any,
    notifier: Notifier | None = None,
    today: date | None = None,
    max_days: int = DEFAULT_MAX_DAYS,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    sources: list[Any] | None = None,
) -> dict[str, int]:
    """Fetch the newest unsettled trading days in the ledger's history, bounded per run.

    Yesterday is the newest day considered; today belongs to the daily refresh, whose
    file may not be published yet.
    """
    today = today or date.today()
    with connection.cursor() as cur:
        cur.execute("SELECT min(txn_date) FROM transaction")
        first = cur.fetchone()[0]
    if first is None:
        return {"pending": 0, "fetched_days": 0, "closed_days": 0, "failed_days": 0}

    start = max(first, NSE_BHAVCOPY_EARLIEST)
    end = today - timedelta(days=1)
    if start > end:
        return {"pending": 0, "fetched_days": 0, "closed_days": 0, "failed_days": 0}

    settled = price_dates_settled(connection, start, end)
    closed = _closed_days(connection, start, end)
    skip = settled | (closed or set())
    sessions = weekend_sessions(connection, start, end)
    pending = [d for d in reversed(trading_days(start, end, sessions)) if d not in skip]

    fetched_days = closed_days = failed_days = 0
    began = time.monotonic()
    for on in pending[:max_days]:
        if time.monotonic() - began > time_budget_seconds:
            break
        result = backfill_days(on, on, sources=sources, notifier=notifier, sessions=sessions)[0]
        try:
            if store_backfilled_days(connection, [result]):
                fetched_days += 1
            elif closed is not None and _is_closed(result, today):
                with connection.cursor() as cur:
                    cur.execute(
                        "INSERT INTO price_day_closed (price_date) VALUES (%s) "
                        "ON CONFLICT DO NOTHING",
                        (on,),
                    )
                closed_days += 1
            if result.failures:
                failed_days += 1
            connection.commit()
        except Exception:
            connection.rollback()
            failed_days += 1
            logger.warning("price backfill failed for %s", on, exc_info=True)
    return {
        "pending": len(pending),
        "fetched_days": fetched_days,
        "closed_days": closed_days,
        "failed_days": failed_days,
    }


def backfill_universe_prices(
    connection: Any,
    start: date,
    end: date,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    coverage: float = 0.9,
    sources: list[Any] | None = None,
) -> dict[str, int]:
    """Fill universe members' price history over ``[start, end]``, newest day first.

    ``backfill_price_history`` treats a day as settled once ANY price exists for it, so a
    universe loaded after the ledger's history would never get its past. Here a day is
    done only when ``coverage`` of current members already have a close on it. Each day
    goes through ``store_day``, the same held + universe filter the daily pipe uses.
    """
    from app.prices.store import store_day

    members = current_universe_isins(connection)
    if not members:
        return {"members": 0, "fetched_days": 0, "skipped_days": 0, "failed_days": 0}
    with connection.cursor() as cur:
        cur.execute(
            "SELECT price_date, count(*) FROM price WHERE isin = ANY(%s) "
            "AND price_date BETWEEN %s AND %s GROUP BY price_date",
            (members, start, end),
        )
        done = {d for d, n in cur.fetchall() if n >= coverage * len(members)}
    sessions = weekend_sessions(connection, start, end)
    pending = [
        d
        for d in reversed(trading_days(max(start, NSE_BHAVCOPY_EARLIEST), end, sessions))
        if d not in done
    ]
    fetched = failed = 0
    began = time.monotonic()
    for on in pending:
        if time.monotonic() - began > time_budget_seconds:
            break
        result = backfill_days(on, on, sources=sources, sessions=sessions)[0]
        try:
            if result.prices:
                store_day(connection, result)
                fetched += 1
            if result.failures:
                failed += 1
            connection.commit()
        except Exception:
            connection.rollback()
            failed += 1
            logger.warning("universe price backfill failed for %s", on, exc_info=True)
    return {
        "members": len(members),
        "fetched_days": fetched,
        "skipped_days": len(done),
        "failed_days": failed,
        "pending": len(pending),
    }
