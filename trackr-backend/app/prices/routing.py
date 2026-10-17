"""Which price source values a security: the exchange, AMFI's NAV, or neither yet.

docs/MF_ETF_PLAN.md section 4. ONE decision, applied at every price boundary -- what a
bhavcopy may record as absent, which dates a backfill must fetch, which ISINs a live quote
is requested for, and which store values a holding -- so the same ISIN is never valued
from two sources on one screen, and a fund that never trades on an exchange never collects
"absent from the bhavcopy" evidence that would later exclude it.

    EXCHANGE    official exchange close (stocks, ETFs) -- the existing listed pipe
    AMFI        AMFI NAV (mutual fund units)
    UNRESOLVED  not enough evidence for either; visible and retried, never defaulted

The rules, in order:

1. An ISIN AMFI prints against MORE THAN ONE scheme code is UNRESOLVED: two schemes'
   NAVs cannot both be its price, and picking one would be a guess.
2. An ISIN AMFI prints under a reviewed ETF heading is EXCHANGE: an ETF's price is its
   official exchange close, and its NAV is reference data only.
3. An ISIN AMFI prints under any other reviewed heading is AMFI.
4. An ``INF`` ISIN AMFI does not list is EXCHANGE only on positive exchange evidence --
   an exchange bhavcopy has printed it -- and otherwise UNRESOLVED. ``INF`` alone cannot
   tell an ETF from an unlisted fund.
5. Anything else keeps the exchange route it has always had. That is the baseline every
   listed stock in the ledger was built on; the plan's "known existing stocks retain their
   exchange path". An unpriced stock is still named as unpriced downstream, as before.

Pure: the caller gathers the evidence (``app.db.fund_reference.valuation_routes``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.prices.fund_category import Exposure, InstrumentKind


class Route(StrEnum):
    EXCHANGE = "EXCHANGE"
    AMFI = "AMFI"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class CatalogueEntry:
    scheme_code: int
    instrument_kind: InstrumentKind
    exposure: Exposure
    category: str
    scheme_name: str
    amc_name: str
    plan: str | None
    option: str | None


@dataclass(frozen=True)
class RouteDecision:
    isin: str
    route: Route
    instrument_kind: InstrumentKind
    reason: str
    exposure: Exposure | None = None
    scheme_code: int | None = None
    category: str | None = None
    scheme_name: str | None = None
    amc_name: str | None = None
    plan: str | None = None
    option: str | None = None


def decide_route(
    isin: str, catalogue: list[CatalogueEntry], exchange_evidence: bool
) -> RouteDecision:
    codes = {entry.scheme_code for entry in catalogue}
    if len(codes) > 1:
        return RouteDecision(
            isin,
            Route.UNRESOLVED,
            InstrumentKind.UNRESOLVED,
            f"AMFI prints this ISIN against {len(codes)} schemes ({sorted(codes)})",
        )
    if catalogue:
        entry = catalogue[0]
        detail = dict(
            exposure=entry.exposure,
            scheme_code=entry.scheme_code,
            category=entry.category,
            scheme_name=entry.scheme_name,
            amc_name=entry.amc_name,
            plan=entry.plan,
            option=entry.option,
        )
        if entry.instrument_kind == InstrumentKind.ETF:
            return RouteDecision(
                isin, Route.EXCHANGE, InstrumentKind.ETF, "AMFI lists it as an ETF", **detail
            )
        return RouteDecision(
            isin,
            Route.AMFI,
            InstrumentKind.MUTUAL_FUND,
            "AMFI lists it as a mutual fund scheme",
            **detail,
        )
    if isin.startswith("INF"):
        if exchange_evidence:
            return RouteDecision(
                isin,
                Route.EXCHANGE,
                InstrumentKind.UNRESOLVED,
                "Fund unit an exchange bhavcopy has priced; AMFI does not list it",
            )
        return RouteDecision(
            isin,
            Route.UNRESOLVED,
            InstrumentKind.UNRESOLVED,
            "Fund unit with no AMFI scheme record and no exchange price yet",
        )
    return RouteDecision(isin, Route.EXCHANGE, InstrumentKind.STOCK, "Listed security")
