"""Persistence for `index_level` -- docs/ENGINE_SPEC.md section 6.

Narrow and typed, exactly like ``app/db/prices.py``. This is a genuinely different table,
not a variant of ``price``: no ISIN, no foreign key into ``security``. Keeping this module
separate (rather than teaching ``app/db/prices.py`` a second key shape) keeps that
structural separation visible in the import graph, not just the schema.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

#: Index levels are published on the same daily/weekday cadence as bhavcopy closes, so
#: the same staleness reasoning app.engine.holdings.MAX_PRICE_STALENESS_DAYS applies: a
#: level older than this is refused rather than carried forward silently.
MAX_INDEX_STALENESS_DAYS = 5


def upsert_index_levels(conn: Any, rows: list[Any]) -> int:
    """Store merged index levels. Idempotent: re-running a range changes nothing new.

    ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING``, same reasoning as
    ``upsert_prices`` -- a republished/corrected value should carry through. An unchanged
    level is not rewritten, so re-fetching an overlap invalidates no cached report.
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO index_level (index_name, level_date, close, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (index_name, level_date) DO UPDATE
                SET close = EXCLUDED.close, source = EXCLUDED.source
                WHERE (index_level.close, index_level.source)
                    IS DISTINCT FROM (EXCLUDED.close, EXCLUDED.source)
                """,
                (row.index_name, row.level_date, row.close, row.source),
            )
    return len(rows)


def get_index_levels(
    conn: Any,
    index_name: str,
    dates: list[date],
    max_staleness_days: int = MAX_INDEX_STALENESS_DAYS,
) -> dict[date, Decimal]:
    """Most-recent-on-or-before level for each requested date, within the staleness window.

    ``dates`` is typically a handful of cash-flow dates spanning years, not a dense daily
    range -- unlike ``latest_prices`` (one row per ISIN, all as of the same date), this
    resolves several dates against ONE index. A single bulk read covering the whole span,
    resolved per date in Python, avoids one round-trip per date without needing a
    correlated-subquery.

    A date with no usable level within the window is simply absent from the result --
    the caller (``app.engine.benchmark.compute_benchmark``) is what turns that into a
    named ``MissingIndexLevelError``, mirroring how ``latest_prices``'s omissions become
    ``MissingPriceError`` one layer up.
    """
    if not dates:
        return {}
    earliest = min(dates) - timedelta(days=max_staleness_days)
    latest = max(dates)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT level_date, close FROM index_level
            WHERE index_name = %s AND level_date BETWEEN %s AND %s
            ORDER BY level_date ASC
            """,
            (index_name, earliest, latest),
        )
        published = cur.fetchall()

    resolved: dict[date, Decimal] = {}
    for target in dates:
        floor = target - timedelta(days=max_staleness_days)
        best: tuple[date, Decimal] | None = None
        for level_date, close in published:
            if floor <= level_date <= target:
                if best is None or level_date > best[0]:
                    best = (level_date, close)
        if best is not None:
            resolved[target] = best[1]
    return resolved


def list_index_levels(
    conn: Any, index_name: str, start: date, end: date
) -> list[tuple[date, Decimal]]:
    """Every stored level for one index in ``[start, end]``, ascending.

    Deliberately NOT ``get_index_levels``. That function answers "what was this index
    worth on each of these specific dates", carrying a staleness window and resolving
    most-recent-on-or-before, because its caller holds a handful of cash-flow dates
    spanning years. This one answers "what does the archive hold across this span" and
    resolves nothing: a plotted line wants the real published points and a gap it can
    draw a break through, not a carried-forward value repeated until the next close.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT level_date, close FROM index_level
            WHERE index_name = %s AND level_date BETWEEN %s AND %s
            ORDER BY level_date ASC
            """,
            (index_name, start, end),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def weekend_sessions(conn: Any, start: date, end: date) -> frozenset[date]:
    """Saturdays and Sundays in ``[start, end]`` on which the exchanges actually traded.

    The evidence is a published index close on that date -- NSE Indices publishes a level
    only for a session that took place (1 February 2026, the Union Budget Sunday, has
    NIFTY50/NIFTY500 TRI closes and full NSE and BSE bhavcopies). No hand-kept calendar:
    a special session is known once its index level is stored. Weekdays are not returned;
    the weekday calendar already covers them and a holiday settles as NotPublished.
    """
    rows = conn.execute(
        "SELECT DISTINCT level_date FROM index_level "
        "WHERE level_date BETWEEN %s AND %s AND extract(isodow FROM level_date) >= 6",
        (start, end),
    ).fetchall()
    return frozenset(row[0] for row in rows)
