"""The FastAPI application: JSON API, CORS, and one exception-handling layer.

The server-rendered screens and ``api/templates/*.html`` from the root package are
RETIRED here, not carried forward -- ``trackr-frontend/`` is the replacement UI, and
this becomes a pure JSON API. See docs/BACKEND_REUSE_AND_GAP_ANALYSIS.md section 1.

Every handler below logs before responding: request id, method, path, and the full
traceback for a 500. Never a bare ``except: pass`` -- Rule 2 has zero exceptions.

    cd trackr-backend && uvicorn app.main:app --reload --env-file .env
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app import securities
from app.auth.ops import (
    CronSecretInvalidError,
    CronSecretMissingError,
    CronSecretNotConfiguredError,
    OpsKeyInvalidError,
    OpsKeyMissingError,
    OpsNotConfiguredError,
)
from app.auth.service import EmailAlreadyRegisteredError, InvalidCredentialsError
from app.config import get_settings
from app.database import NonFiniteFinancialValueError
from app.db.capabilities import WorkspaceCapabilityRequiredError
from app.db.connection import DatabaseNotConfiguredError, connect
from app.db.consent import ConsentMissingError
from app.db.crypto import InvalidPANError
from app.db.grievance import AlreadyResolvedError
from app.db.imports import BatchAlreadyResolvedError, BatchNotFoundError, DuplicateUploadError
from app.db.privacy import LegalHoldError
from app.db.repository import DuplicateInvestorCodeError
from app.engine.action_replay import CorporateActionEvidenceError
from app.engine.errors import MissingPriceError, NegativeHoldingError, UnparseableRowError
from app.ingest.action_source import (
    ActionBatchFailed,
    ActionConflictError,
    ActionIngestionError,
    ActionSourceError,
)
from app.ingest.webhook import (
    NoMatchingAccountError,
    NoSuchInvestorError,
    TooManyAttachmentsError,
    UnroutableAddressError,
    UnsupportedDocumentError,
)
from app.ingest.webhook_providers import (
    InvalidWebhookCredentialError,
    MissingWebhookCredentialError,
    WebhookNotConfiguredError,
)
from app.rate_limit import limiter
from app.research.errors import WorkspaceError
from app.routers import (
    analytical,
    analytics,
    auth,
    brokers,
    butler,
    butler_notebook,
    capital_gains,
    consent,
    corporate_actions,
    demerger_watch,
    dividends_ops,
    email_automation,
    families,
    fno,
    funds,
    grievance,
    holdings,
    ingest,
    investors,
    jobs_ops,
    market,
    market_assistant,
    monitors,
    narrative,
    privacy,
    reconcile,
    reference_sync,
    reports,
    research,
    security_alias,
    tasks,
    transactions,
    workspace,
)
from app.storage.factory import get_store

logger = logging.getLogger("app")

settings = get_settings()

app = FastAPI(
    title="Trackr Portfolio Intelligence", version="0.1.0-mvp",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class CatchAllExceptionMiddleware:
    """Convert any exception that reaches here into the generic 500 JSON response
    (``unhandled_exception_handler`` below), positioned inside ``CORSMiddleware`` so
    that response still carries CORS headers.

    Deliberately NOT ``@app.exception_handler(Exception)``: Starlette hoists a handler
    registered for the bare ``Exception`` class straight into ``ServerErrorMiddleware``
    -- the true outermost ASGI layer, above ``CORSMiddleware`` and every other
    middleware, by design (``starlette/applications.py``:
    ``if key in (500, Exception): error_handler = value``). A response built there is
    sent via the raw, unwrapped ASGI ``send``, bypassing CORS entirely -- every
    unhandled 500 reaches the browser with no ``Access-Control-Allow-Origin`` header,
    which the browser reports as a CORS failure and masks the real error. Catching it
    here instead, below CORS in the middleware stack, is what actually lets CORS add
    its headers -- confirmed against a local repro before this was written; see the
    session that added this comment for the reproduction script if it resurfaces.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            response = await unhandled_exception_handler(Request(scope, receive=receive), exc)
            await response(scope, receive, send)


app.add_middleware(CatchAllExceptionMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestContextMiddleware:
    """Assign a request id up front so every log line for this request -- including
    an exception handler's -- can be correlated, and echo it back so a client-reported
    error can be found in the server log from the header alone.

    Plain ASGI, deliberately not ``@app.middleware("http")`` (Starlette's
    ``BaseHTTPMiddleware``): that runs the downstream app in a background task, and
    when a route raises, its ``call_next()`` re-raises the original exception a second
    time even after ``CatchAllExceptionMiddleware`` above has already produced and sent
    a clean response -- harmless once that middleware exists, but it double-logs every
    error and needlessly surfaces a second traceback at the ASGI-server level. A plain
    ASGI middleware only wraps ``send``, so it never buffers or re-raises around a
    response Starlette's exception handling already sent.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status_code = 0

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # Private API responses must not enter browser/CDN caches, including
                # requests authenticated by a cookie rather than Authorization.
                message["headers"] = [(k, v) for k, v in message["headers"]
                                      if k.lower() != b"cache-control"]
                message["headers"].append((b"cache-control", b"no-store"))
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)
        logger.info(
            "request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request_id,
            scope["method"],
            scope["path"],
            status_code,
            (time.monotonic() - started) * 1000,
        )


app.add_middleware(RequestContextMiddleware)


def _error(request: Request, status_code: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": message,
            "request_id": getattr(request.state, "request_id", None),
            **extra,
        },
    )


# --------------------------------------------------------------------------------------
# engine.errors -- CLAUDE.md rule 4: a missing price raises, never defaults to zero.
# --------------------------------------------------------------------------------------


def _names_for(isins: list[str]) -> dict[str, str]:
    """ISIN -> display name, for naming an error's ISINs to a person instead of showing
    the raw code. Best-effort: a lookup failure degrades to an empty map (the caller
    falls back to the ISIN itself) rather than turning an already-failing request into
    an unhandled 500 over a cosmetic lookup."""
    if not isins:
        return {}
    try:
        with connect(get_settings().database_url) as conn:
            meta = securities.lookup(conn, isins)
    except Exception:
        logger.warning("security name lookup failed for %s", isins, exc_info=True)
        return {}
    return {isin: securities.display_name(meta, isin) for isin in isins}


@app.exception_handler(MissingPriceError)
async def missing_price_handler(request: Request, exc: MissingPriceError) -> JSONResponse:
    return _error(
        request,
        409,
        "one or more held ISINs have no usable price within the staleness window",
        isins=exc.isins,
        names=_names_for(exc.isins),
        # Which date was checked, when the caller knows (contribution checks two). The
        # client (ApiError.asOf) already reads this; without it "no usable price" read as
        # a contradiction of a Dashboard that prices the same holdings today.
        as_of=exc.as_of.isoformat() if hasattr(exc.as_of, "isoformat") else exc.as_of,
    )


@app.exception_handler(NegativeHoldingError)
async def negative_holding_handler(request: Request, exc: NegativeHoldingError) -> JSONResponse:
    deficit = -exc.quantity if exc.quantity is not None else None
    names = _names_for([exc.isin] if exc.isin else [])
    return _error(
        request,
        409,
        f"holding for {exc.isin} went negative; buy history is incomplete",
        isin=exc.isin,
        name=names.get(exc.isin) if exc.isin else None,
        deficit=str(deficit) if deficit is not None else None,
        as_of=str(exc.as_of) if exc.as_of is not None else None,
    )


@app.exception_handler(UnparseableRowError)
async def unparseable_row_handler(request: Request, exc: UnparseableRowError) -> JSONResponse:
    return _error(request, 422, str(exc), row=exc.row, reason=exc.reason)


# --------------------------------------------------------------------------------------
# corporate actions (app/ingest/action_source.py, app/engine/action_replay.py). Only
# 400/409/500, never 422/503 (docs/CORPORATE_ACTIONS_COMPLETION_PLAN.md section 5, H3).
# Starlette picks the most specific registered class, so each subclass below wins over
# the ActionSourceError catch-all.
# --------------------------------------------------------------------------------------


def _corporate_action_error(
    request: Request, status_code: int, code: str, exc: Exception, **extra: Any
) -> JSONResponse:
    context = {"request_id": getattr(request.state, "request_id", None),
               "path": request.url.path, "error": code, **extra}
    if status_code >= 500:
        logger.error("corporate_action_request_failed", extra=context)
    else:
        logger.warning("corporate_action_request_refused", extra=context)
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "detail": str(exc), "request_id": context["request_id"],
                 **extra},
    )


@app.exception_handler(ActionIngestionError)
async def corporate_action_invalid_handler(
    request: Request, exc: ActionIngestionError
) -> JSONResponse:
    return _corporate_action_error(request, 400, "corporate_action_invalid_input", exc)


@app.exception_handler(ActionConflictError)
async def corporate_action_conflict_handler(
    request: Request, exc: ActionConflictError
) -> JSONResponse:
    return _corporate_action_error(request, 409, "corporate_action_conflict", exc,
                                   reason_code=exc.reason_code)


@app.exception_handler(ActionBatchFailed)
async def corporate_action_batch_failed_handler(
    request: Request, exc: ActionBatchFailed
) -> JSONResponse:
    return _corporate_action_error(
        request, 500, "corporate_action_batch_failed", exc,
        rejected=len(exc.rejected), conflicts=len(exc.conflicts),
        rejected_observation_ids=exc.rejected, conflict_observation_ids=exc.conflicts,
    )


@app.exception_handler(ActionSourceError)
async def corporate_action_source_handler(
    request: Request, exc: ActionSourceError
) -> JSONResponse:
    """An upstream source outage or malformed upstream payload: a server-side failure."""
    return _corporate_action_error(request, 500, "corporate_action_source_unavailable", exc)


@app.exception_handler(CorporateActionEvidenceError)
async def corporate_action_evidence_handler(
    request: Request, exc: CorporateActionEvidenceError
) -> JSONResponse:
    """A strict computation over an ISIN whose applied action lost its evidence. Names the
    ISIN; the partitioned reads list it in ``skipped`` instead (invariant 16)."""
    return _corporate_action_error(request, 409, "corporate_action_evidence_required", exc,
                                   isin=exc.isin, reason_code=exc.reason_code)


# --------------------------------------------------------------------------------------
# staging / imports
# --------------------------------------------------------------------------------------


@app.exception_handler(BatchNotFoundError)
async def batch_not_found_handler(request: Request, exc: BatchNotFoundError) -> JSONResponse:
    return _error(request, 404, str(exc))


@app.exception_handler(BatchAlreadyResolvedError)
async def batch_resolved_handler(request: Request, exc: BatchAlreadyResolvedError) -> JSONResponse:
    return _error(request, 409, str(exc))


@app.exception_handler(DuplicateUploadError)
async def duplicate_upload_handler(request: Request, exc: DuplicateUploadError) -> JSONResponse:
    return _error(request, 409, str(exc), batch_id=exc.batch_id)


# --------------------------------------------------------------------------------------
# inbound-email webhook routing (app/ingest/webhook.py)
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# INBOUND-EMAIL ROUTING FAILURES ANSWER 406, NOT 4xx-ANYTHING-ELSE.
#
# Mailgun retries any non-200 at 10 min, 15 min, 30 min, 1 h, 2 h and 4 h -- eight hours
# of retries -- and stops only on 200 or on 406, which its documentation defines as "the
# POST is rejected and it will not be retried". Every exception below is PERMANENT: a
# typo'd forwarding address, an investor or account that does not exist, no adapter for
# that document type, more attachments than this deployment accepts. No retry can ever
# turn one of them into a success, so the 422/404 these used to answer bought nothing and
# cost six redeliveries each -- multiplied, for a bulk forward, by the size of the
# delivery.
#
# Safe to apply globally rather than per-path: all four are raised ONLY inside
# app/ingest/webhook.py, so no other route can reach these handlers. A transient failure
# -- the database being unreachable -- is deliberately NOT in this list and still returns
# 5xx, because that is the one case where retrying is exactly right.
#
# docs/BACKFILL_PIPELINE.md section 5, defect (1).
# --------------------------------------------------------------------------------------

#: What Mailgun (and Postmark) read as "rejected, never retry". See the block above.
_WEBHOOK_PERMANENT = 406


@app.exception_handler(UnroutableAddressError)
async def unroutable_address_handler(request: Request, exc: UnroutableAddressError) -> JSONResponse:
    return _error(request, _WEBHOOK_PERMANENT, str(exc))


@app.exception_handler(NoSuchInvestorError)
async def no_such_investor_handler(request: Request, exc: NoSuchInvestorError) -> JSONResponse:
    return _error(request, _WEBHOOK_PERMANENT, f"no such investor: {exc}")


@app.exception_handler(NoMatchingAccountError)
async def no_matching_account_handler(
    request: Request, exc: NoMatchingAccountError
) -> JSONResponse:
    return _error(request, _WEBHOOK_PERMANENT, str(exc))


@app.exception_handler(UnsupportedDocumentError)
async def unsupported_document_handler(
    request: Request, exc: UnsupportedDocumentError
) -> JSONResponse:
    return _error(request, _WEBHOOK_PERMANENT, str(exc))


@app.exception_handler(TooManyAttachmentsError)
async def too_many_attachments_handler(
    request: Request, exc: TooManyAttachmentsError
) -> JSONResponse:
    """A mailbox bomb, or an over-eager select-all. Permanent by definition: the same
    message will always carry the same number of attachments, so retrying it for eight
    hours enqueues nothing and costs six more deliveries of the same oversized payload."""
    return _error(request, _WEBHOOK_PERMANENT, str(exc))


# --------------------------------------------------------------------------------------
# inbound-email webhook auth (app/ingest/webhook_providers.py) -- POST /ingest/email is
# public internet, so every rejection here is logged with the provider that was
# configured to check it, not just the blanket per-request line request_context already
# writes. Never a 200 that stages an unverified file.
# --------------------------------------------------------------------------------------


def _webhook_provider_for_log(request: Request) -> str | None:
    """The dependency-injected Settings the route actually used, stashed on
    ``request.state`` by app.routers.ingest.ingest_email -- a registered
    ``@app.exception_handler`` is called by Starlette directly, never through FastAPI's
    dependency injection, so it would otherwise see app.main's module-level
    ``get_settings()`` even under a test's ``dependency_overrides``."""
    settings = getattr(request.state, "settings", None)
    return settings.ingest_webhook_provider if settings is not None else None


@app.exception_handler(MissingWebhookCredentialError)
async def missing_webhook_credential_handler(
    request: Request, exc: MissingWebhookCredentialError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.warning(
        "request_id=%s provider=%s inbound-email webhook credential missing/malformed: %s",
        request_id,
        _webhook_provider_for_log(request),
        exc,
    )
    return _error(request, 401, str(exc))


@app.exception_handler(InvalidWebhookCredentialError)
async def invalid_webhook_credential_handler(
    request: Request, exc: InvalidWebhookCredentialError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.warning(
        "request_id=%s provider=%s inbound-email webhook credential did not verify: %s",
        request_id,
        _webhook_provider_for_log(request),
        exc,
    )
    return _error(request, 403, str(exc))


@app.exception_handler(WebhookNotConfiguredError)
async def webhook_not_configured_handler(
    request: Request, exc: WebhookNotConfiguredError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "request_id=%s INGEST_WEBHOOK_PROVIDER is unset; refusing the inbound-email "
        "webhook request rather than accepting an unverifiable one",
        request_id,
    )
    return _error(request, 500, "the server is not configured correctly")


# --------------------------------------------------------------------------------------
# ops endpoints (app/auth/ops.py) -- same three-way shape as the webhook handlers above:
# missing/invalid/not-configured, not collapsed, since there is no account-enumeration
# concern for a single shared operational secret.
# --------------------------------------------------------------------------------------


@app.exception_handler(OpsKeyMissingError)
async def ops_key_missing_handler(request: Request, exc: OpsKeyMissingError) -> JSONResponse:
    logger.warning(
        "request_id=%s path=%s ops request missing X-Ops-Key",
        getattr(request.state, "request_id", None),
        request.url.path,
    )
    return _error(request, 401, "missing X-Ops-Key header")


@app.exception_handler(OpsKeyInvalidError)
async def ops_key_invalid_handler(request: Request, exc: OpsKeyInvalidError) -> JSONResponse:
    logger.warning(
        "request_id=%s path=%s ops request's X-Ops-Key did not verify",
        getattr(request.state, "request_id", None),
        request.url.path,
    )
    return _error(request, 403, "invalid X-Ops-Key")


@app.exception_handler(OpsNotConfiguredError)
async def ops_not_configured_handler(request: Request, exc: OpsNotConfiguredError) -> JSONResponse:
    logger.error(
        "request_id=%s path=%s OPS_API_KEY is unset; refusing the ops request rather "
        "than accepting an unverifiable one",
        getattr(request.state, "request_id", None),
        request.url.path,
    )
    return _error(request, 500, "the server is not configured correctly")


@app.exception_handler(CronSecretMissingError)
async def cron_secret_missing_handler(
    request: Request, exc: CronSecretMissingError
) -> JSONResponse:
    logger.warning(
        "request_id=%s path=%s cron request missing Authorization header",
        getattr(request.state, "request_id", None),
        request.url.path,
    )
    return _error(request, 401, "missing Authorization header")


@app.exception_handler(CronSecretInvalidError)
async def cron_secret_invalid_handler(
    request: Request, exc: CronSecretInvalidError
) -> JSONResponse:
    logger.warning(
        "request_id=%s path=%s cron request's Authorization header did not verify",
        getattr(request.state, "request_id", None),
        request.url.path,
    )
    return _error(request, 403, "invalid Authorization header")


@app.exception_handler(CronSecretNotConfiguredError)
async def cron_secret_not_configured_handler(
    request: Request, exc: CronSecretNotConfiguredError
) -> JSONResponse:
    logger.error(
        "request_id=%s path=%s CRON_SECRET is unset; refusing the cron request rather "
        "than accepting an unverifiable one",
        getattr(request.state, "request_id", None),
        request.url.path,
    )
    return _error(request, 500, "the server is not configured correctly")


# --------------------------------------------------------------------------------------
# privacy (app/db/privacy.py)
# --------------------------------------------------------------------------------------


@app.exception_handler(LegalHoldError)
async def legal_hold_handler(request: Request, exc: LegalHoldError) -> JSONResponse:
    return _error(request, 409, str(exc), investor_id=exc.investor_id, reasons=exc.reasons)


@app.exception_handler(LookupError)
async def lookup_error_handler(request: Request, exc: LookupError) -> JSONResponse:
    return _error(request, 404, str(exc) or "not found")


# --------------------------------------------------------------------------------------
# consent (app/db/consent.py) -- DPDP is consent-first: this is what makes that real.
# --------------------------------------------------------------------------------------


@app.exception_handler(ConsentMissingError)
async def consent_missing_handler(request: Request, exc: ConsentMissingError) -> JSONResponse:
    # 403, not 401/404: the request is otherwise well-formed and the investor exists --
    # it's refused because DPDP requires a live grant before personal data is processed.
    return _error(request, 403, str(exc), investor_id=exc.investor_id)


# --------------------------------------------------------------------------------------
# grievance (app/db/grievance.py)
# --------------------------------------------------------------------------------------


@app.exception_handler(AlreadyResolvedError)
async def grievance_already_resolved_handler(
    request: Request, exc: AlreadyResolvedError
) -> JSONResponse:
    return _error(request, 409, f"grievance {exc} is already resolved and cannot be reopened")


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------


@app.exception_handler(EmailAlreadyRegisteredError)
async def email_taken_handler(request: Request, exc: EmailAlreadyRegisteredError) -> JSONResponse:
    return _error(request, 409, str(exc))


@app.exception_handler(InvalidCredentialsError)
async def invalid_credentials_handler(
    request: Request, exc: InvalidCredentialsError
) -> JSONResponse:
    return _error(request, 401, "invalid email or password")


# --------------------------------------------------------------------------------------
# product capabilities (app/db/capabilities.py) -- which PRODUCT, never whose DATA.
# --------------------------------------------------------------------------------------


@app.exception_handler(WorkspaceCapabilityRequiredError)
async def workspace_capability_required_handler(
    request: Request, exc: WorkspaceCapabilityRequiredError
) -> JSONResponse:
    """403 with a stable ``code``, never 401 and never 404.

    401 would tell a correctly authenticated caller to log in again, which fixes
    nothing and is what trains people to distrust a sign-in prompt. 404 would hide a
    genuine routing bug behind a plausible-looking miss. The request was understood,
    the caller is who they say they are, and the product they are asking for is not one
    they have -- that is 403, exactly.

    Clients branch on ``code``, not on the sentence: the wording here is free to
    improve without breaking a frontend that uses it to decide whether to show an
    upgrade panel. The user id goes to the log, never to the body.
    """
    logger.info(
        "request_id=%s path=%s user=%s refused: no workspace capability",
        getattr(request.state, "request_id", None),
        request.url.path,
        exc.user_id,
    )
    return _error(request, 403, str(exc), code=exc.code)


# --------------------------------------------------------------------------------------
# workspace research (app/routers/workspace.py, app/routers/research.py)
# --------------------------------------------------------------------------------------


@app.exception_handler(WorkspaceError)
async def workspace_error_handler(request: Request, exc: WorkspaceError) -> JSONResponse:
    """BLUEPRINT_BACKEND.md section 7.1's envelope for the research routes:
    ``{"detail": {"code", "message", "fields", "request_id"}}``. Clients branch on ``code``.
    Nothing from a request body or a database message beyond the documented sentence is
    echoed; the log carries the request id, path and code only."""
    request_id = getattr(request.state, "request_id", None)
    logger.info("request_id=%s path=%s workspace_refused code=%s status=%s",
                request_id, request.url.path, exc.code, exc.status)
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message, "fields": exc.fields,
                            "request_id": request_id}},
    )


#: Paths whose 422s must never echo a submitted value. Agent configuration is where a user
#: might paste a provider key into the wrong field; a result submission carries model output.
_REDACTED_VALIDATION_SEGMENTS = ("/workspace/", "/butler/", "/market-assistant/")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Research and Butler 422s in the section 7.1 envelope, WITHOUT input values.

    FastAPI's default body includes each failing ``input`` -- so an ``api_key`` field sent to
    POST /agents would be reflected straight back in the response, and into any log or trace
    that captures response bodies. Here only the location and the error type survive: the
    client learns WHICH field was refused (``api_key``: ``extra_forbidden``), never its value.
    Other routes retain FastAPI's detail-array shape with fixed messages and no inputs.
    """
    if "/corporate-actions" in request.url.path:
        # H3: a malformed operator batch or query is 400 on this feature, never 422.
        # Locations and error types only, never the submitted values.
        return _corporate_action_error(
            request, 400, "corporate_action_invalid_input",
            ValueError("the request does not match the declared contract"),
            fields=[{"loc": [str(p) for p in e.get("loc", ())], "type": str(e.get("type"))}
                    for e in exc.errors()],
        )
    if not any(seg in request.url.path for seg in _REDACTED_VALIDATION_SEGMENTS):
        # Never return input/ctx or validator-written messages: they can contain a
        # password, PAN, uploaded row or arbitrary text pasted into another field.
        return JSONResponse(status_code=422, content={
            "detail": [{"loc": list(e.get("loc", ())), "type": str(e.get("type", "invalid")),
                        "msg": "Invalid request value."} for e in exc.errors()],
            "request_id": getattr(request.state, "request_id", None),
        })
    request_id = getattr(request.state, "request_id", None)
    fields: dict[str, str] = {}
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()) if part != "body") or "body"
        fields.setdefault(loc[:120], str(error.get("type", "invalid"))[:60])
    logger.info("request_id=%s path=%s workspace_refused code=INVALID_INPUT status=422 "
                "fields=%s", request_id, request.url.path, sorted(fields))
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "INVALID_INPUT",
                            "message": "the request does not match the declared contract",
                            "fields": fields, "request_id": request_id}},
    )


# --------------------------------------------------------------------------------------
# onboarding (app/routers/investors.py, app/db/repository.py)
# --------------------------------------------------------------------------------------


@app.exception_handler(DuplicateInvestorCodeError)
async def duplicate_investor_code_handler(
    request: Request, exc: DuplicateInvestorCodeError
) -> JSONResponse:
    return _error(request, 409, str(exc), code=exc.code)


@app.exception_handler(InvalidPANError)
async def invalid_pan_handler(request: Request, exc: InvalidPANError) -> JSONResponse:
    return _error(request, 400, str(exc))


# --------------------------------------------------------------------------------------
# infrastructure
# --------------------------------------------------------------------------------------


@app.exception_handler(OperationalError)
async def database_unavailable_handler(request: Request, exc: OperationalError) -> JSONResponse:
    # Connection errors may contain credentials; log neither the message nor traceback.
    logger.error("request_id=%s database unavailable", getattr(request.state, "request_id", None))
    response = _error(
        request, 503, "The account service is temporarily unavailable. Please try again."
    )
    response.headers["Retry-After"] = "30"
    return response


@app.exception_handler(DatabaseNotConfiguredError)
async def database_not_configured_handler(
    request: Request, exc: DatabaseNotConfiguredError
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.error("request_id=%s database not configured: %s", request_id, exc, exc_info=True)
    # The connection string is never in the client-facing message -- a 500 body is not
    # the place to confirm or deny what DATABASE_URL happens to be.
    return _error(request, 500, "the server is not configured correctly")


@app.exception_handler(NonFiniteFinancialValueError)
async def non_finite_financial_value_handler(
    request: Request, exc: NonFiniteFinancialValueError
) -> JSONResponse:
    """A NaN or infinite figure reached the JSON boundary. That is a 500, on purpose.

    500 rather than 200-with-null, because null on a financial field means "not
    available" and this value IS available -- it is wrong. The alternative to failing
    here is a screen that shows a blank where a rate should be, indistinguishable from
    the legitimate blanks beside it (portfolio CAGR, an unpriced holding's weight). Loud
    beats plausible: CLAUDE.md rule 4, applied to the last inch of the pipe.
    """
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "request_id=%s path=%s non-finite financial value: %s",
        request_id,
        request.url.path,
        exc,
        exc_info=True,
    )
    return _error(request, 500, "a computed financial value is not representable")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the error type, raising location, request id and route; return a generic 500.
    Submitted data and credentials must not enter logs through exception messages.

    Called directly from ``CatchAllExceptionMiddleware`` above, not registered via
    ``@app.exception_handler(Exception)`` -- see that middleware's docstring for why."""
    request_id = getattr(request.state, "request_id", None)
    if request.url.path.startswith("/market-assistant/"):
        # docs/AI_ASSISTANT_PRIVACY_PLAN.md section 11: no raw question in any log. An
        # exception message or a traceback's chained context could carry the typed text,
        # so this path logs the exception TYPE and the raising location only.
        tb = exc.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        where = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}" if tb else "unknown"
        logger.error(
            "request_id=%s method=%s path=%s unhandled_exception_type=%s at=%s",
            request_id, request.method, request.url.path, type(exc).__name__, where,
        )
        return _error(request, 500, "internal server error")
    # Exception messages/chained tracebacks can contain submitted document data or
    # credentials. Keep the error type and location, never the raw exception value.
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}" if tb else "unknown"
    logger.error(
        "request_id=%s method=%s path=%s unhandled_exception_type=%s at=%s",
        request_id, request.method, request.url.path, type(exc).__name__, where,
    )
    return _error(request, 500, "internal server error")


app.include_router(auth.router)
app.include_router(brokers.router)
app.include_router(investors.router)
app.include_router(families.router)
app.include_router(holdings.router)
app.include_router(fno.router)
app.include_router(funds.router)
app.include_router(capital_gains.router)
app.include_router(transactions.router)
app.include_router(ingest.router)
app.include_router(email_automation.router)
app.include_router(analytical.router)
app.include_router(analytics.router)
app.include_router(privacy.router)
app.include_router(reconcile.router)
app.include_router(corporate_actions.router)
app.include_router(corporate_actions.ops_router)
app.include_router(dividends_ops.ops_router)
app.include_router(jobs_ops.ops_router)
app.include_router(security_alias.ops_router)
app.include_router(reference_sync.router)
app.include_router(demerger_watch.ops_router)
app.include_router(grievance.router)
app.include_router(consent.router)
app.include_router(market_assistant.router)
app.include_router(narrative.router)
app.include_router(narrative.ops_router)
app.include_router(market.router)
# Workspace research. Registered unconditionally; every route is guarded by
# require_workspace_enabled (WORKSPACE_ENABLED + a ready schema) and every WRITE by
# require_research_writes_ready (BE-Q04/BE-Q05), so registration enables nothing by itself.
app.include_router(workspace.router)
app.include_router(research.router)
# Butler (BT 01-06): same guards plus 051_butler.sql applied. Workspace-only; no model.
app.include_router(butler.router)
# Agents, tasks, attempts, review and chat (BE 19-25). Same guards plus 052 applied. The server
# never calls a model: the browser does, with a key the server never sees.
app.include_router(tasks.router)
# BT 12-15 notebook (053) and RP 01 report jobs (054). Same guards; each refuses with 503 until
# its migration is applied, and report admission refuses until an operator configures pricing.
app.include_router(butler_notebook.router)
app.include_router(reports.router)
# BE 36-37 monitor schedules and due history. Same guards plus 055/057 applied. Saves schedules
# and shows blocked occurrences; nothing executes while BE-Q01 is open.
app.include_router(monitors.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. No DB round trip -- a probe that touches the database turns a
    slow query into an outage."""
    return {"status": "ok"}


@app.get("/health/storage")
def health_storage() -> JSONResponse:
    """Can we actually reach the document archive? Deliberately NOT part of ``/health``.

    WHY THIS EXISTS. ``Settings`` can prove that a bucket and endpoint are configured but
    not that a credential resolves, because only a real call can prove that and a config
    validator must not make network calls. Meanwhile ``S3DocumentStore`` builds its boto3
    client lazily, so without this the first proof that R2 works is a client's first
    upload -- possibly days after a deploy, and after a key rotation, the first proof that
    it has stopped working. One HEAD against the bucket moves that discovery to a probe
    that can be called on purpose.

    Answers 503 rather than 500 when the archive is unreachable: the process is alive and
    the fault is a dependency, which is what a load balancer and a human both need to
    distinguish. Never leaks the failure detail -- a boto3 error can carry the endpoint,
    the bucket and part of a key id -- so the reason is logged and the response says only
    which check failed.
    """
    settings_now = get_settings()
    try:
        store = get_store(settings_now)
        reachable = store.exists("__health_probe__")
    except Exception:  # noqa: BLE001 - a probe reports, it never propagates
        logger.exception("storage health probe failed")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "check": "storage",
                "backend": settings_now.storage_backend,
            },
        )
    # `exists` returning False is a SUCCESS: the probe key is not meant to be there. What
    # is being proven is that the call completed -- credentials resolved, the bucket
    # answered, the endpoint was right.
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "check": "storage",
            "backend": settings_now.storage_backend,
            "probe_key_present": bool(reachable),
        },
    )
