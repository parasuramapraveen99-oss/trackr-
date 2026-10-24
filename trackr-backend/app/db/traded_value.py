"""``security_traded_value``: rupee turnover per security, per exchange, per session.

Written from the same bhavcopy the close comes from (``app.prices.store``); read by the
Analytics liquidity figures. See migration 090 for why it is per exchange and why an
untraded day has no row rather than a zero.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

#: Sessions in the liquidity median (docs/ANALYTICS_BASIC_NARRATIVE.md, RR.LIQUIDITY).
LIQUIDITY_SESSIONS = 20
#: How far back the 20 sessions may reach. Generous for holidays; a scrip that has not
#: traded 20 times in this span is reported as thinly traded, not padded.
LOOKBACK_DAYS = 60


def upsert(conn: Any, rows: list[tuple[str, date, str, Decimal]]) -> int:
    """(isin, date, exchange, value) rows. Idempotent; a republished file corrects."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO security_traded_value (isin, price_date, exchange, traded_value) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (isin, price_date, exchange) "
            "DO UPDATE SET traded_value = EXCLUDED.traded_value",
            rows,
        )
    return len(rows)


def recent_sessions(
    conn: Any, isins: list[str], on: date, sessions: int = LIQUIDITY_SESSIONS
) -> dict[str, list[tuple[date, Decimal]]]:
    """ISIN -> up to ``sessions`` most recent (date, value summed across exchanges), newest
    first, on or before ``on``. An ISIN with no rows is absent from the result."""
    if not isins:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, price_date, sum(traded_value) FROM security_traded_value "
            "WHERE isin = ANY(%s) AND price_date BETWEEN %s AND %s "
            "GROUP BY isin, price_date ORDER BY isin, price_date DESC",
            (list(isins), on - timedelta(days=LOOKBACK_DAYS), on),
        )
        out: dict[str, list[tuple[date, Decimal]]] = {}
        for isin, day, value in cur.fetchall():
            rows = out.setdefault(isin, [])
            if len(rows) < sessions:
                rows.append((day, Decimal(value)))
    return out
