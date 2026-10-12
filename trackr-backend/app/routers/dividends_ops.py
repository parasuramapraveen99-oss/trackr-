"""POST/GET /ops/dividends/* -- the automated declared-dividend feed and yfinance
discovery, wired the same way as ``app.routers.corporate_actions.ops_router`` wires
BONUS/SPLIT: one private worker, an ``X-Ops-Key`` POST for a human or a non-Vercel
scheduler, a ``CRON_SECRET`` GET for Vercel Cron specifically.

WHY THIS FILE EXISTS AT ALL. ``app.db.dividend_events.refresh_dividend_feed`` and
``discover_dividends_from_yfinance`` were correct and fully tested from the day they
were written, and neither was EVER REACHABLE from outside a test file: no endpoint, no
cron entry, nothing called them. The dividend_event table was therefore empty in any
real, running deployment -- not because the ingestion was wrong, but because nothing
had ever invoked it. See docs/INGESTION.md's Adapter 6b for the write-up of this gap
and why it is fixed here rather than left as a known limitation: an ingestion path that
is correct but never runs produces the identical symptom, from a user's chair, as one
that does not exist.

Gated by the same shared secrets as every other ``/ops/*`` route (never a user login):
this mutates reference data shared across every investor, exactly like the
corporate-action feed it sits beside.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import dividend_events as de
from app.db.dividend_reconcile import reconcile_pending_dividends
from app.schemas.transactions import (
    DiscoverDividendsYfinanceOut,
    ReconcileDividendsOut,
    RefreshDividendFeedOut,
)

ops_router = APIRouter(prefix="/ops/dividends", tags=["ops"])


def _jev_client(settings: Settings) -> Any:
    """A Jev client, or ``None`` when no key is configured -- the ordinary state today.

    Unlike ``app.routers.ingest._llm_client`` this has no separate enabled flag: the one
    thing it sends (an NSE corporate-action subject line, ISIN and symbol) is public
    market data, not an investor's own -- see ``app/config.py``'s ``jev_api_key``.
    """
    if not settings.jev_api_key:
        return None
    from app.ai.jev import JevClient

    return JevClient(settings.jev_api_key, settings.jev_model)

#: The full set of outcome keys `refresh_dividend_feed`/`discover_dividends_from_yfinance`
#: can produce -- see app.db.dividend_events.OUTCOME_*. Used to normalise their dynamic
#: dict (only outcomes that actually occurred are present) into the schemas' fixed,
#: always-present fields, rather than leaving a caller to guess which keys might be
#: missing from a given run.
_OUTCOME_KEYS = (
    de.OUTCOME_INSERTED, de.OUTCOME_CORROBORATED, "classified",
    de.OUTCOME_CONFLICT, de.OUTCOME_AMBIGUOUS, de.OUTCOME_UNCHANGED, "failed",
)


def _normalise(counts: dict[str, Any]) -> dict[str, Any]:
    return {key: counts.get(key, 0) for key in _OUTCOME_KEYS}


def _refresh_dividends(conn: Any, start: date, end: date, settings: Settings) -> dict[str, Any]:
    result = de.refresh_dividend_feed(
        conn, start, end, actor_uid=None, jev_client=_jev_client(settings)
    )
    return {
        "fetched": result.get("fetched", 0),
        "error": result.get("error"),
        **_normalise(result),
    }


@ops_router.post(
    "/refresh",
    response_model=RefreshDividendFeedOut,
    dependencies=[Depends(require_ops_key)],
)
def refresh_dividends(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """``X-Ops-Key``-gated -- a human or a non-Vercel scheduler triggering a fetch
    directly, same posture as ``refresh_corporate_actions``. Defaults to a 2-day window
    ending today, overridable for a backfill."""
    today = date.today()
    return _refresh_dividends(conn, start or today - timedelta(days=1), end or today, settings)


@ops_router.get(
    "/refresh-cron",
    response_model=RefreshDividendFeedOut,
    dependencies=[Depends(require_cron_secret)],
)
def refresh_dividends_cron(
    conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Same job, reachable by Vercel Cron: GET, gated by ``CRON_SECRET`` rather than
    ``X-Ops-Key``, since that is the only credential Vercel's scheduler sends. See
    docs/DEPLOYMENT.md."""
    today = date.today()
    return _refresh_dividends(conn, today - timedelta(days=1), today, settings)


@ops_router.post(
    "/discover-yfinance",
    response_model=DiscoverDividendsYfinanceOut,
    dependencies=[Depends(require_ops_key)],
)
def discover_dividends_yfinance(
    isin: list[str] | None = Query(default=None),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """``X-Ops-Key``-gated, same posture as ``discover_yfinance`` for corporate actions.
    Occasional -- a ticker's whole dividend history comes back in one call and does not
    change day to day. Defaults to every ISIN this system has ever held; pass one or
    more ``isin`` query params to target specific securities instead of paying for the
    full sweep. A second, independently operated source: NSE's notices API also serves
    historical windows (the reference worker's month-by-month sweep reaches them), so
    this is corroboration and a newly-held-security backfill, not the only historical
    path. Per-ISIN failures are recorded as observations and the run raises a summary."""
    result = de.discover_dividends_from_yfinance(
        conn, isins=isin, rights_reference=settings.yahoo_data_rights_reference
    )
    return _normalise(result)


@ops_router.get(
    "/discover-yfinance-cron",
    response_model=DiscoverDividendsYfinanceOut,
    dependencies=[Depends(require_cron_secret)],
)
def discover_dividends_yfinance_cron(
    conn: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Same sweep, reachable by Vercel Cron: GET, gated by ``CRON_SECRET``.

    WEEKLY, never daily, and the distinction is the whole reason this is a separate
    route rather than another entry pointed at ``/refresh-cron``. A ticker's dividend
    HISTORY does not change day to day, so a daily sweep would spend 46 Yahoo calls to
    re-read the same answer; the daily job that does need to run is ``/refresh-cron``,
    which reads NSE's notices for new declarations. What this catches that the daily
    two-day window does not is a NEWLY HELD security -- the moment an import adds an ISIN,
    its dividend history is missing from ``dividend_event`` until a sweep covers those
    months; NSE's API serves them, but only when a historical window is requested.
    ``held_isins`` is re-read on every run,
    so a security bought this week is swept next week without anyone remembering to.

    Sharing ``security-alias``'s weekly slot (Sunday) rather than a weekday one: this is
    reference-data maintenance, not a trading-day job, and nothing downstream of it is
    waiting on a same-day answer."""
    result = de.discover_dividends_from_yfinance(
        conn, isins=None, rights_reference=settings.yahoo_data_rights_reference
    )
    return _normalise(result)


@ops_router.post(
    "/reconcile",
    response_model=ReconcileDividendsOut,
    dependencies=[Depends(require_ops_key)],
)
def reconcile_dividends(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """``X-Ops-Key``-gated second pass over DRAFT dividends the feeds left behind -- see
    ``app.db.dividend_reconcile``."""
    return reconcile_pending_dividends(conn)


@ops_router.get(
    "/reconcile-cron",
    response_model=ReconcileDividendsOut,
    dependencies=[Depends(require_cron_secret)],
)
def reconcile_dividends_cron(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """Same pass for Vercel Cron. Scheduled daily AFTER ``/refresh-cron`` and after the
    Sunday yfinance sweep, so each day's new leftovers are picked up automatically."""
    return reconcile_pending_dividends(conn)
