"""Persistence and refresh for ``exchange_financials`` (migration 092)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.exchange_ratios import Statement
from app.ingest.nse_financials import SOURCE, Filing, NseFinancialsSource

logger = logging.getLogger(__name__)

_COLUMNS = (
    "revenue",
    "other_income",
    "profit_before_tax",
    "finance_costs",
    "net_profit",
    "equity",
    "borrowings_current",
    "borrowings_noncurrent",
    "share_capital",
    "face_value",
    "basic_eps",
)


def upsert(conn: Any, filing: Filing) -> None:
    columns = (
        "isin",
        "period_end",
        "basis",
        "audited",
        "filing_format",
        "period_start",
        *_COLUMNS,
        "source",
        "source_url",
        "filed_at",
    )
    values = (
        filing.isin,
        filing.period_end,
        filing.basis,
        filing.audited,
        filing.filing_format,
        filing.period_start,
        *(filing.values.get(c) for c in _COLUMNS),
        SOURCE,
        filing.source_url,
        filing.filed_at,
    )
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns[3:])
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO exchange_financials ({', '.join(columns)}, fetched_at) "
            f"VALUES ({', '.join(['%s'] * len(columns))}, now()) "
            f"ON CONFLICT (isin, period_end, basis) DO UPDATE SET {updates}, fetched_at = now()",
            values,
        )


def latest(
    conn: Any, isins: list[str], on: date, cutoff: datetime | None = None
) -> dict[str, tuple[Statement, dict]]:
    """Newest filed year ending on or before ``on`` per ISIN, consolidated first.
    Returns (Statement, provenance)."""
    if not isins:
        return {}
    cutoff = cutoff or datetime.combine(on, time.max, ZoneInfo("Asia/Kolkata"))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (isin) isin, period_end, basis, audited, filing_format, source_url,
                filed_at, fetched_at,
                {", ".join(_COLUMNS)}
            FROM exchange_financials
            WHERE isin = ANY(%s) AND period_end <= %s
                AND COALESCE(filed_at, fetched_at) <= %s
            ORDER BY isin, period_end DESC, audited DESC, basis = 'CONSOLIDATED' DESC
            """,
            (list(isins), on, cutoff),
        )
        out = {}
        for row in cur.fetchall():
            isin, period_end, basis, audited, fmt, url, filed_at, fetched_at, *figures = row
            fields = dict(zip(_COLUMNS, figures, strict=True))
            fields.pop("other_income")
            out[isin] = (
                Statement(**fields),
                {
                    "period_end": period_end,
                    "basis": basis,
                    "audited": audited,
                    "filing_format": fmt,
                    "source_url": url,
                    "filed_at": filed_at,
                    "fetched_at": fetched_at,
                },
            )
        return out


#: Filings checked per run, stalest first; a full Nifty 500 pass takes a few runs.
BATCH_LIMIT = 150
#: Re-check a company once a month: results are quarterly, year-end ones annual.
RECHECK_DAYS = 30


def stale_symbols(conn: Any, isins: list[str], limit: int = BATCH_LIMIT) -> dict[str, str]:
    """ISIN -> NSE symbol for the ``limit`` least recently fetched of ``isins``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.isin, s.symbol FROM security s
            LEFT JOIN (SELECT isin, max(fetched_at) AS fetched FROM exchange_financials
                       GROUP BY isin) f USING (isin)
            WHERE s.isin = ANY(%s) AND s.symbol IS NOT NULL
              AND (f.fetched IS NULL OR f.fetched < now() - make_interval(days => %s))
            ORDER BY f.fetched ASC NULLS FIRST, s.isin
            LIMIT %s
            """,
            (list(isins), RECHECK_DAYS, limit),
        )
        return dict(cur.fetchall())


def refresh(conn: Any, symbols: dict[str, str], source: NseFinancialsSource | None = None) -> dict:
    """Fetch the latest audited annual filing for each ISIN -> NSE symbol. Commits per
    filing, so an interrupted run keeps what it fetched. Never raises."""
    source = source or NseFinancialsSource()
    stored, missing = 0, []
    for isin, symbol in sorted(symbols.items()):
        try:
            filing = source.latest_annual(isin, symbol)
        except Exception:
            logger.warning("nse_financials_fetch_failed", extra={"isin": isin}, exc_info=True)
            missing.append(isin)
            continue
        if filing is None:
            missing.append(isin)
            continue
        upsert(conn, filing)
        conn.commit()
        stored += 1
    return {"requested": len(symbols), "stored": stored, "missing": missing}
