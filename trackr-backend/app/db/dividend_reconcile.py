"""Second pass over the dividends the first pass left DRAFT -- and only those.

``refresh_dividend_feed`` corroborates a yfinance declaration when NSE's corporate-actions
feed reports the same (isin, ex_date). It cannot when NSE files the dividend under a
STALE ISIN: NSE keeps a company's pre-split identifier on corporate-action rows long after
the security trades under a new one (CAPLIPOINT is INE475E01018 on the feed, INE475E01026
in the market). Measured 2026-09-29 on the real YSL473 ledger: 16 of the 20 dividends
that applied to a holding and stayed DRAFT were exactly this, and every one was on the
NSE feed with the same date and amount.

WHAT THIS DOES, per leftover event:
  1. Leftovers only: ``DRAFT`` events on a security the ledger holds at or before the
     ex-date. VERIFIED rows are done, DISPUTED rows are a human's (or a real conflict's)
     and are never revisited, and an event nobody was eligible for is not worth a fetch.
  2. Find NSE dividend rows on the SAME ex-date whose ISIN shares the first nine
     characters with the event's security (issuer + security class; the last three are
     the issue number and check digit, which is what a face-value change replaces).
  3. PROVE the identity with NSE's own dated record, never by resemblance: the NSE
     bhavcopy for the ex-date session must list the row's symbol under exactly one ISIN,
     and that ISIN must be the event's (alias-resolved) security.
  4. Corroborate through ``corroborate_dividend_event`` -- the same gate the first pass
     uses, so an amount that disagrees becomes DISPUTED rather than VERIFIED.

Anything that does not meet every condition is left DRAFT and counted, not guessed at.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from typing import Any

from app.db.dividend_events import _NSE_SOURCE_NAME as NSE_FEED_SOURCE
from app.db.dividend_events import corroborate_dividend_event, list_dividend_events
from app.db.security_alias import verified_map
from app.engine.corporate_actions import Verification

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "NSE corporate actions, ISIN via NSE bhavcopy"
_SESSION_LOOKBACK_DAYS = 5


def _pending(conn: Any) -> list[Any]:
    """DRAFT events on a security the ledger held at or before the ex-date (alias-aware)."""
    aliases = verified_map(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT isin, min(txn_date) FROM transaction GROUP BY isin")
        first_seen: dict[str, date] = {}
        for isin, first in cur.fetchall():
            terminal = aliases.get(isin, isin)
            first_seen[terminal] = min(first, first_seen.get(terminal, first))
    events = list_dividend_events(conn, verification=Verification.DRAFT)
    return [
        e
        for e in events
        if (first := first_seen.get(aliases.get(e.isin, e.isin))) is not None and first <= e.ex_date
    ]


def _session(conn: Any, on: date, bhav_source: Any) -> tuple[list[tuple], str] | None:
    """The NSE bhavcopy rows and receipt URL for the ex-date, or the nearest earlier one."""
    from app.db.identity_evidence import _day
    from app.prices.sources import NotPublished

    for back in range(_SESSION_LOOKBACK_DAYS + 1):
        try:
            rows, receipt = _day(conn, on - timedelta(days=back), bhav_source, "NSE")
            return rows, receipt["url"]
        except NotPublished:
            continue
    return None


def reconcile_pending_dividends(
    conn: Any,
    feed_source: Any | None = None,
    bhav_source: Any | None = None,
    max_events: int = 60,
) -> dict[str, int]:
    """Corroborate leftover DRAFT dividends whose NSE row carries a stale ISIN.

    ``max_events`` bounds one run's network work (one NSE feed fetch per month touched,
    one bhavcopy per ex-date). A backlog drains across daily runs; nothing is lost,
    because an unprocessed event simply stays DRAFT and is picked up next time.
    """
    from app.ingest.corporate_action_feed import nse_corporate_action_feed, parse_dividend_feed
    from app.prices.sources import nse_source

    feed = feed_source or nse_corporate_action_feed()
    bhav = bhav_source or nse_source()
    aliases = verified_map(conn)
    counts: Counter[str] = Counter()
    feed_by_month: dict[tuple[int, int], list[Any]] = {}

    pending = sorted(_pending(conn), key=lambda e: e.ex_date, reverse=True)
    counts["pending"] = len(pending)
    for event in pending[:max_events]:
        if event.source == NSE_FEED_SOURCE:
            # NSE's own row cannot be corroborated by NSE's feed read a second time --
            # two reads of one feed are one observation. Its independent twin (usually
            # yfinance under an older ISIN) is what gets corroborated instead.
            counts["nse_sourced"] += 1
            continue
        terminal = aliases.get(event.isin, event.isin)
        month = (event.ex_date.year, event.ex_date.month)
        if month not in feed_by_month:
            start = event.ex_date.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            try:
                feed_by_month[month] = parse_dividend_feed(feed.fetch(start, end))
            except Exception as exc:
                logger.warning("NSE dividend feed failed for %s: %s", month, exc)
                feed_by_month[month] = []
        rows = [
            r
            for r in feed_by_month[month]
            if r.ex_date == event.ex_date and r.isin[:9] in (terminal[:9], event.isin[:9])
        ]
        if not rows:
            counts["no_nse_row"] += 1
            continue
        if len(rows) > 1:
            counts["ambiguous"] += 1  # e.g. interim + special on one date
            continue
        row = rows[0]

        try:
            session = _session(conn, event.ex_date, bhav)
        except Exception as exc:
            logger.warning("bhavcopy unavailable for %s: %s", event.ex_date, exc)
            session = None
        if session is None:
            counts["no_bhavcopy"] += 1
            continue
        bhav_rows, receipt_url = session
        # The ISIN the exchange printed THAT DAY, resolved through verified aliases only:
        # a dividend before a split was paid on the pre-split ISIN that later became
        # the terminal one.
        traded = {r[0] for r in bhav_rows if r[1] == row.symbol}
        if len(traded) != 1 or aliases.get(next(iter(traded)), next(iter(traded))) != terminal:
            counts["identity_unproven"] += 1
            continue

        try:
            outcome = corroborate_dividend_event(
                conn,
                event.id,
                f"{SOURCE_PREFIX} ({row.isin} -> {terminal})",
                receipt_url,
                row.amount_per_share,
            )
            conn.commit()
            counts["verified" if outcome is Verification.VERIFIED else "disputed"] += 1
        except Exception:
            conn.rollback()
            logger.warning("reconcile failed for dividend %s", event.id, exc_info=True)
            counts["failed"] += 1
    return dict(counts)
