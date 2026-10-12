"""Persistence for the intraday live-price cache (`live_price` table).

Deliberately separate from ``app/db/prices.py``: that module's own docstring claims
ownership of the staleness rule for the ledger's authoritative source, bhavcopy, and this
is not that. ``live_price`` holds one row per ISIN -- the latest yfinance quote seen, with
no history -- and a row older than its TTL is treated as absent, the same "omit rather than
serve something stale" posture ``app/db/prices.py`` documents, just on a minutes-not-days
clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

#: How long a cached quote is trusted before a fresh fetch is attempted. Minutes, not
#: seconds: Yahoo's rate limits are real (yfinance is unofficial scraping, not a licensed
#: API) and a portfolio's LTP does not need second-by-second precision to read as "live".
DEFAULT_MAX_AGE_SECONDS = 180


def upsert_live_prices(
    conn: Any,
    prices: dict[str, Decimal],
    fetched_at: datetime,
    session_dates: dict[str, date] | None = None,
) -> int:
    """Overwrite the cached quote for each ISIN in ``prices``. Idempotent by construction:
    there is no history to preserve, just the latest value. ``session_dates`` records the
    trading day each quote belongs to (the provider bar's date)."""
    if not prices:
        return 0
    session_dates = session_dates or {}
    with conn.cursor() as cur:
        for isin, last_price in prices.items():
            cur.execute(
                """
                INSERT INTO live_price (isin, last_price, fetched_at, session_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (isin) DO UPDATE
                SET last_price = EXCLUDED.last_price, fetched_at = EXCLUDED.fetched_at,
                    session_date = EXCLUDED.session_date
                """,
                (isin, last_price, fetched_at, session_dates.get(isin)),
            )
    return len(prices)


def get_fresh(
    conn: Any,
    isins: list[str],
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Decimal]:
    """Cached quotes for ``isins`` no older than ``max_age_seconds``.

    A stale row is OMITTED rather than returned -- same contract as
    ``app.db.prices.latest_prices``: the caller either refreshes it or lets it fall through
    to bhavcopy/unpriced, never silently serves a five-minutes-old-and-counting number as
    if it were current.
    """
    if not isins:
        return {}
    quotes = get_fresh_quotes(conn, isins, max_age_seconds)
    return {isin: price for isin, (price, _) in quotes.items()}


def get_fresh_quotes(
    conn: Any,
    isins: list[str],
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, tuple[Decimal, date | None]]:
    """``get_fresh`` with each quote's trading day. A fresh row whose session is not today
    still answers "the provider has nothing newer" for the TTL, so a weekend dashboard does
    not ask the provider again on every load. A row cached before session dates were
    recorded carries None and is never taken for today's."""
    if not isins:
        return {}
    floor = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, last_price, session_date FROM live_price "
            "WHERE isin = ANY(%s) AND fetched_at >= %s",
            (list(isins), floor),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}
