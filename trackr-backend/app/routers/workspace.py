"""Workspace research routes: companies and the question ritual (session 08), notes and
revision history (09), questions, answers and Home (10), thesis versions and events (11), and
the human exit-trigger override (15). BLUEPRINT_BACKEND.md section 7.2.

Every route is under ``/investors/{investor_id}/workspace`` and requires, in order: a valid
session (401), ownership of the investor (403/404, unchanged from the portfolio routes), the
``workspace`` product capability (403), and an enabled, ready Workspace (503). WRITE routes
also require ``require_research_writes_ready`` and a UUID ``Idempotency-Key``; a first
success is 201, an exact retry replays the original result with 200, and the same key with a
different payload is 409 IDEMPOTENCY_CONFLICT.

Reads run as the READER capability, a person's writes as HUMAN attributed to the
authenticated user. No route here reads or writes the financial ledger except the optional
company position, which is the existing holdings engine on the existing financial
connection -- never stored.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.database import get_conn
from app.db import research as rs
from app.db import tasks as tk
from app.db import workspace as ws
from app.db.connection import WorkspaceCapability
from app.prices.live import ist_today
from app.research import canonical
from app.research import text as tx
from app.research.errors import COMMITMENT_CONFLICT, invalid
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_calendar_provider,
    get_metric_registry,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.workspace import (
    Answer,
    AnswerIn,
    CompanyLinkIn,
    CompanyListItem,
    CompanyView,
    HomeOut,
    Identity,
    Note,
    NoteCreateIn,
    NoteLink,
    NoteLinkIn,
    NoteRevisionIn,
    OverrideIn,
    Page,
    Question,
    QuestionIn,
    RitualIn,
    RitualOut,
    Thesis,
    ThesisEvent,
    ThesisEventIn,
    ThesisIn,
    ThesisPublishOut,
)

logger = logging.getLogger(__name__)

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN

router = APIRouter(
    prefix="/investors/{investor_id}/workspace",
    tags=["workspace"],
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


def _digest(operation: str, body: Any) -> str:
    """Canonical request digest. Path scope is part of the receipt namespace already."""
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
    return canonical.digest({"operation": operation, "body": payload})


def _actor(user: dict[str, Any]) -> str:
    return str(user["id"])


# =========================================================================================
# session 08 -- companies and the ritual
# =========================================================================================


@router.get("/companies", response_model=Page[CompanyListItem])
def list_companies(
    investor_id: str,
    q: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.list_companies(conn, investor_id, q, cursor, limit)


@router.get("/securities", response_model=Page[Identity])
def search_securities(
    investor_id: str,
    q: str = Query(..., min_length=1, max_length=64),
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.search_securities(conn, investor_id, q, cursor, limit)


@router.post("/companies", response_model=Identity, status_code=201, dependencies=WRITE)
def link_company(
    investor_id: str,
    body: CompanyLinkIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.link_company(
            conn, investor_id, _actor(user), body.isin, key,
            _digest(ws.OP_COMPANY_LINK, {"isin": body.isin.strip().upper()})))


def _position(
    fin_conn: Any, investor_id: str, family: list[str], as_of: date, advised_only: bool
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """The existing holdings engine's read for this company's family, unchanged: priced
    holdings with Decimal-string figures, unpriced ones named and excluded from values
    (CLAUDE.md rule 4). Computed on read, never stored in ``company``."""
    from app.routers.holdings import _portfolio, _row

    notices: list[dict[str, str]] = []
    try:
        book = _portfolio(fin_conn, investor_id, as_of, advised_only=advised_only)
    except Exception:
        logger.exception("position read failed for investor %s", investor_id)
        return None, [{"code": "POSITION_UNAVAILABLE",
                       "message": "the portfolio position could not be read"}]
    fam = set(family)
    held = [_row(book, h) for h in book["valued"] if h.isin in fam]
    unpriced = [{"isin": h.isin, "invested": format(h.invested, "f")}
                for h in book["unpriced"] if h.isin in fam]
    if unpriced:
        notices.append({"code": "PRICE_MISSING",
                        "message": "no price is available; this holding is not valued"})
    if not held and not unpriced:
        return None, notices
    return {"as_of": book["as_of"].isoformat(), "advised_only": advised_only,
            "holdings": held, "unpriced": unpriced}, notices


@router.get("/companies/{isin}", response_model=CompanyView)
def company_view(
    investor_id: str,
    isin: str,
    as_of: date | None = None,
    advised_only: bool = False,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """RITUAL_REQUIRED (identity only) or READY with real stored records."""
    as_of = as_of or ist_today()
    with db_errors(), sessions.open(READER, investor_id) as conn:
        isin = ws.require_linked(conn, investor_id, isin)
        identity = ws.identity(conn, isin)
        if not ws.ritual_complete(conn, investor_id, isin):
            return {"state": "RITUAL_REQUIRED", "identity": identity}
        family = ws.isin_family(conn, isin)
        open_questions = ws.open_questions_for(conn, investor_id, family)
        selection, _ = rs.select_in_force(conn, investor_id, isin, datetime.now(UTC))
        notices: list[dict[str, str]] = []
        thesis = None
        if selection.state == "ACTIVE":
            thesis = rs.thesis_by_id(conn, investor_id, selection.thesis.id)
        elif selection.state == "CONFLICT":
            notices.append({
                "code": COMMITMENT_CONFLICT,
                "message": "verified aliases hold independent active commitments: "
                + ", ".join(v.id for v in selection.candidates),
            })
        notes = ws.list_notes(conn, investor_id, isin, None, False, None, 20)
    position, position_notices = _position(fin_conn, investor_id, family, as_of, advised_only)
    return {
        "state": "READY",
        "identity": identity,
        "open_questions": open_questions,
        "thesis": thesis,
        "trigger": thesis["latest_evaluation"] if thesis else None,
        "notes": notes,
        "position": position,
        "notices": notices + position_notices,
        "as_of": as_of,
    }


@router.post("/companies/{isin}/rituals", response_model=RitualOut, status_code=201,
             dependencies=WRITE)
def create_ritual(
    investor_id: str,
    isin: str,
    body: RitualIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    for i, question in enumerate(body.questions):
        tx.check_text(question.text, f"questions[{i}].text", tx.QUESTION_MAX_BYTES)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.create_ritual(
            conn, investor_id, _actor(user), isin,
            [q.model_dump() for q in body.questions], body.written_on, key,
            _digest(ws.OP_RITUAL, body)))


# =========================================================================================
# session 09 -- notes
# =========================================================================================


def _note_body(body: NoteCreateIn) -> dict[str, Any]:
    tx.check_text(body.title, "title", tx.TITLE_MAX_BYTES)
    tx.check_text(body.body, "body", tx.NOTE_BODY_MAX_BYTES, required=False)
    return {
        "kind": body.kind,
        "title": body.title,
        "body": body.body,
        "written_on": body.written_on,
        "primary_source_id": body.primary_source_id,
        "links": [link.model_dump() for link in body.links],
    }


@router.get("/companies/{isin}/notes", response_model=Page[Note])
def list_notes(
    investor_id: str,
    isin: str,
    kind: str | None = None,
    history: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.list_notes(conn, investor_id, isin, kind, history, cursor, limit)


@router.post("/companies/{isin}/notes", response_model=Note, status_code=201,
             dependencies=WRITE)
def create_note(
    investor_id: str,
    isin: str,
    body: NoteCreateIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    note = _note_body(body)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.create_note(
            conn, investor_id, _actor(user), isin, note, key,
            _digest(ws.OP_NOTE_CREATE, body)))


# Static path BEFORE /notes/{note_id}, or "search" would be parsed as a note id.
@router.get("/notes/search", response_model=Page[Note])
def search_notes(
    investor_id: str,
    q: str = Query(..., min_length=1, max_length=200),
    isin: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.search_notes(conn, investor_id, q, isin, kind, cursor, limit)


@router.get("/notes/{note_id}", response_model=Note)
def get_note(
    investor_id: str,
    note_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.note_by_id(conn, investor_id, note_id)


@router.post("/notes/{note_id}/revisions", response_model=Note, status_code=201,
             dependencies=WRITE)
def revise_note(
    investor_id: str,
    note_id: str,
    body: NoteRevisionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    note = _note_body(body)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.revise_note(
            conn, investor_id, _actor(user), note_id, note, body.expected_latest_id, key,
            _digest(ws.OP_NOTE_REVISE, body)))


@router.post("/notes/{note_id}/links", response_model=NoteLink, status_code=201,
             dependencies=WRITE)
def link_note(
    investor_id: str,
    note_id: str,
    body: NoteLinkIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.link_note(
            conn, investor_id, _actor(user), note_id, body.model_dump(), key,
            _digest(ws.OP_NOTE_LINK, body)))


# =========================================================================================
# session 10 -- questions, answers, Home
# =========================================================================================


@router.get("/questions", response_model=Page[Question])
def list_questions(
    investor_id: str,
    isin: str | None = None,
    status: str | None = None,
    blocking: bool | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.list_questions(conn, investor_id, isin, status, blocking, cursor, limit)


@router.post("/companies/{isin}/questions", response_model=Question, status_code=201,
             dependencies=WRITE)
def create_question(
    investor_id: str,
    isin: str,
    body: QuestionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    tx.check_text(body.text, "text", tx.QUESTION_MAX_BYTES)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.create_question(
            conn, investor_id, _actor(user), isin, body.text, body.blocking, body.written_on,
            key, _digest(ws.OP_QUESTION_CREATE, body)))


@router.get("/questions/{question_id}", response_model=Question)
def get_question(
    investor_id: str,
    question_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return ws.question_by_id(conn, investor_id, question_id)


@router.post("/questions/{question_id}/answers", response_model=Answer, status_code=201,
             dependencies=WRITE)
def answer_question(
    investor_id: str,
    question_id: str,
    body: AnswerIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    tx.check_text(body.answer, "answer", tx.ANSWER_MAX_BYTES)
    if body.expected_latest_answer_id is not None:
        ws._uuid(body.expected_latest_answer_id, "answer")
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, ws.answer_question(
            conn, investor_id, _actor(user), question_id, body.answer, body.answered_on,
            body.expected_latest_answer_id, key, _digest(ws.OP_ANSWER, body)))


@router.get("/home", response_model=HomeOut)
def home(
    investor_id: str,
    as_of: date | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """``as_of`` defaults to TODAY IN ASIA/KOLKATA, not UTC: between 00:00 and 05:30 IST a
    UTC default would resurface yesterday's anniversaries."""
    with db_errors(), sessions.open(READER, investor_id) as conn:
        out = ws.home(conn, investor_id, as_of or ist_today())
        # Real pending reviews, each with its continuation. Tables from 042 always exist
        # in a ready Workspace, so an empty list means none are pending -- not "unwired".
        reviews = tk.pending_reviews(conn, investor_id,
                                     f"/investors/{investor_id}/workspace")
        if len(reviews) > tk.HOME_REVIEWS:
            reviews = reviews[:tk.HOME_REVIEWS]
            out["notices"].append({
                "code": "REVIEWS_TRUNCATED",
                "message": f"showing the latest {tk.HOME_REVIEWS} proposals awaiting review; "
                           "the rest are listed under tasks?state=review"})
        out["reviews"] = reviews
        return out


# =========================================================================================
# session 11 -- thesis versions and events
# =========================================================================================


def _thesis_body(body: ThesisIn) -> dict[str, Any]:
    for name in ("argument", "anti_thesis", "pre_mortem", "intended_holding_period"):
        tx.check_text(getattr(body, name), name, tx.THESIS_FIELD_MAX_BYTES, required=False)
    thesis = body.model_dump()
    # Validate the rule grammar BEFORE anything digests the request: a JSON number in a rule
    # is a 422 INVALID_RULE, and the canonical digest refuses floats outright.
    rs._parse_thesis_rules(thesis)
    return thesis


@router.get("/companies/{isin}/theses", response_model=Page[Thesis])
def list_theses(
    investor_id: str,
    isin: str,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rs.list_theses(conn, investor_id, isin, cursor, limit)


@router.post("/companies/{isin}/theses", response_model=ThesisPublishOut, status_code=201,
             dependencies=WRITE)
def create_thesis(
    investor_id: str,
    isin: str,
    body: ThesisIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    if body.expected_version != 0:
        raise invalid("a new thesis branch expects version 0", {"expected_version": "NOT_ZERO"})
    thesis = _thesis_body(body)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.create_thesis(
            conn, investor_id, _actor(user), isin, thesis, key,
            _digest(rs.OP_THESIS_CREATE, body)))


@router.get("/theses/{thesis_id}", response_model=Thesis)
def get_thesis(
    investor_id: str,
    thesis_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return rs.thesis_by_id(conn, investor_id, thesis_id)


@router.post("/theses/{thesis_id}/versions", response_model=ThesisPublishOut, status_code=201,
             dependencies=WRITE)
def create_thesis_version(
    investor_id: str,
    thesis_id: str,
    body: ThesisIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    thesis = _thesis_body(body)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.create_thesis_version(
            conn, investor_id, _actor(user), thesis_id, thesis, key,
            _digest(rs.OP_THESIS_VERSION, body)))


@router.post("/theses/{thesis_id}/events", response_model=ThesisEvent, status_code=201,
             dependencies=WRITE)
def record_thesis_event(
    investor_id: str,
    thesis_id: str,
    body: ThesisEventIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    tx.check_text(body.reason, "reason", tx.REASON_MAX_BYTES)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.record_thesis_event(
            conn, investor_id, _actor(user), thesis_id, body.model_dump(), key,
            _digest(rs.OP_THESIS_EVENT, body)))


# =========================================================================================
# session 15 -- human override of a TRIGGERED exit
# =========================================================================================


@router.post("/theses/{thesis_id}/overrides", response_model=ThesisEvent, status_code=201,
             dependencies=WRITE)
def override_trigger(
    investor_id: str,
    thesis_id: str,
    body: OverrideIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    registry: Any = Depends(get_metric_registry),
    calendar: Any = Depends(get_calendar_provider),
) -> Any:
    """Human role only. The receipt stays TRIGGERED; nothing is cleared, retired or traded."""
    tx.check_text(body.reason, "reason", tx.REASON_MAX_BYTES)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, rs.override_trigger(
            conn, investor_id, _actor(user), thesis_id, body.evaluation_id, body.reason,
            body.occurred_on, registry, calendar, key, _digest(rs.OP_THESIS_OVERRIDE, body)))
