"""AMFI jobs: the daily NAV refresh, history windows, and AMC-id discovery.

docs/MF_ETF_PLAN.md section 7. Three rules shape every function here:

* HTTP happens OUTSIDE any open database transaction. Each job commits (or rolls back)
  before it fetches, parses the whole response, and only then opens a short write.
* A failed fetch or an unreadable file publishes NOTHING and keeps the previous valid
  data; the failure is recorded as an ``amfi_batch`` row so it is visible.
* Success is measured by HELD coverage, not HTTP status: the daily result names every
  held fund whose newest NAV is stale or missing, even when every download succeeded.

The daily file runs from its cron (``GET /ops/cron/mf-nav-refresh``) under its own
advisory lock. History windows and AMC discovery are tasks on the shared reference queue
(``reference_sync_task`` kinds ``AMFI_NAV_WINDOW`` / ``AMFI_AMC_DISCOVERY``), so they
inherit its restart recovery, retry backoff and single-worker lock.
"""

from __future__ import annotations

import calendar
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from app.db import fund_reference as ref
from app.db.fund_tax import derive_category_profiles
from app.prices import amfi

logger = logging.getLogger(__name__)

#: Session advisory lock for the daily refresh: one run at a time, across hosts.
DAILY_LOCK = 804732951
#: Plan section 7.2: re-read the last 14 days every day for late or corrected NAVs.
CORRECTION_LOOKBACK_DAYS = 14
#: AMC ids probed by discovery. AMFI answered ids 3..80 on 13-Oct-2026 and an HTML page
#: for 1, 2 and 999; the ceiling leaves room for new AMCs.
AMC_ID_CEILING = 150
#: Plan section 7.2: at most this many AMFI tasks in one five-task batch while other
#: kinds are due, so a history backlog cannot starve identity/action work.
AMFI_TASKS_PER_BATCH = 2
AMFI_TASK_KINDS = ("AMFI_NAV_WINDOW", "AMFI_AMC_DISCOVERY")


class AmfiFilterIgnored(RuntimeError):
    """The history report answered for a different AMC than the one asked for."""


@dataclass
class DailyResult:
    status: str
    batch_id: int | None = None
    reason: str | None = None
    bytes: int | None = None
    elapsed_ms: int | None = None
    rows: int = 0
    rejects: int = 0
    conflicts: int = 0
    navs_published: int = 0
    navs_revised: int = 0
    absences_repaired: int = 0
    routes_changed: list[str] = field(default_factory=list)
    dirty_marked: int = 0
    tax_profiles: int = 0
    history_tasks: int = 0
    held_funds: int = 0
    stale: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lock(conn: Any, key: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        return bool(cur.fetchone()[0])


def _unlock(conn: Any, key: int) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    conn.commit()


def refresh_daily(
    conn: Any,
    *,
    timeout_seconds: float,
    today: date | None = None,
    allow_catalogue_drop: bool = False,
    client: Any = None,
) -> DailyResult:
    """Fetch NAVAll.txt once, validate, publish, then repair and schedule."""
    today = today or date.today()
    if not ref.fund_tables_present(conn):
        raise RuntimeError("migration 081_fund_reference.sql is not applied")
    conn.commit()
    if not _lock(conn, DAILY_LOCK):
        return DailyResult(status="BUSY", reason="another AMFI daily refresh holds the lock")
    try:
        began = time.monotonic()
        try:
            payload = amfi.fetch(amfi.DAILY_URL, timeout_seconds, client=client)
            parsed = amfi.parse(payload, today)
        except (amfi.AmfiFetchError, amfi.AmfiFormatError) as exc:
            elapsed = int((time.monotonic() - began) * 1000)
            batch_id = ref.record_failed_batch(
                conn,
                amfi.Layout.DAILY,
                amfi.DAILY_URL,
                f"{type(exc).__name__}: {exc}",
                elapsed_ms=elapsed,
            )
            conn.commit()
            logger.error(
                "amfi_daily_failed",
                extra={"error_type": type(exc).__name__, "batch_id": batch_id},
            )
            return DailyResult(
                status="FAILED", batch_id=batch_id, reason=str(exc), elapsed_ms=elapsed
            )
        elapsed = int((time.monotonic() - began) * 1000)
        if parsed.layout != amfi.Layout.DAILY:
            batch_id = ref.record_failed_batch(
                conn,
                amfi.Layout.DAILY,
                amfi.DAILY_URL,
                "daily URL served the history layout",
                elapsed_ms=elapsed,
            )
            conn.commit()
            return DailyResult(status="FAILED", batch_id=batch_id, reason="wrong layout")

        published = ref.publish(
            conn,
            parsed,
            amfi.DAILY_URL,
            len(payload),
            elapsed_ms=elapsed,
            allow_catalogue_drop=allow_catalogue_drop,
        )
        conn.commit()
        result = DailyResult(
            status=published.status,
            batch_id=published.batch_id,
            reason=published.reason,
            bytes=len(payload),
            elapsed_ms=elapsed,
            rows=parsed.rows,
            rejects=len(parsed.rejects),
            conflicts=len(parsed.conflicts),
            navs_published=published.navs_published,
            navs_revised=published.navs_revised,
        )
        if parsed.conflicts:
            logger.error("amfi_daily_quarantined", extra={"batch_id": published.batch_id})
            return result

        # Derived repairs, each in its own short transaction.
        result.absences_repaired = ref.repair_fund_absences(conn)
        result.routes_changed = ref.sync_route_observations(conn)
        result.dirty_marked = ref.mark_fund_dates_dirty(conn, published.changed, "AMFI daily NAV")
        result.tax_profiles = derive_category_profiles(conn)
        conn.commit()
        result.history_tasks = schedule_history(conn, today)
        conn.commit()
        result.held_funds, result.stale = held_freshness(conn, today)
        if result.stale:
            logger.warning(
                "amfi_held_funds_stale",
                extra={"isins": [s["isin"] for s in result.stale], "batch_id": result.batch_id},
            )
        return result
    finally:
        _unlock(conn, DAILY_LOCK)


def held_freshness(conn: Any, today: date) -> tuple[int, list[dict[str, Any]]]:
    """Every ledger fund unit on the AMFI route whose newest NAV is older than the policy
    or missing -- named, by ISIN and scheme, whatever the download status said."""
    from app.prices.routing import Route

    held = ref.isins_on_route(conn, ref.relevant_isins(conn), Route.AMFI)
    if not held:
        return 0, []
    fresh = ref.latest_fund_navs(conn, held, today)
    routes = ref.valuation_routes(conn, held)
    stale = []
    with conn.cursor() as cur:
        for isin in held:
            if isin in fresh:
                continue
            cur.execute(
                """SELECT max(n.nav_date) FROM amfi_scheme_isin i JOIN fund_nav n
                   USING (scheme_code) WHERE i.isin = %s""",
                (isin,),
            )
            newest = cur.fetchone()[0]
            stale.append(
                {
                    "isin": isin,
                    "scheme_code": routes[isin].scheme_code,
                    "scheme_name": routes[isin].scheme_name,
                    "newest_nav_date": newest,
                }
            )
    return len(held), stale


# --- history ---------------------------------------------------------------------------------


def _full_months(start: date, before: date) -> list[tuple[date, date]]:
    """Whole calendar months from ``start``'s month up to (not including) ``before``'s
    month. Whole months give every scheme of an AMC the same window keys."""
    out = []
    cursor = start.replace(day=1)
    while cursor < before.replace(day=1):
        last = cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1])
        out.append((cursor, last))
        cursor = last + timedelta(days=1)
    return out


def recent_window_start(today: date) -> date:
    """The rolling window starts on the 1st of the month holding ``today`` minus the
    correction lookback, so late corrections across a month boundary are re-read."""
    return (today - timedelta(days=CORRECTION_LOOKBACK_DAYS)).replace(day=1)


def schedule_history(conn: Any, today: date) -> int:
    """Enqueue the AMFI history work relevant schemes still need.

    Per AMC with a discovered id:

    * one task per WHOLE calendar month, older than the rolling window, that some relevant
      scheme lacks coverage for -- from its first transaction less the 7-day carry --
      newest month first. Coverage is per scheme, so a scheme added later reopens a month
      another scheme already completed, with only itself in the payload;
    * one rolling ``recent`` task, refreshed daily, from ``recent_window_start`` to
      yesterday: the current month as it fills in, and the 14-day correction lookback.
      It records no coverage -- it is re-read every day by design.

    An AMC with no discovered id gets a discovery task instead; its schemes stay named as
    unpriced meanwhile.
    """
    from app.db.reference_sync import enqueue

    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.amc_name, s.scheme_code, min(t.txn_date)
               FROM transaction t JOIN amfi_scheme_isin i ON i.isin = t.isin
               JOIN amfi_scheme s ON s.scheme_code = i.scheme_code
               WHERE s.instrument_kind = 'MUTUAL_FUND'
               GROUP BY s.amc_name, s.scheme_code"""
        )
        needs = cur.fetchall()
        cur.execute("SELECT amc_name, amc_id FROM amfi_amc")
        amc_ids = dict(cur.fetchall())
    if not needs:
        return 0
    yesterday = today - timedelta(days=1)
    recent_start = recent_window_start(today)
    enqueued = 0
    missing_amcs = sorted({amc for amc, _code, _first in needs if amc not in amc_ids})
    if missing_amcs:
        # Reopened only once a previous pass has FINISHED: a pass in progress keeps its
        # checkpoint rather than restarting from id 1.
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reference_sync_task (kind, task_key, payload)
                   VALUES ('AMFI_AMC_DISCOVERY', 'all', %s::jsonb)
                   ON CONFLICT (kind, task_key) DO UPDATE SET
                       payload = CASE WHEN reference_sync_task.state = 'done'
                                      THEN EXCLUDED.payload ELSE reference_sync_task.payload END,
                       state = CASE WHEN reference_sync_task.state = 'done'
                                    THEN 'pending' ELSE reference_sync_task.state END,
                       available_at = CASE WHEN reference_sync_task.state = 'done'
                                    THEN now() ELSE reference_sync_task.available_at END""",
                (
                    json.dumps(
                        {
                            "next_id": 1,
                            "ceiling": AMC_ID_CEILING,
                            "probe_date": yesterday.isoformat(),
                            "missing": missing_amcs,
                        }
                    ),
                ),
            )
        logger.info("amfi_amc_ids_missing", extra={"amcs": missing_amcs})
        enqueued += 1
    by_amc: dict[str, list[tuple[int, date]]] = {}
    for amc, code, first in needs:
        if amc in amc_ids:
            by_amc.setdefault(amc, []).append((code, first))
    with conn.cursor() as cur:
        for amc, schemes in by_amc.items():
            amc_id = amc_ids[amc]
            codes = sorted(code for code, _first in schemes)
            cur.execute(
                """SELECT scheme_code, window_start, window_end FROM amfi_history_coverage
                   WHERE scheme_code = ANY(%s)""",
                (codes,),
            )
            covered = {(c, s, e) for c, s, e in cur.fetchall()}
            wanted: dict[tuple[date, date], set[int]] = {}
            for code, first in schemes:
                start = first - timedelta(days=ref.MAX_NAV_AGE_DAYS + 3)
                for window in _full_months(start, recent_start):
                    if (code, window[0], window[1]) not in covered:
                        wanted.setdefault(window, set()).add(code)
            # Newest first: recent valuations are what screens ask for. ``available_at``
            # orders the shared queue, so older windows are made due a little later.
            for rank, (window, missing) in enumerate(
                sorted(wanted.items(), key=lambda item: item[0], reverse=True)
            ):
                key = f"{amc_id}:{window[0].isoformat()}:{window[1].isoformat()}"
                payload = {
                    "amc_id": amc_id,
                    "amc_name": amc,
                    "start": window[0].isoformat(),
                    "end": window[1].isoformat(),
                    "schemes": sorted(missing),
                }
                cur.execute(
                    """INSERT INTO reference_sync_task (kind, task_key, payload, available_at)
                       VALUES ('AMFI_NAV_WINDOW', %s, %s::jsonb,
                               now() + %s * interval '1 second')
                       ON CONFLICT (kind, task_key) DO UPDATE SET
                           payload = EXCLUDED.payload,
                           state = CASE WHEN reference_sync_task.state = 'done'
                                        THEN 'pending' ELSE reference_sync_task.state END,
                           available_at = CASE WHEN reference_sync_task.state = 'done'
                                        THEN EXCLUDED.available_at
                                        ELSE reference_sync_task.available_at END""",
                    (key, json.dumps(payload), rank + 1),
                )
                enqueued += 1
            if recent_start <= yesterday:
                enqueue(
                    conn,
                    "AMFI_NAV_WINDOW",
                    f"{amc_id}:recent",
                    {
                        "amc_id": amc_id,
                        "amc_name": amc,
                        "start": recent_start.isoformat(),
                        "end": yesterday.isoformat(),
                        "schemes": codes,
                        "lookback": True,
                    },
                    refresh=True,
                )
                enqueued += 1
    return enqueued


def process_nav_window(
    conn: Any, payload: dict[str, Any], timeout_seconds: float, client: Any = None
) -> dict[str, Any] | None:
    """One AMC history window. Returns a checkpoint (a split window) or None when done.

    The filter is VERIFIED, not assumed: the file must name exactly the AMC asked for, or
    the task fails. A timeout splits the window in half, down to a single day.
    """
    windows = payload.get("windows") or [[payload["start"], payload["end"]]]
    start, end = (date.fromisoformat(d) for d in windows[0])
    url = amfi.history_url(payload["amc_id"], start, end)
    conn.commit()
    began = time.monotonic()
    try:
        body = amfi.fetch(url, timeout_seconds, client=client)
        parsed = amfi.parse(body, date.today())
    except amfi.AmfiFetchError as exc:
        if "Timeout" in str(exc) and start < end:
            middle = start + (end - start) // 2
            halves = [
                [start.isoformat(), middle.isoformat()],
                [(middle + timedelta(days=1)).isoformat(), end.isoformat()],
            ]
            logger.info("amfi_window_split", extra={"amc_id": payload["amc_id"], "url": url})
            return {**payload, "windows": halves + windows[1:]}
        ref.record_failed_batch(
            conn,
            amfi.Layout.HISTORY,
            url,
            f"{type(exc).__name__}: {exc}",
            amc_id=payload["amc_id"],
            window=(start, end),
        )
        conn.commit()
        raise
    except amfi.AmfiFormatError as exc:
        ref.record_failed_batch(
            conn,
            amfi.Layout.HISTORY,
            url,
            f"{type(exc).__name__}: {exc}",
            amc_id=payload["amc_id"],
            window=(start, end),
        )
        conn.commit()
        raise
    elapsed = int((time.monotonic() - began) * 1000)
    if parsed.amc_names and parsed.amc_names != {payload["amc_name"]}:
        ref.record_failed_batch(
            conn,
            amfi.Layout.HISTORY,
            url,
            f"AMC filter ignored: asked {payload['amc_name']!r}, got {sorted(parsed.amc_names)}",
            amc_id=payload["amc_id"],
            window=(start, end),
            elapsed_ms=elapsed,
        )
        conn.commit()
        raise AmfiFilterIgnored(f"AMFI ignored mf={payload['amc_id']}")
    published = ref.publish(
        conn,
        parsed,
        url,
        len(body),
        amc_id=payload["amc_id"],
        window=(start, end),
        elapsed_ms=elapsed,
    )
    if published.status == "QUARANTINED" and parsed.conflicts:
        conn.commit()
        raise ValueError(f"AMFI history window {start}..{end} has conflicting NAVs")
    if not payload.get("lookback"):
        found: dict[int, int] = {}
        for code, _on in parsed.navs:
            found[code] = found.get(code, 0) + 1
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO amfi_history_coverage (scheme_code, window_start, window_end,
                       batch_id, navs_found) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (scheme_code, window_start, window_end) DO UPDATE SET
                       batch_id = EXCLUDED.batch_id, navs_found = EXCLUDED.navs_found,
                       covered_at = now()""",
                [
                    (
                        code,
                        date.fromisoformat(payload["start"]),
                        date.fromisoformat(payload["end"]),
                        published.batch_id,
                        found.get(code, 0),
                    )
                    for code in payload["schemes"]
                ]
                if len(windows) == 1
                else [],
            )
    ref.mark_fund_dates_dirty(conn, published.changed, "AMFI history NAV")
    derive_category_profiles(conn)
    if len(windows) > 1:
        return {**payload, "windows": windows[1:]}
    return None


def process_amc_discovery(
    conn: Any,
    payload: dict[str, Any],
    timeout_seconds: float,
    budget_seconds: float,
    client: Any = None,
) -> dict[str, Any] | None:
    """Learn AMFI's numeric AMC ids from AMFI's own answers, a few ids per invocation.

    A valid id answers with a file naming exactly one AMC; an invalid one with an HTML
    page (``AmfiFormatError``), recorded as "no AMC" and passed over. Nothing is inferred
    from a name.
    """
    probe = date.fromisoformat(payload["probe_date"])
    next_id = int(payload["next_id"])
    ceiling = int(payload["ceiling"])
    began = time.monotonic()
    conn.commit()
    while next_id <= ceiling and time.monotonic() - began < budget_seconds:
        url = amfi.history_url(next_id, probe, probe)
        try:
            parsed = amfi.parse(amfi.fetch(url, timeout_seconds, client=client), date.today())
        except amfi.AmfiFormatError:
            next_id += 1
            continue
        if len(parsed.amc_names) == 1:
            name = next(iter(parsed.amc_names))
            with conn.cursor() as cur:
                # First id observed for a name wins; a second id answering with the same
                # name is logged, not allowed to flip the mapping silently.
                cur.execute(
                    """INSERT INTO amfi_amc (amc_id, amc_name, observed_url) VALUES (%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (next_id, name, url),
                )
                if cur.rowcount == 0:
                    logger.info("amfi_amc_id_seen", extra={"amc_id": next_id, "amc": name})
            conn.commit()
        next_id += 1
    if next_id <= ceiling:
        return {**payload, "next_id": next_id}
    return None
