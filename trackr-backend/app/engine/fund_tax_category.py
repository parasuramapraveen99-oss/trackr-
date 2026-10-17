"""Fund-tax facts PROVEN by a scheme's SEBI category -- docs/MF_ETF_PLAN.md section 5.

A SEBI category is a regulatory mandate on the scheme's asset allocation (SEBI circular
"Categorization and Rationalization of Mutual Fund Schemes", 6-Oct-2017, revised
6-Nov-2020, consolidated as clause 2.6 of the Master Circular of 27-Jun-2024 and replaced
by HO/24/13/15(2)2026-IMD-RAC4/I/5764/2026 of 26-Feb-2026). Where that mandate settles a
statutory test on its own, the scheme's observed category is evidence of the fact; where
it does not, NO fact is derived and the slice stays unresolved until someone records
evidence (``/ops/funds/tax-profile``). A category is never stretched to fit.

What the mandates settle (each line is the circular's own definition):

* EQUITY-ORIENTED (s.112A / 2025 Act s.198: a minimum of 65% in equity of domestic listed
  companies). Proven only where the mandate is in Indian market-capitalisation buckets
  (large/mid/small cap companies are ranked within India, Master Circular clause 2.7):
  Large Cap 80%, Large & Mid Cap 35% + 35%, Mid Cap 65%, Small Cap 65%, Multi Cap 25% x 3
  (the cap-wise split applies from the 2020 revision, complied with by 31-Jan-2021), and
  ELSS 80% (ELSS 2005 scheme). NOT proven for Flexi Cap, Value, Contra, Focused, Dividend
  Yield, Sectoral or Thematic: their minimum is "equity & equity related instruments"
  and foreign securities are not a separate asset class (2026 circular 2.6.3.17), so the
  domestic share is not fixed by the mandate. Not for hybrids, index funds or ETFs either.
* SPECIFIED FUND. Old definition (disposals FY 2023-24, 2024-25): not more than 35% in
  equity shares of domestic companies. New definition (from FY 2025-26): more than 65% in
  debt and money-market instruments. Debt-scheme categories invest predominantly in debt
  with at most InvIT/REIT units as residual (each capped at 10% of NAV by the MF
  Regulations): both definitions hold. Conservative Hybrid (75-90% debt, 10-25% equity):
  both hold. Gold/Silver ETFs (>=95% in the metal): the old one holds (no domestic
  equity), the new one does not (not debt). Debt ETFs and debt index funds (>=95% in a
  debt index): both hold.
* LISTED: an ETF is listed (that is what makes it one); an open-ended fund's units are
  not; a close-ended scheme's listing is not settled by its category.

Pure: the caller supplies the observed heading interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.engine.fund_tax import FundTaxFacts

#: SEBI categorisation in force (schemes re-categorised by mid-2018).
CATEGORISATION_FROM = date(2018, 6, 1)
#: The 2020 revision's cap-wise Multi Cap mandate, compliance date.
MULTI_CAP_SPLIT_FROM = date(2021, 2, 1)
OLD_SPECIFIED_DEFINITION_TO = date(2025, 3, 31)
NEW_SPECIFIED_DEFINITION_FROM = date(2025, 4, 1)

#: The 26-Feb-2026 circular (text read 14-Oct-2026; the mandates cited above are its
#: own definitions, unchanged in substance from the 2017/2020 circulars for the
#: categories used here).
CIRCULAR = (
    "https://www.sebi.gov.in/legal/circulars/feb-2026/"
    "categorization-and-rationalization-of-mutual-fund-schemes_99983.html"
)

_EQUITY_PROVEN = {
    "Large Cap Fund": CATEGORISATION_FROM,
    "Large & Mid Cap Fund": CATEGORISATION_FROM,
    "Mid Cap Fund": CATEGORISATION_FROM,
    "Small Cap Fund": CATEGORISATION_FROM,
    "Multi Cap Fund": MULTI_CAP_SPLIT_FROM,
}
_DEBT_CATEGORIES = {
    "Banking and PSU Fund",
    "Corporate Bond Fund",
    "Credit Risk Fund",
    "Dynamic Bond",
    "Gilt Fund",
    "Liquid Fund",
    "Long Duration Fund",
    "Low Duration Fund",
    "Medium Duration Fund",
    "Medium to Long Duration Fund",
    "Money Market Fund",
    "Overnight Fund",
    "Short Duration Fund",
    "Ultra Short Duration Fund",
}


@dataclass(frozen=True)
class DerivedFacts:
    valid_from: date
    valid_to: date
    facts: FundTaxFacts
    note: str


def _scheme_family(heading_key: str) -> tuple[str, str] | None:
    """(family, name) from a reviewed heading key, e.g. ("equity", "Large Cap Fund")."""
    for prefix, family in (
        ("Equity Scheme - ", "equity"),
        ("Equity Schemes - ", "equity"),
        ("Debt Scheme - ", "debt"),
    ):
        if heading_key.startswith(prefix):
            return family, heading_key[len(prefix) :]
    return None


def derive_facts(
    heading_key: str,
    structure: str,
    is_etf: bool,
    observed_from: date,
    observed_to: date,
) -> list[DerivedFacts]:
    """Facts the category proves over ``[observed_from, observed_to]``; empty when it
    proves nothing (the slice then stays unresolved, never defaulted)."""
    open_ended = structure == "Open Ended Schemes"
    listed = True if is_etf else (False if open_ended else None)
    family = _scheme_family(heading_key)

    def clip(start: date, end: date, facts: FundTaxFacts, note: str) -> list[DerivedFacts]:
        lo, hi = max(start, observed_from), min(end, observed_to)
        return [DerivedFacts(lo, hi, facts, note)] if lo <= hi else []

    far = date(9999, 12, 31)
    elss = heading_key in ("Equity Scheme - ELSS", "Equity Schemes - ELSS- Tax Saver Fund")
    if family and family[0] == "equity" and (family[1] in _EQUITY_PROVEN or elss):
        start = CATEGORISATION_FROM if elss else _EQUITY_PROVEN[family[1]]
        return clip(
            start,
            far,
            FundTaxFacts(
                equity_oriented=True, specified_fund=False, listed=listed, evidence=CIRCULAR
            ),
            f"SEBI category '{heading_key}': mandated minimum in equity of Indian "
            "market-cap-ranked companies is at least 65%",
        )

    debt_like = (
        family is not None and family[0] == "debt" and family[1] in _DEBT_CATEGORIES
    ) or heading_key in (
        "Hybrid Scheme - Conservative Hybrid Fund",
        "Hybrid Schemes - Conservative Hybrid Fund",
        "Exchange Traded Funds (ETFs) - Debt ETF",
        "Index Funds - Debt Funds",
    )
    if debt_like:
        return clip(
            CATEGORISATION_FROM,
            far,
            FundTaxFacts(
                equity_oriented=False, specified_fund=True, listed=listed, evidence=CIRCULAR
            ),
            f"SEBI category '{heading_key}': predominantly debt; at most 35% domestic "
            "equity and more than 65% debt/money-market by mandate",
        )

    if heading_key in (
        "Exchange Traded Funds (ETFs) - Gold ETF",
        "Exchange Traded Funds (ETFs) - Silver ETF",
        "Other Scheme - Gold ETF",
    ):
        note = f"SEBI category '{heading_key}': at least 95% in the metal"
        return clip(
            CATEGORISATION_FROM,
            OLD_SPECIFIED_DEFINITION_TO,
            FundTaxFacts(
                equity_oriented=False, specified_fund=True, listed=listed, evidence=CIRCULAR
            ),
            note + "; specified under the old (<=35% domestic equity) definition",
        ) + clip(
            NEW_SPECIFIED_DEFINITION_FROM,
            far,
            FundTaxFacts(
                equity_oriented=False, specified_fund=False, listed=listed, evidence=CIRCULAR
            ),
            note + "; not specified under the new (>65% debt) definition",
        )
    return []
