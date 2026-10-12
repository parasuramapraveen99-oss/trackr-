"""Research evidence routes: sources and retained text (session 12), sourced facts (13),
evaluation receipts (15), method versions (16-17) and the read-only tools (18).
BLUEPRINT_BACKEND.md section 7.3.

Same guards as ``app/routers/workspace.py``: session, ownership, Workspace capability,
enabled/ready Workspace; WRITE routes also need write readiness and an Idempotency-Key.

THE FINANCIAL/WORKSPACE BOUNDARY, for source documents. Bytes go through the EXISTING
private document store on the EXISTING financial connection (the workspace capability roles
hold no grant on ``document`` -- migration 045), with doctype RESEARCH and parse status
RESEARCH_ONLY so no financial parser ever picks them up. The research rows that cite those
bytes are then written on the HUMAN workspace connection. Two databases transactions, two
failure domains -- so every newly stored object whose citing source then fails to commit is
recorded in the audit log as a pending research artifact, never silently deleted: a
same-investor duplicate may be shared with an existing (financial) document, and deleting
it would destroy someone else's evidence.

NOTHING HERE FETCHES A URL, PARSES A BROKER FILE, STAGES A ROW OR CALLS A MODEL.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import ValidationError

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import audit
from app.db import research as rs
from app.db import research_skills as rsk
from app.db import tasks as tk
from app.db import workspace as ws
from app.db.connection import WorkspaceCapability
from app.db.documents import find_by_hash, store_document
from app.prices.live import ist_today
from app.research import canonical
from app.research import documents as docs
from app.research import skills as sk
from app.research import tools as tl
from app.research.errors import (
    CONTEXT_DRIFT,
    EXTRACTION_UNAVAILABLE,
    INVALID_PAGE,
    PAGE_REQUIRED,
    PAYLOAD_TOO_LARGE,
    SOURCE_CONTENT_UNAVAILABLE,
    UNSUPPORTED_MEDIA,
    WorkspaceError,
    invalid,
)
from app.research.source_policy import (
    USE_EXTRACT,
    USE_METADATA,
    USE_READ,
    USE_STORE,
    SourcePolicy,
)
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_calendar_provider,
    get_formula_registry,
    get_metric_registry,
    get_source_adapters,
    get_source_policy,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.research import (
    CatalogueOut,
    Evaluation,
    EvaluationDetail,
    EvaluationRequestIn,
    EventVerificationIn,
    Fact,
    FactCreateIn,
    FactRevisionIn,
    FactVerificationIn,
    ResearchEvent,
    Skill,
    SkillCreateIn,
    SourceContentOut,
    SourceCreateIn,
    SourceRefreshIn,
    SourceRefreshOut,
    ToolRequestIn,
    ToolResponse,
)
from app.schemas.workspace import Page, Source
from app.storage.base import ObjectNotFoundError
from app.storage.factory import get_store

logger = logging.getLogger(__name__)

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN
ENGINE = WorkspaceCapability.ENGINE

#: audit_log action for a stored research object whose citing source did not commit.
ACTION_RESEARCH_ARTIFACT_PENDING = "RESEARCH_ARTIFACT_PENDING"
DOCTYPE_RESEARCH = "RESEARCH"
DOCTYPE_RESEARCH_TEXT = "RESEARCH_TEXT"
PARSE_RESEARCH_ONLY = "RESEARCH_ONLY"
_CHUNK = 1024 * 1024

router = APIRouter(
    prefix="/investors/{investor_id}/workspace",
    tags=["research"],
    dependencies=[
        Depends(require_owned_investor),
        Depends(require_workspace_access),
        Depends(require_workspace_enabled),
    ],
)

WRITE = [Depends(require_research_writes_ready)]


def _reply(response: Response, result: tuple[Any, int]) -> Any:
    body, status = result
    response.status_code = status
    return body


def _actor(user: dict[str, Any]) -> str:
    return str(user["id"])


def _digest(operation: str, body: Any) -> str:
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
    return canonical.digest({"operation": operation, "body": payload})


# =========================================================================================
# session 12 -- sources and retained documents
# =========================================================================================


def _investor_code(fin_conn: Any, investor_id: str) -> str:
    with fin_conn.cursor() as cur:
        cur.execute("SELECT code FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None or not (row[0] or "").strip():
        raise WorkspaceError(409, "INVESTOR_CODE_MISSING",
                             "this investor has no storage code; documents cannot be stored")
    return str(row[0])


def _store_research_object(
    fin_conn: Any, store: Any, investor_id: str, code: str, doctype: str, content: bytes,
    filename: str, content_type: str, actor_uid: str,
) -> dict[str, Any]:
    """store_document, converging on a concurrent same-bytes insert instead of failing.

    The object key is content-addressed, so a losing concurrent writer overwrote the
    winner's object with IDENTICAL bytes -- nothing is lost and nothing is orphaned; the
    loser simply adopts the winner's row. Committed immediately so a later workspace
    failure cannot roll back a row whose object already exists.
    """
    try:
        stored = store_document(
            fin_conn, store, investor_id, code, doctype, content, ist_today(),
            filename=filename, content_type=content_type, actor_uid=actor_uid,
            parse_status=PARSE_RESEARCH_ONLY,
        )
        fin_conn.commit()
        return stored
    except psycopg.errors.UniqueViolation:
        fin_conn.rollback()
        digest = hashlib.sha256(content).hexdigest()
        existing = find_by_hash(fin_conn, digest, investor_id)
        if existing is None:
            raise
        return {**existing, "id": str(existing["id"]), "sha256": digest,
                "already_present": True}


def _record_pending(fin_conn: Any, investor_id: str, actor_uid: str,
                    created: list[dict[str, Any]]) -> None:
    """Newly created objects whose citing source failed: recorded, never deleted here."""
    fresh = [d for d in created if not d.get("already_present")]
    if not fresh:
        return
    try:
        audit.record(fin_conn, ACTION_RESEARCH_ARTIFACT_PENDING, "investor", investor_id,
                     actor_uid, {"document_ids": sorted(d["id"] for d in fresh),
                                 "count": len(fresh)})
        fin_conn.commit()
    except Exception:  # the original failure matters more; log and let it propagate
        logger.exception("could not record pending research artifacts for %s", investor_id)


def _extract_and_retain(
    fin_conn: Any, store: Any, investor_id: str, code: str, content: bytes, media: str,
    sha: str, actor_uid: str, policy: SourcePolicy, source_type: str,
    created: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pin extraction for retained bytes; keep the text as its own RESEARCH_TEXT document."""
    policy.require(source_type, USE_EXTRACT)
    extraction = docs.extract(content, media)
    text_document_id = None
    if extraction.status == "EXTRACTED":
        text_doc = _store_research_object(
            fin_conn, store, investor_id, code, DOCTYPE_RESEARCH_TEXT, extraction.text_bytes,
            f"{sha[:16]}.txt", "text/plain; charset=utf-8", actor_uid)
        created.append(text_doc)
        text_document_id = text_doc["id"]
    return {
        "status": extraction.status,
        "unavailable_reason": extraction.unavailable_reason,
        "extractor_id": extraction.extractor_id,
        "extractor_version": extraction.extractor_version,
        "normalization": extraction.normalization,
        "text_sha256": extraction.text_sha256,
        "text_document_id": text_document_id,
        "page_count": extraction.page_count,
    }


def _verified_bytes(fin_conn: Any, store: Any, investor_id: str, document_id: str
                    ) -> tuple[dict[str, Any], bytes]:
    """This investor's document row and its retained bytes, rehashed. Never trusts a
    client hash; another investor's document id is simply not found."""
    document_id = ws._uuid(document_id, "document")
    with fin_conn.cursor() as cur:
        cur.execute("SELECT id, storage_key, sha256, content_type, filename FROM document "
                    "WHERE investor_id = %s AND id = %s", (investor_id, document_id))
        row = cur.fetchone()
    if row is None:
        raise WorkspaceError(404, "NOT_FOUND", "no such document")
    doc = {"id": str(row[0]), "storage_key": row[1], "sha256": row[2],
           "content_type": row[3], "filename": row[4]}
    try:
        content = store.get(doc["storage_key"])
    except (ObjectNotFoundError, FileNotFoundError) as exc:
        raise WorkspaceError(409, SOURCE_CONTENT_UNAVAILABLE,
                             "the retained bytes for this document are missing") from exc
    if hashlib.sha256(content).hexdigest() != doc["sha256"]:
        raise WorkspaceError(409, CONTEXT_DRIFT,
                             "the retained bytes no longer match their recorded hash")
    return doc, content


@router.get("/companies/{isin}/sources", response_model=Page[Source])
def list_sources(
    investor_id: str,
    isin: str,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rs.list_sources(conn, investor_id, isin, cursor, limit)


@router.post("/companies/{isin}/sources", response_model=Source, status_code=201,
             dependencies=WRITE)
def create_source(
    investor_id: str,
    isin: str,
    body: SourceCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Metadata citation, optionally referencing an EXISTING document of this investor.

    A metadata-only source records that something was cited, not that it was read; its
    content reads say SOURCE_CONTENT_UNAVAILABLE. With a document_id, ownership and the
    retained bytes are verified and extraction is pinned now."""
    policy.require(body.source_type, USE_METADATA)
    rs.check_source_url(body.source_url)
    if body.content_sha256 is not None and body.document_id is None:
        raise invalid("content_sha256 without retained bytes proves nothing; omit it",
                      {"content_sha256": "WITHOUT_DOCUMENT"})
    payload_digest = _digest(rs.OP_SOURCE_CREATE, body)
    actor = _actor(user)
    verified = extraction = None
    created: list[dict[str, Any]] = []
    if body.document_id is not None:
        with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
            ws.require_unlocked(conn, investor_id, isin)
            receipt = rs.peek_receipt(conn, investor_id, rs.OP_SOURCE_CREATE,
                                      isin.strip().upper(), key, payload_digest)
            if receipt is not None:
                response.status_code = 200
                return rs.source_row(conn, investor_id, receipt.result["source_id"])
        policy.require(body.source_type, USE_STORE)
        store = get_store(settings)
        doc, content = _verified_bytes(fin_conn, store, investor_id, body.document_id)
        if body.content_sha256 is not None and body.content_sha256 != doc["sha256"]:
            raise invalid("content_sha256 does not match the retained bytes",
                          {"content_sha256": "MISMATCH"})
        try:
            media = docs.sniff_media_type(content, doc["content_type"], doc["filename"])
        except docs.UnsupportedDocumentError as exc:
            raise WorkspaceError(415, UNSUPPORTED_MEDIA, str(exc)) from exc
        verified = {"id": doc["id"], "sha256": doc["sha256"]}
        extraction = _extract_and_retain(
            fin_conn, store, investor_id, _investor_code(fin_conn, investor_id), content, media,
            doc["sha256"], actor, policy, body.source_type, created)
    try:
        with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
            return _reply(response, rs.create_source(
                conn, investor_id, actor, isin, body.model_dump(), rs.OP_SOURCE_CREATE, key,
                payload_digest, verified, extraction))
    except Exception:
        _record_pending(fin_conn, investor_id, actor, created)
        raise


def _read_bounded(upload: UploadFile) -> bytes:
    """Read at most MAX_UPLOAD_BYTES, refusing as soon as one more byte appears. The size
    is enforced on the bytes actually read, never on a Content-Length claim."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = upload.file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > docs.MAX_UPLOAD_BYTES:
            raise WorkspaceError(413, PAYLOAD_TOO_LARGE,
                                 f"documents are limited to {docs.MAX_UPLOAD_BYTES} bytes")
        chunks.append(chunk)
    if total == 0:
        raise invalid("the uploaded file is empty", {"file": "EMPTY"})
    return b"".join(chunks)


@router.post("/companies/{isin}/source-documents", response_model=Source, status_code=201,
             dependencies=WRITE)
def upload_source_document(
    investor_id: str,
    isin: str,
    response: Response,
    file: UploadFile = File(...),
    provenance: str = Form(...),
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Multipart ``file`` (PDF or UTF-8 text, <= 20 MiB) + ``provenance`` (SourceCreate JSON).

    The server computes the document id and hash; ``provenance`` may not name either.
    Idempotency binds the canonical provenance AND the file's SHA-256, never the filename.
    """
    try:
        body = SourceCreateIn.model_validate_json(provenance)
    except ValidationError as exc:
        raise invalid("provenance is not a valid SourceCreate",
                      {"provenance": [e["loc"][-1] if e["loc"] else "body"
                                      for e in exc.errors()]}) from exc
    if body.document_id is not None or body.content_sha256 is not None:
        raise invalid("document_id and content_sha256 are computed by the server on upload",
                      {"provenance": "SERVER_DERIVED"})
    policy.require(body.source_type, USE_METADATA)
    policy.require(body.source_type, USE_STORE)
    rs.check_source_url(body.source_url)
    content = _read_bounded(file)
    try:
        media = docs.sniff_media_type(content, file.content_type, file.filename)
    except docs.UnsupportedDocumentError as exc:
        raise WorkspaceError(415, UNSUPPORTED_MEDIA, str(exc)) from exc
    sha = hashlib.sha256(content).hexdigest()
    payload_digest = canonical.digest({"operation": rs.OP_SOURCE_UPLOAD,
                                       "provenance": body.model_dump(mode="json"),
                                       "file_sha256": sha, "media_type": media})
    actor = _actor(user)
    scope = isin.strip().upper()

    # A retry answers from its receipt before touching storage again.
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        ws.require_unlocked(conn, investor_id, isin)
        receipt = rs.peek_receipt(conn, investor_id, rs.OP_SOURCE_UPLOAD, scope, key,
                                  payload_digest)
        if receipt is not None:
            response.status_code = 200
            return rs.source_row(conn, investor_id, receipt.result["source_id"])

    store = get_store(settings)
    code = _investor_code(fin_conn, investor_id)
    created: list[dict[str, Any]] = []
    stored = _store_research_object(fin_conn, store, investor_id, code, DOCTYPE_RESEARCH,
                                    content, file.filename or "", media, actor)
    created.append(stored)
    try:
        extraction = _extract_and_retain(fin_conn, store, investor_id, code, content, media,
                                         sha, actor, policy, body.source_type, created)
        with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
            return _reply(response, rs.create_source(
                conn, investor_id, actor, isin, body.model_dump(), rs.OP_SOURCE_UPLOAD, key,
                payload_digest, {"id": stored["id"], "sha256": sha}, extraction))
    except Exception:
        _record_pending(fin_conn, investor_id, actor, created)
        raise


@router.get("/sources/{source_id}", response_model=Source)
def get_source(
    investor_id: str,
    source_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Metadata only. No download, no signed URL."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rs.source_row(conn, investor_id, source_id)


@router.get("/sources/{source_id}/content", response_model=SourceContentOut)
def get_source_content(
    investor_id: str,
    source_id: str,
    page: int | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Retained text verified against BOTH pinned digests. Never re-extracted under a newer
    library and never silently truncated: an oversized result demands a page."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        source = rs.source_row(conn, investor_id, source_id)
        pin = rs.extraction_row(conn, investor_id, source["id"])
    policy.require(source["source_type"], USE_READ)
    if source["document_id"] is None or pin is None:
        raise WorkspaceError(409, SOURCE_CONTENT_UNAVAILABLE,
                             "this is a metadata-only citation; no text was retained")
    if pin["status"] != "EXTRACTED":
        raise WorkspaceError(409, SOURCE_CONTENT_UNAVAILABLE,
                             f"no text could be extracted ({pin['unavailable_reason']})",
                             {"reason": pin["unavailable_reason"]})
    store = get_store(settings)
    _, content = _verified_bytes(fin_conn, store, investor_id, source["document_id"])
    if hashlib.sha256(content).hexdigest() != source["content_sha256"]:
        raise WorkspaceError(409, CONTEXT_DRIFT, "retained bytes do not match the source pin")

    text: str | None = None
    try:
        _, text_bytes = _verified_bytes(fin_conn, store, investor_id, pin["text_document_id"])
        if hashlib.sha256(text_bytes).hexdigest() != pin["text_sha256"]:
            raise WorkspaceError(409, CONTEXT_DRIFT, "retained text does not match its pin")
        text = text_bytes.decode("utf-8")
    except WorkspaceError as exc:
        if exc.code == CONTEXT_DRIFT:
            raise
        # Retained text is gone. Only the PINNED extractor may regenerate it, and the
        # result must hash to the pinned digest; otherwise the content is unavailable.
        if docs.extractor_version(pin["extractor_id"]) != pin["extractor_version"] \
                or docs.NORMALIZATION != pin["normalization"]:
            raise WorkspaceError(409, SOURCE_CONTENT_UNAVAILABLE,
                                 "retained text is missing and the pinned extractor is not "
                                 "installed", {"reason": EXTRACTION_UNAVAILABLE}) from exc
        media = docs.PDF_MEDIA if pin["extractor_id"] == docs.EXTRACTOR_PDF else docs.TEXT_MEDIA
        redo = docs.extract(content, media)
        if redo.text_sha256 != pin["text_sha256"] or redo.text is None:
            raise WorkspaceError(409, CONTEXT_DRIFT,
                                 "re-extraction does not reproduce the pinned text") from exc
        text = redo.text
    try:
        chosen, page_count = docs.page_text(text, page)
    except IndexError as exc:
        raise invalid("page is outside this document", {"page": INVALID_PAGE},
                      code=INVALID_PAGE) from exc
    except OverflowError as exc:
        raise WorkspaceError(413, PAGE_REQUIRED,
                             "the text exceeds 256 KiB; request a single page",
                             {"page": PAGE_REQUIRED}) from exc
    audit.record(fin_conn, audit.ACTION_DOCUMENT_ACCESSED, "document", source["document_id"],
                 _actor(user), {"purpose": "research_source_content", "page": page})
    return {
        "source_id": source["id"],
        "content_sha256": source["content_sha256"],
        "text_sha256": pin["text_sha256"],
        "extractor_id": pin["extractor_id"],
        "extractor_version": pin["extractor_version"],
        "normalization": pin["normalization"],
        "page": page,
        "page_count": page_count,
        "text": chosen,
        "document_reference": source["document_reference"],
    }


# =========================================================================================
# session 13 -- facts
# =========================================================================================


@router.get("/companies/{isin}/facts", response_model=Page[Fact])
def list_facts(
    investor_id: str,
    isin: str,
    metric: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rs.list_facts(conn, investor_id, isin, metric, period_start, period_end,
                             cursor, limit)


@router.post("/companies/{isin}/facts", response_model=Fact, status_code=201,
             dependencies=WRITE)
def create_fact(
    investor_id: str,
    isin: str,
    body: FactCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    registry: Any = Depends(get_metric_registry),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.create_fact(
            conn, investor_id, _actor(user), isin, body.model_dump(), registry, key,
            _digest(rs.OP_FACT_CREATE, body)))


# =========================================================================================
# session 29 -- approved external evidence (refresh) and dated events
# =========================================================================================


def _require_evidence_schema(conn: Any) -> None:
    from app.db.migrate import evidence_schema_ready

    if not evidence_schema_ready(conn):
        raise WorkspaceError(503, "EVIDENCE_UNAVAILABLE",
                             "external evidence storage (migration 055) is not installed")


@router.post("/companies/{isin}/source-refreshes", response_model=SourceRefreshOut,
             status_code=201, dependencies=WRITE)
def refresh_sources(
    investor_id: str,
    isin: str,
    body: SourceRefreshIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
    registry: Any = Depends(get_metric_registry),
    adapters: dict[str, Any] = Depends(get_source_adapters),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> Any:
    """WRITE. One bounded retrieval through one APPROVED adapter, stored append-only.

    No adapter is approved on this server (BE-Q04), so a live call is 403 SOURCE_NOT_APPROVED
    before anything is fetched. Extracted values arrive UNVERIFIED; a repeat refresh over the
    same periods is a no-op; a revised document appends and supersedes within its own origin
    only. Nothing here touches financial ingestion or the ledger."""
    from app.db import research_evidence as ev
    from app.research import sources as src

    adapter = adapters.get(body.adapter_id)
    if adapter is None:
        raise WorkspaceError(403, src.SOURCE_NOT_APPROVED,
                             "no approved adapter has this id; nothing was fetched (BE-Q04)")
    _require_evidence_schema(fin_conn)
    actor = _actor(user)
    scope = isin.strip().upper()
    payload_digest = _digest(ev.OP_REFRESH, body)
    with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
        ws.require_unlocked(conn, investor_id, isin)
        receipt = rs.peek_receipt(conn, investor_id, ev.OP_REFRESH, scope, key,
                                  payload_digest)
        if receipt is not None:
            response.status_code = 200
            return {**receipt.result, "replayed": True, "synthetic": adapter.synthetic}

    items = src.retrieve(adapter, scope, body.periods)
    prepared, unknowns = src.prepare(items, policy, adapter)

    retained: dict[str, dict[str, Any]] = {}
    created: list[dict[str, Any]] = []
    try:
        store = get_store(settings) if any(p.store_bytes for p in prepared) else None
        code = _investor_code(fin_conn, investor_id) if store is not None else ""
        for p in prepared:
            if not p.store_bytes or p.item.content is None:
                continue
            try:
                media = docs.sniff_media_type(p.item.content, p.item.media_type, None)
            except docs.UnsupportedDocumentError:
                p.store_bytes = False
                p.unknowns.append({"origin_item_id": p.item.origin_item_id,
                                   "code": "MEDIA_NOT_SUPPORTED"})
                continue
            stored = _store_research_object(fin_conn, store, investor_id, code,
                                            DOCTYPE_RESEARCH, p.item.content,
                                            f"{p.item.origin_item_id[:40]}", media, actor)
            created.append(stored)
            extraction = None
            if policy.allows(p.item.source_type, USE_EXTRACT):
                extraction = _extract_and_retain(
                    fin_conn, store, investor_id, code, p.item.content, media,
                    p.item.content_sha256 or "", actor, policy, p.item.source_type, created)
            retained[p.item.origin_item_id] = {"id": stored["id"], "extraction": extraction}
        with db_errors(), sessions.open(HUMAN, investor_id, actor) as conn:
            summary, status = ev.record_refresh(
                conn, investor_id, actor, isin, adapter, prepared, unknowns, retained,
                registry, key, payload_digest, list(body.periods))
            return _reply(response, summary, status)
    except Exception:
        _record_pending(fin_conn, investor_id, actor, created)
        raise


@router.get("/companies/{isin}/events", response_model=list[ResearchEvent])
def list_events(
    investor_id: str,
    isin: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> list[dict[str, Any]]:
    """Every held event for the company, history included: SCHEDULED, OBSERVED, CLAIMED and
    CANCELLED distinct, reschedules and verification copies linked. Not a feed."""
    from app.db import research_evidence as ev

    _require_evidence_schema(fin_conn)
    with db_errors(), sessions.open(READER, investor_id) as conn:
        isin = ws.require_unlocked(conn, investor_id, isin)
        return ev.list_events(conn, investor_id, ws.isin_family(conn, isin))


@router.post("/events/{event_id}/verifications", response_model=ResearchEvent,
             status_code=201, dependencies=WRITE)
def verify_event(
    investor_id: str,
    event_id: str,
    body: EventVerificationIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> Any:
    """A person rechecked the event's identity against its source. HUMAN only. A claimed
    (rumoured) date, or one that is not exact, cannot be verified."""
    from app.db import research_evidence as ev

    _require_evidence_schema(fin_conn)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ev.verify_event(
            conn, investor_id, _actor(user), event_id, body.verification_reference,
            body.expected_event_id, key, _digest(ev.OP_EVENT_VERIFY, body)))


@router.post("/facts/{fact_id}/verifications", response_model=Fact, status_code=201,
             dependencies=WRITE)
def verify_fact(
    investor_id: str,
    fact_id: str,
    body: FactVerificationIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """An authenticated person's recheck. Runs as HUMAN only; no runner/scheduler path."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.verify_fact(
            conn, investor_id, _actor(user), fact_id, body.verification_reference,
            body.expected_fact_id, key, _digest(rs.OP_FACT_VERIFY, body)))


@router.post("/facts/{fact_id}/revisions", response_model=Fact, status_code=201,
             dependencies=WRITE)
def revise_fact(
    investor_id: str,
    fact_id: str,
    body: FactRevisionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    registry: Any = Depends(get_metric_registry),
) -> Any:
    fact = body.model_dump(exclude={"expected_fact_id"})
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.revise_fact(
            conn, investor_id, _actor(user), fact_id, fact, body.expected_fact_id, registry,
            key, _digest(rs.OP_FACT_REVISE, body)))


# =========================================================================================
# session 15 -- evaluations
# =========================================================================================


@router.post("/theses/{thesis_id}/evaluate", response_model=Evaluation)
def evaluate(
    investor_id: str,
    thesis_id: str,
    body: EvaluationRequestIn,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    registry: Any = Depends(get_metric_registry),
    calendar: Any = Depends(get_calendar_provider),
) -> dict[str, Any]:
    """READ. A POST because it carries a body; it stores nothing -- no receipt, no
    evaluation_fact, no verification. One REPEATABLE READ snapshot for all inputs."""
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        return rs.evaluate_readonly(conn, investor_id, thesis_id, body.evidence_cutoff,
                                    registry, calendar)


@router.post("/theses/{thesis_id}/evaluations", response_model=Evaluation, status_code=201,
             dependencies=WRITE)
def record_evaluation(
    investor_id: str,
    thesis_id: str,
    body: EvaluationRequestIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    registry: Any = Depends(get_metric_registry),
    calendar: Any = Depends(get_calendar_provider),
) -> Any:
    """WRITE, as the ENGINE capability: the server computes the receipt; the request can
    only choose the cutoff."""
    with db_errors(), sessions.open(ENGINE, investor_id) as conn:
        return _reply(response, rs.record_evaluation(
            conn, investor_id, thesis_id, body.evidence_cutoff, registry, calendar, key,
            _digest(rs.OP_EVALUATION, body), requested_by=_actor(user)))


@router.get("/theses/{thesis_id}/evaluations", response_model=Page[Evaluation])
def list_evaluations(
    investor_id: str,
    thesis_id: str,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rs.list_evaluations(conn, investor_id, thesis_id, cursor, limit)


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationDetail)
def get_evaluation(
    investor_id: str,
    evaluation_id: str,
    replay: bool = False,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """The stored result and its pinned provenance. ``replay=true`` re-runs it from the
    pinned inputs and reports VERIFIED / DRIFT / UNAVAILABLE; the receipt never changes."""
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        stored = rs.evaluation_by_id(conn, investor_id, evaluation_id)
        if replay:
            stored["replay"] = rs.replay_evaluation(conn, investor_id, evaluation_id)
        return stored


# =========================================================================================
# sessions 16-17 -- method versions
# =========================================================================================


@router.get("/skill-catalogue", response_model=CatalogueOut)
def skill_catalogue(
    investor_id: str,
    formulas: Any = Depends(get_formula_registry),
    policy: SourcePolicy = Depends(get_source_policy),
    adapters: dict[str, Any] = Depends(get_source_adapters),
) -> dict[str, Any]:
    """READ, no database: the version-controlled catalogue a person may publish from, with
    each method's availability under the CURRENT formula registry and source policy, the
    approved formulas and source adapters that actually exist (none, while BE-Q03/Q04 are
    open), and the eight-method audit. This route never seeds a row."""
    return sk.catalogue_listing(formulas, policy, adapters)


@router.get("/skills", response_model=Page[Skill])
def list_skills(
    investor_id: str,
    history: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Latest version per published method, enabled or disabled. An investor who has
    published nothing gets an empty page, and nothing is inserted."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rsk.list_skills(conn, investor_id, history, cursor, limit)


@router.get("/skills/{skill_id}/versions/{version}", response_model=Skill)
def get_skill_version(
    investor_id: str,
    skill_id: str,
    version: int,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rsk.skill_version(conn, investor_id, skill_id, version)


@router.post("/skills/{skill_id}/versions", response_model=Skill, status_code=201,
             dependencies=WRITE)
def publish_skill(
    investor_id: str,
    skill_id: str,
    body: SkillCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Publish the next version of a KNOWN catalogue method (``expected_version=0`` for the
    first). Disabling is a new version with ``enabled=false``; nothing is deleted."""
    digest = canonical.digest({"operation": rsk.OP_SKILL_PUBLISH, "skill_id": skill_id,
                               "body": body.model_dump(mode="json")})
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rsk.publish_skill(
            conn, investor_id, _actor(user), skill_id, body.model_dump(), key, digest))


# =========================================================================================
# session 18 -- read-only tools
# =========================================================================================


@router.post("/tools/{tool_name}", response_model=ToolResponse)
def call_tool(
    investor_id: str,
    tool_name: str,
    body: ToolRequestIn,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
    registry: Any = Depends(get_metric_registry),
    calendar: Any = Depends(get_calendar_provider),
    formulas: Any = Depends(get_formula_registry),
    policy: SourcePolicy = Depends(get_source_policy),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """READ. The name is checked against the code-owned allowlist before anything else runs
    (404 otherwise). The handler gets a READER connection -- no INSERT privilege anywhere --
    and ``task_id`` can only narrow what it sees to that task's pinned references. The caller's
    identity is passed only as the origin of scenario assumptions on a direct call."""
    name = tl.resolve_name(tool_name)
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        scope = tk.task_scope(conn, investor_id, body.task_id) if body.task_id else None
        return tl.dispatch(name, conn=conn, fin_conn=fin_conn, investor_id=investor_id,
                           isin=body.isin, args=body.args, scope=scope, registry=registry,
                           calendar=calendar, formulas=formulas, policy=policy,
                           actor_uid=_actor(user))
