"""Persistence for ``quote_mapping`` -- which provider ticker is this ISIN (migration 075).

Written from the exchange's own bhavcopy row for each ISIN (``upsert_listings``), checked
every evening against that day's official close (``app.prices.quote_check``), and read by
the indicative-quote path (``app.prices.live``). Never read by anything that stores or
computes a value of record.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.prices.yfinance_live import resolve_ticker

UNVERIFIED, VERIFIED, BLOCKED = "UNVERIFIED", "VERIFIED", "BLOCKED"


def upsert_listings(conn: Any, listings: dict[str, tuple[str, str]], seen_on: date) -> int:
    """Record where each ISIN was listed on ``seen_on``. An older day never overwrites a
    newer one (a history backfill runs newest-first and old days arrive later), and a
    changed exchange or symbol resets the check -- a rename or a reused symbol must be
    proved again before it is trusted. ISINs with no ``security`` row are skipped."""
    if not listings:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
                INSERT INTO quote_mapping (isin, exchange, symbol, ticker, seen_on)
                SELECT %s, %s, %s, %s, %s WHERE EXISTS (SELECT 1 FROM security WHERE isin = %s)
                ON CONFLICT (isin) DO UPDATE SET
                    exchange = EXCLUDED.exchange,
                    symbol = EXCLUDED.symbol,
                    ticker = EXCLUDED.ticker,
                    seen_on = EXCLUDED.seen_on,
                    status = CASE WHEN quote_mapping.ticker = EXCLUDED.ticker
                                  THEN quote_mapping.status ELSE 'UNVERIFIED' END,
                    checked_on = CASE WHEN quote_mapping.ticker = EXCLUDED.ticker
                                      THEN quote_mapping.checked_on END
                WHERE quote_mapping.seen_on <= EXCLUDED.seen_on
                """,
            [
                (isin, exchange, symbol, resolve_ticker(symbol, exchange), seen_on, isin)
                for isin, (exchange, symbol) in listings.items()
            ],
        )
        return cur.rowcount


def usable_tickers(conn: Any, isins: list[str]) -> tuple[dict[str, str], set[str]]:
    """(ISIN -> ticker for every mapping not BLOCKED, the set of BLOCKED ISINs)."""
    if not isins:
        return {}, set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, ticker, status FROM quote_mapping WHERE isin = ANY(%s)", (list(isins),)
        )
        rows = cur.fetchall()
    return (
        {isin: ticker for isin, ticker, status in rows if status != BLOCKED},
        {isin for isin, _, status in rows if status == BLOCKED},
    )


def due_for_check(conn: Any, isins: list[str]) -> list[tuple[str, str, date, Decimal]]:
    """(ISIN, ticker, D, official close on D) for each mapping not yet checked against its
    newest official close D. The close is the merged one (NSE wins), the same exchange the
    mapping prefers, so the two are comparable."""
    if not isins:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.isin, m.ticker, p.price_date, p.close
            FROM quote_mapping m
            JOIN LATERAL (
                SELECT price_date, close FROM price
                WHERE price.isin = m.isin ORDER BY price_date DESC LIMIT 1
            ) p ON true
            WHERE m.isin = ANY(%s) AND (m.checked_on IS NULL OR m.checked_on < p.price_date)
            """,
            (list(isins),),
        )
        return [(r[0], r[1], r[2], Decimal(r[3])) for r in cur.fetchall()]


def record_check(
    conn: Any,
    isin: str,
    on: date,
    status: str,
    provider_close: Decimal,
    official_close: Decimal,
    note: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE quote_mapping SET status = %s, checked_on = %s, provider_close = %s,
                   official_close = %s, note = %s
            WHERE isin = %s
            """,
            (status, on, provider_close, official_close, note, isin),
        )


def record_no_evidence(conn: Any, isin: str, on: date, note: str) -> None:
    """Checked against ``on`` with nothing to compare: the status stands, and the mapping
    is not asked about again until a newer official close exists."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE quote_mapping SET checked_on = %s, note = %s WHERE isin = %s",
            (on, note, isin),
        )
