"""Store official index ratios; dated reference observations, never page-derived data."""

from datetime import date, timedelta
from typing import Any

from app.prices.index_valuation import IndexValuation


def upsert(conn: Any, rows: list[IndexValuation]) -> int:
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """INSERT INTO index_valuation
                (index_name, valuation_date, pe, pb, dividend_yield, source_url)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (index_name, valuation_date) DO UPDATE SET
                pe=excluded.pe, pb=excluded.pb, dividend_yield=excluded.dividend_yield,
                source_url=excluded.source_url, fetched_at=now()""",
                (
                    row.index_name,
                    row.valuation_date,
                    row.pe,
                    row.pb,
                    row.dividend_yield,
                    row.source_url,
                ),
            )
    return len(rows)


def latest(conn: Any, index_name: str, on: date) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT valuation_date, pe, pb, dividend_yield, source_url
            FROM index_valuation WHERE index_name=%s AND valuation_date BETWEEN %s AND %s
            ORDER BY valuation_date DESC LIMIT 1""",
            (index_name, on - timedelta(days=5), on),
        )
        row = cur.fetchone()
    if not row:
        return {
            "benchmark": {},
            "benchmark_as_of": None,
            "benchmark_source": "No official index ratios cached within five days of this date.",
        }
    return {
        "benchmark": {
            k: float(v) if v is not None else None
            for k, v in zip(("pe", "pb", "dividend_yield"), row[1:4], strict=True)
        },
        "benchmark_as_of": row[0],
        "benchmark_source": row[4],
    }
