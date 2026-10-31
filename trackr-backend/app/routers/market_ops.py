"""Scheduled market-data collection on hosting, and the checks that it is working.

Owner amendment 2026-10-31 (docs/INSTANT_IMPORT_PLAN.md): cron (``CRON_SECRET``) and ops
(``X-Ops-Key``) endpoints may collect published archive files through
``archive_collection``. Nothing here takes a URL, host or source parameter, and no
investor-facing request reaches this module.

- ``/ops/cron/market-history``: fills the full-market store (``market_close``) newest day
  first, a bounded slice per run, from ``MARKET_HISTORY_START``.
- ``/ops/cron/market-reference``: Nifty 500 list, risk-free series and index P/E, which
  were laptop-only.
- ``/ops/exchange/reachability``: one live request per archive source, to decide where each
  feed runs. Its answers are stored like any collected response.
- ``/ops/feeds/status`` and ``/ops/cron/feeds-check``: how old each shared feed is, with an
  alert naming any feed more than one trading day behind, and BSE-only stocks whose prices
  suggest an action that needs the BSE file.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends

from app.auth.ops import require_cron_secret, require_ops_key
from app.config import Settings, get_settings
from app.database import get_conn
from app.exchange_reference import archive_collection, urlopen
from app.prices.alerts import get_notifier

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])

#: Inside Vercel's 300 s function limit, with room for the day in progress.
HISTORY_BUDGET_SECONDS = 200.0
#: A feed is behind when its newest day is older than this many weekdays.
FEED_LAG_WEEKDAYS = 1
INDEX_VALUATION_DAYS = 7


def _last_closed_day() -> date:
    """Today after the evening file is due (18:30 IST), else yesterday."""
    from app.routers.analytical import last_closed_trading_day

    return last_closed_trading_day()


def _history(connection: Any, settings: Settings) -> dict[str, Any]:
    from app.prices.market import backfill_market_history

    with archive_collection(connection):
        return backfill_market_history(
            connection,
            settings.market_history_start,
            _last_closed_day(),
            time_budget_seconds=HISTORY_BUDGET_SECONDS,
            notifier=get_notifier(settings),
        )


@router.get("/ops/cron/market-history", dependencies=[Depends(require_cron_secret)])
def market_history_cron(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """One bounded slice of the full-market history. ``complete`` once every trading
    day from ``MARKET_HISTORY_START`` is stored or proven closed; then each run checks a
    few recent days only."""
    return _history(connection, settings)


@router.post("/ops/market/history", dependencies=[Depends(require_ops_key)])
def market_history(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """``X-Ops-Key`` counterpart of the cron, for a manual slice."""
    return _history(connection, settings)


def _step(out: dict[str, Any], name: str, connection: Any, action) -> None:
    """Run one independent feed. A failure is reported by name and does not stop the
    others; nothing is substituted for the missing data."""
    try:
        out[name] = action()
        connection.commit()
    except Exception as exc:
        connection.rollback()
        logger.warning("market_reference_step_failed step=%s", name, exc_info=True)
        out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


@router.get("/ops/cron/market-reference", dependencies=[Depends(require_cron_secret)])
def market_reference_cron(connection: Any = Depends(get_conn)) -> dict[str, Any]:
    """Nifty 500 constituents, the risk-free series and index P/E / P/B for recent days,
    from the published archive files. The same functions ``daily_jobs.sh`` runs."""
    from app.db import index_valuation as valuation_store
    from app.db import risk_free as risk_free_store
    from app.db.universe import DEFAULT_UNIVERSE, sync_index_universe
    from app.prices import index_valuation, risk_free
    from app.prices.index_sources import TRI_INDICES
    from app.prices.sector_sources import nse_nifty500_source, parse_nse_sector_list

    today = date.today()
    out: dict[str, Any] = {}
    with archive_collection(connection):
        _step(
            out,
            "universe",
            connection,
            lambda: {
                "stored": sync_index_universe(
                    connection,
                    DEFAULT_UNIVERSE,
                    parse_nse_sector_list(nse_nifty500_source().fetch()),
                    today,
                )
            },
        )
        _step(
            out,
            "risk_free",
            connection,
            lambda: {
                "stored": risk_free_store.upsert(
                    connection, risk_free.fetch(date(today.year, 1, 1), today)
                )
            },
        )
        for name in TRI_INDICES:
            _step(
                out,
                f"index_valuation:{name}",
                connection,
                lambda name=name: {
                    "stored": valuation_store.upsert(
                        connection,
                        index_valuation.fetch(
                            name, today - timedelta(days=INDEX_VALUATION_DAYS), today
                        ),
                    )
                },
            )
    return out


def _probe_day(today: date) -> date:
    """A weekday at least two days old: published everywhere if it was a trading day."""
    day = today - timedelta(days=2)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _refreshing(request, **kwargs):
    """Always ask the network (inside the archive scope), never a stored answer."""
    return urlopen(request, refresh=True, **kwargs)


def _probe(name: str, call) -> dict[str, Any]:
    began = time.monotonic()
    try:
        body = call()
        return {
            "source": name,
            "ok": True,
            "bytes": len(body),
            "ms": round((time.monotonic() - began) * 1000),
        }
    except urllib.error.HTTPError as exc:
        error = f"HTTP {exc.code}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return {
        "source": name,
        "ok": False,
        "error": error,
        "ms": round((time.monotonic() - began) * 1000),
    }


NSE_API_PROBES = (
    (
        "nse_api_corporate_actions",
        "https://www.nseindia.com/api/corporates-corporateActions"
        "?index=equities&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}",
    ),
    (
        "nse_api_filings",
        "https://www.nseindia.com/api/integrated-filing-results"
        "?index=equities&symbol=INFY&type=Integrated%20Filing-%20Financials",
    ),
)
NSE_RSS_PROBES = (
    ("nse_rss_financial_results", "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"),
    ("nse_rss_corporate_actions", "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"),
)
_PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130 Safari/537.36",
    "Accept": "application/json, text/xml, */*",
    "Referer": "https://www.nseindia.com/",
}


def _raw_probe(opener, name: str, url: str, expect_json: bool = False) -> dict[str, Any]:
    """One measured request. Never stored. ``json_ok`` separates a real API answer from
    an HTML challenge page served with status 200."""
    began = time.monotonic()
    out: dict[str, Any] = {"source": name, "url": url}
    try:
        request = urllib.request.Request(url, headers=_PROBE_HEADERS)
        with opener.open(request, timeout=20) as response:
            body = response.read()
            out.update(status=response.status, content_type=response.headers.get("content-type"))
        out.update(ok=True, bytes=len(body))
        if expect_json:
            try:
                json.loads(body)
                out["json_ok"] = True
            except ValueError:
                out.update(ok=False, json_ok=False, error="not JSON (likely a block page)")
    except urllib.error.HTTPError as exc:
        out.update(ok=False, status=exc.code, error=f"HTTP {exc.code}")
    except Exception as exc:
        out.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
    out["ms"] = round((time.monotonic() - began) * 1000)
    return out


def _nse_api_probes(today: date) -> list[dict[str, Any]]:
    """NSE API and RSS from THIS host, with a cookie session like the collector's."""
    from app.exchange_reference import reachability_probe

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    results = []
    with reachability_probe():
        results.append(_raw_probe(opener, "nse_homepage", "https://www.nseindia.com/"))
        for name, template in NSE_API_PROBES:
            url = template.format(start=today - timedelta(days=7), end=today)
            results.append(_raw_probe(opener, name, url, expect_json=True))
        for name, url in NSE_RSS_PROBES:
            results.append(_raw_probe(opener, name, url))
    return results


@router.get("/ops/exchange/reachability", dependencies=[Depends(require_ops_key)])
def exchange_reachability(connection: Any = Depends(get_conn)) -> dict[str, Any]:
    """Can THIS deployment reach each archive source? One live request each.

    Decides where each feed runs (docs/INSTANT_IMPORT_PLAN.md, rollout step 3). A failed
    source keeps its feed on the laptop collector; nothing else changes.
    """
    from app.prices.index_sources import nifty_tri_source
    from app.prices.sector_sources import NSE_NIFTY500_URL
    from app.prices.sources import BROWSER_HEADERS, BSE_URL, NSE_URL, TIMEOUT_SECONDS

    day = _probe_day(date.today())

    def get(url: str) -> bytes:
        request = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with _refreshing(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()

    results = []
    with archive_collection(connection):
        results.append(_probe("nse_bhavcopy", lambda: get(NSE_URL.format(d=day))))
        results.append(_probe("bse_bhavcopy", lambda: get(BSE_URL.format(d=day))))
        results.append(_probe("nse_nifty500_list", lambda: get(NSE_NIFTY500_URL)))
        results.append(
            _probe(
                "niftyindices_tri",
                lambda: nifty_tri_source(opener=_refreshing).fetch(
                    day - timedelta(days=7), day
                ),
            )
        )
    connection.commit()
    # The NSE API decides where corporate actions and filings are collected. The homepage
    # is informative only: the collector's API calls work even when it answers 403.
    nse = _nse_api_probes(date.today())
    api_ok = all(r["ok"] for r in nse if r["source"].startswith("nse_api"))
    return {
        "probe_day": day,
        "results": results,
        "all_ok": all(r["ok"] for r in results),
        "nse_api": nse,
        "nse_api_ok": api_ok,
        "verdict": (
            "NSE API reachable from this deployment: corporate actions and filings can run "
            "on a cron" if api_ok else
            "NSE API blocked from this deployment: those jobs stay on the laptop collector"
        ),
    }


def _sessions_between(older: date, newer: date, closed: set[date]) -> int:
    """Weekdays after ``older`` up to ``newer`` that are not known exchange holidays."""
    count, day = 0, older
    while day < newer:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in closed:
            count += 1
    return count


#: (feed, where it is collected, newest-day SQL). Each answers "how recent is the newest
#: stored day", never whether a particular security is covered.
FEEDS = (
    ("market_close", "vercel", "SELECT max(price_date) FROM market_close_day WHERE complete"),
    ("price", "vercel", "SELECT max(price_date) FROM price"),
    ("fno_price", "vercel", "SELECT max(price_date) FROM fno_price"),
    ("index_level", "vercel", "SELECT min(d) FROM (SELECT max(level_date) d FROM index_level "
     "GROUP BY index_name) x"),
    ("index_valuation", "vercel", "SELECT max(valuation_date) FROM index_valuation"),
    ("risk_free", "vercel", "SELECT max(level_date) FROM risk_free_level"),
    ("fund_nav", "vercel", "SELECT max(nav_date) FROM fund_nav"),
    # The laptop's NSE API collection. Every attempt refreshes `fetched_at`, even when the
    # answer is unchanged, so a quiet week of notices does not look like a stopped laptop.
    ("nse_api", "laptop", "SELECT (max(r.fetched_at) AT TIME ZONE 'Asia/Kolkata')::date "
     "FROM exchange_reference_response r JOIN exchange_reference_request q USING (request_key) "
     "WHERE q.url LIKE 'https://www.nseindia.com/api/%'"),
)


def feed_status(
    connection: Any, settings: Settings | None = None, today: date | None = None
) -> dict[str, Any]:
    today = today or date.today()
    # The newest day a feed could have: yesterday until the evening file, so a feed that
    # is current as of yesterday's close is never reported behind.
    expected = today - timedelta(days=1)
    closed = {
        row[0]
        for row in connection.execute(
            "SELECT price_date FROM price_day_closed WHERE price_date > %s",
            (today - timedelta(days=60),),
        ).fetchall()
    }
    funds_on = bool(settings and settings.mf_reference_enabled)
    feeds = []
    for name, collector, sql in FEEDS:
        if name == "fund_nav" and not funds_on:
            continue
        newest = connection.execute(sql).fetchone()[0]
        lag = None if newest is None else _sessions_between(newest, expected, closed)
        feeds.append(
            {
                "feed": name,
                "collected_on": collector,
                "newest": newest,
                "weekdays_behind": lag,
                "behind": newest is None or lag > FEED_LAG_WEEKDAYS,
            }
        )
    connection.commit()
    return {"as_of": today, "feeds": feeds, "behind": [f["feed"] for f in feeds if f["behind"]]}


#: A one-day close move outside these bounds is not ordinary trading on BSE (whose daily
#: price bands are at most 20%): a split, bonus or consolidation is the likely reason.
JUMP_LOW = Decimal("0.7")
JUMP_HIGH = Decimal("1.4")
BSE_ALERT_WINDOW_DAYS = 15
#: Sessions without a close, while the market feed is current, before a BSE-only stock is
#: named: it may be suspended, or its action may need the BSE file.
BSE_SILENT_SESSIONS = 5


def price_jumps(closes: list[tuple[date, Decimal]]) -> list[tuple[date, date, Decimal]]:
    """``(previous day, day, ratio)`` for each consecutive move outside the bounds.
    ``closes`` must be in date order. Pure, so the threshold is testable alone."""
    out = []
    for (before, prev), (on, close) in zip(closes, closes[1:], strict=False):
        ratio = close / prev
        if ratio < JUMP_LOW or ratio > JUMP_HIGH:
            out.append((before, on, ratio))
    return out


def bse_only_alerts(connection: Any, today: date | None = None) -> list[dict[str, Any]]:
    """Ledger securities only BSE quotes that probably need a corporate-action file.

    There is no automated BSE announcement path (NSE's API covers NSE listings only), so
    the BSE file is downloaded by hand when this names a stock. Two signals:

    - a one-day close move outside ``JUMP_LOW``..``JUMP_HIGH`` with no corporate action on
      file for that ISIN between the two days;
    - no close for ``BSE_SILENT_SESSIONS`` sessions while the market feed has newer days.

    An alert changes nothing: no action, price or holding is created from it.
    """
    today = today or date.today()
    since = today - timedelta(days=BSE_ALERT_WINDOW_DAYS)
    rows = connection.execute(
        """WITH latest AS (
               SELECT DISTINCT ON (p.isin) p.isin, p.source, p.price_date
               FROM price p WHERE p.isin IN (SELECT DISTINCT isin FROM transaction)
               ORDER BY p.isin, p.price_date DESC)
           SELECT l.isin, s.symbol, s.name, l.price_date FROM latest l
           JOIN security s USING (isin) WHERE l.source = 'BSE_BHAVCOPY'
           ORDER BY l.isin"""
    ).fetchall()
    if not rows:
        connection.commit()
        return []
    isins = [row[0] for row in rows]
    closes: dict[str, list[tuple[date, Decimal]]] = {}
    for isin, on, close in connection.execute(
        "SELECT isin, price_date, close FROM price WHERE isin = ANY(%s) AND price_date >= %s "
        "ORDER BY isin, price_date",
        (isins, since),
    ).fetchall():
        closes.setdefault(isin, []).append((on, close))
    actions: dict[str, list[date]] = {}
    for isin, ex_date in connection.execute(
        "SELECT isin, ex_date FROM corporate_action WHERE isin = ANY(%s) AND ex_date >= %s",
        (isins, since),
    ).fetchall():
        actions.setdefault(isin, []).append(ex_date)
    market_newest = connection.execute(
        "SELECT max(price_date) FROM market_close_day WHERE complete"
    ).fetchone()[0]
    closed = {
        row[0]
        for row in connection.execute(
            "SELECT price_date FROM price_day_closed WHERE price_date >= %s", (since,)
        ).fetchall()
    }
    connection.commit()

    alerts = []
    for isin, symbol, name, newest in rows:
        reasons = []
        for before, on, ratio in price_jumps(closes.get(isin, [])):
            if not any(before < ex <= on for ex in actions.get(isin, [])):
                reasons.append(
                    f"close moved x{ratio.quantize(Decimal('0.01'))} from {before} to {on} "
                    "with no corporate action on file"
                )
        if (
            market_newest is not None
            and _sessions_between(newest, market_newest, closed) >= BSE_SILENT_SESSIONS
        ):
            reasons.append(f"no BSE close since {newest}; market data runs to {market_newest}")
        if reasons:
            alerts.append({"isin": isin, "symbol": symbol, "name": name, "reasons": reasons})
    return alerts


@router.get("/ops/feeds/status", dependencies=[Depends(require_ops_key)])
def feeds_status(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Newest stored day of every shared feed, and BSE-only stocks needing a file."""
    return {**feed_status(connection, settings), "bse_only": bse_only_alerts(connection)}


@router.get("/ops/cron/feeds-check", dependencies=[Depends(require_cron_secret)])
def feeds_check_cron(
    connection: Any = Depends(get_conn), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Alert, naming each feed, when one is more than a trading day behind. A laptop that
    stopped running shows up here as ``nse_notices`` falling behind."""
    status = feed_status(connection, settings)
    notifier = get_notifier(settings)
    if status["behind"]:
        notifier.notify(
            "Market data feeds behind",
            "Behind: " + ", ".join(status["behind"]),
            {"feeds": [f for f in status["feeds"] if f["behind"]]},
        )
    bse = bse_only_alerts(connection)
    if bse:
        notifier.notify(
            "BSE-only stocks need their corporate-action file",
            "Download the BSE corporate-action file for: "
            + ", ".join(a["symbol"] or a["isin"] for a in bse),
            {"stocks": bse},
        )
    return {**status, "bse_only": bse}
