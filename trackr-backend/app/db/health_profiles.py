"""``security_health_profile``: which business-quality rubric applies to a company.

Filled only from the exchange's own classification (BSE scrip header). Read by the
advisor analytics materialiser. See migration 087 for the profile meanings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SOURCE = "BSE_SCRIP_HEADER"
FINANCIAL_SECTOR = "financial services"
BANK_GROUP = "banks"
#: Bank/non-bank status changes rarely (licence conversions); re-check quarterly.
REFRESH_AFTER = timedelta(days=90)
#: Per job run. BSE is called one scrip at a time with a pause; keep a run bounded.
BATCH_LIMIT = 60
WORK_KEY = "security-health-profiles"


@dataclass(frozen=True, slots=True)
class HealthProfile:
    isin: str
    profile: str
    exchange_sector: str
    industry_group: str | None
    industry: str | None
    source_ref: str
    retrieved_at: datetime


def classify(sector: str, industry_group: str | None) -> str:
    """Exchange classification -> rubric profile. Pure; the whole mapping is here."""
    if sector.strip().casefold() != FINANCIAL_SECTOR:
        return "non_financial"
    if (industry_group or "").strip().casefold() == BANK_GROUP:
        return "bank"
    return "unsupported_financial"


def due(conn: Any, isins: list[str], now: datetime | None = None) -> list[str]:
    """Company ISINs with no profile, or one older than REFRESH_AFTER."""
    if not isins:
        return []
    cutoff = (now or datetime.now(UTC)) - REFRESH_AFTER
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.isin FROM security s "
            "LEFT JOIN security_health_profile p ON p.isin = s.isin "
            "WHERE s.isin = ANY(%s) AND s.asset_class = 'EQ' AND s.isin NOT LIKE 'INF%%' "
            "AND (p.isin IS NULL OR p.retrieved_at < %s) "
            "ORDER BY p.retrieved_at NULLS FIRST, s.isin",
            (list(isins), cutoff),
        )
        return [r[0] for r in cur.fetchall()]


def refresh(conn: Any, isins: list[str], fetch=None, now: datetime | None = None) -> dict:
    """Fetch and upsert profiles for ``isins``; failures are returned by name."""
    from app.prices.bse_classification import classification_headers

    batch = isins[:BATCH_LIMIT]
    found, failed = (fetch or classification_headers)(batch)
    stamp = now or datetime.now(UTC)
    with conn.cursor() as cur:
        for isin, row in found.items():
            cur.execute(
                "INSERT INTO security_health_profile (isin, profile, exchange_sector, "
                "industry_group, industry, source, source_ref, retrieved_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (isin) DO UPDATE SET "
                "profile=EXCLUDED.profile, exchange_sector=EXCLUDED.exchange_sector, "
                "industry_group=EXCLUDED.industry_group, industry=EXCLUDED.industry, "
                "source=EXCLUDED.source, source_ref=EXCLUDED.source_ref, "
                "retrieved_at=EXCLUDED.retrieved_at",
                (
                    isin,
                    classify(row["sector"], row["industry_group"]),
                    row["sector"],
                    row["industry_group"],
                    row["industry"],
                    SOURCE,
                    row["source_ref"],
                    stamp,
                ),
            )
    conn.commit()
    return {
        "classified": len(found),
        "failed": failed,
        "remaining": max(0, len(isins) - len(batch)),
    }


def read(conn: Any, isins: list[str]) -> dict[str, HealthProfile]:
    if not isins:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, profile, exchange_sector, industry_group, industry, source_ref, "
            "retrieved_at FROM security_health_profile WHERE isin = ANY(%s)",
            (list(isins),),
        )
        return {r[0]: HealthProfile(*r) for r in cur.fetchall()}
