"""Persistence for vendor fundamentals (``security_fundamentals``).

Keyed ``(isin, as_of)`` rather than one row per ISIN because a vendor RESTATES. A ratio
that changes between two fetches is either a new filing or a correction, and collapsing the
two loses the ability to tell them apart -- the same reason ``price`` is keyed by date.

A NULL ratio means UNKNOWN and is never coerced to zero. This matters more here than
anywhere else in the schema: the upstream feed returns null for return-on-equity on most
Indian scrips, and a zero-filled ROE would silently drag every quality score, every
valuation-map status and every portfolio aggregate toward the floor while looking like data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

#: Columns carried in and out, in one place so the INSERT and the SELECT cannot drift.
_RATIO_COLUMNS = (
    "pe",
    "pb",
    "roe",
    "net_margin",
    "debt_to_equity",
    "interest_cover",
    "dividend_yield",
    "sales_growth_1y",
    "profit_growth_1y",
    "market_cap",
    "roce",
)

#: Full column order for both the INSERT and the SELECT.
_ALL_COLUMNS = (
    "isin",
    "as_of",
    *_RATIO_COLUMNS,
    "roe_basis",
    "fiscal_period",
    "source",
    "fetched_at",
)


@dataclass(frozen=True, slots=True)
class Fundamentals:
    isin: str
    as_of: date
    pe: Decimal | None = None
    pb: Decimal | None = None
    roe: Decimal | None = None
    net_margin: Decimal | None = None
    debt_to_equity: Decimal | None = None
    interest_cover: Decimal | None = None
    dividend_yield: Decimal | None = None
    sales_growth_1y: Decimal | None = None
    profit_growth_1y: Decimal | None = None
    market_cap: Decimal | None = None
    roce: Decimal | None = None
    #: 'vendor' when the feed supplied ROE directly, 'derived_statements' when we computed
    #: it from net income and shareholders' equity. Two different claims, so two labels.
    roe_basis: str | None = None
    fiscal_period: date | None = None
    source: str = "YFINANCE"
    fetched_at: datetime | None = None


def upsert(conn: Any, rows: list[Fundamentals]) -> int:
    """Store one observation per (isin, as_of). Idempotent within a day."""
    if not rows:
        return 0
    columns = _ALL_COLUMNS
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns[2:])
    with conn.cursor() as cur:
        for row in rows:
            data = asdict(row)
            cur.execute(
                f"INSERT INTO security_fundamentals ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (isin, as_of) DO UPDATE SET {updates}",
                tuple(data[c] for c in columns),
            )
    return len(rows)


def latest(conn: Any, isins: list[str], as_of: date | None = None) -> dict[str, Fundamentals]:
    """Newest observation per ISIN on or before ``as_of`` (today when omitted).

    NO STALENESS WINDOW, unlike prices, and the difference is deliberate: a fundamental
    describes a filed quarter, not a moment. Last quarter's ROE is still the best available
    answer to "how profitable is this business", where last week's price is not the answer
    to "what is it worth now". Staleness is reported to the UI via ``as_of`` instead of
    being used to suppress the row.
    """
    if not isins:
        return {}
    cutoff = as_of or date.today()
    columns = _ALL_COLUMNS
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (isin) {", ".join(columns)}
            FROM security_fundamentals
            WHERE isin = ANY(%s) AND as_of <= %s
            ORDER BY isin, as_of DESC
            """,
            (list(isins), cutoff),
        )
        return {
            row[0]: Fundamentals(**dict(zip(columns, row, strict=True))) for row in cur.fetchall()
        }


def stalest_isins(conn: Any, candidates: list[str], limit: int) -> list[str]:
    """The ``limit`` ISINs most overdue a refresh: never-fetched first, then oldest.

    Drives the cron's bounded batch. Rotating oldest-first means a rate-limited run still
    makes progress on a different slice next time rather than re-fetching the same head of
    the list forever.
    """
    if not candidates or limit <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.isin
            FROM unnest(%s::text[]) AS c(isin)
            LEFT JOIN (
                SELECT isin, max(as_of) AS newest
                FROM security_fundamentals GROUP BY isin
            ) f ON f.isin = c.isin
            ORDER BY f.newest ASC NULLS FIRST, c.isin
            LIMIT %s
            """,
            (list(candidates), limit),
        )
        return [row[0] for row in cur.fetchall()]
