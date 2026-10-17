"""GET /investors/{id}/capital-gains?fy=YYYY-YY -- the year's capital-gains schedule.

Listed shares and fund units together, because the s.112A exemption is one per taxpayer
per year and losses set off across them (``app.engine.capital_gains_schedule``). Each
realised slice is classified on its own path:

* shares: the existing equity contract (STT-paid listed shares, 365-day term,
  ``app.engine.lots``), with s.112A grandfathering from ``security.fmv_31jan2018``;
* fund units: ``app.engine.fund_tax.classify_fund_slice`` per account FIFO, with the
  31-Jan-2018 NAV (unlisted units) or loaded FMV (ETFs) for grandfathering and the CBDT
  CII for indexation.

A slice that cannot be classified is never guessed: it is listed in ``unresolved`` with
its reason, and the schedule says it is incomplete. F&O profit is business income and is
not part of this schedule.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import securities
from app.auth.dependencies import require_owned_investor
from app.config import Settings, get_settings
from app.database import get_conn, money, rate
from app.db.fund_reference import valuation_routes
from app.db.fund_tax import facts_by_isin, facts_on, taxpayer_profile
from app.db.repository import load_transactions
from app.engine.capital_gains_schedule import Bucket, ScheduleSlice, build_schedule
from app.engine.errors import MissingCIIError
from app.engine.fund_tax import (
    FINANCE_ACT_2024_CUTOVER,
    GRANDFATHERING_CUTOFF,
    Branch,
    FundTerm,
    act_for,
    classify_fund_slice,
)
from app.engine.holdings import exclude_unreconciled
from app.engine.indexation import indexed_cost_of_acquisition
from app.engine.realised import compute_realised, tax_cost, tax_proceeds
from app.engine.tax_classification import classification_sensitive
from app.engine.types import Term
from app.prices.fund_category import InstrumentKind
from app.prices.routing import Route
from app.routers.transactions import _financial_year_bounds, _fund_realised

router = APIRouter(tags=["tax"])


class LineOut(BaseModel):
    bucket: str
    base_rate: str | None
    slices: int
    gains: str
    losses: str
    net: str
    set_off_received: str
    exemption: str
    taxable: str
    base_tax: str | None


class SliceOut(BaseModel):
    isin: str
    name: str | None
    instrument_kind: str
    sell_date: date
    acquired: date
    quantity: str
    consideration: str
    cost: str
    gain: str
    bucket: str
    base_rate: str | None
    section: str | None
    note: str | None


class UnresolvedOut(BaseModel):
    isin: str
    name: str | None
    sell_date: date | None
    gain: str
    reason: str


class CapitalGainsOut(BaseModel):
    fy: str
    complete: bool
    lines: list[LineOut]
    exemption_limit: str
    exemption_used: str
    special_rate_tax: str
    ordinary_rate_taxable: str
    carry_forward_short_term_loss: str
    carry_forward_long_term_loss: str
    set_off_order: list[str]
    slices: list[SliceOut]
    unresolved: list[UnresolvedOut]
    excluded: list[dict[str, Any]]
    notes: list[str]


def fy_label(on: date) -> str:
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def _grandfathered(cost: Decimal, consideration: Decimal, qty: Decimal, fmv: Decimal) -> Decimal:
    """s.55(2)(ac): cost = max(actual, min(FMV on 31-Jan-2018, sale value)), per unit."""
    per_unit = max(cost / qty, min(fmv, consideration / qty))
    return per_unit * qty


def _equity_rate(long_term: bool, sold: date) -> Decimal:
    before = sold < FINANCE_ACT_2024_CUTOVER
    if long_term:
        return Decimal("0.10") if before else Decimal("0.125")
    return Decimal("0.15") if before else Decimal("0.20")


def _section(bucket: Bucket, sold: date) -> str:
    new = sold >= date(2026, 4, 1)
    return {
        Bucket.EQUITY_STCG: "s.196" if new else "s.111A",
        Bucket.EQUITY_LTCG: "s.198" if new else "s.112A",
        Bucket.OTHER_LTCG: "s.197" if new else "s.112",
        Bucket.ORDINARY_STCG: "slab",
    }[bucket]


@router.get(
    "/investors/{investor_id}/capital-gains",
    response_model=CapitalGainsOut,
    dependencies=[Depends(require_owned_investor)],
)
def capital_gains(
    investor_id: str,
    fy: str,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    start, end = _financial_year_bounds(fy)
    if start < date(2018, 4, 1):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Capital-gains schedules start at FY 2018-19"
        )
    rows, skipped = exclude_unreconciled(load_transactions(connection, investor_id=investor_id))
    routes = valuation_routes(connection, {t.isin for t in rows})
    fund_isins = {i for i, d in routes.items() if d.instrument_kind != InstrumentKind.STOCK}
    in_year = lambda g: g.sell_date is not None and start <= g.sell_date <= end  # noqa: E731

    slices: list[ScheduleSlice] = []
    unresolved: list[dict[str, Any]] = []

    with connection.cursor() as cur:
        cur.execute(
            "SELECT isin, fmv_31jan2018 FROM security WHERE isin = ANY(%s) "
            "AND fmv_31jan2018 IS NOT NULL",
            (sorted(routes),),
        )
        fmv = dict(cur.fetchall())
        cur.execute("SELECT financial_year, cii_value FROM cost_inflation_index")
        cii = dict(cur.fetchall())

    # --- shares: the existing equity contract -------------------------------------------
    for g in compute_realised([t for t in rows if t.isin not in fund_isins]):
        if not in_year(g):
            continue
        acquired = g.sell_date - timedelta(days=g.holding_days)
        consideration, cost = tax_proceeds(g), tax_cost(g)
        long_term = g.term == Term.LONG
        note = None
        if long_term and acquired < GRANDFATHERING_CUTOFF:
            if g.isin not in fmv:
                unresolved.append(
                    _unresolved(
                        g,
                        "Acquired before 1-Feb-2018: the 31-Jan-2018 FMV for this "
                        "share is not loaded, so grandfathering cannot be applied",
                    )
                )
                continue
            cost = _grandfathered(cost, consideration, g.quantity, fmv[g.isin])
            note = "grandfathered (31-Jan-2018 FMV)"
        if classification_sensitive(g, sensitivity_days=settings.bonus_allotment_sensitivity_days):
            note = "term rests on a provisional bonus allotment date"
        bucket = Bucket.EQUITY_LTCG if long_term else Bucket.EQUITY_STCG
        slices.append(
            ScheduleSlice(
                isin=g.isin,
                instrument_kind="STOCK",
                sell_date=g.sell_date,
                acquired=acquired,
                quantity=g.quantity,
                consideration=consideration,
                cost=cost,
                bucket=bucket,
                base_rate=_equity_rate(long_term, g.sell_date),
                section=_section(bucket, g.sell_date),
                note=note,
            )
        )

    # --- fund units --------------------------------------------------------------------
    fund_slices, fund_skipped = _fund_realised([t for t in rows if t.isin in fund_isins], routes)
    skipped = skipped + fund_skipped
    fund_slices = [(a, g) for a, g in fund_slices if in_year(g)]
    if fund_slices:
        profiles = facts_by_isin(connection, sorted({g.isin for _a, g in fund_slices}))
        taxpayer = taxpayer_profile(connection, investor_id)
        navs_31jan = _nav_31jan2018(connection, [g.isin for _a, g in fund_slices])
    for _account, g in fund_slices:
        if not settings.mf_tax_enabled:
            unresolved.append(
                _unresolved(
                    g, "Fund capital-gains classification is not enabled on this deployment"
                )
            )
            continue
        acquired = g.sell_date - timedelta(days=g.holding_days)
        route = routes[g.isin]
        fair_value = navs_31jan.get(g.isin) if route.route == Route.AMFI else fmv.get(g.isin)
        result = classify_fund_slice(
            acquired,
            g.sell_date,
            facts_on(profiles.get(g.isin, []), g.sell_date),
            taxpayer,
            # A sale or redemption of a unit carries STT by statute where it applies
            # (Finance (No. 2) Act 2004, Chapter VII); the ledger SELL is that transfer.
            stt_paid_on_transfer=True,
            fmv_31jan2018=fair_value,
        )
        if not result.resolved:
            unresolved.append(_unresolved(g, result.unresolved_reason))
            continue
        consideration, cost = tax_proceeds(g), tax_cost(g)
        note = None
        if result.branch == Branch.SPECIFIED_FUND or (
            result.branch == Branch.OTHER_FUND and result.term == FundTerm.SHORT
        ):
            bucket, base = Bucket.ORDINARY_STCG, None
        elif result.branch == Branch.EQUITY_ORIENTED:
            bucket = Bucket.EQUITY_LTCG if result.term == FundTerm.LONG else Bucket.EQUITY_STCG
            base = result.base_rate
            if result.grandfathering_fmv is not None:
                cost = _grandfathered(cost, consideration, g.quantity, result.grandfathering_fmv)
                note = "grandfathered (31-Jan-2018 NAV/FMV)"
        else:
            bucket, base = Bucket.OTHER_LTCG, result.base_rate
            if result.indexation:
                try:
                    cost = indexed_cost_of_acquisition(
                        cost, fy_label(acquired), fy_label(g.sell_date), cii
                    )
                except MissingCIIError as exc:
                    unresolved.append(_unresolved(g, f"Cost Inflation Index missing: {exc}"))
                    continue
                note = f"indexed ({fy_label(acquired)} -> {fy_label(g.sell_date)})"
        slices.append(
            ScheduleSlice(
                isin=g.isin,
                instrument_kind=str(route.instrument_kind),
                sell_date=g.sell_date,
                acquired=acquired,
                quantity=g.quantity,
                consideration=consideration,
                cost=cost,
                bucket=bucket,
                base_rate=base,
                section=result.section or _section(bucket, g.sell_date),
                note=note,
            )
        )

    schedule = build_schedule(fy, start, slices)
    meta = securities.lookup(
        connection, sorted({s.isin for s in slices} | {u["isin"] for u in unresolved})
    )
    for item in unresolved:
        item["name"] = securities.display_name(meta, item["isin"])
    notes = list(schedule.notes)
    notes.append(f"Act: {act_for(end)} for disposals in this year.")
    if unresolved or skipped:
        notes.insert(
            0,
            "INCOMPLETE: the gains listed under 'unresolved' and 'excluded' are "
            "not in these totals.",
        )
    return {
        "fy": fy,
        "complete": not unresolved and not skipped,
        "lines": [
            {
                "bucket": str(line.bucket),
                "base_rate": rate(line.base_rate),
                "slices": line.slices,
                "gains": money(line.gains),
                "losses": money(line.losses),
                "net": money(line.net),
                "set_off_received": money(line.set_off_received),
                "exemption": money(line.exemption),
                "taxable": money(line.taxable),
                "base_tax": money(line.base_tax),
            }
            for line in schedule.lines
        ],
        "exemption_limit": money(schedule.exemption_limit),
        "exemption_used": money(schedule.exemption_used),
        "special_rate_tax": money(schedule.special_rate_tax),
        "ordinary_rate_taxable": money(schedule.ordinary_rate_taxable),
        "carry_forward_short_term_loss": money(schedule.carry_forward_short_term_loss),
        "carry_forward_long_term_loss": money(schedule.carry_forward_long_term_loss),
        "set_off_order": list(schedule.set_off_order),
        "slices": [
            {
                "isin": s.isin,
                "name": securities.display_name(meta, s.isin),
                "instrument_kind": s.instrument_kind,
                "sell_date": s.sell_date,
                "acquired": s.acquired,
                "quantity": money(s.quantity),
                "consideration": money(s.consideration),
                "cost": money(s.cost),
                "gain": money(s.gain),
                "bucket": str(s.bucket),
                "base_rate": rate(s.base_rate),
                "section": s.section,
                "note": s.note,
            }
            for s in sorted(slices, key=lambda s: (s.sell_date, s.isin))
        ],
        "unresolved": unresolved,
        "excluded": [{"isin": s.isin, "message": s.message} for s in skipped],
        "notes": notes,
    }


def _unresolved(g: Any, reason: str) -> dict[str, Any]:
    return {
        "isin": g.isin,
        "name": None,
        "sell_date": g.sell_date,
        "gain": money(g.gain),
        "reason": reason,
    }


def _nav_31jan2018(connection: Any, isins: list[str]) -> dict[str, Decimal]:
    """An unlisted unit's FMV on 31-Jan-2018 is its NAV that day (s.55(2)(ac))."""
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('fund_nav') IS NOT NULL")
        if not cur.fetchone()[0]:
            return {}
        cur.execute(
            """SELECT i.isin, n.nav FROM amfi_scheme_isin i JOIN fund_nav n USING (scheme_code)
               WHERE i.isin = ANY(%s) AND n.nav_date = '2018-01-31'""",
            (isins,),
        )
        return dict(cur.fetchall())
