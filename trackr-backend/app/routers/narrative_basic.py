"""The Analytics Basic view's one-liners: read, generate, and the scheduled run.

docs/ANALYTICS_BASIC_NARRATIVE.md is the specification. Built on the same payload the
Analytics page renders (``cached_analytics``, advisor_v2), so a line can only cite a
figure the page itself shows.

READ NEVER CALLS A MODEL. ``GET`` serves stored lines, re-verified against the live pack
(V1-V13) and re-rendered from today's figures. Generation happens on the scheduled run,
after prices settle, or when the investor explicitly asks (``POST .../generate``, the
page's "Write summary" / "Retry").

STATES, NEVER A PLACEHOLDER SENTENCE: ``disabled`` (off on this deployment),
``consent_required`` (no live ANALYTICS_INSIGHTS consent), ``not_generated`` (nothing
written for these exact figures yet), ``ready``. A failed generation is an HTTP error
naming why, not a code-written fallback line.

WHAT LEAVES. With consent, the whole portfolio: every computed fact and the dated
ledger. Never PAN (or a demat/UCC/GSTIN), never a secret, never the portfolio's name.
``_assert_nothing_identifying`` scans the serialised request immediately before the
call and BLOCKS it on a hit -- it never strips the match and sends the rest.
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.analytics.basic_slots import RULE_VERSION
from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import consent
from app.db import narrative as store
from app.db.repository import list_investor_ids, load_transactions
from app.db.universe import DEFAULT_UNIVERSE
from app.ingest.redaction import BARE_PAN, DEMAT, DEMAT_LABELLED, EMAIL, GSTIN, PHONE, UCC_LABELLED
from app.narrative import basic
from app.narrative.digest import definition_pack_sha, factpack_sha256, quantise
from app.narrative.factpack import FactPack, build_pack
from app.prices.index_sources import NIFTY_TRI_INDEX_NAME

logger = logging.getLogger("routers.narrative_basic")

router = APIRouter(tags=["narrative"])
ops_router = APIRouter(prefix="/ops/narrative/basic", tags=["narrative"])

TABS = tuple(basic.TAB_SURFACE)

_IDENTIFIER_PATTERNS = {
    "PAN": BARE_PAN,
    "GSTIN": GSTIN,
    "demat account": DEMAT,
    "demat account (labelled)": DEMAT_LABELLED,
    "broker client code": UCC_LABELLED,
    "email": EMAIL,
    "phone": PHONE,
}


class IdentifierInRequest(Exception):
    """The outgoing request contained something that must never leave. Nothing was sent."""


def _ref(investor_id: str) -> str:
    return hashlib.sha256(investor_id.encode()).hexdigest()[:16]


def _portfolio_identity(connection: Any, investor_id: str) -> list[str]:
    with connection.cursor() as cur:
        cur.execute("SELECT display_name, code FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "investor not found")
    return [value for value in row if value]


def _strings(node: Any, path: str = "$"):
    """Every string key and string value in the request, with where it sits."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", str(key)
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _is_formatted_number(text: str) -> bool:
    """A decimal string with a point or sign ("-436129.76", "0.6123456789") is a figure,
    never a phone number or an account id, which have neither."""
    if not any(c in text for c in ".-"):
        return False
    try:
        Decimal(text)
    except InvalidOperation:
        return False
    return True


def _assert_nothing_identifying(request: dict[str, Any], names: list[str]) -> None:
    lowered = [(n, re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE)) for n in names]
    for where, text in _strings(request):
        for label, pattern in lowered:
            if pattern.search(text):
                raise IdentifierInRequest(f"the portfolio's name ({label!r}) at {where}")
        if _is_formatted_number(text):
            continue
        for label, pattern in _IDENTIFIER_PATTERNS.items():
            if pattern.search(text):
                raise IdentifierInRequest(f"a {label}-shaped value at {where}")


def _ledger(
    connection: Any, investor_id: str, on: date, advised_only: bool, symbols: dict[str, str]
) -> dict[str, Any]:
    rows = []
    for t in load_transactions(connection, investor_id, advised_only=advised_only, date_to=on):
        rows.append(
            {
                "date": t.txn_date.isoformat(),
                "isin": t.isin,
                "symbol": symbols.get(t.isin),
                "type": getattr(t.txn_type, "value", str(t.txn_type)),
                "quantity": str(t.quantity),
                "price": str(t.price),
                "charges": str(t.charges),
                "dividend_amount": str(t.dividend_amount),
            }
        )
    return basic.ledger_for_model(rows)


def _model_context(revamp: dict[str, Any]) -> dict[str, Any]:
    """The revamp block without its bulky per-pair matrix: summary figures only."""
    out = {k: v for k, v in revamp.items() if k != "diversification"}
    div = revamp.get("diversification") or {}
    corr = div.get("correlation") or {}
    out["diversification"] = {
        "correlation": {
            k: corr.get(k) for k in ("symbols", "weighted_average", "covered_weight_pct", "method")
        },
        "sector": div.get("sector"),
        "amfi_list_period": div.get("amfi_list_period"),
    }
    return out


def _live(
    connection: Any, investor_id: str, on: date, advised_only: bool
) -> tuple[dict[str, Any], FactPack, str]:
    from app.routers.analytics_advisor import cached_analytics

    payload = cached_analytics(
        connection, investor_id, on, NIFTY_TRI_INDEX_NAME, advised_only, DEFAULT_UNIVERSE
    )
    if not payload.get("basic"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The analytics for this date have no Basic findings block; reload the page.",
        )
    pack = build_pack(payload)
    return payload, pack, factpack_sha256(quantise(pack))


def _key(settings: Settings, sha: str) -> dict[str, Any]:
    return dict(
        advised_only=False,
        benchmark=NIFTY_TRI_INDEX_NAME,
        universe=DEFAULT_UNIVERSE,
        factpack_sha256=sha,
        rule_version=RULE_VERSION,
        template_version=basic.BASIC_TEMPLATE_VERSION,
        definition_pack_sha=definition_pack_sha(),
        model=settings.openrouter_model,
    )


def _resolve_on(as_of: date | None) -> date:
    from app.routers.analytics_advisor import IST

    return as_of or datetime.now(IST).date()


def _check_tab(tab: str) -> None:
    if tab not in TABS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown tab {tab!r}; known: {list(TABS)}"
        )


@router.get(
    "/investors/{investor_id}/narrative/basic", dependencies=[Depends(require_owned_investor)]
)
def read_basic(
    investor_id: str,
    tab: str = Query(...),
    as_of: date | None = None,
    settings: Settings = Depends(get_settings),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    _check_tab(tab)
    on = _resolve_on(as_of)
    base = {"as_of": on, "tab": tab, "template_version": basic.BASIC_TEMPLATE_VERSION}
    if not settings.insight_generation_enabled:
        return {**base, "state": "disabled"}
    if not consent.has_live_consent(connection, investor_id, consent.PURPOSE_ANALYTICS_INSIGHTS):
        return {**base, "state": "consent_required"}

    payload, pack, sha = _live(connection, investor_id, on, False)
    stored = store.get_artifact(connection, investor_id, f"basic.{tab}", on, **_key(settings, sha))
    if stored is None:
        return {**base, "state": "not_generated"}
    request = basic.build_request(pack, payload["basic"], tab, {"basis": "not_sent"})
    kept, rejected = basic.verify_lines(basic.lines_from_json(stored), request, pack)
    return {
        **base,
        "state": "ready",
        "lines": basic.render_lines(kept, pack),
        "rejected": [r.rule for r in rejected],
    }


def _checked_batch(client: Any, request: dict[str, Any], pack: FactPack) -> tuple:
    """One bounded model answer, preserving verified lines through its one retry."""
    from app.narrative.client import NarrativeUnavailable, generate_basic_lines

    try:
        lines, dropped = generate_basic_lines(client, request)
    except NarrativeUnavailable:
        if request["tab"] != "insights":
            raise
        return (
            [],
            [
                basic.LineRejection("SERVICE", c["slot"], "The provider returned no complete reply")
                for c in request["candidates"]
            ],
            [],
        )
    kept, rejected = basic.verify_lines(lines, request, pack)
    if request["tab"] == "insights":
        have = {line.line.slot for line in kept}
        rejected.extend(
            basic.LineRejection("V8", item["slot"], "Every supplied finding needs an explanation")
            for item in request["candidates"]
            if item["slot"] not in have
        )
    if rejected or dropped:
        feedback = [f"{r.slot}: {r.rule} {r.reason}" for r in rejected] + dropped
        try:
            retry_lines, retry_dropped = generate_basic_lines(client, request, feedback)
        except NarrativeUnavailable:
            # Already verified lines remain useful when a correction request fails.
            return kept, rejected, dropped
        retry_kept, retry_rejected = basic.verify_lines(retry_lines, request, pack)
        # Retry the failed lines without throwing away already verified prose. A real
        # model retry corrected two lines while introducing jargon into two that had
        # passed; replacing the whole answer lost valid findings for no reason.
        by_slot = {item.line.slot: item.line for item in kept}
        for item in retry_kept:
            by_slot.setdefault(item.line.slot, item.line)
        combined, overflow = basic.verify_lines(list(by_slot.values()), request, pack)
        unresolved = {r.slot: r for r in [*rejected, *retry_rejected] if r.slot not in by_slot}
        kept = combined
        rejected = [*unresolved.values(), *overflow]
        dropped = retry_dropped
    return kept, rejected, dropped


def _generate(
    connection: Any, settings: Settings, investor_id: str, on: date, tab: str
) -> dict[str, Any]:
    """Build, scan, generate bounded groups, verify, store, render."""
    from app.ai.llm import LlmClient

    payload, pack, sha = _live(connection, investor_id, on, False)
    symbols = {p["isin"]: p.get("symbol") for p in payload.get("positions") or []}
    request = basic.build_request(
        pack,
        payload["basic"],
        tab,
        _ledger(connection, investor_id, on, False, symbols),
        revamp=_model_context(payload.get("revamp") or {}),
    )
    _assert_nothing_identifying(request, _portfolio_identity(connection, investor_id))
    client = LlmClient(settings.openrouter_api_key or "", settings.openrouter_model)
    if tab == "insights":
        candidates = request["candidates"]
        batches = []
        for start in range(0, len(candidates), basic.INSIGHT_BATCH_SIZE):
            group = candidates[start : start + basic.INSIGHT_BATCH_SIZE]
            batches.append(
                {
                    **request,
                    "candidates": group,
                    "max_lines": len(group),
                    "task": (
                        "Explain every supplied candidate exactly once. "
                        "Use no OBS or INSUFFICIENT lines."
                    ),
                }
            )
        # Only independent provider calls run concurrently. All database reads and
        # writes remain on this request's thread; results retain candidate order.
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda batch: _checked_batch(client, batch, pack), batches))
        kept = [line for accepted, _, _ in results for line in accepted]
        rejected = [r for _, failures, _ in results for r in failures]
        dropped = [d for _, _, failures in results for d in failures]
    else:
        kept, rejected, dropped = _checked_batch(client, request, pack)

    kept_lines = [c.line for c in kept]
    store.upsert_artifact(
        connection,
        investor_id,
        f"basic.{tab}",
        on,
        **_key(settings, sha),
        claims=basic.lines_to_json(kept_lines),
        rejections=[
            {"rule": r.rule, "index": -1, "reason": f"{r.slot}: {r.reason}"} for r in rejected
        ]
        + [{"rule": "PARSE", "index": -1, "reason": d} for d in dropped],
    )
    connection.commit()
    logger.info(
        "basic_narrative_generated",
        extra={
            "investor_ref": _ref(investor_id),
            "tab": tab,
            "as_of": on.isoformat(),
            "kept": len(kept),
            "rejected": [r.rule for r in rejected],
            "dropped": len(dropped),
        },
    )
    return {
        "as_of": on,
        "tab": tab,
        "template_version": basic.BASIC_TEMPLATE_VERSION,
        "state": "ready",
        "lines": basic.render_lines(kept, pack),
        "rejected": [r.rule for r in rejected],
        "generated_at": datetime.now(UTC),
    }


def _guard(settings: Settings) -> None:
    if not settings.insight_generation_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "INSIGHT_GENERATION_ENABLED is false on this deployment",
        )


@router.post(
    "/investors/{investor_id}/narrative/basic/generate",
    dependencies=[Depends(require_owned_investor)],
)
def generate_basic(
    investor_id: str,
    tab: str = Query(...),
    as_of: date | None = None,
    settings: Settings = Depends(get_settings),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """The investor asked for this tab's summary (first write, or Retry)."""
    from app.narrative.client import NarrativeUnavailable

    _check_tab(tab)
    _guard(settings)
    if not consent.has_live_consent(connection, investor_id, consent.PURPOSE_ANALYTICS_INSIGHTS):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Turn on written insights to let the assistant read this portfolio.",
        )
    try:
        return _generate(connection, settings, investor_id, _resolve_on(as_of), tab)
    except IdentifierInRequest as exc:
        logger.error(
            "basic_narrative_blocked",
            extra={"investor_ref": _ref(investor_id), "tab": tab, "reason": str(exc)},
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Nothing was sent: the request contained {exc}. This is a data problem to fix, "
            "not something to retry.",
        ) from None
    except NarrativeUnavailable as exc:
        logger.warning(
            "basic_narrative_unavailable",
            extra={"investor_ref": _ref(investor_id), "tab": tab, "reason": str(exc)},
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The writing service didn't respond with usable lines. The figures are in "
            "Advanced; try again shortly.",
        ) from None


def _run(connection: Any, settings: Settings, as_of: date | None) -> dict[str, Any]:
    """Every consenting investor, every tab. One broken book or tab never stops the run."""
    from app.narrative.client import NarrativeUnavailable

    on = _resolve_on(as_of)
    attempted = stored = hits = 0
    failures: list[str] = []
    for investor_id in list_investor_ids(connection):
        if not consent.has_live_consent(
            connection, investor_id, consent.PURPOSE_ANALYTICS_INSIGHTS
        ):
            continue
        for tab in TABS:
            attempted += 1
            try:
                _, _, sha = _live(connection, investor_id, on, False)
                if (
                    store.get_artifact(
                        connection, investor_id, f"basic.{tab}", on, **_key(settings, sha)
                    )
                    is not None
                ):
                    hits += 1
                    continue
                _generate(connection, settings, investor_id, on, tab)
                stored += 1
            except (IdentifierInRequest, NarrativeUnavailable, HTTPException) as exc:
                connection.rollback()
                reason = getattr(exc, "detail", None) or str(exc)
                logger.warning(
                    "basic_narrative_run_failed",
                    extra={"investor_ref": _ref(investor_id), "tab": tab, "reason": reason},
                )
                failures.append(f"{_ref(investor_id)}/{tab}: {reason}")
    return {
        "as_of": on,
        "attempted": attempted,
        "stored": stored,
        "cache_hits": hits,
        "failures": failures,
    }


@ops_router.post("/run", dependencies=[Depends(require_ops_key)])
def run(
    as_of: date | None = None,
    settings: Settings = Depends(get_settings),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    _guard(settings)
    return _run(connection, settings, as_of)


@ops_router.get("/run-cron", dependencies=[Depends(require_cron_secret)])
def run_cron(
    settings: Settings = Depends(get_settings), connection: Any = Depends(get_conn)
) -> dict[str, Any]:
    """After the snapshot and analytics-warm jobs: narration of unsettled figures is
    narration of the wrong figures."""
    _guard(settings)
    return _run(connection, settings, None)


__all__ = ["router", "ops_router"]
