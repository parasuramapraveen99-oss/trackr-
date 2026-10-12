"""GET /investors/{id}/narrative -- the generated explanations, and the job that writes them.

READ PATH NEVER CALLS A MODEL. This is the same promise ``app/routers/analytics.py``
makes about third parties ("the difference between 'analytics' and 'a proxy'") and it is
load-bearing for the same reason, plus one more: the analytics page refetches every two
minutes while as_of is today, so a generation call on the read path would be hundreds of
model calls per open tab per day, each with a thirty-second timeout in a request someone
is waiting on. Generation happens on the cron below, after prices settle.

A CACHE HIT IS RE-VERIFIED, NOT REPLAYED. The stored artifact holds CLAIMS, not prose,
and every read rebuilds the fact pack live, re-runs V1-V7 and re-renders. So the figures
a reader sees are always today's, and a comparison that has stopped holding is dropped
rather than served. The cache saves the model call and nothing else -- which is the only
part that is expensive, and the only part that is not reproducible.

THREE STATES, NOT AN EMPTY LIST. ``disabled`` (off on this deployment), ``not_generated``
(nothing written for this exact set of figures yet -- the normal state between a price
move and the next run) and ``ready``. "We have nothing to say" and "we have not looked"
are different claims and the page must not show the second as the first.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.analytics.verdict import RULE_VERSION
from app.auth.dependencies import require_owned_investor
from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import narrative as store
from app.db.repository import list_investor_ids
from app.db.universe import DEFAULT_UNIVERSE
from app.narrative.claims import Claim, Segment
from app.narrative.digest import definition_pack_sha, factpack_sha256, quantise
from app.narrative.factpack import FactPack, attach_previous, build_pack
from app.narrative.prompt import TEMPLATE_VERSION
from app.narrative.render import render_all
from app.narrative.select import SURFACES, UnknownSurface, select
from app.narrative.verify import verify
from app.prices.index_sources import NIFTY_TRI_INDEX_NAME
from app.routers.analytics import analytics as analytics_payload
from app.schemas.narrative import NarrativeResponse, NarrativeRunResponse

logger = logging.getLogger("routers.narrative")

router = APIRouter(tags=["narrative"])
ops_router = APIRouter(prefix="/ops/narrative", tags=["narrative"])

#: Generated ahead of time by the cron. The per-holding surface is deliberately NOT here:
#: one artifact per holding per investor per day is a different order of cost, and it is
#: generated on demand by a future explicit request rather than speculatively for a page
#: nobody may open.
PREGENERATED_SURFACES = (
    "analytics.overview",
    "analytics.risk",
    "analytics.diversification",
    "dashboard",
)


def _pack_for(
    connection: Any,
    investor_id: str,
    *,
    as_of: date | None,
    advised_only: bool,
    benchmark: str,
    universe: str,
) -> tuple[FactPack, dict[str, Any], str, date]:
    """One live ledger replay -> (pack with history attached, quantised, sha, as_of).

    Calls the analytics endpoint function directly rather than over HTTP: it is a plain
    function of a connection, and going through the network to talk to ourselves would
    add a failure mode and a round trip for nothing.
    """
    payload = analytics_payload(
        investor_id=investor_id,
        as_of=as_of,
        benchmark=benchmark,
        advised_only=advised_only,
        universe=universe,
        connection=connection,
    )
    pack = build_pack(payload)
    resolved = payload["as_of"]
    quantised = quantise(pack)

    earlier = store.get_previous_factpack(
        connection, investor_id, resolved,
        advised_only=advised_only, benchmark=benchmark, universe=universe,
    )
    if earlier is not None:
        previous_as_of, previous_facts = earlier
        pack = attach_previous(pack, previous_facts, str(previous_as_of))

    return pack, quantised, factpack_sha256(quantised), resolved


def _claims_from_json(rows: list[dict[str, Any]]) -> list[Claim]:
    return [
        Claim(
            relation=row["relation"],
            args=row.get("args") or {},
            segments=tuple(
                Segment(s["t"], v=s.get("v"), id=s.get("id")) for s in row["segments"]
            ),
            cites=tuple(row.get("cites") or ()),
            card=row.get("card"),
        )
        for row in rows
    ]


def _claims_to_json(claims: list[Claim]) -> list[dict[str, Any]]:
    return [
        {
            "relation": c.relation,
            "args": c.args,
            "segments": [
                {k: v for k, v in asdict(s).items() if v is not None} for s in c.segments
            ],
            "cites": list(c.cites),
            "card": c.card,
        }
        for c in claims
    ]


@router.get(
    "/investors/{investor_id}/narrative",
    response_model=NarrativeResponse,
    dependencies=[Depends(require_owned_investor)],
)
def narrative(
    investor_id: str,
    surface: str = Query(..., description="One of app.narrative.select.SURFACES"),
    as_of: date | None = None,
    isin: str | None = None,
    benchmark: str = NIFTY_TRI_INDEX_NAME,
    advised_only: bool = False,
    universe: str = DEFAULT_UNIVERSE,
    settings: Settings = Depends(get_settings),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    if surface not in SURFACES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown surface {surface!r}; known: {sorted(SURFACES)}",
        )

    pack, _, sha, resolved = _pack_for(
        connection, investor_id, as_of=as_of, advised_only=advised_only,
        benchmark=benchmark, universe=universe,
    )
    base = {
        "as_of": resolved,
        "surface": surface,
        "compared_with": pack.previous_as_of,
    }

    if not settings.insight_generation_enabled:
        # Stated rather than silently empty: this is a deployment decision, not an
        # absence of anything to say.
        return {**base, "state": "disabled"}

    try:
        selected = select(pack, surface, isin=isin if surface == "holding" else None)
    except (UnknownSurface, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    stored = store.get_artifact(
        connection, investor_id, surface, resolved,
        advised_only=advised_only, benchmark=benchmark, universe=universe,
        factpack_sha256=sha, rule_version=RULE_VERSION,
        template_version=TEMPLATE_VERSION,
        definition_pack_sha=definition_pack_sha(),
        model=settings.openrouter_model,
    )
    if stored is None:
        return {**base, "state": "not_generated"}

    # Re-verified against a LIVE pack, not trusted because it was verified once.
    result = verify(_claims_from_json(stored), selected)
    return {
        **base,
        "state": "ready",
        "generated_at": datetime.now(UTC),
        "statements": render_all(result.claims, selected),
        "rejected": result.rejected_rules,
    }


def _run(connection: Any, settings: Settings, as_of: date | None) -> dict[str, Any]:
    """Generate and store for every investor. The only caller of the model."""
    from app.ai.llm import LlmClient
    from app.narrative.client import NarrativeUnavailable, generate_claims

    client = LlmClient(settings.openrouter_api_key or "", settings.openrouter_model)
    investors = list_investor_ids(connection)

    attempted = stored_count = hits = kept = rejected = 0
    unavailable: list[str] = []
    resolved = as_of or date.today()

    for investor_id in investors:
        try:
            pack, quantised, sha, resolved = _pack_for(
                connection, investor_id, as_of=as_of, advised_only=False,
                benchmark=NIFTY_TRI_INDEX_NAME, universe=DEFAULT_UNIVERSE,
            )
        except Exception as exc:  # noqa: BLE001 - one broken book must not stop the run
            logger.warning("narrative: pack failed for %s: %s", investor_id, exc)
            unavailable.append(f"{investor_id}: pack: {exc}")
            continue

        # Stored FIRST and unconditionally. The pack is what tomorrow's "what changed"
        # compares against, and it is worth keeping even on a day the model is down.
        store.upsert_factpack(
            connection, investor_id, resolved,
            advised_only=False, benchmark=NIFTY_TRI_INDEX_NAME,
            universe=DEFAULT_UNIVERSE, facts=quantised, sha256=sha,
        )
        connection.commit()

        for surface in PREGENERATED_SURFACES:
            attempted += 1
            selected = select(pack, surface)
            if not selected.facts:
                continue
            if store.get_artifact(
                connection, investor_id, surface, resolved,
                advised_only=False, benchmark=NIFTY_TRI_INDEX_NAME,
                universe=DEFAULT_UNIVERSE, factpack_sha256=sha,
                rule_version=RULE_VERSION, template_version=TEMPLATE_VERSION,
                definition_pack_sha=definition_pack_sha(), model=settings.openrouter_model,
            ) is not None:
                hits += 1
                continue

            try:
                outcome = generate_claims(client, selected, surface)
            except NarrativeUnavailable as exc:
                logger.warning("narrative: %s/%s unavailable: %s", investor_id, surface, exc)
                unavailable.append(f"{investor_id}/{surface}: {exc}")
                continue

            result = verify(outcome.claims, selected)
            kept += len(result.claims)
            rejected += len(result.rejections)
            store.upsert_artifact(
                connection, investor_id, surface, resolved,
                advised_only=False, benchmark=NIFTY_TRI_INDEX_NAME,
                universe=DEFAULT_UNIVERSE, factpack_sha256=sha,
                rule_version=RULE_VERSION, template_version=TEMPLATE_VERSION,
                definition_pack_sha=definition_pack_sha(), model=settings.openrouter_model,
                claims=_claims_to_json(result.claims),
                rejections=[asdict(r) for r in result.rejections]
                + [{"rule": "PARSE", "index": -1, "reason": d} for d in outcome.dropped],
            )
            connection.commit()
            stored_count += 1

    return {
        "as_of": resolved,
        "investors": len(investors),
        "surfaces_attempted": attempted,
        "artifacts_stored": stored_count,
        "cache_hits": hits,
        "claims_kept": kept,
        "claims_rejected": rejected,
        "unavailable": unavailable,
    }


def _guard(settings: Settings) -> None:
    if not settings.insight_generation_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "INSIGHT_GENERATION_ENABLED is false on this deployment",
        )


@ops_router.post(
    "/run", response_model=NarrativeRunResponse, dependencies=[Depends(require_ops_key)]
)
def run(
    as_of: date | None = None,
    settings: Settings = Depends(get_settings),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    _guard(settings)
    return _run(connection, settings, as_of)


@ops_router.get(
    "/run-cron",
    response_model=NarrativeRunResponse,
    dependencies=[Depends(require_cron_secret)],
)
def run_cron(
    settings: Settings = Depends(get_settings),
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Vercel Cron's counterpart. Scheduled AFTER the snapshot job, which is itself
    after the price refresh -- narration of figures that have not settled is narration
    of the wrong figures."""
    _guard(settings)
    return _run(connection, settings, None)
