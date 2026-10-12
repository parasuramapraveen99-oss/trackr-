"""Company research report jobs (RP 01; docs/BUTLER_REPORTS.md section 9).

Under ``/investors/{investor_id}/reports``, Workspace only, with the same dependency chain as
Butler: session, ownership, the ``workspace`` capability, an enabled Workspace, and an applied
``054_hosted_jobs.sql`` (503 otherwise). Every POST also needs write readiness and a UUID
``Idempotency-Key``.

THE API ENQUEUES AND RETURNS. A request is validated, sealed and admitted as a durable job;
the worker (``app/jobs/hosted.py``) does the work. There is no four-hour request, no streaming
model proxy and no route that accepts a credential. Closing the browser changes nothing.

ADMISSION IS WHERE IMPOSSIBLE JOBS STOP. Missing operator pricing/model/credential/SDK, a
budget above the operator bound, a deadline above four hours (or the operator bound), an
unknown ISIN, a point-in-time request without a cutoff, and a private-research scope without a
completed ritual are all refused before any row is written.

PUBLISHED OUTPUT (RP 05-07). ``GET /{id}/document`` returns the validated published document,
its artifacts, retractions and what was withheld; it is 404 DOCUMENT_NOT_PUBLISHED while the
job runs or when it ended blocked. ``GET /{id}/artifacts/{artifact_id}`` streams a private HTML
or PDF through this authenticated route only -- no public bucket, no signed or guessable link --
after rechecking ownership, the Workspace capability, retraction, current export rights and the
stored bytes' hash. ``POST /{id}/retractions`` lets the owner withdraw a report; a file already
downloaded cannot be recalled. This module imports no notebook code (``make lint``).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import Response as RawResponse

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import hosted_jobs as hj
from app.db import workspace as ws
from app.db.connection import WorkspaceCapability
from app.db.migrate import hosted_jobs_schema_ready
from app.research import canonical
from app.research import platform_inference as pi
from app.research.errors import WorkspaceError, invalid, ritual_required
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.reports import (
    REPORT_CONTRACT_VERSION,
    JobEventsPage,
    ReportDocumentOut,
    ReportJob,
    ReportRequestIn,
    ReportRetractionIn,
    ReportRevisionIn,
)

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN

_ready = False


def require_hosted_ready(conn: Any = Depends(get_conn)) -> None:
    global _ready
    if _ready:
        return
    if not hosted_jobs_schema_ready(conn):
        raise WorkspaceError(503, "REPORTS_UNAVAILABLE",
                             "report storage is not installed on this server")
    _ready = True


router = APIRouter(
    prefix="/investors/{investor_id}/reports",
    tags=["reports"],
    dependencies=[
        Depends(require_owned_investor),
        Depends(require_workspace_access),
        Depends(require_workspace_enabled),
        Depends(require_hosted_ready),
    ],
)
WRITE = [Depends(require_research_writes_ready)]


def _reply(response: Response, body: Any, status: int) -> Any:
    response.status_code = status
    return body


def _platform(settings: Settings) -> pi.Pricing:
    try:
        return pi.ready(settings)
    except pi.PlatformNotConfigured as exc:
        raise hj.not_configured(exc.reason) from exc


def _bound(value: str, what: str) -> Decimal:
    try:
        d = Decimal(value)
    except Exception as exc:
        raise hj.not_configured(f"{what.upper()}_NOT_CONFIGURED") from exc
    if not d.is_finite() or d <= 0:
        raise hj.not_configured(f"{what.upper()}_NOT_CONFIGURED")
    return d


def _seal(conn: Any, investor_id: str, body: ReportRequestIn, settings: Settings,
          revision_of: str | None) -> hj.Sealed:
    pricing = _platform(settings)
    isin = ws._valid_isin(body.isin)
    if ws._one(conn, "SELECT 1 AS x FROM security WHERE isin = %s", (isin,)) is None:
        raise invalid("unknown ISIN; confirm the company first", {"isin": "UNKNOWN_SECURITY"},
                      code="UNKNOWN_SECURITY")
    canonical_isin = ws.canonical_isin(conn, isin)
    if body.mode == "POINT_IN_TIME" and body.cutoff is None:
        raise invalid("a point-in-time report needs a cutoff date", {"cutoff": "REQUIRED"})
    if body.mode == "CURRENT_WITH_HISTORY" and body.cutoff is not None:
        raise invalid("cutoff applies only to POINT_IN_TIME", {"cutoff": "UNEXPECTED"})
    if body.scope == "ISSUER_PLUS_PRIVATE_RESEARCH":
        # The report-first brief needs no ritual; adding PRIVATE research does (BE 08 gate).
        ws.require_linked(conn, investor_id, canonical_isin)
        if not ws.ritual_complete(conn, investor_id, canonical_isin):
            raise ritual_required()
    budget = Decimal(body.budget)
    if budget <= 0:
        raise invalid("budget must be positive", {"budget": "RANGE"})
    if budget > _bound(settings.report_max_budget, "report_max_budget"):
        raise invalid("budget exceeds the operator maximum", {"budget": "ABOVE_OPERATOR_MAX"})
    if body.deadline_minutes > min(settings.report_max_deadline_minutes, 240):
        raise invalid("deadline exceeds the operator maximum",
                      {"deadline_minutes": "ABOVE_OPERATOR_MAX"})
    # Strings only: 054 refuses a JSON number anywhere in the sealed request.
    request = {
        "isin": canonical_isin,
        "requested_isin": isin,
        "question": body.question,
        "mode": body.mode,
        "cutoff": body.cutoff.isoformat() if body.cutoff else None,
        "window_years": str(body.window_years),
        "statements": list(body.statements),
        "basis": body.basis,
        "scope": body.scope,
        "budget": str(budget),
        "deadline_minutes": str(body.deadline_minutes),
    }
    return hj.Sealed(
        purpose="COMPANY_REPORT", trigger_kind="USER_REQUEST", isin=canonical_isin,
        request=request, consent_version=None, provider=pricing.provider, model=pricing.model,
        price_version=pricing.price_version, currency=pricing.currency, budget_limit=budget,
        max_input_tokens=settings.report_stage_max_input_tokens,
        max_output_tokens=settings.report_stage_max_output_tokens, max_stage_attempts=2,
        deadline=timedelta(minutes=body.deadline_minutes), revision_of=revision_of,
        contract_version=REPORT_CONTRACT_VERSION)


@router.post("", response_model=ReportJob, status_code=201, dependencies=WRITE)
def create_report(
    investor_id: str,
    body: ReportRequestIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Validate, seal and enqueue. Returns the queued job immediately."""
    actor = str(user["id"])
    digest = canonical.digest({"operation": hj.OP_JOB, "body": body.model_dump(mode="json")})
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        sealed = _seal(conn, investor_id, body, settings, None)
        job_id, status = hj.admit(conn, investor_id, actor, sealed, key, digest)
        return _reply(response, hj.job_view(conn, investor_id, job_id), status)


@router.get("", response_model=list[ReportJob])
def list_reports(
    investor_id: str,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> list[dict[str, Any]]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return hj.list_jobs(conn, investor_id, "COMPANY_REPORT", limit, before)


def _owned_report(conn: Any, investor_id: str, report_id: str) -> str:
    report_id = ws._uuid(report_id, "report")
    if hj.job_row(conn, investor_id, report_id)["purpose"] != "COMPANY_REPORT":
        from app.research.errors import not_found

        raise not_found("report")
    return report_id


@router.get("/{report_id}", response_model=ReportJob)
def get_report(
    investor_id: str, report_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return hj.job_view(conn, investor_id, _owned_report(conn, investor_id, report_id))


@router.get("/{report_id}/events", response_model=JobEventsPage)
def report_events(
    investor_id: str, report_id: str,
    after: int = Query(default=0, ge=0),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Reconnectable progress: completed stages and state changes, codes only."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return hj.events(conn, investor_id, _owned_report(conn, investor_id, report_id), after)


@router.post("/{report_id}/cancel", response_model=ReportJob, status_code=201,
             dependencies=WRITE)
def cancel_report(
    investor_id: str, report_id: str, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Idempotent. Returns at once; the late-output fence applies from this commit. Provider
    cancellation and billing reversal for a call already in flight are NOT guaranteed."""
    actor = str(user["id"])
    digest = canonical.digest({"operation": hj.OP_CANCEL, "report": report_id})
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        rid = _owned_report(conn, investor_id, report_id)
        body, status = hj.cancel(conn, investor_id, actor, rid, key, digest)
        return _reply(response, body, status)


@router.post("/{report_id}/revisions", response_model=ReportJob, status_code=201,
             dependencies=WRITE)
def revise_report(
    investor_id: str, report_id: str, body: ReportRevisionIn, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    settings: Settings = Depends(get_settings),
) -> Any:
    """A NEW bounded job linked to the prior one. The prior report is never rewritten."""
    actor = str(user["id"])
    digest = canonical.digest({"operation": hj.OP_JOB, "revision_of": report_id,
                               "body": body.model_dump(mode="json")})
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        rid = _owned_report(conn, investor_id, report_id)
        prior = hj.job_row(conn, investor_id, rid)["request"]
        request = ReportRequestIn.model_validate({
            "isin": prior["requested_isin"], "question": body.question or prior["question"],
            "mode": prior["mode"], "cutoff": prior["cutoff"],
            "window_years": body.window_years or int(prior["window_years"]),
            "statements": prior["statements"], "basis": prior["basis"],
            "scope": prior["scope"], "budget": body.budget,
            "deadline_minutes": body.deadline_minutes,
        })
        sealed = _seal(conn, investor_id, request, settings, rid)
        job_id, status = hj.admit(conn, investor_id, actor, sealed, key, digest)
        return _reply(response, hj.job_view(conn, investor_id, job_id), status)


# =========================================================================================
# RP 05-07 -- published document, private artifacts, owner retraction
# =========================================================================================

OP_RETRACT = "report.retract"


def _require_documents(fin_conn: Any) -> None:
    from app.db.migrate import report_documents_schema_ready

    if not report_documents_schema_ready(fin_conn):
        raise WorkspaceError(503, "REPORT_DOCUMENTS_UNAVAILABLE",
                             "report document storage (migration 056) is not installed")


def _published(conn: Any, investor_id: str, report_id: str) -> tuple[str, dict[str, Any]]:
    from app.db import reports as rdb

    rid = _owned_report(conn, investor_id, report_id)
    row = rdb.document_for_job(conn, investor_id, rid)
    if row is None:
        state = hj.current_state(conn, investor_id, rid) or {}
        raise WorkspaceError(404, "DOCUMENT_NOT_PUBLISHED",
                             "no published document for this report "
                             f"(state {state.get('state', 'QUEUED')}, "
                             f"{state.get('reason_code', '')})")
    return rid, row


def _rights_changed(doc: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    """Sources whose CURRENT export verdict is no longer ALLOWED although the document recorded
    it as ALLOWED. A lost right blocks downloads until a new report version is rendered. A
    source whose type was not recorded is treated as changed -- never as still permitted."""
    from app.db.workspace_privacy import export_verdict

    types = ledger.get("source_types") or {}
    return [s["source_id"] for s in doc.get("sources", [])
            if s["rights"].get("EXPORT") == "ALLOWED"
            and (s["source_id"] not in types
                 or export_verdict(types[s["source_id"]]) != "ALLOWED")]


@router.get("/{report_id}/document", response_model=ReportDocumentOut)
def get_document(
    investor_id: str, report_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The published, validated document (never a draft), with its status labels, artifacts,
    retractions and the claim ids withheld at publication with their reason codes."""
    import json

    from app.db import reports as rdb

    _require_documents(fin_conn)
    with db_errors(), sessions.open(READER, investor_id) as conn:
        rid, row = _published(conn, investor_id, report_id)
        doc = json.loads(row["document_json"])
        ledger = json.loads(row["ledger_json"])
        retractions = rdb.retractions(conn, investor_id, row["id"])
        return {
            "report_id": rid, "report_document_id": row["id"], "status": row["status"],
            "document_sha256": row["document_sha256"], "retracted": bool(retractions),
            "retractions": retractions, "rights_changed": _rights_changed(doc, ledger),
            "document": doc, "artifacts": rdb.artifacts(conn, investor_id, row["id"]),
            "withheld": {k: [str(x) for x in v] for k, v in
                         (ledger.get("withheld") or {}).items()},
        }


@router.get("/{report_id}/artifacts/{artifact_id}")
def download_artifact(
    investor_id: str, report_id: str, artifact_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> RawResponse:
    """Private download through this authenticated route only. Rechecked on every request:
    ownership and capability (router dependencies), retraction, current export rights and the
    stored bytes' SHA-256. Never cached, never a redirect to storage."""
    import hashlib
    import json

    from app.db import reports as rdb
    from app.storage.factory import get_store

    _require_documents(fin_conn)
    with db_errors(), sessions.open(READER, investor_id) as conn:
        rid, row = _published(conn, investor_id, report_id)
        if rdb.retractions(conn, investor_id, row["id"]):
            raise WorkspaceError(410, "REPORT_RETRACTED",
                                 "this report was withdrawn; it can no longer be downloaded")
        if _rights_changed(json.loads(row["document_json"]), json.loads(row["ledger_json"])):
            raise WorkspaceError(409, "EXPORT_RIGHTS_CHANGED",
                                 "a source's export permission changed since this report was "
                                 "rendered; request a revision")
        art = rdb.artifact(conn, investor_id, row["id"], artifact_id)
    with fin_conn.cursor() as cur:
        cur.execute("SELECT storage_key FROM document WHERE investor_id = %s AND id = %s",
                    (investor_id, art["document_id"]))
        found = cur.fetchone()
    if found is None:
        raise WorkspaceError(410, "ARTIFACT_ERASED", "the artifact's bytes no longer exist")
    try:
        data = get_store(settings).get(found[0])
    except Exception as exc:  # noqa: BLE001 - absent bytes are a stated state, not a 500
        raise WorkspaceError(410, "ARTIFACT_ERASED",
                             "the artifact's bytes no longer exist") from exc
    if hashlib.sha256(data).hexdigest() != art["sha256"]:
        raise WorkspaceError(409, "ARTIFACT_INTEGRITY",
                             "the stored artifact does not match its recorded hash")
    pdf = art["format"] == "PDF"
    name = f"report-{row['isin']}-v{row['report_version']}.{'pdf' if pdf else 'html'}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"',
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
               "X-Report-Status": row["status"], "X-Artifact-Sha256": art["sha256"]}
    return RawResponse(content=data,
                       media_type="application/pdf" if pdf else "text/html; charset=utf-8",
                       headers=headers)


@router.post("/{report_id}/retractions", status_code=201, dependencies=WRITE)
def retract_report(
    investor_id: str, report_id: str, body: ReportRetractionIn, response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> Any:
    """The owner withdraws a published report. Idempotent. Downloads stop at once; a copy
    already downloaded cannot be recalled."""
    from app.db import reports as rdb

    _require_documents(fin_conn)
    actor = str(user["id"])
    digest = canonical.digest({"operation": OP_RETRACT, "report": report_id,
                               "body": body.model_dump(mode="json")})
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        rid, row = _published(conn, investor_id, report_id)
        ws.lock_tenant(conn, investor_id)
        receipt = ws.check_replay(conn, investor_id, OP_RETRACT, rid, key, digest)
        if receipt is not None:
            response.status_code = 200
            return receipt.result
        retraction_id = rdb.retract(conn, investor_id, row["id"], "OWNER_REQUEST",
                                    body.detail, actor)
        result = {"retraction_id": retraction_id, "report_document_id": row["id"],
                  "note": "downloads stopped; files already downloaded cannot be recalled"}
        ws.record_receipt(conn, investor_id, OP_RETRACT, rid, key, digest, result, actor)
        return result
