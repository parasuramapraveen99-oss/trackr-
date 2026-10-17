"""Fund-tax inputs: evidenced scheme profiles for a period, and the taxpayer's profile.

docs/MF_ETF_PLAN.md section 5. A profile is a dated legal fact with its evidence -- the
AMC's statutory tax disclosure for that period, typically. Today's AMFI category never
answers it: ``amfi_scheme.exposure`` is a display candidate only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.engine.fund_tax import FundTaxFacts, TaxpayerProfile


class ConflictingProfile(ValueError):
    """A new profile overlaps an existing interval for the ISIN with different facts."""


@dataclass(frozen=True)
class ProfileRow:
    isin: str
    valid_from: date
    valid_to: date
    facts: FundTaxFacts
    profile_version: int
    source: str = "OPERATOR"


def _rows(conn: Any, isins: list[str]) -> dict[str, list[ProfileRow]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (isin, source, valid_from) isin, valid_from, valid_to,
                      equity_oriented, specified_fund, listed, domiciled_in_india,
                      evidence_url, profile_version, source
               FROM fund_tax_profile WHERE isin = ANY(%s)
               ORDER BY isin, source, valid_from, profile_version DESC""",
            (isins,),
        )
        out: dict[str, list[ProfileRow]] = {}
        for isin, start, end, eq, spec, listed, domestic, url, version, source in cur.fetchall():
            out.setdefault(isin, []).append(
                ProfileRow(
                    isin,
                    start,
                    end,
                    FundTaxFacts(
                        equity_oriented=eq,
                        specified_fund=spec,
                        listed=listed,
                        domiciled_in_india=domestic,
                        evidence=url,
                    ),
                    version,
                    source,
                )
            )
        return out


def facts_by_isin(conn: Any, isins: list[str]) -> dict[str, list[ProfileRow]]:
    if not isins:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('fund_tax_profile') IS NOT NULL")
        if not cur.fetchone()[0]:
            return {}
    return _rows(conn, isins)


def facts_on(rows: list[ProfileRow], on: date) -> FundTaxFacts | None:
    """The facts covering ``on``; None when no interval covers it, or when two covering
    intervals disagree (a conflict is not resolved by picking one). Operator-recorded
    evidence, when it covers the date, is used instead of category-derived facts."""
    covering = [r for r in rows if r.valid_from <= on <= r.valid_to]
    operator = [r for r in covering if r.source == "OPERATOR"]
    covering = operator or covering
    distinct = {
        (
            r.facts.equity_oriented,
            r.facts.specified_fund,
            r.facts.listed,
            r.facts.domiciled_in_india,
        )
        for r in covering
    }
    if len(distinct) != 1:
        return None
    return covering[0].facts


def record_profile(
    conn: Any,
    isin: str,
    valid_from: date,
    valid_to: date,
    facts: FundTaxFacts,
    evidence_url: str,
    evidence_note: str,
    evidence_sha256: str | None = None,
) -> int:
    """Record evidenced facts for an interval. Refuses an overlap that disagrees."""
    if valid_from > valid_to:
        raise ValueError("valid_from must be on or before valid_to")
    existing = [r for r in _rows(conn, [isin]).get(isin, []) if r.source == "OPERATOR"]
    key = (facts.equity_oriented, facts.specified_fund, facts.listed, facts.domiciled_in_india)
    for row in existing:
        overlaps = row.valid_from <= valid_to and valid_from <= row.valid_to
        same = (
            row.facts.equity_oriented,
            row.facts.specified_fund,
            row.facts.listed,
            row.facts.domiciled_in_india,
        ) == key
        if overlaps and not same and row.valid_from != valid_from:
            raise ConflictingProfile(
                f"{isin}: {valid_from}..{valid_to} overlaps {row.valid_from}..{row.valid_to} "
                "with different facts; close or revise that interval first"
            )
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO fund_tax_profile (isin, valid_from, valid_to, equity_oriented,
                   specified_fund, listed, domiciled_in_india, evidence_url, evidence_sha256,
                   evidence_note, profile_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   COALESCE((SELECT max(profile_version) + 1 FROM fund_tax_profile
                             WHERE isin=%s AND valid_from=%s AND source='OPERATOR'), 1))
               RETURNING profile_version""",
            (
                isin,
                valid_from,
                valid_to,
                facts.equity_oriented,
                facts.specified_fund,
                facts.listed,
                facts.domiciled_in_india,
                evidence_url,
                evidence_sha256,
                evidence_note,
                isin,
                valid_from,
            ),
        )
        return cur.fetchone()[0]


def taxpayer_profile(conn: Any, investor_id: str) -> TaxpayerProfile | None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('investor_tax_profile') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute(
            "SELECT taxpayer_type, residency FROM investor_tax_profile WHERE investor_id=%s",
            (investor_id,),
        )
        row = cur.fetchone()
        return TaxpayerProfile(row[0], row[1]) if row else None


def set_taxpayer_profile(conn: Any, investor_id: str, taxpayer_type: str, residency: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO investor_tax_profile (investor_id, taxpayer_type, residency)
               VALUES (%s,%s,%s) ON CONFLICT (investor_id) DO UPDATE SET
               taxpayer_type=EXCLUDED.taxpayer_type, residency=EXCLUDED.residency,
               updated_at=now()""",
            (investor_id, taxpayer_type, residency),
        )


def derive_category_profiles(conn: Any, isins: list[str] | None = None) -> int:
    """Regenerate the SEBI-category fund-tax facts for relevant fund ISINs.

    For each ISIN printed against exactly one AMFI scheme, every heading that scheme
    was observed under yields the facts its SEBI mandate proves, over the dates it was
    observed there (plus the 7-day NAV carry). Derived rows are replaced wholesale;
    operator-recorded rows are never touched. Returns rows written.
    """
    from datetime import timedelta

    from app.db.fund_reference import MAX_NAV_AGE_DAYS, fund_tables_present, relevant_isins
    from app.engine.fund_tax_category import derive_facts

    if not fund_tables_present(conn):
        return 0
    wanted = sorted(isins if isins is not None else relevant_isins(conn))
    if not wanted:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """WITH single AS (
                   SELECT isin, min(scheme_code) AS scheme_code FROM amfi_scheme_isin
                   WHERE isin = ANY(%s) GROUP BY isin HAVING count(DISTINCT scheme_code) = 1
               )
               SELECT s.isin, h.heading_key, h.structure, a.instrument_kind,
                      h.first_seen_on, h.last_seen_on
               FROM single s JOIN amfi_scheme a USING (scheme_code)
               JOIN amfi_scheme_heading h USING (scheme_code)""",
            (wanted,),
        )
        observed = cur.fetchall()
        cur.execute(
            "DELETE FROM fund_tax_profile WHERE source = 'SEBI_CATEGORY' AND isin = ANY(%s)",
            (wanted,),
        )
        rows = []
        for isin, key, structure, kind, first, last in observed:
            for derived in derive_facts(
                key, structure, kind == "ETF", first, last + timedelta(days=MAX_NAV_AGE_DAYS)
            ):
                f = derived.facts
                rows.append(
                    (
                        isin,
                        derived.valid_from,
                        derived.valid_to,
                        f.equity_oriented,
                        f.specified_fund,
                        f.listed,
                        f.domiciled_in_india,
                        f.evidence,
                        derived.note,
                    )
                )
        if rows:
            cur.executemany(
                """INSERT INTO fund_tax_profile (isin, valid_from, valid_to, equity_oriented,
                       specified_fund, listed, domiciled_in_india, evidence_url,
                       evidence_note, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'SEBI_CATEGORY')
                   ON CONFLICT DO NOTHING""",
                rows,
            )
    return len(rows)
