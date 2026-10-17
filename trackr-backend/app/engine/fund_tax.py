"""Capital-gains branch, term and base rate for one realised slice of a FUND unit.

docs/MF_ETF_PLAN.md section 5. Pure: the caller supplies the dated facts. Equity shares
are NOT classified here -- their path (``app.engine.lots`` / ``tax_classification``) is
unchanged and covered by the golden fixture.

The order of tests is the statute's (plan section 5.2):

1. SPECIFIED FUND, for units acquired on/after 1-Apr-2023: deemed short term whatever
   the holding period, taxed at ordinary rates, no indexation (1961 Act s.50AA; 2025 Act
   s.76). The DEFINITION differs by disposal period -- not more than 35% domestic equity
   for FY 2023-24 and 2024-25 disposals, >65% debt/money-market (or a fund of such funds)
   from FY 2025-26 -- so the profile fact supplied must be for the disposal's period.
2. EQUITY-ORIENTED fund: long term after MORE than 12 months; special rates
   (s.111A / s.112A; 2025 Act s.196 / s.198), which require STT on the transfer.
3. Anything else: ordinary fund rules -- long term after more than 36 months before
   23-Jul-2024; afterwards more than 12 months if listed, 24 if not; LTCG 20% with
   indexation before 23-Jul-2024, 12.5% without it after (s.112; 2025 Act s.197).

A fact the branch needs and the caller does not have is NOT defaulted: the slice comes
back unresolved with the missing fact named. Economic gain is always reported; a legal
rate is never guessed (plan section 5.5).

Rates are BASE rates. Surcharge, cess, the annual equity exemption (one threshold per
taxpayer per year, shared with listed shares -- never per folio or per broker) and the
taxpayer's slab rate for ordinary-rate gains are applied at the report level with the
taxpayer's own inputs; they are not this function's to decide.

Holding periods are CALENDAR months, not 365-day multiples: exactly 12 (24, 36) months
is short term, strictly beyond is long term (plan section 5.3).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

SPECIFIED_FUND_ACQUIRED_FROM = date(2023, 4, 1)
FINANCE_ACT_2024_CUTOVER = date(2024, 7, 23)
INCOME_TAX_ACT_2025_FROM = date(2026, 4, 1)
GRANDFATHERING_CUTOFF = date(2018, 2, 1)

RULE_VERSION = "fund-tax-2026-10-13"


class Branch(StrEnum):
    SPECIFIED_FUND = "SPECIFIED_FUND"
    EQUITY_ORIENTED = "EQUITY_ORIENTED"
    OTHER_FUND = "OTHER_FUND"


class FundTerm(StrEnum):
    SHORT = "SHORT"
    LONG = "LONG"
    DEEMED_SHORT = "DEEMED_SHORT"


class RateStatus(StrEnum):
    #: A special base rate applies (``base_rate`` set).
    SPECIAL_RATE = "SPECIAL_RATE"
    #: Taxed at the taxpayer's ordinary (slab) rates: needs their income to compute.
    ORDINARY_RATES = "ORDINARY_RATES"


@dataclass(frozen=True)
class TaxpayerProfile:
    taxpayer_type: str  # INDIVIDUAL, HUF, ...
    residency: str  # RESIDENT, NON_RESIDENT


@dataclass(frozen=True)
class FundTaxFacts:
    """Evidenced facts for the disposal's period. ``None`` is UNKNOWN, never False."""

    equity_oriented: bool | None
    specified_fund: bool | None
    listed: bool | None
    domiciled_in_india: bool = True
    evidence: str | None = None


@dataclass(frozen=True)
class FundTaxResult:
    act: str
    branch: Branch | None = None
    term: FundTerm | None = None
    rate_status: RateStatus | None = None
    base_rate: Decimal | None = None
    indexation: bool = False
    section: str | None = None
    #: Set when no legal classification could be made; names the missing fact.
    unresolved_reason: str | None = None
    #: For a grandfathered equity-oriented lot: the 31-Jan-2018 fair value per unit the
    #: caller must apply (cost = max(actual, min(fmv, sale price))).
    grandfathering_fmv: Decimal | None = None
    rule_version: str = RULE_VERSION

    @property
    def resolved(self) -> bool:
        return self.unresolved_reason is None


def act_for(disposal: date) -> str:
    """The Act governing the income: the transfer's date, never the filing date."""
    return (
        "Income-tax Act, 2025" if disposal >= INCOME_TAX_ACT_2025_FROM else "Income-tax Act, 1961"
    )


#: (1961 Act, 2025 Act) section labels, per the CBDT correspondence table.
_SECTIONS = {
    "specified": ("s.50AA", "s.76"),
    "equity_short": ("s.111A", "s.196"),
    "equity_long": ("s.112A", "s.198"),
    "other_long": ("s.112", "s.197"),
}


def _section(key: str, disposal: date) -> str:
    old, new = _SECTIONS[key]
    return new if disposal >= INCOME_TAX_ACT_2025_FROM else old


def add_months(start: date, months: int) -> date:
    """``start`` plus whole calendar months, clamped to the target month's last day."""
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def held_more_than(acquired: date, disposed: date, months: int) -> bool:
    return disposed > add_months(acquired, months)


def classify_fund_slice(
    acquired: date,
    disposed: date,
    facts: FundTaxFacts | None,
    taxpayer: TaxpayerProfile | None,
    *,
    stt_paid_on_transfer: bool | None,
    fmv_31jan2018: Decimal | None = None,
) -> FundTaxResult:
    """Classify one FIFO slice. See the module docstring for the rules.

    ``fmv_31jan2018`` is the unit's fair market value on 31-Jan-2018 (an unlisted unit's
    NAV that day; a listed unit's highest exchange price), needed only for an
    equity-oriented long-term lot acquired before 1-Feb-2018. None means not evidenced.
    """
    act = act_for(disposed)

    def unresolved(reason: str, **known: object) -> FundTaxResult:
        return FundTaxResult(act=act, unresolved_reason=reason, **known)  # type: ignore[arg-type]

    if taxpayer is None:
        return unresolved("Taxpayer type and residency are not recorded for this investor")
    if taxpayer.residency != "RESIDENT" or taxpayer.taxpayer_type not in ("INDIVIDUAL", "HUF"):
        return unresolved(
            f"Fund tax rules here cover resident individuals and HUFs; this investor is "
            f"{taxpayer.residency.lower()} {taxpayer.taxpayer_type.lower()}"
        )
    if facts is None:
        return unresolved("No evidenced tax profile for this fund covers the disposal date")
    if not facts.domiciled_in_india:
        return unresolved("Foreign-domiciled fund units are not covered by these rules")

    if acquired >= SPECIFIED_FUND_ACQUIRED_FROM:
        if facts.specified_fund is None:
            return unresolved(
                "Acquired on/after 1-Apr-2023: whether the fund was a specified fund for "
                "this disposal's period is not evidenced"
            )
        if facts.specified_fund:
            return FundTaxResult(
                act=act,
                branch=Branch.SPECIFIED_FUND,
                term=FundTerm.DEEMED_SHORT,
                rate_status=RateStatus.ORDINARY_RATES,
                section=_section("specified", disposed),
            )

    if facts.equity_oriented is None:
        return unresolved("Whether the fund was equity-oriented for this period is not evidenced")

    if facts.equity_oriented:
        long_term = held_more_than(acquired, disposed, 12)
        term = FundTerm.LONG if long_term else FundTerm.SHORT
        known = dict(branch=Branch.EQUITY_ORIENTED, term=term)
        grandfathered = long_term and acquired < GRANDFATHERING_CUTOFF
        if grandfathered and fmv_31jan2018 is None:
            return unresolved(
                "Acquired before 1-Feb-2018: the 31-Jan-2018 fair value (NAV for an unlisted "
                "unit) is needed for grandfathering",
                **known,
            )
        if stt_paid_on_transfer is not True:
            return unresolved(
                "STT on the transfer is not evidenced; the special equity rate requires it",
                **known,
            )
        before = disposed < FINANCE_ACT_2024_CUTOVER
        if long_term:
            rate = Decimal("0.10") if before else Decimal("0.125")
            section = _section("equity_long", disposed)
        else:
            rate = Decimal("0.15") if before else Decimal("0.20")
            section = _section("equity_short", disposed)
        return FundTaxResult(
            act=act,
            rate_status=RateStatus.SPECIAL_RATE,
            base_rate=rate,
            section=section,
            grandfathering_fmv=fmv_31jan2018 if grandfathered else None,
            **known,  # type: ignore[arg-type]
        )

    # Ordinary fund rules.
    if disposed < FINANCE_ACT_2024_CUTOVER:
        long_term = held_more_than(acquired, disposed, 36)
        if not long_term:
            return FundTaxResult(
                act=act,
                branch=Branch.OTHER_FUND,
                term=FundTerm.SHORT,
                rate_status=RateStatus.ORDINARY_RATES,
            )
        return FundTaxResult(
            act=act,
            branch=Branch.OTHER_FUND,
            term=FundTerm.LONG,
            rate_status=RateStatus.SPECIAL_RATE,
            base_rate=Decimal("0.20"),
            indexation=True,
            section=_section("other_long", disposed),
        )
    if facts.listed is None:
        return unresolved(
            "Whether the units were listed at transfer is not evidenced (it sets the 12- or "
            "24-month test)",
            branch=Branch.OTHER_FUND,
        )
    long_term = held_more_than(acquired, disposed, 12 if facts.listed else 24)
    if not long_term:
        return FundTaxResult(
            act=act,
            branch=Branch.OTHER_FUND,
            term=FundTerm.SHORT,
            rate_status=RateStatus.ORDINARY_RATES,
        )
    return FundTaxResult(
        act=act,
        branch=Branch.OTHER_FUND,
        term=FundTerm.LONG,
        rate_status=RateStatus.SPECIAL_RATE,
        base_rate=Decimal("0.125"),
        section=_section("other_long", disposed),
    )
