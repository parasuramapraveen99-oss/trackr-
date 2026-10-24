"""Company fundamentals and rubric profiles for portfolio-health-v2, point in time.

Source order per ISIN:
  1. NSE integrated-filing XBRL (``exchange_financials``): the latest year-end filing,
     audited before unaudited, consolidated before standalone. Standalone observations
     retain their actual basis and are not asserted to describe a corporate group.
     Profile from the SEBI filing format: BANKING -> bank, NBFC -> nbfc, else non_financial.
  2. Yahoo Finance annual statements (``security_fundamentals``) for a security with no
     NSE filing (BSE-only scrips). Provisional; profile from the exchange sector
     (anything outside Financial Services is non_financial).

Every observation carries its fiscal year end and when it became known, so the rubric's
look-ahead and freshness checks apply unchanged. docs/PORTFOLIO_HEALTH.md.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.db import exchange_financials
from app.db.fundamentals import latest as latest_vendor
from app.engine.exchange_ratios import ratios

IST = ZoneInfo("Asia/Kolkata")
SOURCE_REF = "company_financials"
RATIO_FIELDS = ("pe", "pb", "debt_to_equity", "interest_cover")
FIELDS = ("roce", "roe", "net_margin", "debt_to_equity", "interest_cover", "pe", "pb")
PROFILE = {"BANKING": "bank", "NBFC": "nbfc", "INDAS": "non_financial", "NONINDAS": "non_financial"}
FINANCIAL_SECTOR = "financial services"


def _observation(
    isin, field, value, fiscal_end, known_at, on, reasons, consolidation="consolidated"
) -> dict:
    row = dict(
        isin=isin,
        field=field,
        value=str(value),
        unit="ratio" if field in RATIO_FIELDS else "fraction",
        basis="annual",
        consolidation=consolidation,
        fiscal_end=fiscal_end.isoformat(),
        published_at=known_at.isoformat(),
        retrieved_at=known_at.isoformat(),
        applicability="applicable",
        source_refs=[SOURCE_REF],
        quality="provisional" if reasons else "available",
        reasons=list(reasons),
    )
    if field in ("pe", "pb"):
        row["valuation_date"] = on.isoformat()
    return row


def build(
    conn: Any,
    isins: list[str],
    on: date,
    cutoff: datetime,
    prices: dict[str, Decimal],
    sectors: dict[str, str | None],
) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """(observations, ISIN -> profile, ISIN -> source label). Nothing known after
    ``cutoff`` is used."""
    filed = exchange_financials.latest(conn, isins, on, cutoff)
    share_changes: dict[str, list[date]] = {}
    if filed:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT isin, ex_date FROM corporate_action WHERE isin = ANY(%s) "
                "AND ex_date > %s AND ex_date <= %s AND created_at <= %s "
                "AND action_type IN ('BONUS', 'SPLIT', 'DEMERGER')",
                (list(filed), min(p["period_end"] for _, p in filed.values()), on, cutoff),
            )
            for isin, ex_date in cur.fetchall():
                share_changes.setdefault(isin, []).append(ex_date)
    observations: list[dict] = []
    profiles: dict[str, str] = {}
    sources: dict[str, str] = {}
    for isin, (statement, provenance) in filed.items():
        known_at = provenance["filed_at"] or provenance["fetched_at"]
        if known_at > cutoff:
            continue
        reasons = [] if provenance["audited"] else ["UNAUDITED_YEAR_END_FILING"]
        basis = provenance["basis"].lower()
        if basis == "standalone":
            reasons.append("STANDALONE_FILING_NOT_GROUP_ACCOUNTS")
        values = ratios(statement, prices.get(isin))
        changed_basis = any(d > provenance["period_end"] for d in share_changes.get(isin, []))
        if changed_basis:
            reasons.append("SUBSEQUENT_CORPORATE_ACTION_PER_SHARE_VALUATION_WITHHELD")
        for field in FIELDS:
            if changed_basis and field in ("pe", "pb"):
                continue
            value = getattr(values, field)
            if value is not None:
                observations.append(
                    _observation(
                        isin, field, value, provenance["period_end"], known_at, on, reasons, basis
                    )
                )
        profiles[isin] = PROFILE[provenance["filing_format"]]
        sources[isin] = provenance["source_url"]
    for isin, row in latest_vendor(conn, [i for i in isins if i not in filed], on).items():
        known_at = row.fetched_at or datetime.combine(row.as_of, time.min, IST)
        if row.fiscal_period is None or known_at > cutoff or row.source.count(":") > 1:
            continue  # no fiscal year, look-ahead, or a failed identity check
        for field in FIELDS:
            value = getattr(row, field, None)
            if value is not None:
                observations.append(
                    _observation(
                        isin,
                        field,
                        value,
                        row.fiscal_period,
                        known_at,
                        on,
                        ["VENDOR_STATEMENTS_NO_EXCHANGE_FILING"],
                    )
                )
        sector = (sectors.get(isin) or "").strip().casefold()
        if sector and sector != FINANCIAL_SECTOR:
            profiles[isin] = "non_financial"
        sources[isin] = row.source
    return observations, profiles, sources
