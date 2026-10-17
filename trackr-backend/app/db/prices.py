"""Price persistence, and the staleness rule.

Staleness is enforced HERE and not in the engine, deliberately. ``engine/valuation.py``
takes a flat ISIN-to-price mapping with no dates in it and therefore cannot know whether a
price is current. This layer builds that mapping, so this is the only place that can refuse
to put a stale price into it.

Refusing matters: a stale price does not fail, it produces a confidently wrong market value
and a confidently wrong XIRR. Omitting it instead surfaces as ``MissingPriceError`` naming
the ISIN, which is the failure the whole system is designed to make loud.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.engine.holdings import MAX_PRICE_STALENESS_DAYS


def adjusted_price_series(
    conn: Any, isins: Sequence[str], start: date, end: date
) -> dict[str, list[tuple[date, Decimal]]]:
    """Price-return series across evidenced identifiers, adjusted for verified splits and
    bonuses and for exchange-discovered demerger factors (``app.db.price_adjustments``).

    Stored exchange closes remain untouched. This is not a dividend total-return series.
    Ambiguous same-date action records suppress the affected series instead of applying
    an arbitrary adjustment twice.
    """
    from app.db.corporate_actions import list_corporate_actions
    from app.db.security_alias import verified_map
    from app.engine.corporate_actions import CorpActionType, Verification

    if not isins:
        return {}
    aliases = verified_map(conn, end)
    wanted = set(isins)
    expanded = sorted(wanted | {old for old, new in aliases.items() if new in wanted})
    raw = price_series(conn, expanded, start, end)
    events: dict[str, dict[date, Decimal]] = {}
    ambiguous = set()
    for action in list_corporate_actions(conn, expanded, Verification.VERIFIED):
        if action.action_type not in (CorpActionType.SPLIT, CorpActionType.BONUS):
            continue
        if not start < action.ex_date <= end:
            continue
        isin = aliases.get(action.isin, action.isin)
        by_date = events.setdefault(isin, {})
        if action.ex_date in by_date:
            ambiguous.add(isin)
        multiplier = action.multiplier()
        by_date[action.ex_date] = Decimal(multiplier.numerator) / Decimal(multiplier.denominator)
    from app.db.price_adjustments import factors as demerger_factors

    # A demerger keeps ``factor`` of the parent's value: pre-ex closes are multiplied by
    # it, i.e. divided by 1/factor, the same direction as a split's multiplier.
    for source_isin, ex_date, factor in demerger_factors(conn, expanded, start, end):
        isin = aliases.get(source_isin, source_isin)
        by_date = events.setdefault(isin, {})
        if ex_date in by_date:
            ambiguous.add(isin)
        by_date[ex_date] = Decimal(1) / Decimal(factor)
    combined: dict[str, dict[date, Decimal]] = {}
    for source_isin, points in raw.items():
        isin = aliases.get(source_isin, source_isin)
        if isin not in wanted or isin in ambiguous:
            continue
        target = combined.setdefault(isin, {})
        for on, close in points:
            # Overlap between retired/current histories is ambiguous; don't silently
            # select whichever record happens to be returned first.
            factor = Decimal(1)
            for ex_date, multiplier in events.get(isin, {}).items():
                if on < ex_date:
                    factor *= multiplier
            adjusted = close / factor
            if on in target and target[on] != adjusted:
                ambiguous.add(isin)
            target[on] = adjusted
    return {
        isin: sorted(points.items()) for isin, points in combined.items() if isin not in ambiguous
    }


def observed_listing(conn: Any, isins: Sequence[str], since: date) -> dict[str, str]:
    """Where an ISIN demonstrably trades, from which exchange's bhavcopy priced it.

    ``security.exchange`` is NULL for most of the master, and a ticker resolver that
    defaults NULL to NSE sends a BSE-only company (Kalyani Cast-Tech, BSE 544023: 683
    sessions, every one from the BSE bhavcopy) to whatever NSE lists under the same
    symbol -- Kalyani Commercials. Evidence, not a default: 'BSE' only when no NSE
    bhavcopy has priced the ISIN since ``since``; 'NSE' when one has. Other sources
    (live quotes) say nothing about the listing and are ignored.
    """
    if not isins:
        return {}
    rows = conn.execute(
        "SELECT isin, array_agg(DISTINCT source) FROM price "
        "WHERE isin = ANY(%s) AND price_date >= %s "
        "AND source IN ('NSE_BHAVCOPY','BSE_BHAVCOPY') GROUP BY isin",
        (list(isins), since),
    ).fetchall()
    return {isin: "NSE" if "NSE_BHAVCOPY" in sources else "BSE" for isin, sources in rows}


def upsert_prices(conn: Any, prices: list[Any]) -> int:
    """Store merged prices. Idempotent: re-running a day changes nothing.

    ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING`` because a re-run should carry a
    correction through -- exchanges do republish a bhavcopy after a data error, and the
    later file is the better one.
    """
    if not prices:
        return 0
    with conn.cursor() as cur:
        for price in prices:
            cur.execute(
                """
                INSERT INTO price (isin, price_date, close, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (isin, price_date) DO UPDATE
                SET close = EXCLUDED.close, source = EXCLUDED.source
                """,
                (price.isin, price.price_date, price.close, price.source),
            )
    return len(prices)


def latest_prices(
    conn: Any,
    isins: list[str],
    as_of: date,
    max_staleness_days: int = MAX_PRICE_STALENESS_DAYS,
) -> dict[str, Decimal]:
    """Most recent close per ISIN, on or before ``as_of``, within the staleness window.

    An ISIN whose newest price is older than the window is OMITTED rather than returned.
    The caller then gets a ``MissingPriceError`` naming it, which is a fixable, visible
    problem -- unlike a week-old price presented as today's, which is not.
    """
    if not isins:
        return {}
    floor = as_of - timedelta(days=max_staleness_days)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (isin) isin, close
            FROM price
            WHERE isin = ANY(%s) AND price_date <= %s AND price_date >= %s
            ORDER BY isin, price_date DESC
            """,
            (list(isins), as_of, floor),
        )
        result = {row[0]: row[1] for row in cur.fetchall()}
        missing = [isin for isin in isins if isin not in result]
        if missing:
            # Some archives print the retired ISIN on the ex-date (Tata Steel 2022).
            # Use only that exact observed quote, tied to a verified dated evidence
            # record. Never carry a pre-split old-ISIN quote into new share units.
            cur.execute(
                """SELECT e.current_isin,b.close FROM security_alias_evidence e
                   JOIN security_alias a ON a.old_isin=e.old_isin
                       AND a.current_isin=e.current_isin AND a.verification='VERIFIED'
                       AND a.effective_date=e.effective_date
                   JOIN reference_bhavcopy_security b
                       ON b.exchange='NSE' AND b.isin=e.evidence->>'ex_price_isin'
                       AND b.price_date=(e.evidence->'after'->>'date')::date
                       AND b.series=e.evidence->>'series'
                   WHERE e.current_isin=ANY(%s) AND e.effective_date <= %s
                       AND b.price_date BETWEEN %s AND %s""",
                (missing, as_of, floor, as_of),
            )
            observations: dict[str, set[Decimal]] = {}
            for isin, close in cur.fetchall():
                observations.setdefault(isin, set()).add(close)
            result.update(
                {
                    isin: next(iter(values))
                    for isin, values in observations.items()
                    if len(values) == 1
                }
            )
        return result


def latest_price_dates(
    conn: Any,
    isins: list[str],
    as_of: date,
    max_staleness_days: int = MAX_PRICE_STALENESS_DAYS,
) -> dict[str, date]:
    """The date of each ISIN's close that ``latest_prices`` would use.

    Same window and same ordering as ``latest_prices``, so the two always agree on which
    row won. Used to label a holding's quote with its observation date (dashboard audit
    gap: a carried close must not look like today's price). An ISIN priced only through
    the verified alias fallback has no row date here and is omitted.
    """
    if not isins:
        return {}
    floor = as_of - timedelta(days=max_staleness_days)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (isin) isin, price_date
            FROM price
            WHERE isin = ANY(%s) AND price_date <= %s AND price_date >= %s
            ORDER BY isin, price_date DESC
            """,
            (list(isins), as_of, floor),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def price_series(
    conn: Any,
    isins: Sequence[str],
    start: date,
    end: date,
) -> dict[str, list[tuple[date, Decimal]]]:
    """Every stored close per ISIN in ``[start, end]``, ascending by date.

    The analytics layer needs a SERIES, not a point: volatility, beta, correlation and
    12-1 momentum are all functions of a return path. Every other reader in this module
    answers "what is it worth now", which is a different question with a different
    staleness rule -- hence a separate function rather than a parameter on ``latest_prices``.

    ONE bulk range scan, resolved in Python, following the pattern
    ``app.db.index_levels.get_index_levels`` documents: it avoids the round-trip-per-date
    loop that ``app.routers.analytical._run_snapshots`` is forced into, which would be
    ~1,250 queries for a five-year window. The existing ``price_isin_date_desc_idx``
    ``(isin, price_date DESC)`` serves this directly; no new index is needed.

    NO STALENESS WINDOW AND NO CARRY-FORWARD, deliberately. A gap here is information:
    ``price_absent`` says a scrip genuinely did not trade that day, while a date missing
    from both tables was simply never fetched. Filling either one would manufacture a
    zero-return day and quietly understate volatility. The caller aligns the series it
    got and reports coverage on what it did not.

    An ISIN with no rows in the window is absent from the result rather than mapped to an
    empty list -- "we have nothing for this security" and "this security has an empty
    history" are the same fact here, and one representation is enough.
    """
    if not isins or start > end:
        return {}
    out: dict[str, list[tuple[date, Decimal]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT isin, price_date, close
            FROM price
            WHERE isin = ANY(%s) AND price_date BETWEEN %s AND %s
            ORDER BY isin, price_date
            """,
            (list(isins), start, end),
        )
        for isin, price_date, close in cur.fetchall():
            out.setdefault(isin, []).append((price_date, close))
    return out


def prices_on_date(conn: Any, isins: list[str], price_date: date) -> dict[str, Decimal]:
    """Closes stored for EXACTLY ``price_date`` -- no carry-forward, no staleness window.

    ``latest_prices`` deliberately walks back up to ``MAX_PRICE_STALENESS_DAYS`` to find
    the newest usable close, which is right for valuing a portfolio and wrong for asking
    "has tonight's bhavcopy landed yet?". That second question is what decides whether an
    intraday yfinance quote is still needed for today (app.routers.holdings._portfolio),
    and answering it with a carried-forward close would say "settled" on every trading
    morning -- the live quote would then never be reached.

    Same omit-rather-than-substitute contract as everything else in this module: an ISIN
    with no row on that exact date is simply absent.
    """
    if not isins:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin, close FROM price WHERE isin = ANY(%s) AND price_date = %s",
            (list(isins), price_date),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def missing_prices(
    conn: Any,
    isins: list[str],
    as_of: date,
    max_staleness_days: int = MAX_PRICE_STALENESS_DAYS,
) -> list[str]:
    """ISINs with no usable price. Named, so a UI can name them too.

    "Cannot value your portfolio" is unactionable. "No price for KDL-SM since 6 August" is
    something a person can go and fix.
    """
    have = latest_prices(conn, isins, as_of, max_staleness_days)
    return sorted(set(isins) - set(have))


def price_dates_settled(conn: Any, start: date, end: date) -> set[date]:
    """Dates a backfill may skip, because every security it would ask about already has
    an answer -- a stored price, or a recorded absence.

    A three-year backfill is roughly 750 requests. It has to be resumable, or one timeout
    means starting again. This is what makes it resumable WITHOUT also making it blind.

    WHY THIS IS NOT "DATES THAT HAVE ANY PRICE ROW", which is what it used to be. That
    asks whether SOME security has a price on a date, and uses the answer for a different
    question: whether we have fetched that date for the securities we actually care about.
    The two agree only while the ledger never changes. Buy a new stock and every
    historical date already has prices for the older holdings -- so every date looks
    "present", bhavcopy is never re-fetched, and the new holding never gets any history.
    It stays permanently unvaluable, and every snapshot touching it is skipped forever.

    TWO THINGS BOUND THE WORK, and both are needed or the fix is worse than the bug:

    1. **A security is only wanted from its first transaction onward.** Nothing needs a
       price for a stock on a date nobody owned it. Without this clause, one new holding
       would re-open the entire archive -- hundreds of requests to establish nothing.
    2. **A recorded absence counts as an answer** (``price_absent``, migration 022). A
       scrip that legitimately did not trade that day -- suspended, pre-listing, an
       illiquid SME line -- would otherwise leave its dates unsettled and re-fetched on
       every run, forever.

    Dates with no answer of any kind are simply absent from the result, so a range that
    was never fetched comes back empty and the whole of it is fetched, as it should be.

    3. **Only exchange-routed securities are wanted** (docs/MF_ETF_PLAN.md section 4). A
       mutual fund is never in a bhavcopy, and an unresolved fund unit may not be either;
       neither can ever be answered by one -- no price, and no absence may be recorded for
       them -- so wanting them would leave every date unsettled and re-fetched forever.
    """
    from app.db.fund_reference import non_exchange_isins

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT isin FROM transaction")
        off_exchange = non_exchange_isins(conn, [row[0] for row in cur.fetchall()])
        cur.execute(
            """
            WITH wanted AS (
                SELECT isin, min(txn_date) AS first_held FROM transaction
                WHERE isin <> ALL(%s) GROUP BY isin
            ),
            answered AS (
                SELECT DISTINCT price_date FROM price WHERE price_date BETWEEN %s AND %s
                UNION
                SELECT DISTINCT price_date FROM price_absent
                WHERE price_date BETWEEN %s AND %s
            )
            SELECT a.price_date
            FROM answered a
            WHERE NOT EXISTS (
                SELECT 1 FROM wanted w
                WHERE w.first_held <= a.price_date
                  AND NOT EXISTS (
                      SELECT 1 FROM price p
                      WHERE p.isin = w.isin AND p.price_date = a.price_date
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM price_absent x
                      WHERE x.isin = w.isin AND x.price_date = a.price_date
                  )
            )
            """,
            (off_exchange, start, end, start, end),
        )
        return {row[0] for row in cur.fetchall()}


def absent_on_or_before(conn: Any, isins: Sequence[str], as_of: date) -> set[str]:
    """Of ``isins``, the ones the archive has ANSWERED about on or before ``as_of``
    without carrying them -- a CONFIRMED absence, not a price nobody has fetched yet.

    That distinction is the whole point. A holding with no price might mean the pipe has
    not run for that date (fixable: fetch the bhavcopy) or that the security genuinely
    does not trade any more -- a retired ISIN after a reorganisation, say, which no
    future fetch will ever produce. Only the second kind may be excluded from a total,
    and only ``price_absent`` can tell them apart. Treating them alike would either
    block every snapshot forever on a dead ISIN, or quietly drop a live holding whose
    price simply had not been fetched.

    EXCHANGE-ROUTED ISINS ONLY (docs/MF_ETF_PLAN.md section 4). A bhavcopy omitting a mutual
    fund, or a fund unit whose listing is not established, is not evidence it stopped
    trading; such a unit must never be excluded merely because a bhavcopy omitted it.
    """
    from app.db.fund_reference import exchange_isins

    isins = exchange_isins(conn, isins)
    if not isins:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT isin FROM price_absent WHERE isin = ANY(%s) AND price_date <= %s",
            (list(isins), as_of),
        )
        return {row[0] for row in cur.fetchall()}


def record_absent_prices(conn: Any, on_date: date, isins: Sequence[str]) -> int:
    """Remember that these securities were NOT in the bhavcopy we fetched for this date.

    Call this ONLY after a fetch that actually returned data. Recording an absence for a
    day the archive never answered -- a weekend, a holiday, a timed-out request -- would
    mark that date settled forever and permanently lose the prices it should have had,
    which is a far more expensive mistake than fetching it again.

    Idempotent: re-recording an absence leaves the original ``recorded_at`` alone, so a
    repeated run neither fails nor rewrites history.

    Only exchange-routed ISINs are recorded: a bhavcopy can say nothing about a security
    it was never going to carry (docs/MF_ETF_PLAN.md section 4).
    """
    from app.db.fund_reference import exchange_isins

    isins = exchange_isins(conn, isins)
    if not isins:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO price_absent (isin, price_date) VALUES (%s, %s) "
            "ON CONFLICT (isin, price_date) DO NOTHING",
            [(isin, on_date) for isin in isins],
        )
    return len(isins)


def held_isins(conn: Any, investor_id: str | None = None) -> list[str]:
    """Every ISIN in the ledger -- what a backfill needs prices for.

    Every ISIN EVER traded, not only those currently held: a historical snapshot values the
    portfolio as it stood then, which includes positions since closed.
    """
    with conn.cursor() as cur:
        if investor_id:
            cur.execute(
                "SELECT DISTINCT t.isin FROM transaction t "
                "JOIN account a ON a.id = t.account_id WHERE a.investor_id = %s",
                (investor_id,),
            )
        else:
            cur.execute("SELECT DISTINCT isin FROM transaction")
        return sorted(row[0] for row in cur.fetchall())
