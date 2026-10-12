"""POST/GET /investors/{id}/corporate-actions, POST /investors/{id}/corporate-actions/match,
GET /investors/{id}/corporate-actions/timeline -- verified corporate-action reference
data, the MProfit-style "Apply" step that matches it against a reconciliation mismatch
and stages the result, and the per-account timeline derived on read
(``app.db.corporate_action_timeline``). Global reference writes live on ``ops_router``
only (operator file import, activation, dividend issuer verification, the feeds).

Review, promote and discard are NOT duplicated here: a batch ``/match`` stages lands in
the same ``import_batch``/``import_row`` tables a file upload does, so the existing
``GET /ingest/stage/{batch_id}``, ``POST /ingest/stage/{batch_id}/promote`` and
``POST /ingest/stage/{batch_id}/discard`` endpoints (app/routers/ingest.py) handle it
unchanged. Nothing here writes to the ledger.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.auth.dependencies import assert_owned_investor, get_current_user, require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import NSE_API_DISABLED_NOTE, Settings, get_settings
from app.database import get_conn, money
from app.db import audit, repository
from app.db import corporate_action_timeline as timeline_db
from app.db import corporate_actions as ca_db
from app.db import holdings as db_holdings
from app.engine.corporate_actions import (
    CorpActionType,
    CorporateAction,
    CorporateActionRatioError,
    Verification,
)
from app.schemas.corporate_actions import (
    ActionActivation,
    AuthorizedImport,
    CorporateActionIn,
    CorporateActionOut,
    CorporateActionTimelineOut,
    DiscoverYfinanceOut,
    DisputeCorporateActionIn,
    DividendVerification,
    MatchCorporateActionsOut,
    MatchCorporateActionsRequest,
    RefreshCorporateActionFeedOut,
    WaivePriceEvidenceIn,
)

router = APIRouter(prefix="/investors", tags=["ops"])

#: Separate from ``router`` above on purpose: everything below is global reference-data
#: administration (the ``corporate_action`` table, and the automated feed that
#: populates it), never scoped to one investor's own ledger the way
#: ``/investors/{id}/corporate-actions`` is -- see ``docs/DATA_MODEL.md``'s
#: ``corporate_action`` section. Gated by the same ``X-Ops-Key``/``CRON_SECRET`` shared
#: secrets ``app.routers.analytical``'s ``/ops/*`` routes use, not a user login, for the
#: identical reason: this mutates reference data that affects every investor at once.
ops_router = APIRouter(prefix="/ops/corporate-actions", tags=["ops"])


def _to_action(body: CorporateActionIn) -> CorporateAction:
    try:
        return CorporateAction(
            isin=body.isin,
            action_type=CorpActionType(body.action_type),
            ex_date=body.ex_date,
            allotment_date=body.allotment_date,
            source=body.source,
            source_url=body.source_url,
            verification=Verification(body.verification),
            bonus_new=body.bonus_new,
            bonus_for=body.bonus_for,
            face_value_before=body.face_value_before,
            face_value_after=body.face_value_after,
            resulting_isin=body.resulting_isin,
            demerger_new=body.demerger_new,
            demerger_for=body.demerger_for,
            price_cum=body.price_cum,
            price_ex=body.price_ex,
            notes=body.notes,
        )
    except (CorporateActionRatioError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _out(a: CorporateAction) -> dict[str, Any]:
    return {
        "id": a.id,
        "isin": a.isin,
        "action_type": a.action_type.value,
        "ex_date": a.ex_date,
        "allotment_date": a.allotment_date,
        "cancelled": a.cancelled,
        "automatic_application": a.automatic_application,
        "bonus_new": a.bonus_new,
        "bonus_for": a.bonus_for,
        "face_value_before": money(a.face_value_before),
        "face_value_after": money(a.face_value_after),
        "resulting_isin": a.resulting_isin,
        "demerger_new": a.demerger_new,
        "demerger_for": a.demerger_for,
        "price_cum": money(a.price_cum),
        "price_ex": money(a.price_ex),
        "source": a.source,
        "source_url": a.source_url,
        "verification": a.verification.value,
        "notes": a.notes,
        "price_waived": a.price_waived,
        "price_waiver_reason": a.price_waiver_reason,
    }


@ops_router.post(
    "",
    response_model=CorporateActionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ops_key)],
)
def add_corporate_action(
    body: CorporateActionIn,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Manual/bulk entry of one verified corporate action -- the same product shape as
    MProfit's Bulk Corporate Action form (security, date, action type, the primitive
    ratio facts). ``investor_id`` gates who may add a record; the record itself is
    security-wide reference data, not scoped to one investor's ledger."""
    action = _to_action(body)
    try:
        action_id = ca_db.add_corporate_action(conn, action, actor_uid=current_user["id"])
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{action.action_type.value} on {action.isin} dated {action.ex_date} "
            "is already on file",
        ) from exc
    return _out(replace(action, id=action_id))


@router.get(
    "/{investor_id}/corporate-actions",
    response_model=list[CorporateActionOut],
    dependencies=[Depends(require_owned_investor)],
)
def list_corporate_actions(
    investor_id: str,
    isin: str | None = Query(default=None),
    conn: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Every corporate action on file, optionally filtered to one ISIN. ``investor_id``
    gates access only -- see ``add_corporate_action``'s docstring."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT t.isin FROM transaction t JOIN account a ON a.id=t.account_id "
            "WHERE a.investor_id=%s", (investor_id,),
        )
        owned_isins = [r[0] for r in cur.fetchall()]
    if isin is not None and isin not in owned_isins:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such portfolio security")
    actions = ca_db.list_corporate_actions(conn, [isin] if isin else owned_isins)
    return [_out(a) for a in actions]


@router.post("/{investor_id}/corporate-actions", dependencies=[Depends(require_owned_investor)])
def refuse_global_reference_write(investor_id: str) -> None:
    raise HTTPException(status.HTTP_403_FORBIDDEN,
                        "Global corporate action reference changes require operator authorization")


@router.get(
    "/{investor_id}/corporate-actions/timeline",
    response_model=CorporateActionTimelineOut,
    dependencies=[Depends(require_owned_investor)],
)
def corporate_action_timeline(
    investor_id: str,
    isin: str | None = Query(default=None, pattern=r"^IN[A-Z0-9]{10}$"),
    account_id: UUID | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Every corporate action, dividend, ISIN change and review-only notice touching this
    investor's securities, with each account's own state, eligible quantity and effect.
    Read-only; each account's ledger is replayed once. 404 for a foreign ``account_id``
    or an ``isin`` this investor never held."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "date_from is after date_to")
    accounts = timeline_db.investor_accounts(
        conn, investor_id, str(account_id) if account_id else None
    )
    if account_id is not None and not accounts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account for this investor")
    try:
        result = timeline_db.build_timeline(
            conn, accounts, isin=isin, date_from=date_from, date_to=date_to
        )
    except timeline_db.SecurityNotOwned as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such portfolio security") from exc
    events = result["events"]
    return {
        "events": events[offset : offset + limit],
        "coverage": result["coverage"],
        "has_more": len(events) > offset + limit,
        "offset": offset,
    }


@router.post(
    "/{investor_id}/corporate-actions/match",
    response_model=MatchCorporateActionsOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def match_corporate_actions(
    investor_id: str,
    body: MatchCorporateActionsRequest,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """The "Apply" step: reconcile this account against ``body.holdings``, match any
    mismatch against verified corporate actions on file, and stage the result.

    Never applied silently. The caller reviews and promotes through the existing
    ``GET /ingest/stage/{batch_id}`` / ``POST /ingest/stage/{batch_id}/promote``
    endpoints, exactly as for a file upload -- see this module's docstring.

    ``body.holdings`` may be omitted, in which case the declared side comes from the
    account's own most recent CAS snapshot (``app.db.holdings.latest_snapshot``) instead
    of the caller having to carry those quantities around. That is what lets a screen
    that already knows something is wrong -- the skipped-holdings notice -- ask for a
    reconciliation with nothing but an account id.
    """
    assert_owned_investor(conn, current_user, investor_id)
    if not repository.account_belongs_to(conn, body.account_id, investor_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no such account for this investor: {body.account_id}",
        )
    declared = {h.isin: h.quantity for h in body.holdings} or db_holdings.latest_snapshot(
        conn, body.account_id
    )
    if not declared:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no declared holdings supplied and no CAS statement on file for this "
            "account -- import a CAS to reconcile against",
        )
    return ca_db.stage_corporate_actions(
        conn,
        investor_id=investor_id,
        account_id=body.account_id,
        declared_holdings=declared,
        actor_uid=current_user["id"],
        as_of=body.as_of,
    )


# ---------------------------------------------------------------------------
# Exceptions -- DRAFT rows the automated feed (or a manual entry) could not
# auto-verify. Expected to be short and rare, not a daily queue -- see
# docs/ENGINE_SPEC.md section 7's "Automated ingestion".
# ---------------------------------------------------------------------------


@ops_router.get(
    "/exceptions",
    response_model=list[CorporateActionOut],
    dependencies=[Depends(require_ops_key)],
)
def list_exceptions(conn: Any = Depends(get_conn)) -> list[dict[str, Any]]:
    """Every DRAFT row on file -- the one screen a human ever needs to look at for
    corporate actions the deterministic checks couldn't resolve on their own."""
    return [_out(a) for a in ca_db.list_pending_corporate_actions(conn)]


@ops_router.post(
    "/{action_id}/verify",
    response_model=CorporateActionOut,
    dependencies=[Depends(require_ops_key)],
)
def verify_exception(
    action_id: str,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Promote one DRAFT row to VERIFIED by hand -- triggers the same retroactive
    sweep an automated auto-verify does. See ``app.db.corporate_actions.
    verify_corporate_action``.

    409, never 400/500, when verification is blocked by state (D12): a DISPUTED row is
    terminal, and a row whose latest source revision is not a confirmed, accepted notice
    (a pending or cancelled operator revision, or an NSE notice that contradicted it)
    stays unverified until that evidence changes."""
    from app.ingest.action_source import ActionIngestionError

    action = ca_db.get_corporate_action(conn, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such corporate action: {action_id}")
    if action.verification is Verification.DISPUTED:
        raise HTTPException(status.HTTP_409_CONFLICT, "a disputed corporate action is terminal")
    try:
        ca_db.verify_corporate_action(conn, action_id, actor_uid=current_user["id"])
    except ActionIngestionError as exc:
        # verify_corporate_action raises this only when the latest revision blocks it.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _out(ca_db.get_corporate_action(conn, action_id))


@ops_router.post(
    "/{action_id}/waive-price-evidence",
    response_model=CorporateActionOut,
    dependencies=[Depends(require_ops_key)],
)
def waive_price_evidence(
    action_id: str,
    body: WaivePriceEvidenceIn,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """A human accepting a BONUS/SPLIT whose ex_date predates this system's price
    archive, so the usual cum/ex corroboration will never be obtainable -- see
    ``app.db.corporate_actions.waive_price_evidence`` for exactly what this does and
    does not bypass. 400 if the row does not qualify (a recent-enough ex_date, no
    source, or an already-disputed row)."""
    try:
        ca_db.waive_price_evidence(conn, action_id, body.reason, actor_uid=current_user["id"])
    except ca_db.PriceEvidenceWaiverError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    action = ca_db.get_corporate_action(conn, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such corporate action: {action_id}")
    return _out(action)


@ops_router.post(
    "/{action_id}/dispute",
    response_model=CorporateActionOut,
    dependencies=[Depends(require_ops_key)],
)
def dispute_exception(
    action_id: str,
    body: DisputeCorporateActionIn,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Reject a DRAFT row permanently -- it will never match a Discrepancy again and
    the automated feed's retry sweep will never re-attempt it. The row is never
    deleted: it stays as the record of what was rejected and why."""
    ca_db.dispute_corporate_action(
        conn, action_id, actor_uid=current_user["id"], reason=body.reason
    )
    action = ca_db.get_corporate_action(conn, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such corporate action: {action_id}")
    return _out(action)


# ---------------------------------------------------------------------------
# Automated feed -- mirrors app.routers.analytical's refresh_prices/refresh_prices_cron
# pair exactly: one private worker, an X-Ops-Key POST for a human or a non-Vercel
# scheduler, a CRON_SECRET GET for Vercel Cron specifically.
# ---------------------------------------------------------------------------


def _refresh_corporate_actions(
    conn: Any, start: date, end: date, settings: Settings
) -> dict[str, Any]:
    return ca_db.refresh_corporate_action_feed(
        conn, start, end, actor_uid=None,
        rights_reference=settings.nse_automated_access_rights_reference,
    )


@ops_router.post(
    "/refresh",
    response_model=RefreshCorporateActionFeedOut,
    dependencies=[Depends(require_ops_key)],
)
def refresh_corporate_actions(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """``X-Ops-Key``-gated -- a human or a non-Vercel scheduler triggering a fetch
    directly, same posture as ``app.routers.analytical.refresh_prices``. Defaults to a
    2-day window ending today (yesterday's notices often still show ex-dates landing
    today), overridable for a backfill."""
    today = date.today()
    return _refresh_corporate_actions(
        conn, start or today - timedelta(days=1), end or today, settings
    )


@ops_router.get(
    "/refresh-cron",
    response_model=RefreshCorporateActionFeedOut,
    dependencies=[Depends(require_cron_secret)],
)
def refresh_corporate_actions_cron(
    conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Same job, reachable by Vercel Cron specifically: GET, gated by ``CRON_SECRET``
    rather than ``X-Ops-Key``, since that is the only credential Vercel's scheduler
    sends and the only HTTP method it calls with. See docs/DEPLOYMENT.md.

    Skipped where ``nse_api_enabled`` is false: NSE blocks that deployment, and the same
    window is read by ``app.nse_worker`` instead."""
    if not settings.nse_api_enabled:
        logging.getLogger(__name__).info("corporate_action_cron_skipped: %s", NSE_API_DISABLED_NOTE)
        return {"fetched": 0, "verified": 0, "exceptions": 0, "conflicts": 0}
    today = date.today()
    return _refresh_corporate_actions(conn, today - timedelta(days=1), today, settings)


# ---------------------------------------------------------------------------
# yfinance discovery -- a second, independent free candidate source. NOT a historical
# fallback: NSE's notices API does serve arbitrary historical windows (re-probed live
# 2026-09-26), and the month-by-month sweep in app.db.reference_sync.schedule is what
# actually reaches a years-deep gap. See app.ingest.corporate_action_yfinance's module
# docstring for the correction and what the remaining uncovered gap really is (BSE).
# ---------------------------------------------------------------------------


@ops_router.post(
    "/discover-yfinance",
    response_model=DiscoverYfinanceOut,
    dependencies=[Depends(require_ops_key)],
)
def discover_yfinance(
    isin: list[str] | None = Query(default=None),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """``X-Ops-Key``-gated, same posture as ``refresh_corporate_actions`` above. Occasional
    -- run when onboarding turns up a gap the live feed can't explain, not on a daily
    cron (a ticker's split history does not change day to day). Defaults to every ISIN
    this system has ever held; pass one or more ``isin`` query params to target specific
    securities instead of paying for the full sweep."""
    return ca_db.discover_from_yfinance(
        conn, isins=isin, rights_reference=settings.yahoo_data_rights_reference
    )


# ---------------------------------------------------------------------------
# Operator file import and its follow-ups. No HTTP provider client exists (plan OD-8):
# an operator imports an authorized file, users can never poison shared facts.
# ---------------------------------------------------------------------------


@ops_router.post("/import", dependencies=[Depends(require_ops_key)])
def import_actions(
    body: AuthorizedImport,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Import one operator file. A malformed body is 400; a batch with rejected or
    conflicting rows commits its good rows and answers 500 with the counts
    (``ActionBatchFailed``, mapped in app.main)."""
    from app.db.action_ingestion import ingest_batch

    return ingest_batch(
        conn,
        body.batch,
        rights_reference=body.rights_reference,
        auto_apply=settings.corporate_action_auto_apply,
    )


@ops_router.post("/import/bse", dependencies=[Depends(require_ops_key)])
async def import_bse_export(
    file: UploadFile = File(...),
    start: date = Form(...),
    end: date = Form(...),
    rights_reference: str = Form(..., min_length=1, max_length=2000),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Import the CSV export of BSE's public corporate-actions search over
    ``[start, end]`` (the range the operator searched). BSE's API refuses automated
    clients (plan OD-8), so this file is the BSE entry point; it is what covers BSE-only
    securities. Scrip codes resolve through the latest BSE bhavcopy. A malformed file or
    range is 400; rejected/unresolved/conflicting rows commit nothing of their own and
    answer 500 with observation IDs, after every good row is imported."""
    from app.db.action_ingestion import ingest_bse_export
    from app.ingest.bse_corporate_actions import fetch_scrip_isin_map
    from app.prices.sources import bse_source

    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must not follow end")
    payload = await file.read(settings.bse_export_max_bytes + 1)
    if len(payload) > settings.bse_export_max_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "BSE export exceeds the size limit")
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "BSE export is empty")
    scrip_to_isin = fetch_scrip_isin_map(
        bse_source(), date.today(), settings.bse_scrip_map_lookback_days
    )
    return ingest_bse_export(
        conn,
        payload,
        start=start,
        end=end,
        scrip_to_isin=scrip_to_isin,
        rights_reference=rights_reference,
        auto_apply=settings.bse_corporate_action_auto_apply,
    )


@ops_router.get("/observations", dependencies=[Depends(require_ops_key)])
def list_observations(
    limit: int = Query(default=100, ge=1, le=500), conn: Any = Depends(get_conn)
) -> list[dict[str, Any]]:
    """Recent source observations, newest first -- where conflicts and rejections land."""
    columns = ("id", "provider", "isin", "ex_date", "classification", "event_id",
               "component_id", "result", "reason_code", "reason", "fetched_at")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM corporate_action_observation "
        "ORDER BY fetched_at DESC, id LIMIT %s",
        (limit,),
    ).fetchall()
    return [dict(zip(columns, r, strict=True)) for r in rows]


@ops_router.post("/{action_id}/activation", dependencies=[Depends(require_ops_key)])
def activate_action(
    action_id: UUID, body: ActionActivation, conn: Any = Depends(get_conn)
) -> dict[str, Any]:
    """Turn automatic application of one VERIFIED, non-cancelled action on or off, then
    queue a replay of every account that holds it."""
    from app.db.reference_sync import enqueue

    row = conn.execute(
        "SELECT isin, verification::text, cancelled FROM corporate_action WHERE id=%s FOR UPDATE",
        (action_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such corporate action")
    if body.enabled and (row[1] != "VERIFIED" or row[2]):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "only a verified, non-cancelled action can be activated"
        )
    conn.execute(
        "UPDATE corporate_action SET automatic_application=%s WHERE id=%s",
        (body.enabled, action_id),
    )
    audit.record(conn, "CORPORATE_ACTION_ACTIVATION", "corporate_action", str(action_id),
                 None, {"enabled": body.enabled})
    for (account,) in conn.execute(
        "SELECT DISTINCT account_id::text FROM transaction WHERE isin=%s", (row[0],)
    ).fetchall():
        enqueue(conn, "REPLAY", account, {"account_id": account}, refresh=True)
    return {"action_id": str(action_id), "automatic_application": body.enabled}


@ops_router.post("/dividends/{event_id}/verify", dependencies=[Depends(require_ops_key)])
def verify_dividend(
    event_id: UUID,
    body: DividendVerification,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """An operator records issuer evidence for a declared dividend. 409 when the latest
    provider revision is not CONFIRMED."""
    from app.db.dividend_events import get_dividend_event, verify_dividend_event

    event = get_dividend_event(conn, str(event_id))
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such dividend declaration")
    if event.verification is Verification.DISPUTED:
        raise HTTPException(status.HTTP_409_CONFLICT, "a disputed dividend declaration is terminal")
    if not body.evidence_reference.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "issuer evidence reference is required")
    latest = conn.execute(
        "SELECT terms->>'status' FROM corporate_action_revision WHERE dividend_id=%s "
        "ORDER BY recorded_at DESC, revision DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    if latest and latest[0] != "CONFIRMED":
        raise HTTPException(status.HTTP_409_CONFLICT, "latest dividend revision is not confirmed")
    verify_dividend_event(conn, str(event_id), actor_uid=current_user["id"])
    audit.record(conn, "DIVIDEND_ISSUER_REVIEW", "dividend_event", str(event_id),
                 current_user["id"], {"evidence_reference": body.evidence_reference})
    return {"event_id": str(event_id), "state": "DECLARATION_VERIFIED"}
