"""/market-assistant -- Ask about markets, Trackr+'s public-only assistant.

Replaces POST /investors/{id}/ai/chat, which sent an investor's derived positions and
the typed conversation to OpenRouter. That route is removed, not hidden: under
docs/AI_ASSISTANT_PRIVACY_PLAN.md section 3 a portfolio chat must not survive as an
undocumented fallback, including for direct API calls.

What this router may know about the caller: that they are signed in. The user record is
used for authentication only and is never passed to ``app.market_assistant``. There is no
investor in any path, query or body. ``history`` carries only the user's own earlier
questions from one drawer conversation, which the client clears on a portfolio switch,
sign-out or reopen; the server keeps none of it between requests.

Definitions come from the local concept library and make no external call. Phase 2 adds
one kind of external call: "the latest from RBI / SEBI / NSE / BSE" runs a fixed catalogue
search restricted to that body's own website, inspected before sending
(``app.market_assistant.exa``), bounded by a daily ceiling and cached. The typed question
is never part of that request.

Current owner direction (11 Oct 2026): every question passing local privacy checks goes,
with ``allow_external``, to the LLM for finance relevance/planning before Exa. Queries
search trusted official/news sources and broader discovery; unknown issuer hosts need an
explicit official website reference. Answers are short, cited and checked by the server.

``PUBLIC_RESEARCH_ENABLED`` switches the whole feature off;
``MARKET_ASSISTANT_EXA_API_KEY`` unset (or a daily limit of 0) switches searching off alone.

Two rate limits: 60 a minute per network address (slowapi, before authentication) and 20
a minute per signed-in user (after it), so one user cannot use up a shared address's
allowance and many sessions cannot multiply one user's.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.dependencies import get_current_user
from app.config import Settings, get_settings
from app.db.market_assistant_usage import PostgresUsageStore
from app.market_assistant.admission import (
    Outcome,
    admit_history,
    admit_text,
    admit_topic,
    as_research,
)
from app.market_assistant.answer import (
    EVIDENCE_TTL_SECONDS,
    Reply,
    SearchDeps,
    all_topics,
    lead,
    render,
)
from app.market_assistant.embedding import load_embedder
from app.market_assistant.exa import SearchClient
from app.market_assistant.limits import DailyBudget, PerKeyRateLimit, TTLCache
from app.market_assistant.model import SelectorClient
from app.market_assistant.retrieval import RetrievalMode
from app.rate_limit import limiter
from app.schemas.market_assistant import (
    AnswerOut,
    AskIn,
    AskOut,
    EvidenceOut,
    SummarySentenceOut,
    TopicOut,
)

logger = logging.getLogger("routers.market_assistant")

router = APIRouter(prefix="/market-assistant", tags=["market-assistant"])

#: docs/AI_ASSISTANT_PRIVACY_PLAN.md section 10: 8 KiB request body.
MAX_BODY_BYTES = 8 * 1024

USER_LIMIT_PER_MINUTE = 20
_user_limit = PerKeyRateLimit(USER_LIMIT_PER_MINUTE, 60.0)

#: The daily ceilings count in Postgres (migration 078) so they hold across restarts and
#: processes; the store carries a day and a count, nothing about the caller. Rebuilt when
#: the configured limit changes (tests, or a settings reload). The cache is per process.
_budget: DailyBudget | None = None
_model_budget: DailyBudget | None = None
_cache: TTLCache = TTLCache(EVIDENCE_TTL_SECONDS)


def search_deps(settings: Settings = Depends(get_settings)) -> SearchDeps:
    global _budget, _model_budget
    limit = settings.market_assistant_search_daily_limit
    if _budget is None or _budget.limit != limit:
        _budget = DailyBudget(limit, store=PostgresUsageStore(settings.database_url, "search"))
    model_limit = settings.market_assistant_model_daily_limit
    if _model_budget is None or _model_budget.limit != model_limit:
        _model_budget = DailyBudget(
            model_limit, store=PostgresUsageStore(settings.database_url, "model")
        )
    key = settings.market_assistant_exa_api_key
    client = SearchClient(key) if key and limit > 0 else None
    model_key = settings.market_assistant_openrouter_api_key
    model = settings.market_assistant_model
    selector = (
        SelectorClient(model_key, model) if model_key and model and model_limit > 0 else None
    )
    retrieval = RetrievalMode(settings.market_assistant_retrieval)
    embedder = (
        load_embedder(
            settings.market_assistant_embedding_model,
            settings.market_assistant_embedding_cache_dir or "",
        )
        if retrieval is RetrievalMode.HYBRID
        else None
    )
    return SearchDeps(
        client=client,
        budget=_budget,
        cache=_cache,
        selector=selector,
        model_budget=_model_budget,
        retrieval=retrieval,
        embedder=embedder,
    )


def require_enabled(settings: Settings = Depends(get_settings)) -> None:
    if not settings.public_research_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "MARKET_ASSISTANT_DISABLED", "message": "Ask about markets is turned off."},
        )


def require_small_body(request: Request) -> None:
    length = request.headers.get("content-length")
    if length is None:
        raise HTTPException(status.HTTP_411_LENGTH_REQUIRED, "content-length is required")
    try:
        size = int(length)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content-length is not a number") from None
    if size > MAX_BODY_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"request body is over {MAX_BODY_BYTES} bytes",
        )


def _out(reply: Reply) -> AskOut:
    return AskOut(
        outcome=reply.outcome.value,
        message=reply.message,
        text=reply.text,
        reason_code=reply.reason_code,
        answer=AnswerOut(**vars(reply.answer)) if reply.answer else None,
        suggestions=[TopicOut(topic_id=t.topic_id, label=t.label) for t in reply.suggestions],
        evidence=[
            EvidenceOut(
                publisher=e.publisher,
                domain=e.domain,
                title=e.title,
                url=e.url,
                published=e.published,
                excerpt=e.text,
                lead=lead(e),
                template=e.template,
                doc_id=e.doc_id,
                chunk_id=e.chunk_id,
                start=e.start,
                end=e.end,
                source_kind=e.source_kind,
                verification_url=e.verification_url,
            )
            for e in reply.evidence
        ],
        note=reply.note,
        checked_at=reply.checked_at,
        selection=reply.selection,
        retrieval=reply.retrieval,
        summary=[SummarySentenceOut(text=s.text, cites=list(s.cites)) for s in reply.summary],
    )


@router.get(
    "/topics",
    response_model=list[TopicOut],
    dependencies=[Depends(require_enabled)],
)
def topics(_user: dict[str, Any] = Depends(get_current_user)) -> list[TopicOut]:
    return [TopicOut(topic_id=t.topic_id, label=t.label) for t in all_topics()]


@router.post(
    "/ask",
    response_model=AskOut,
    dependencies=[Depends(require_enabled), Depends(require_small_body)],
)
@limiter.limit("60/minute")
def ask(
    request: Request,
    body: AskIn,
    user: dict[str, Any] = Depends(get_current_user),
    deps: SearchDeps = Depends(search_deps),
) -> AskOut:
    if not _user_limit.allow(str(user["id"])):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"more than {USER_LIMIT_PER_MINUTE} questions a minute",
        )
    if body.topic_id is not None:
        try:
            admission = admit_topic(body.topic_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown topic") from None
        source = "chip"
    else:
        # The question is never logged or stored; only the outcome and reason code below
        # are. It is forwarded only on the open path, only after the local privacy checks,
        # and only with ``allow_external`` (owner amendment 11 Oct 2026).
        admission = admit_text(body.question or "")
        source = "typed"

    if body.allow_external:
        admission = as_research(admission)
    if admission.outcome is Outcome.OPEN and body.history:
        # Follow-up context: earlier questions of this conversation, each re-admitted
        # here; one that would be refused as a question is dropped, never forwarded.
        admission = replace(admission, earlier=admit_history(body.history))
    reply = render(admission, deps, allow_external=body.allow_external)
    question_type = admission.question.question_type.value if admission.question else None
    # Reading the shared counter is a database round trip; only a LATEST question can
    # have spent from it, so a definition does not pay for the log line.
    searches_today = deps.budget.used if body.allow_external and question_type == "latest" else None
    logger.info(
        "request_id=%s market_assistant source=%s outcome=%s reason=%s topic=%s "
        "type=%s evidence=%d searches_today=%s",
        getattr(request.state, "request_id", None),
        source,
        reply.outcome.value,
        reply.reason_code,
        reply.answer.topic_id if reply.answer else None,
        question_type,
        len(reply.evidence),
        searches_today,
    )
    return _out(reply)
