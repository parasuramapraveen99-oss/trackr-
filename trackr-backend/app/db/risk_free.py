"""Stored risk-free levels, and the annualised rate over a window's exact dates."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.prices.risk_free import RATE_INDEX, RateLevel

#: An endpoint level more than this many days from the window edge is not that edge.
MAX_EDGE_GAP_DAYS = 7


def upsert(conn: Any, rows: list[RateLevel]) -> int:
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """INSERT INTO risk_free_level (index_name, level_date, close, source_url)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (index_name, level_date) DO UPDATE SET
                   close=excluded.close, source_url=excluded.source_url, fetched_at=now()""",
                (row.index_name, row.level_date, row.close, row.source_url),
            )
    return len(rows)


def annualised_rate(conn: Any, start: date, end: date, index: str = RATE_INDEX) -> float | None:
    """(L_end / L_start) ** (365 / days) - 1, from the levels on or just before each edge.

    The rate index accrues on calendar days, so 365 is its own convention. None when
    either edge has no level within ``MAX_EDGE_GAP_DAYS`` -- an unsourced rate withholds
    Sharpe and Sortino rather than inventing one.
    """
    def level_at(on: date):
        # A missing risk_free_level table (migration not applied) raises; only a missing
        # LEVEL withholds the ratio.
        return conn.execute(
            "SELECT level_date, close FROM risk_free_level WHERE index_name=%s "
            "AND level_date <= %s AND level_date >= %s ORDER BY level_date DESC LIMIT 1",
            (index, on, on - timedelta(days=MAX_EDGE_GAP_DAYS)),
        ).fetchone()

    a, b = level_at(start), level_at(end)
    if not a or not b or b[0] <= a[0]:
        return None
    days = (b[0] - a[0]).days
    return float(b[1] / a[1]) ** (365.0 / days) - 1.0
