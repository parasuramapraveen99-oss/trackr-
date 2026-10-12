"""Butler routes: follows, memory, inbox, decision reviews, corrections and context (BT 01-06).

docs/BUTLER_ARCHITECTURE.md section 9. Every route is under ``/investors/{investor_id}/butler``
and requires, in order: a valid session (401), ownership of the investor (403/404, the
existing portfolio semantics), the ``workspace`` product capability (403), an enabled and
ready Workspace (503) and an applied ``051_butler.sql`` (503 BUTLER_UNAVAILABLE). WRITE routes
also need write readiness (BE-Q04/BE-Q05) and a UUID ``Idempotency-Key``: 201 first time, 200
replay, 409 for the same key with a different payload.

WORKSPACE ONLY. Plus has no client for these paths and must never gain one; the support
assistant stays a separate surface and is never a Butler fallback (BE-Q02).

NO MODEL IS CALLED FROM THIS FILE. Cards come from the deterministic worker and are rendered
from reviewed templates at read time; reviews record the person's own answers; ``/context``
builds a sealed packet for inspection only. BT 08's optional model selection is ``/selections``:
an explicit click seals a packet as an ordinary BE 20 task, the BROWSER calls the person's own
model endpoint through the BE 21-23 attempt routes, and ``GET /selections/{id}`` re-validates
the returned identifiers on every read and renders only BT 04's checked cards. There is no chat
proxy, no provider key and no model submission route here.

Reads run as READER; a person's writes as HUMAN attributed to the authenticated user. Nothing
here reads or writes the financial ledger.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from app.auth.dependencies import (
    get_current_user,
    require_owned_investor,
    require_workspace_access,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import butler as bd
from app.db import butler_teaching as bt
from app.db.connection import WorkspaceCapability
from app.db.migrate import butler_schema_ready
from app.research import butler_context as bc
from app.research import butler_policy as bp
from app.research import butler_selection as bsel
from app.research import butler_validation as bv
from app.research import canonical
from app.research import text as tx
from app.research.errors import BUTLER_UNAVAILABLE, WorkspaceError, invalid
from app.research.source_policy import SourcePolicy
from app.routers.workspace_deps import (
    WorkspaceSessions,
    db_errors,
    get_source_policy,
    get_workspace_sessions,
    idempotency_key,
    require_research_writes_ready,
    require_workspace_enabled,
)
from app.schemas.butler import (
    ButlerStatus,
    Card,
    Catalogue,
    ContextIn,
    ContextOut,
    Feedback,
    FeedbackIn,
    FollowIn,
    InboxEventIn,
    InboxEventOut,
    InboxPage,
    Memory,
    MemoryEventIn,
    Preference,
    PreferenceIn,
    RememberIn,
    Retraction,
    RetractionIn,
    Review,
    ReviewCompleteIn,
    ReviewResponseIn,
    ReviewStartIn,
    ReviewSummary,
    SelectionIn,
    SelectionOut,
    Subscription,
    SubscriptionEventIn,
    Teaching,
    TeachingIn,
)
from app.schemas.workspace import Page

logger = logging.getLogger(__name__)

READER = WorkspaceCapability.READER
HUMAN = WorkspaceCapability.HUMAN

_butler_ready = False


def require_butler_ready(conn: Any = Depends(get_conn)) -> None:
    """503 BUTLER_UNAVAILABLE until 051_butler.sql is applied -- never a 500 from a missing
    relation. Checked on the financial connection, which may read schema_migrations."""
    global _butler_ready
    if _butler_ready:
        return
    if not butler_schema_ready(conn):
        raise WorkspaceError(503, BUTLER_UNAVAILABLE,
                             "Butler storage is not installed on this server")
    _butler_ready = True


router = APIRouter(
    prefix="/investors/{investor_id}/butler",
    tags=["butler"],
    dependencies=[
        Depends(require_owned_investor),
        Depends(require_workspace_access),
        Depends(require_workspace_enabled),
        Depends(require_butler_ready),
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


def _now() -> datetime:
    return datetime.now(UTC)


def _worker(conn: Any, investor_id: str, now: datetime, settings: Settings) -> dict[str, Any]:
    """The deterministic worker's truthful status, plus what model selection can do NOW:
    only on an explicit click, from the browser -- or not at all when switched off."""
    status = bd.worker_status(conn, investor_id, now)
    status["model_execution"] = (
        "BROWSER_ON_EXPLICIT_REQUEST"
        if settings.butler_model_selection_enabled and settings.research_model_execution_enabled
        else "DISABLED")
    return status


# =========================================================================================
# catalogue and preferences
# =========================================================================================


@router.get("/catalogue", response_model=Catalogue)
def catalogue(investor_id: str) -> dict[str, Any]:
    """The reviewed, versioned question/lesson catalogue and presentation defaults."""
    return bp.catalogue_manifest()


@router.get("/preferences", response_model=Preference)
def get_preferences(
    investor_id: str, sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bd.preference_dto(bd.current_preference(conn, investor_id))


@router.post("/preferences", response_model=Preference, status_code=201, dependencies=WRITE)
def set_preferences(
    investor_id: str,
    body: PreferenceIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.set_preference(
            conn, investor_id, _actor(user), body.model_dump(), key,
            _digest(bd.OP_PREFERENCE, body)))


# =========================================================================================
# BT 02 -- subscriptions
# =========================================================================================


@router.get("/subscriptions", response_model=Page[Subscription])
def list_subscriptions(
    investor_id: str,
    include_ended: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bd.list_subscriptions(conn, investor_id, include_ended, cursor, limit)


@router.post("/subscriptions", response_model=Subscription, status_code=201, dependencies=WRITE)
def follow(
    investor_id: str,
    body: FollowIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    if body.subject_kind == "COMPANY" and (body.topic is not None or body.topic_aliases):
        raise invalid("a company follow takes an ISIN, not a topic", {"topic": "UNEXPECTED"})
    if body.subject_kind == "TOPIC":
        if body.isin is not None:
            raise invalid("a topic follow takes no ISIN", {"isin": "UNEXPECTED"})
        if body.checkpoint_days is not None:
            raise invalid("checkpoints apply to followed companies",
                          {"checkpoint_days": "SUBJECT"})
        tx.check_text(body.topic or "", "topic", tx.SHORT_FIELD_MAX_BYTES)
        for i, alias in enumerate(body.topic_aliases):
            tx.check_text(alias, f"topic_aliases[{i}]", tx.SHORT_FIELD_MAX_BYTES)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.follow(conn, investor_id, _actor(user), body.model_dump(),
                                          key, _digest(bd.OP_FOLLOW, body)))


@router.get("/subscriptions/{subscription_id}", response_model=Subscription)
def get_subscription(
    investor_id: str, subscription_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bd.subscription_by_id(conn, investor_id, subscription_id)


@router.post("/subscriptions/{subscription_id}/events", response_model=Subscription,
             status_code=201, dependencies=WRITE)
def subscription_event(
    investor_id: str,
    subscription_id: str,
    body: SubscriptionEventIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    if body.action != "SET_CHECKPOINT" and body.checkpoint_days is not None:
        raise invalid("checkpoint_days applies only to SET_CHECKPOINT",
                      {"checkpoint_days": "UNEXPECTED"})
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.subscription_event(
            conn, investor_id, _actor(user), subscription_id, body.model_dump(), key,
            _digest(bd.OP_SUBSCRIPTION_EVENT, body)))


# =========================================================================================
# BT 02 -- memory
# =========================================================================================


@router.get("/memories", response_model=Page[Memory])
def list_memories(
    investor_id: str,
    isin: str | None = None,
    include_ended: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bd.list_memories(conn, investor_id, isin, include_ended, cursor, limit)


@router.post("/memories", response_model=Memory, status_code=201, dependencies=WRITE)
def remember(
    investor_id: str,
    body: RememberIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.remember(conn, investor_id, _actor(user), body.model_dump(),
                                            key, _digest(bd.OP_REMEMBER, body)))


@router.post("/memories/{memory_id}/events", response_model=Memory, status_code=201,
             dependencies=WRITE)
def memory_event(
    investor_id: str,
    memory_id: str,
    body: MemoryEventIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    if (body.action == "SUPERSEDE") != (body.superseded_by is not None):
        raise invalid("superseded_by is required for SUPERSEDE and only for it",
                      {"superseded_by": "PAIRING"})
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.memory_event(
            conn, investor_id, _actor(user), memory_id, body.model_dump(), key,
            _digest(bd.OP_MEMORY_EVENT, body)))


# =========================================================================================
# BT 04/05 -- inbox
# =========================================================================================


def _visible_now(row: dict[str, Any], status: dict[str, Any], now: datetime) -> bool:
    return (row["deliver_after"] <= now and not status["dismissed"] and not status["resolved"]
            and status["snoozed_until"] is None and row["superseded_by_item_id"] is None)


@router.get("/inbox", response_model=InboxPage)
def inbox(
    investor_id: str,
    view: str = Query("current", pattern="^(current|all)$"),
    cursor: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Currently visible validated cards (``current``) or the full history (``all``).

    Every card is re-rendered from current records, rights and tombstones on this read; a card
    whose evidence is gone comes back WITHDRAWN with a fixed reason, never with stale text.
    """
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "butler.inbox", {"view": view})
    after = canonical.decode_cursor(cursor, scope)
    now = _now()
    with db_errors(), sessions.open(READER, investor_id) as conn:
        chosen: list[dict[str, Any]] = []
        after_seq = int(after[0]) if after else None
        exhausted = False
        while len(chosen) <= size and not exhausted:
            batch = bd.inbox_rows(conn, investor_id, after_seq=after_seq, limit=200)
            exhausted = len(batch) < 200
            for row in batch:
                row["status"] = bd.item_status(row.pop("status_events"), now)
                if view == "all" or _visible_now(row, row["status"], now):
                    chosen.append(row)
            if batch:
                after_seq = batch[-1]["seq"]
        more = len(chosen) > size
        chosen = chosen[:size]
        pref = bd.current_preference(conn, investor_id)
        tz = bp.zone(pref["timezone"]) if pref else UTC
        inputs = bd.render_inputs(conn, investor_id, chosen)
        cards = [bv.render_item(row, inputs, policy, tz, now, row["status"]) for row in chosen]
        new_count = 0
        if view == "current":
            new_count = sum(1 for c in cards if not c["status"]["seen"])
        return {
            "items": cards,
            "next_cursor": (canonical.encode_cursor(scope, [chosen[-1]["seq"]])
                            if more and chosen else None),
            "new_count": new_count,
            "worker": _worker(conn, investor_id, now, settings),
            "preference": bd.preference_dto(pref),
        }


@router.get("/inbox/{item_id}", response_model=Card)
def inbox_item(
    investor_id: str,
    item_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    policy: SourcePolicy = Depends(get_source_policy),
) -> dict[str, Any]:
    now = _now()
    with db_errors(), sessions.open(READER, investor_id) as conn:
        rows = bd.inbox_rows(conn, investor_id, [bd._uuid(item_id, "inbox item")])
        if not rows:
            raise WorkspaceError(404, "NOT_FOUND", "no such inbox item")
        row = rows[0]
        row["status"] = bd.item_status(row.pop("status_events"), now)
        pref = bd.current_preference(conn, investor_id)
        tz = bp.zone(pref["timezone"]) if pref else UTC
        return bv.render_item(row, bd.render_inputs(conn, investor_id, [row]), policy, tz, now,
                              row["status"])


@router.post("/inbox/{item_id}/events", response_model=InboxEventOut, status_code=201,
             dependencies=WRITE)
def inbox_event(
    investor_id: str,
    item_id: str,
    body: InboxEventIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        out = bd.inbox_event(conn, investor_id, _actor(user), item_id, body.model_dump(), key,
                             _digest(bd.OP_INBOX_EVENT, body), _now())
    response.status_code = 200 if out["replayed"] else 201
    return out


# =========================================================================================
# BT 06 -- decision reviews
# =========================================================================================


@router.get("/reviews", response_model=list[ReviewSummary])
def list_reviews(
    investor_id: str,
    isin: str | None = None,
    limit: int | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> list[dict[str, Any]]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bd.list_reviews(conn, investor_id, isin, limit)


@router.post("/reviews", response_model=Review, status_code=201, dependencies=WRITE)
def start_review(
    investor_id: str,
    body: ReviewStartIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """An explicitly user-started review with the thesis version and checklist pinned."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        review_id, status = bd.start_review(conn, investor_id, _actor(user), body.model_dump(),
                                            key, _digest(bd.OP_REVIEW_START, body))
        response.status_code = status
        return bd.review_view(conn, investor_id, review_id, _now())


@router.get("/reviews/{review_id}", response_model=Review)
def get_review(
    investor_id: str, review_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bd.review_view(conn, investor_id, review_id, _now())


@router.post("/reviews/{review_id}/responses", response_model=Review, status_code=201,
             dependencies=WRITE)
def respond(
    investor_id: str,
    review_id: str,
    body: ReviewResponseIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    tx.check_text(body.answer, "answer", tx.ANSWER_MAX_BYTES, required=False)
    if body.not_applicable_reason is not None:
        tx.check_text(body.not_applicable_reason, "not_applicable_reason", tx.REASON_MAX_BYTES)
    if not body.answer.strip() and body.not_applicable_reason is None:
        raise invalid("answer the question or give a not-applicable reason",
                      {"answer": "REQUIRED"})
    for i, ev in enumerate(body.evidence):
        if (ev.source_id is None) == (ev.note_id is None):
            raise invalid("each evidence item names exactly one source or note",
                          {f"evidence[{i}]": "ONE_OF"})
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.respond(
            conn, investor_id, _actor(user), review_id, body.model_dump(), key,
            _digest(bd.OP_REVIEW_RESPONSE, body)))


@router.post("/reviews/{review_id}/complete", response_model=Review, status_code=201,
             dependencies=WRITE)
def complete_review(
    investor_id: str,
    review_id: str,
    body: ReviewCompleteIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """The person's explicit continue/pause/revise/more-information choice. "Review
    documented", never an approval; no trade, thesis change or gate waiver follows."""
    tx.check_text(body.reason, "reason", tx.REASON_MAX_BYTES, required=False)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.complete_review(
            conn, investor_id, _actor(user), review_id, body.model_dump(), key,
            _digest(bd.OP_REVIEW_COMPLETE, body)))


# =========================================================================================
# BT 04 -- source corrections
# =========================================================================================


@router.post("/source-retractions", response_model=Retraction, status_code=201,
             dependencies=WRITE)
def retract_source(
    investor_id: str,
    body: RetractionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Mark a saved source retracted/corrected/duplicate. The source row is not changed;
    cards and review evidence citing it are withdrawn on read and a correction card follows."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bd.retract_source(
            conn, investor_id, _actor(user), body.model_dump(), key,
            _digest(bd.OP_RETRACTION, body)))


# =========================================================================================
# BT 03 -- context (READ-class)
# =========================================================================================


@router.post("/context", response_model=ContextOut)
def prepare_context(
    investor_id: str,
    body: ContextIn,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """Build the authorized, bounded evidence packet for one company. READ-class: it persists
    nothing -- no task, no manifest, no memory. BE 20 will seal it into a task; until then the
    digest binds it to exactly the records it was built from."""
    now = _now()
    as_of = body.as_of or now
    if as_of.tzinfo is None:
        raise invalid("as_of needs a timezone", {"as_of": "NAIVE"})
    if as_of > now:
        raise invalid("as_of cannot be in the future", {"as_of": "FUTURE"})
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        sealed = bc.build_packet(conn, investor_id, body.isin.strip().upper(), as_of,
                                 body.intent)
    return {
        "packet": sealed.packet,
        "digest": sealed.digest,
        "complete": sealed.complete,
        "expires_at": sealed.expires_at,
        "versions": sealed.versions,
        "task_id": None,
        "model_execution": "NOT_CONNECTED",
        "size_chars": bc.packet_size(sealed.packet),
    }


# =========================================================================================
# BT 08 -- optional browser model selection
# =========================================================================================


def _execution_ready(conn: Any = Depends(get_conn)) -> None:
    from app.routers.tasks import require_execution_ready

    require_execution_ready(conn)


@router.post("/selections", response_model=SelectionOut, status_code=201,
             dependencies=[*WRITE, Depends(_execution_ready)])
def create_selection(
    investor_id: str,
    body: SelectionIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    fin_conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> Any:
    """One explicit Review/Ask click: seal the current packet as a task. Calls no model; the
    browser then starts an attempt and calls the person's endpoint itself."""
    if not (settings.butler_model_selection_enabled and settings.research_model_execution_enabled):
        raise WorkspaceError(503, "EXECUTION_DISABLED",
                             "Butler model selection is switched off; checked cards remain")
    digest = _digest(bsel.OP_SELECTION, body)
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bsel.seal(conn, fin_conn, investor_id, _actor(user),
                                          body.model_dump(), key, digest))


@router.get("/selections", response_model=list[SelectionOut],
            dependencies=[Depends(_execution_ready)])
def list_selections(
    investor_id: str,
    isin: str | None = None,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> list[dict[str, Any]]:
    """Recent selections, each re-validated on this read. No model call on load."""
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        return bsel.list_selections(conn, investor_id, isin.strip().upper() if isin else None)


@router.get("/selections/{task_id}", response_model=SelectionOut,
            dependencies=[Depends(_execution_ready)])
def get_selection(
    investor_id: str,
    task_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    """BT 04's cards for the stored identifiers, rebuilt and revalidated against current
    records and access. STALE / WITHDRAWN / UNAVAILABLE carry no cards -- never raw output."""
    with db_errors(), sessions.open(READER, investor_id, snapshot=True) as conn:
        return bsel.view(conn, investor_id, task_id)


# =========================================================================================
# BT 09 -- teaching preferences and item feedback
# =========================================================================================


@router.get("/teaching", response_model=Teaching, dependencies=[Depends(_execution_ready)])
def get_teaching(
    investor_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> dict[str, Any]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bt.current_teaching(conn, investor_id)


@router.post("/teaching", response_model=Teaching, status_code=201,
             dependencies=[*WRITE, Depends(_execution_ready)])
def set_teaching(
    investor_id: str,
    body: TeachingIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Explicit presentation choices, versioned. Never relaxes a check or adds evidence."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bt.set_teaching(conn, investor_id, _actor(user),
                                                body.model_dump(), key,
                                                _digest(bt.OP_TEACHING, body)))


@router.get("/feedback", response_model=list[Feedback], dependencies=[Depends(_execution_ready)])
def list_feedback(
    investor_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> list[dict[str, Any]]:
    with db_errors(), sessions.open(READER, investor_id) as conn:
        return bt.latest_feedback(conn, investor_id)


@router.post("/feedback", response_model=Feedback, status_code=201,
             dependencies=[*WRITE, Depends(_execution_ready)])
def record_feedback(
    investor_id: str,
    body: FeedbackIn,
    response: Response,
    key: str = Depends(idempotency_key),
    user: dict[str, Any] = Depends(get_current_user),
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
) -> Any:
    """Useful / not useful on one reviewed catalogue item. No free text by design."""
    with db_errors(), sessions.open(HUMAN, investor_id, _actor(user)) as conn:
        return _reply(response, bt.record_feedback(conn, investor_id, _actor(user),
                                                   body.model_dump(), key,
                                                   _digest(bt.OP_FEEDBACK, body)))


# =========================================================================================
# BT 10 -- operational truth
# =========================================================================================


@router.get("/status", response_model=ButlerStatus)
def status(
    investor_id: str,
    sessions: WorkspaceSessions = Depends(get_workspace_sessions),
    settings: Settings = Depends(get_settings),
    fin_conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    """What is actually running, what is switched off, how far behind the worker is and what
    was never checked. An outage reads as an outage: no "all clear" is derived from absence."""
    from app.db.migrate import execution_schema_ready

    now = _now()
    with db_errors(), sessions.open(READER, investor_id) as conn:
        worker = _worker(conn, investor_id, now, settings)
        ticks = bd._rows(conn, """
            SELECT outcome, count(*) AS n, COALESCE(sum(failed), 0) AS failed
              FROM butler_tick WHERE investor_id = %s AND started_at > %s
             GROUP BY outcome
            """, (investor_id, now - timedelta(days=7)))
        selections = {}
        if execution_schema_ready(fin_conn):
            selections = bsel.selection_stats(conn, investor_id, now - timedelta(days=30))
    return {
        "worker": worker,
        "switches": {
            "model_execution_enabled": settings.research_model_execution_enabled,
            "butler_model_selection_enabled": settings.butler_model_selection_enabled,
            "butler_source_events_enabled": settings.butler_source_events_enabled,
        },
        "selections": {"window_days": 30, **selections},
        "coverage": {
            "saved_workspace_records": "CHECKED_BY_WORKER",
            "external_sources": "NOT_CHECKED",
            "external_feed": "NONE_CONFIGURED",
            "ticks_last_7_days": {r["outcome"]: {"count": int(r["n"]),
                                                 "failed_events": int(r["failed"])}
                                  for r in ticks},
            "all_clear": False,
        },
        "limits": {
            "one_in_flight_per_investor": True,
            "automatic_retry": False,
            "client_timeout_seconds": bsel.CLIENT_TIMEOUT_SECONDS,
            "max_input_tokens": bsel.MAX_INPUT_TOKENS,
            "max_output_tokens": bsel.MAX_OUTPUT_TOKENS,
            "catch_up": "MANUAL_ONLY",
            "delivery": "IN_APP_ONLY",
        },
    }
