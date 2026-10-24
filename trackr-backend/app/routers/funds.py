"""Mutual funds: the fund series, the taxpayer profile, and AMFI operations.

docs/MF_ETF_PLAN.md sections 7, 8 and 10. Investor routes are owner-scoped like every
other ``/investors/{id}`` route. Operator routes take ``X-Ops-Key``; the daily job takes
``CRON_SECRET`` (Vercel Cron's convention, GET only).
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import securities
from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn, money, rate
from app.db import cas_folios, consent, fund_tax
from app.db import fund_reference as ref
from app.engine.fund_tax import FundTaxFacts
from app.engine.holdings import compute_holdings, exclude_unreconciled
from app.engine.snapshot import compute_snapshot
from app.engine.twrr import compute_twrr
from app.engine.types import Snapshot
from app.engine.xirr import compute_xirr, compute_xirr_holdings
from app.funds import series
from app.ingest import mf_cas_pdf
from app.prices import amfi, amfi_jobs
from app.prices.current import resolve_current_prices
from app.routers import exclusions
from app.schemas.analytical import ExcludedOut

logger = logging.getLogger(__name__)
router = APIRouter(tags=["funds"])

_DISABLED = "Mutual fund valuation is not enabled on this deployment (MF_VALUATION_ENABLED)"


class FundHoldingOut(BaseModel):
    isin: str
    name: str | None
    scheme_code: int | None
    category: str | None
    plan: str | None
    option: str | None
    quantity: str
    invested: str
    nav: str | None
    nav_date: date | None
    market_value: str | None
    reason: str | None = None


class FundPerformanceOut(BaseModel):
    as_of: date
    scope: Literal["MUTUAL_FUND"] = "MUTUAL_FUND"
    xirr_all: str | None
    xirr_holdings: str | None
    xirr_unavailable_reason: str | None
    twrr: str | None
    twrr_unavailable_reason: str | None
    invested: str
    market_value: str
    unrealised: str
    realised_to_date: str
    complete: bool
    latest_final_snapshot: date | None
    holdings: list[FundHoldingOut]
    excluded: list[ExcludedOut]


class FundPointOut(BaseModel):
    as_of: date
    invested: str
    market_value: str
    unrealised: str
    realised_to_date: str
    final: bool
    pending_isins: list[str]
    excluded_isins: list[str]


class FundStatementHoldingOut(BaseModel):
    folio_key: str
    folio_label: str
    isin: str
    name: str
    amc_name: str
    as_of: date
    quantity: str | None
    nav: str | None
    market_value: str | None
    status: Literal['DECLARED', 'CONFLICT']
    reason: str | None
    source_batch_ids: list[str]


@router.get('/investors/{investor_id}/funds/statements',
            response_model=list[FundStatementHoldingOut],
            dependencies=[Depends(require_owned_investor)])
def fund_statement_holdings(investor_id: str, as_of: date | None = None,
                           connection: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    """Dated CAS balances, available even when NAV-based return valuation is disabled."""
    return cas_folios.latest(connection, investor_id, as_of or date.today())


class MfTransactionOut(BaseModel):
    date: str
    kind: str
    description: str
    units: str | None
    nav: str | None
    amount: str | None
    balance: str | None


class MfSchemeOut(BaseModel):
    #: Masked ("…6789"), like /funds/statements. The full folio number is never returned.
    folio_label: str
    amc: str
    scheme: str
    isin: str | None
    registrar: str
    opening_units: str
    closing_units: str
    calculated_units: str
    nav: str | None
    nav_date: str | None
    value: str | None
    checks_passed: bool
    issues: list[str]
    evidence_rows: int
    transactions: list[MfTransactionOut]


class MfStatementPreviewOut(BaseModel):
    #: Always false in this version: the preview is never written to the ledger.
    #: docs/MF_ETF_PLAN.md P5 -- see app/ingest/mf_cas_pdf.py's module docstring.
    saved: Literal[False] = False
    issuer: str
    period_from: str
    period_to: str
    investor_name: str
    folio_count: int
    scheme_count: int
    transaction_count: int
    schemes_needing_attention: int
    #: False when the reader skipped or doubted part of the statement (see ``warnings``).
    complete: bool
    #: Funds with no NAV/value printed; left out of ``total_value``, never counted as 0.
    unvalued_count: int
    total_value: str
    warnings: list[str]
    schemes: list[MfSchemeOut]


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


@router.post(
    "/investors/{investor_id}/funds/statement-preview",
    response_model=MfStatementPreviewOut,
    dependencies=[Depends(require_owned_investor)],
)
async def preview_fund_statement(
    investor_id: str,
    file: UploadFile = File(...),
    password: str = Form(...),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Read a CAMS/KFintech DETAILED CAS and say what is in it. Writes nothing.

    The "Import Mutual Funds" flow's upload step. Ownership and consent are checked
    before the file is opened (MF_ETF_PLAN section 6: "Account ownership and consent
    checks apply before staging"). The file is read in memory and not stored; the
    password is passed to the PDF library once and dropped -- never saved, unlike
    /ingest/files, and never logged.
    """
    consent.require_consent(connection, investor_id)
    content = await file.read(mf_cas_pdf.MAX_BYTES + 1)
    try:
        preview = await run_in_threadpool(mf_cas_pdf.parse_statement, content, password)
    except mf_cas_pdf.MfStatementError as exc:
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "too_large"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(code, str(exc)) from exc
    finally:
        del password

    return {
        "saved": False,
        "issuer": preview.issuer,
        "period_from": preview.period_from,
        "period_to": preview.period_to,
        "investor_name": preview.investor_name,
        "folio_count": preview.folio_count,
        "scheme_count": len(preview.schemes),
        "transaction_count": preview.transaction_count,
        "schemes_needing_attention": preview.schemes_needing_attention,
        "complete": preview.complete,
        "unvalued_count": preview.unvalued_count,
        "total_value": str(preview.total_value),
        "warnings": preview.warnings,
        "schemes": [
            {
                "folio_label": s.folio_label,
                "amc": s.amc,
                "scheme": s.scheme,
                "isin": s.isin,
                "registrar": s.registrar,
                "opening_units": str(s.opening_units),
                "closing_units": str(s.closing_units),
                "calculated_units": str(s.calculated_units),
                "nav": _text(s.nav),
                "nav_date": s.nav_date,
                "value": _text(s.value),
                "checks_passed": s.checks_passed,
                "issues": s.issues,
                "evidence_rows": s.evidence_rows,
                "transactions": [
                    {
                        "date": t.date,
                        "kind": t.kind,
                        "description": t.description,
                        "units": _text(t.units),
                        "nav": _text(t.nav),
                        "amount": _text(t.amount),
                        "balance": _text(t.balance),
                    }
                    for t in s.transactions
                ],
            }
            for s in preview.schemes
        ],
    }


class TaxProfileIn(BaseModel):
    taxpayer_type: Literal["INDIVIDUAL", "HUF", "COMPANY", "FIRM", "TRUST", "OTHER"]
    residency: Literal["RESIDENT", "NON_RESIDENT"]


class TaxProfileOut(TaxProfileIn):
    supported_for_fund_tax: bool


class FundTaxProfileIn(BaseModel):
    isin: str
    valid_from: date
    valid_to: date
    equity_oriented: bool | None
    specified_fund: bool | None
    listed: bool | None
    domiciled_in_india: bool = True
    evidence_url: str
    evidence_note: str
    evidence_sha256: str | None = None


def _require_valuation(settings: Settings) -> None:
    if not settings.mf_valuation_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _DISABLED)


@router.get(
    "/investors/{investor_id}/funds/performance",
    response_model=FundPerformanceOut,
    dependencies=[Depends(require_owned_investor)],
)
def fund_performance(
    investor_id: str,
    as_of: date | None = None,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The MUTUAL_FUND scope: XIRR and TWRR over fund units only, valued at AMFI NAVs.

    Never blended with the listed-securities figures on ``/performance`` (plan section
    1). An undefined XIRR -- too few dates, no sign change -- is null with its reason,
    never 0%. What could not be valued is named in ``excluded``.
    """
    _require_valuation(settings)
    on = as_of or date.today()
    transactions = series.fund_ledger(connection, investor_id, advised_only, date_to=on)
    from app.db.fund_reference import isins_on_route
    from app.db.repository import load_transactions
    from app.prices.routing import Route

    all_tx = load_transactions(connection, investor_id=investor_id,
                               advised_only=advised_only, date_to=on)
    cas_gaps = exclusions.missing_cas_history(
        connection, investor_id, all_tx, [], on, advised_only
    )
    fund_gaps = set(isins_on_route(connection, {g.isin for g in cas_gaps}, Route.AMFI))
    transactions, skipped = exclude_unreconciled(transactions, on)
    skipped.extend(g for g in cas_gaps if g.isin in fund_gaps
                   and g.isin not in {s.isin for s in skipped})
    holdings = compute_holdings(transactions, on)
    isins = [h.isin for h in holdings]
    current = resolve_current_prices(connection, isins, on)
    transactions, isins, skipped = exclusions.drop_unpriced(
        transactions, isins, current.prices, on, skipped
    )
    prices = {i: p for i, p in current.prices.items() if i in isins}
    snapshot = compute_snapshot(transactions, prices, on)
    xirr_all = compute_xirr(transactions, prices, on) if transactions else None
    xirr_holdings = compute_xirr_holdings(transactions, prices, on) if transactions else None
    xirr_reason = None
    if not transactions:
        xirr_reason = "No fund transactions that can be valued"
    elif xirr_all is None:
        xirr_reason = "XIRR is undefined for these cash flows (too few dates or no sign change)"

    points = series.list_points(connection, investor_id, advised_only, date(1990, 1, 1), on)
    chain = [
        Snapshot(
            as_of=p["as_of"],
            invested=p["invested"],
            market_value=p["market_value"],
            unrealised=p["unrealised"],
            realised_to_date=p["realised_to_date"],
        )
        for p in points
    ]
    if not chain or chain[-1].as_of != on:
        chain.append(snapshot)
    twrr = compute_twrr(chain, transactions) if transactions else None
    twrr_reason = None if twrr is not None else "Not enough fund snapshots to chain a TWRR"
    latest_final = max((p["as_of"] for p in points if p["final"]), default=None)

    routes = ref.valuation_routes(connection, [h.isin for h in holdings])
    meta = securities.lookup(connection, [h.isin for h in holdings])
    out_holdings = []
    for h in holdings:
        quote = current.quotes.get(h.isin)
        d = routes.get(h.isin)
        out_holdings.append(
            {
                "isin": h.isin,
                "name": (d.scheme_name if d else None) or securities.display_name(meta, h.isin),
                "scheme_code": d.scheme_code if d else None,
                "category": d.category if d else None,
                "plan": d.plan if d else None,
                "option": d.option if d else None,
                "quantity": money(h.quantity),
                "invested": money(h.invested),
                "nav": str(quote.price) if quote else None,
                "nav_date": quote.price_date if quote else None,
                "market_value": money(h.quantity * quote.price) if quote else None,
                "reason": current.unpriced_reasons.get(h.isin) if not quote else None,
            }
        )
    return {
        "as_of": on,
        "xirr_all": rate(xirr_all),
        "xirr_holdings": rate(xirr_holdings),
        "xirr_unavailable_reason": xirr_reason,
        "twrr": rate(twrr),
        "twrr_unavailable_reason": twrr_reason,
        "invested": money(snapshot.invested),
        "market_value": money(snapshot.market_value),
        "unrealised": money(snapshot.unrealised),
        "realised_to_date": money(snapshot.realised_to_date),
        "complete": not skipped,
        "latest_final_snapshot": latest_final,
        "holdings": out_holdings,
        "excluded": exclusions.describe(connection, skipped),
    }


@router.get(
    "/investors/{investor_id}/funds/snapshots",
    response_model=list[FundPointOut],
    dependencies=[Depends(require_owned_investor)],
)
def fund_snapshots(
    investor_id: str,
    start: date,
    end: date,
    advised_only: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """The stored fund series in ``[start, end]``. ``final`` false means a NAV the row
    depends on is still awaited, or a holding is excluded -- never a finished figure."""
    _require_valuation(settings)
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be on or before end")
    return [
        {
            **p,
            **{
                k: money(p[k])
                for k in ("invested", "market_value", "unrealised", "realised_to_date")
            },
        }
        for p in series.list_points(connection, investor_id, advised_only, start, end)
    ]


@router.get(
    "/investors/{investor_id}/tax-profile",
    response_model=TaxProfileOut,
    dependencies=[Depends(require_owned_investor)],
)
def get_tax_profile(investor_id: str, connection: Any = Depends(get_conn)) -> dict[str, Any]:
    profile = fund_tax.taxpayer_profile(connection, investor_id)
    if profile is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No taxpayer type/residency recorded; fund tax classification needs it",
        )
    return _profile_out(profile.taxpayer_type, profile.residency)


@router.put(
    "/investors/{investor_id}/tax-profile",
    response_model=TaxProfileOut,
    dependencies=[Depends(require_owned_investor)],
)
def put_tax_profile(
    investor_id: str, body: TaxProfileIn, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    fund_tax.set_taxpayer_profile(connection, investor_id, body.taxpayer_type, body.residency)
    connection.commit()
    return _profile_out(body.taxpayer_type, body.residency)


def _profile_out(taxpayer_type: str, residency: str) -> dict[str, Any]:
    return {
        "taxpayer_type": taxpayer_type,
        "residency": residency,
        "supported_for_fund_tax": residency == "RESIDENT"
        and taxpayer_type in ("INDIVIDUAL", "HUF"),
    }


# --- operations ----------------------------------------------------------------------------


def _run_daily(connection: Any, settings: Settings, allow_catalogue_drop: bool) -> dict[str, Any]:
    if not settings.mf_reference_enabled:
        return {"skipped": "MF_REFERENCE_ENABLED is false on this deployment"}
    if not settings.amfi_fetch_enabled:
        return {
            "skipped": "AMFI_FETCH_ENABLED is false on this host; run the reference worker "
            "on a host that can reach AMFI"
        }
    began = time.monotonic()
    daily = amfi_jobs.refresh_daily(
        connection,
        timeout_seconds=settings.amfi_http_timeout_seconds,
        allow_catalogue_drop=allow_catalogue_drop,
    )
    out: dict[str, Any] = {"daily": daily.as_dict()}
    remaining = settings.amfi_job_budget_seconds - (time.monotonic() - began)
    if daily.status in ("PUBLISHED", "QUARANTINED") and remaining > 10:
        snapshots = series.catch_up_fund_snapshots(
            connection, today=date.today(), time_budget_seconds=remaining - 5
        )
        out["fund_snapshots"] = snapshots.__dict__
    if daily.status == "FAILED":
        # A failed fetch is a failed job: the cron's own status shows it, not only a log.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, out)
    return out


@router.get("/ops/cron/mf-nav-refresh", dependencies=[Depends(require_cron_secret)])
def mf_nav_refresh_cron(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Daily AMFI NAV refresh (plan section 7.2): fetch NAVAll.txt once, validate,
    publish relevant NAVs and the catalogue, repair, schedule history, then bring the
    fund series forward within the job budget. Independent of every exchange job."""
    return _run_daily(connection, settings, allow_catalogue_drop=False)


@router.post("/ops/funds/nav-refresh", dependencies=[Depends(require_ops_key)])
def mf_nav_refresh(
    allow_catalogue_drop: bool = False,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The same refresh, operator-triggered. ``allow_catalogue_drop`` is the reasoned
    override for a daily file more than 20% smaller than the last published one."""
    return _run_daily(connection, settings, allow_catalogue_drop=allow_catalogue_drop)


@router.post("/ops/funds/snapshots/catch-up", dependencies=[Depends(require_ops_key)])
def fund_snapshot_catch_up(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    _require_valuation(settings)
    return series.catch_up_fund_snapshots(
        connection, today=date.today(), time_budget_seconds=settings.amfi_job_budget_seconds
    ).__dict__


@router.get("/ops/funds/status", dependencies=[Depends(require_ops_key)])
def fund_status(connection: Any = Depends(get_conn)) -> dict[str, Any]:
    """Health measured by held coverage and finalised dates, not HTTP success (plan
    section 10)."""
    if not ref.fund_tables_present(connection):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "migration 081_fund_reference.sql is not applied"
        )
    with connection.cursor() as cur:
        cur.execute(
            """SELECT id, layout, status, reason, rows_parsed, rows_rejected, conflicts,
                      navs_published, navs_revised, bytes, elapsed_ms, fetched_at
               FROM amfi_batch ORDER BY fetched_at DESC LIMIT 10"""
        )
        cols = [c.name for c in cur.description]
        batches = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        cur.execute(
            "SELECT max(fetched_at) FROM amfi_batch WHERE layout='DAILY' AND status='PUBLISHED'"
        )
        last_daily = cur.fetchone()[0]
        cur.execute(
            """SELECT kind, state, count(*), min(available_at), max(attempts)
               FROM reference_sync_task WHERE kind = ANY(%s) GROUP BY kind, state""",
            (list(amfi_jobs.AMFI_TASK_KINDS),),
        )
        queue = [
            {"kind": k, "state": s, "count": n, "oldest_due": due, "max_attempts": a}
            for k, s, n, due, a in cur.fetchall()
        ]
        cur.execute("SELECT count(*), min(marked_at) FROM fund_snapshot_dirty")
        dirty, oldest_dirty = cur.fetchone()
        cur.execute("SELECT count(*) FROM fund_snapshot WHERE NOT final")
        provisional = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM amfi_amc")
        amcs = cur.fetchone()[0]
    held, stale = amfi_jobs.held_freshness(connection, date.today())
    return {
        "last_published_daily": last_daily,
        "batches": batches,
        "held_funds": held,
        "stale_or_missing": stale,
        "queue": queue,
        "dirty_snapshot_dates": dirty,
        "oldest_dirty_marked_at": oldest_dirty,
        "provisional_snapshots": provisional,
        "amc_ids_known": amcs,
    }


@router.get("/ops/amfi/probe", dependencies=[Depends(require_ops_key)])
def amfi_probe(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Can THIS host reach AMFI, and does a full parse fit? (plan section 7.1, P0).

    Fixed URLs only -- never a caller-supplied one. Fetches the daily file and a three-day
    single-AMC history window, parses both fully, and writes nothing.
    """
    out: dict[str, Any] = {}
    today = date.today()
    probes = {
        "daily": amfi.DAILY_URL,
        # mf=21 answered as Nippon India Mutual Fund on 13-Oct-2026.
        "history": amfi.history_url(21, today - timedelta(days=4), today - timedelta(days=2)),
    }
    for name, url in probes.items():
        began = time.monotonic()
        try:
            body = amfi.fetch(url, settings.amfi_http_timeout_seconds)
            parsed = amfi.parse(body, today)
            out[name] = {
                "ok": True,
                "bytes": len(body),
                "elapsed_ms": int((time.monotonic() - began) * 1000),
                "layout": str(parsed.layout),
                "rows": parsed.rows,
                "schemes": len(parsed.schemes),
                "navs": len(parsed.navs),
                "rejects": len(parsed.rejects),
                "amcs": sorted(parsed.amc_names)[:5],
                "sha256": parsed.sha256,
            }
        except (amfi.AmfiFetchError, amfi.AmfiFormatError) as exc:
            out[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": int((time.monotonic() - began) * 1000),
            }
    return out


@router.post("/ops/funds/tax-profile", dependencies=[Depends(require_ops_key)])
def record_fund_tax_profile(
    body: FundTaxProfileIn, connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    """Record evidenced tax facts for one fund over an interval (plan section 5.1).
    Evidence is mandatory; an overlapping interval with different facts is refused."""
    try:
        version = fund_tax.record_profile(
            connection,
            body.isin,
            body.valid_from,
            body.valid_to,
            FundTaxFacts(
                equity_oriented=body.equity_oriented,
                specified_fund=body.specified_fund,
                listed=body.listed,
                domiciled_in_india=body.domiciled_in_india,
            ),
            body.evidence_url,
            body.evidence_note,
            body.evidence_sha256,
        )
    except fund_tax.ConflictingProfile as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    connection.commit()
    return {"isin": body.isin, "valid_from": body.valid_from, "profile_version": version}
