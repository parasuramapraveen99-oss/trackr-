"""AMFI catalogue, fund NAVs, valuation routes, and the repairs fund data needs.

docs/MF_ETF_PLAN.md sections 3-4 and 7. Raw SQL, like every repository here. The pure
pieces live elsewhere: parsing in ``app.prices.amfi``, headings in
``app.prices.fund_category``, the route rule in ``app.prices.routing``.

Every write here is to PUBLIC reference data (AMFI's catalogue and NAVs) or to derived
caches (snapshots and their markers). No ledger row is ever written or changed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.prices.amfi import PARSER_VERSION, Layout, ParsedFile
from app.prices.fund_category import Exposure, InstrumentKind
from app.prices.routing import CatalogueEntry, Route, RouteDecision, decide_route

logger = logging.getLogger(__name__)

#: Plan section 1: an AMFI NAV is usable for date D when dated D or up to 7 calendar days
#: before it (inclusive); eight days is stale. A product cutoff, not a statutory one.
MAX_NAV_AGE_DAYS = 7
EXCHANGE_SOURCES = ("NSE_BHAVCOPY", "BSE_BHAVCOPY")


def fund_tables_present(conn: Any) -> bool:
    """Migration 081 applied? Code can deploy ahead of the migration; until it lands there
    is simply no AMFI evidence, and every fund unit routes as rules 4-5 say."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('amfi_scheme_isin') IS NOT NULL")
        return bool(cur.fetchone()[0])


# --- routes -------------------------------------------------------------------------------


def catalogue_entries(conn: Any, isins: Sequence[str]) -> dict[str, list[CatalogueEntry]]:
    if not isins or not fund_tables_present(conn):
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT i.isin, s.scheme_code, s.instrument_kind, s.exposure, s.category,
                      s.scheme_name, s.amc_name, s.plan, s.option
               FROM amfi_scheme_isin i JOIN amfi_scheme s USING (scheme_code)
               WHERE i.isin = ANY(%s)""",
            (list(isins),),
        )
        out: dict[str, list[CatalogueEntry]] = {}
        for isin, code, kind, exposure, category, name, amc, plan, option in cur.fetchall():
            out.setdefault(isin, []).append(
                CatalogueEntry(
                    scheme_code=code,
                    instrument_kind=InstrumentKind(kind),
                    exposure=Exposure(exposure),
                    category=category,
                    scheme_name=name,
                    amc_name=amc,
                    plan=plan,
                    option=option,
                )
            )
        return out


def _exchange_evidence(conn: Any, isins: Sequence[str]) -> set[str]:
    """ISINs an exchange bhavcopy has priced, ever. Positive evidence only."""
    if not isins:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i FROM unnest(%s::text[]) AS i
               WHERE EXISTS (SELECT 1 FROM price p WHERE p.isin = i AND p.source = ANY(%s))""",
            (list(isins), list(EXCHANGE_SOURCES)),
        )
        return {row[0] for row in cur.fetchall()}


def valuation_routes(conn: Any, isins: Iterable[str]) -> dict[str, RouteDecision]:
    """The route for every ISIN asked about, decided in one batch."""
    wanted = sorted(set(isins))
    if not wanted:
        return {}
    catalogue = catalogue_entries(conn, wanted)
    evidence = _exchange_evidence(
        conn, [i for i in wanted if i.startswith("INF") and i not in catalogue]
    )
    return {isin: decide_route(isin, catalogue.get(isin, []), isin in evidence) for isin in wanted}


def isins_on_route(conn: Any, isins: Iterable[str], route: Route) -> list[str]:
    routes = valuation_routes(conn, isins)
    return sorted(isin for isin, decision in routes.items() if decision.route == route)


def exchange_isins(conn: Any, isins: Iterable[str]) -> list[str]:
    """The subset an exchange bhavcopy may answer for -- price it, or record it absent."""
    return isins_on_route(conn, isins, Route.EXCHANGE)


def non_exchange_isins(conn: Any, isins: Iterable[str]) -> list[str]:
    routes = valuation_routes(conn, isins)
    return sorted(isin for isin, d in routes.items() if d.route != Route.EXCHANGE)


# --- NAV reads ------------------------------------------------------------------------------


@dataclass(frozen=True)
class NavQuote:
    isin: str
    scheme_code: int
    nav: Decimal
    nav_date: date


def latest_fund_navs(
    conn: Any, isins: Sequence[str], as_of: date, max_age_days: int = MAX_NAV_AGE_DAYS
) -> dict[str, NavQuote]:
    """Newest NAV per ISIN dated on or before ``as_of`` and no older than the policy.

    Relative to ``as_of``, never today's clock, so a historical replay never sees a NAV
    published after its date. An ISIN mapped to several schemes is omitted (rule 1 of
    ``app.prices.routing``); so is a stale one -- the caller names it unpriced.
    """
    if not isins or not fund_tables_present(conn):
        return {}
    floor = as_of - timedelta(days=max_age_days)
    with conn.cursor() as cur:
        cur.execute(
            """WITH single AS (
                   SELECT isin, min(scheme_code) AS scheme_code FROM amfi_scheme_isin
                   WHERE isin = ANY(%s) GROUP BY isin HAVING count(DISTINCT scheme_code) = 1
               )
               SELECT s.isin, s.scheme_code, n.nav, n.nav_date
               FROM single s CROSS JOIN LATERAL (
                   SELECT nav, nav_date FROM fund_nav f
                   WHERE f.scheme_code = s.scheme_code
                     AND f.nav_date <= %s AND f.nav_date >= %s
                   ORDER BY f.nav_date DESC LIMIT 1
               ) n""",
            (list(isins), as_of, floor),
        )
        return {
            isin: NavQuote(isin, code, nav, nav_date)
            for isin, code, nav, nav_date in cur.fetchall()
        }


def fund_nav_dates(
    conn: Any, isins: Sequence[str], start: date, end: date
) -> dict[str, list[date]]:
    """Every NAV date per ISIN in ``[start, end]`` -- the fund series' own calendar."""
    if not isins or not fund_tables_present(conn):
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i.isin, n.nav_date FROM amfi_scheme_isin i
               JOIN fund_nav n USING (scheme_code)
               WHERE i.isin = ANY(%s) AND n.nav_date BETWEEN %s AND %s
               GROUP BY i.isin, n.nav_date ORDER BY i.isin, n.nav_date""",
            (list(isins), start, end),
        )
        out: dict[str, list[date]] = {}
        for isin, on in cur.fetchall():
            out.setdefault(isin, []).append(on)
        return out


def nav_history(conn: Any, isin: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Observed NAVs for one ISIN in ``[start, end]``, ascending -- never a carried value.

    Empty for an ISIN AMFI prints against several schemes, for the same reason
    ``latest_fund_navs`` omits it: which scheme's history is "this fund" is unresolved.
    """
    if not fund_tables_present(conn):
        return []
    with conn.cursor() as cur:
        cur.execute(
            """WITH single AS (
                   SELECT min(scheme_code) AS scheme_code FROM amfi_scheme_isin
                   WHERE isin = %s HAVING count(DISTINCT scheme_code) = 1
               )
               SELECT n.nav_date, n.nav FROM single s
               JOIN fund_nav n ON n.scheme_code = s.scheme_code
               WHERE n.nav_date BETWEEN %s AND %s ORDER BY n.nav_date""",
            (isin, start, end),
        )
        return [(on, nav) for on, nav in cur.fetchall()]


def has_nav_after(conn: Any, isins: Sequence[str], on: date) -> set[str]:
    """ISINs with a NAV dated strictly after ``on``: evidence that AMFI has moved past it."""
    if not isins or not fund_tables_present(conn):
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT i.isin FROM amfi_scheme_isin i JOIN fund_nav n USING (scheme_code)
               WHERE i.isin = ANY(%s) AND n.nav_date > %s""",
            (list(isins), on),
        )
        return {row[0] for row in cur.fetchall()}


# --- relevance ----------------------------------------------------------------------------


def relevant_isins(conn: Any) -> set[str]:
    """Fund ISINs whose NAVs we keep: anything in any ledger (sold positions included --
    history needs them) and anything a CAS declared (reconciliation needs them)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT isin FROM transaction WHERE isin LIKE 'INF%%' "
            "UNION SELECT DISTINCT isin FROM holding_snapshot WHERE isin LIKE 'INF%%'"
        )
        return {row[0] for row in cur.fetchall()}


def scheme_codes_for(conn: Any, isins: Iterable[str]) -> set[int]:
    isins = list(isins)
    if not isins:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT scheme_code FROM amfi_scheme_isin WHERE isin = ANY(%s)", (isins,)
        )
        return {row[0] for row in cur.fetchall()}


# --- publishing ------------------------------------------------------------------------------


@dataclass
class PublishResult:
    batch_id: int
    status: str
    reason: str | None = None
    schemes: int = 0
    navs_published: int = 0
    navs_revised: int = 0
    #: (scheme_code, nav_date) whose stored NAV is new or changed -- drives repair.
    changed: list[tuple[int, date]] = field(default_factory=list)


def record_failed_batch(
    conn: Any,
    layout: Layout,
    url: str,
    reason: str,
    *,
    amc_id: int | None = None,
    window: tuple[date, date] | None = None,
    elapsed_ms: int | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO amfi_batch (layout, source_url, parser_version, amc_id, window_start,
                   window_end, status, reason, elapsed_ms)
               VALUES (%s,%s,%s,%s,%s,%s,'FAILED',%s,%s) RETURNING id""",
            (
                str(layout),
                url,
                PARSER_VERSION,
                amc_id,
                window[0] if window else None,
                window[1] if window else None,
                reason[:1000],
                elapsed_ms,
            ),
        )
        return cur.fetchone()[0]


def previous_daily_rows(conn: Any) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rows_parsed FROM amfi_batch WHERE layout='DAILY' AND status='PUBLISHED' "
            "ORDER BY fetched_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row[0] if row else None


#: Plan section 3.2: a daily file this much smaller than the last published one does not
#: get to rewrite the catalogue. A starting guardrail, to be tuned from observed batches.
CATALOGUE_DROP_LIMIT = Decimal("0.20")


def publish(
    conn: Any,
    parsed: ParsedFile,
    url: str,
    payload_bytes: int,
    *,
    amc_id: int | None = None,
    window: tuple[date, date] | None = None,
    elapsed_ms: int | None = None,
    allow_catalogue_drop: bool = False,
) -> PublishResult:
    """Record the batch and publish what passed, in the caller's transaction.

    * Conflicting NAVs for one scheme/date inside the file fail promotion outright.
    * A >20% smaller daily file keeps the previous catalogue (QUARANTINED) unless an
      operator explicitly allows it; its NAV observations are still individually valid
      and are published.
    * NAVs are stored only for schemes relevant to some investor. A later different NAV
      for a stored scheme/date is a correction: the old one moves to fund_nav_revision.
    """
    status, reason = "PUBLISHED", None
    write_catalogue = True
    if parsed.conflicts:
        status = "QUARANTINED"
        reason = f"{len(parsed.conflicts)} scheme/date pairs printed with two different NAVs"
    elif parsed.layout == Layout.DAILY and not allow_catalogue_drop:
        previous = previous_daily_rows(conn)
        if previous and Decimal(parsed.rows) < Decimal(previous) * (1 - CATALOGUE_DROP_LIMIT):
            status, write_catalogue = "QUARANTINED", False
            reason = (
                f"daily file has {parsed.rows} rows, previous published had {previous}: "
                "catalogue kept, NAVs published"
            )

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO amfi_batch (layout, source_url, payload_sha256, bytes, parser_version,
                   amc_id, window_start, window_end, rows_parsed, rows_rejected,
                   rows_unavailable, conflicts, schemes_seen, status, reason, elapsed_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                str(parsed.layout),
                url,
                parsed.sha256,
                payload_bytes,
                PARSER_VERSION,
                amc_id,
                window[0] if window else None,
                window[1] if window else None,
                parsed.rows,
                len(parsed.rejects),
                parsed.unavailable,
                len(parsed.conflicts),
                len(parsed.schemes),
                status,
                reason,
                elapsed_ms,
            ),
        )
        batch_id = cur.fetchone()[0]
    result = PublishResult(batch_id=batch_id, status=status, reason=reason)
    if parsed.conflicts:
        return result

    relevant = relevant_isins(conn)
    # Schemes already in the catalogue keep receiving metadata even when the catalogue
    # is held back; a scheme new to us in a quarantined file waits for a healthy one.
    known = _known_scheme_codes(conn, list(parsed.schemes))
    schemes = [s for s in parsed.schemes.values() if write_catalogue or s.scheme_code in known]
    _upsert_schemes(conn, schemes, batch_id)
    result.schemes = len(schemes)

    stored_codes = known | {s.scheme_code for s in schemes}
    relevant_codes = {
        s.scheme_code
        for s in parsed.schemes.values()
        if s.scheme_code in stored_codes and ({s.isin_growth, s.isin_reinvest} & relevant)
    }
    # Category observations are kept for relevant schemes: they are what dated fund-tax
    # facts are derived from (app.engine.fund_tax_category).
    _upsert_headings(conn, parsed, relevant_codes)
    navs = [o for o in parsed.navs.values() if o.scheme_code in relevant_codes]
    published, revised, changed = _upsert_navs(conn, navs, batch_id)
    result.navs_published, result.navs_revised, result.changed = published, revised, changed

    rejects = [r for r in parsed.rejects if r.scheme_code in relevant_codes]
    if rejects:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO amfi_reject (batch_id, line_no, scheme_code, reason, raw) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(batch_id, r.line_no, r.scheme_code, r.reason, r.raw[:500]) for r in rejects],
            )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE amfi_batch SET navs_published=%s, navs_revised=%s, published_at=now() "
            "WHERE id=%s",
            (published, revised, batch_id),
        )
    return result


def _known_scheme_codes(conn: Any, codes: list[int]) -> set[int]:
    if not codes:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT scheme_code FROM amfi_scheme WHERE scheme_code = ANY(%s)", (codes,))
        return {row[0] for row in cur.fetchall()}


def _upsert_schemes(conn: Any, schemes: list[Any], batch_id: int) -> None:
    """Bulk upsert through a temp table: ~14,000 rows a day must not be 14,000 round
    trips from a serverless function. Metadata only moves forward in NAV date."""
    if not schemes:
        return
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE IF NOT EXISTS _amfi_scheme_in (
                   scheme_code integer, amc_name text, scheme_name text, plan text,
                   option text, heading text, category text, instrument_kind text,
                   exposure text, seen_on date, isin_growth text, isin_reinvest text
               ) ON COMMIT DROP"""
        )
        cur.execute("TRUNCATE _amfi_scheme_in")
        with cur.copy("COPY _amfi_scheme_in FROM STDIN") as copy:
            for s in schemes:
                copy.write_row(
                    (
                        s.scheme_code,
                        s.amc_name,
                        s.scheme_name,
                        s.plan,
                        s.option,
                        s.heading,
                        s.category.display,
                        str(s.category.kind),
                        str(s.category.exposure),
                        s.seen_on,
                        s.isin_growth,
                        s.isin_reinvest,
                    )
                )
        cur.execute(
            """INSERT INTO amfi_scheme (scheme_code, amc_name, scheme_name, plan, option,
                   heading, category, instrument_kind, exposure, first_seen_on, last_seen_on,
                   first_batch_id, last_batch_id)
               SELECT scheme_code, amc_name, scheme_name, plan, option, heading, category,
                      instrument_kind, exposure, seen_on, seen_on, %(b)s, %(b)s
               FROM _amfi_scheme_in
               ON CONFLICT (scheme_code) DO UPDATE SET
                   amc_name = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.amc_name ELSE amfi_scheme.amc_name END,
                   scheme_name = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.scheme_name ELSE amfi_scheme.scheme_name END,
                   plan = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.plan ELSE amfi_scheme.plan END,
                   option = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.option ELSE amfi_scheme.option END,
                   heading = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.heading ELSE amfi_scheme.heading END,
                   category = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.category ELSE amfi_scheme.category END,
                   instrument_kind = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.instrument_kind
                                   ELSE amfi_scheme.instrument_kind END,
                   exposure = CASE WHEN EXCLUDED.last_seen_on >= amfi_scheme.last_seen_on
                                   THEN EXCLUDED.exposure ELSE amfi_scheme.exposure END,
                   first_seen_on = least(amfi_scheme.first_seen_on, EXCLUDED.first_seen_on),
                   last_seen_on = greatest(amfi_scheme.last_seen_on, EXCLUDED.last_seen_on),
                   last_batch_id = EXCLUDED.last_batch_id""",
            {"b": batch_id},
        )
        cur.execute(
            """INSERT INTO amfi_scheme_isin (scheme_code, isin, role, first_seen_on,
                   last_seen_on, first_batch_id, last_batch_id)
               SELECT scheme_code, isin, role, seen_on, seen_on, %(b)s, %(b)s FROM (
                   SELECT scheme_code, isin_growth AS isin, 'GROWTH_OR_PAYOUT' AS role, seen_on
                   FROM _amfi_scheme_in WHERE isin_growth IS NOT NULL
                   UNION ALL
                   SELECT scheme_code, isin_reinvest, 'REINVESTMENT', seen_on
                   FROM _amfi_scheme_in WHERE isin_reinvest IS NOT NULL
               ) x
               ON CONFLICT (scheme_code, isin, role) DO UPDATE SET
                   first_seen_on = least(amfi_scheme_isin.first_seen_on, EXCLUDED.first_seen_on),
                   last_seen_on = greatest(amfi_scheme_isin.last_seen_on, EXCLUDED.last_seen_on),
                   last_batch_id = EXCLUDED.last_batch_id""",
            {"b": batch_id},
        )


def _upsert_headings(conn: Any, parsed: ParsedFile, codes: set[int]) -> None:
    """Widen each scheme's observed interval under its heading by this file's NAV dates."""
    from app.prices.fund_category import normalize_heading

    span: dict[int, list[date]] = {}
    for code, on in parsed.navs:
        if code in codes:
            span.setdefault(code, []).append(on)
    rows = []
    for code in codes:
        record = parsed.schemes[code]
        normalized = normalize_heading(record.heading)
        if normalized is None:
            continue
        dates = span.get(code, []) + [record.seen_on]
        rows.append((code, normalized[1], normalized[0], min(dates), max(dates)))
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO amfi_scheme_heading (scheme_code, heading_key, structure,
                   first_seen_on, last_seen_on) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (scheme_code, heading_key) DO UPDATE SET
                   first_seen_on = least(amfi_scheme_heading.first_seen_on,
                                         EXCLUDED.first_seen_on),
                   last_seen_on = greatest(amfi_scheme_heading.last_seen_on,
                                           EXCLUDED.last_seen_on)""",
            rows,
        )


def _upsert_navs(
    conn: Any, navs: list[Any], batch_id: int
) -> tuple[int, int, list[tuple[int, date]]]:
    if not navs:
        return 0, 0, []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT scheme_code, nav_date, nav, revision, batch_id FROM fund_nav
               WHERE (scheme_code, nav_date) IN (SELECT * FROM unnest(%s::int[], %s::date[]))""",
            ([n.scheme_code for n in navs], [n.nav_date for n in navs]),
        )
        stored = {(c, d): (nav, rev, b) for c, d, nav, rev, b in cur.fetchall()}
        fresh, revised, changed = [], [], []
        for n in navs:
            existing = stored.get((n.scheme_code, n.nav_date))
            if existing is None:
                fresh.append(n)
                changed.append((n.scheme_code, n.nav_date))
            elif existing[0] != n.nav:
                revised.append((n, existing))
                changed.append((n.scheme_code, n.nav_date))
        if fresh:
            cur.executemany(
                "INSERT INTO fund_nav (scheme_code, nav_date, nav, batch_id) VALUES (%s,%s,%s,%s)",
                [(n.scheme_code, n.nav_date, n.nav, batch_id) for n in fresh],
            )
        for n, (old_nav, old_rev, old_batch) in revised:
            cur.execute(
                """INSERT INTO fund_nav_revision (scheme_code, nav_date, revision, nav, batch_id,
                       superseded_by) VALUES (%s,%s,%s,%s,%s,%s)""",
                (n.scheme_code, n.nav_date, old_rev, old_nav, old_batch, batch_id),
            )
            cur.execute(
                """UPDATE fund_nav SET nav=%s, batch_id=%s, revision=revision+1, observed_at=now()
                   WHERE scheme_code=%s AND nav_date=%s""",
                (n.nav, batch_id, n.scheme_code, n.nav_date),
            )
            logger.warning(
                "fund_nav_revised",
                extra={
                    "scheme_code": n.scheme_code,
                    "nav_date": n.nav_date.isoformat(),
                    "old": str(old_nav),
                    "new": str(n.nav),
                },
            )
    return len(fresh), len(revised), changed


# --- repair: dirty markers, route changes, poisoned absences -------------------------------


def investors_holding(conn: Any, isins: Iterable[str]) -> dict[str, date]:
    """Investor -> first transaction date in any of ``isins``."""
    isins = list(isins)
    if not isins:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.investor_id::text, min(t.txn_date) FROM transaction t
               JOIN account a ON a.id = t.account_id WHERE t.isin = ANY(%s)
               GROUP BY a.investor_id""",
            (isins,),
        )
        return dict(cur.fetchall())


def mark_fund_dates_dirty(conn: Any, changed: Sequence[tuple[int, date]], reason: str) -> int:
    """A new or corrected NAV on D changes every fund snapshot that valued that scheme on
    D or carried D's NAV forward (up to the 7-day policy), and makes an earlier provisional
    row in the week before D final. Mark them all; the catch-up rebuilds marked dates."""
    if not changed or not fund_tables_present(conn):
        return 0
    by_scheme: dict[int, list[date]] = {}
    for code, on in changed:
        by_scheme.setdefault(code, []).append(on)
    marked = 0
    with conn.cursor() as cur:
        for code, dates in by_scheme.items():
            # Only mutual funds have a fund series. An ETF's NAV is stored as reference
            # data; marking its holders would leave markers nothing ever clears.
            cur.execute(
                "SELECT i.isin FROM amfi_scheme_isin i JOIN amfi_scheme s USING (scheme_code) "
                "WHERE i.scheme_code=%s AND s.instrument_kind='MUTUAL_FUND'",
                (code,),
            )
            isins = [row[0] for row in cur.fetchall()]
            holders = investors_holding(conn, isins)
            if not holders:
                continue
            lo = min(dates) - timedelta(days=MAX_NAV_AGE_DAYS)
            hi = max(dates) + timedelta(days=MAX_NAV_AGE_DAYS)
            for investor_id in holders:
                # The changed dates themselves (a new calendar date for the series) plus
                # every stored row whose valuation could have used them.
                cur.execute(
                    """INSERT INTO fund_snapshot_dirty (investor_id, as_of, reason, version)
                       SELECT %s, d, %s, nextval('fund_snapshot_dirty_version_seq')
                       FROM (SELECT unnest(%s::date[]) AS d
                             UNION SELECT as_of FROM fund_snapshot
                             WHERE investor_id=%s AND as_of BETWEEN %s AND %s) x
                       ON CONFLICT (investor_id, as_of) DO UPDATE SET
                           reason = EXCLUDED.reason, version = EXCLUDED.version,
                           marked_at = now()""",
                    (investor_id, reason, dates, investor_id, lo, hi),
                )
                marked += cur.rowcount
    return marked


def sync_route_observations(conn: Any, isins: Iterable[str] | None = None) -> list[str]:
    """Record each ledger fund unit's current route; where it CHANGED, drop the cached
    series computed under the old one. Returns the ISINs whose route changed.

    Listed series (``portfolio_snapshot``): rows from the ISIN's first trade onward are
    deleted -- a cache, recomputable from the ledger -- leaving the investor's series
    short, which ``app.routers.analytical._catch_up_start`` already refills. Fund series:
    the investor's ledger fingerprint is cleared, so its whole fund series rebuilds.
    """
    if not fund_tables_present(conn):
        return []
    candidates = sorted(set(isins) if isins is not None else relevant_isins(conn))
    if not candidates:
        return []
    routes = valuation_routes(conn, candidates)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, route FROM security_route_observation WHERE isin = ANY(%s)",
            (candidates,),
        )
        before = dict(cur.fetchall())
        changed = []
        for isin, decision in routes.items():
            # Before this table existed every ISIN was valued on the exchange route, so
            # the snapshots already stored were computed under EXCHANGE.
            previous = before.get(isin, str(Route.EXCHANGE))
            if isin in before and previous == str(decision.route):
                continue
            cur.execute(
                """INSERT INTO security_route_observation (isin, route, instrument_kind, reason)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (isin) DO UPDATE SET route=EXCLUDED.route,
                   instrument_kind=EXCLUDED.instrument_kind, reason=EXCLUDED.reason,
                   observed_at=now()""",
                (isin, str(decision.route), str(decision.instrument_kind), decision.reason),
            )
            if previous != str(decision.route):
                changed.append(isin)
        for isin in changed:
            for investor_id, first in investors_holding(conn, [isin]).items():
                cur.execute(
                    "DELETE FROM portfolio_snapshot WHERE investor_id=%s AND as_of >= %s",
                    (investor_id, first),
                )
                cur.execute("DELETE FROM fund_snapshot_ledger WHERE investor_id=%s", (investor_id,))
            logger.info(
                "security_route_changed",
                extra={"isin": isin, "from": before.get(isin), "to": str(routes[isin].route)},
            )
    return changed


def repair_fund_absences(conn: Any) -> int:
    """Remove ``price_absent`` rows for POSITIVELY classified NAV-valued funds.

    A bhavcopy not printing an unlisted fund is not evidence it stopped trading, and such
    a row would let ``absent_on_or_before`` exclude the holding. Each removal is audited in
    ``price_absent_fund_repair``. Never cleared by ``INF`` prefix: an ETF's absence is real
    exchange evidence and is kept; an unresolved unit's is ignored by readers, not deleted.
    """
    if not fund_tables_present(conn):
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT isin FROM price_absent WHERE isin LIKE 'INF%%'")
        candidates = [row[0] for row in cur.fetchall()]
    funds = isins_on_route(conn, candidates, Route.AMFI)
    if not funds:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """WITH gone AS (
                   DELETE FROM price_absent WHERE isin = ANY(%s)
                   RETURNING isin, price_date, recorded_at
               )
               INSERT INTO price_absent_fund_repair (isin, price_date, recorded_at, reason)
               SELECT isin, price_date, recorded_at,
                      'AMFI lists this ISIN as a mutual fund: not an exchange absence'
               FROM gone ON CONFLICT DO NOTHING""",
            (funds,),
        )
        removed = cur.rowcount
    if removed:
        for investor_id, first in investors_holding(conn, funds).items():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM portfolio_snapshot WHERE investor_id=%s AND as_of >= %s",
                    (investor_id, first),
                )
        logger.warning("price_absent_fund_repair", extra={"isins": funds, "rows": removed})
    return removed
