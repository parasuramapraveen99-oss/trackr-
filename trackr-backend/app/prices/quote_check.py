"""The evening check: is each held ISIN's quote ticker really that ISIN?

After a bhavcopy day D is stored, the provider's close for each mapped ticker on D is
compared with the ISIN's official close on D. Close enough -> VERIFIED; not -> BLOCKED,
and ``app.prices.live`` never asks for that ticker again until a new listing replaces it.
A wrong mapping is therefore caught the first evening after it appears and never shown
the next morning -- the fix for a cause, where the in-session plausibility guard in
``app.prices.live`` only screens symptoms.

State-driven and idempotent: it checks whatever mapping has not been checked against its
newest official close, so it can run from any job after any bhavcopy store (the reference
worker's cycle, the daily price cron) without depending on which ran first. One batched
provider call per run; never raises.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.db.prices import held_isins
from app.db.quote_mapping import (
    BLOCKED,
    VERIFIED,
    due_for_check,
    record_check,
    record_no_evidence,
)
from app.prices.yfinance_live import closes_by_date, download

logger = logging.getLogger("prices.quote_check")

#: How far a provider's close may sit from the official one and still be the same
#: security. NSE's official close is the last half-hour's weighted average, while a
#: provider may report the last trade, so the two differ by a little on a normal day. A
#: namesake's close differs by a lot (KALYANI.NS 144.99 against 1,115.00).
TOLERANCE = Decimal("0.03")


def verify_quote_mappings(conn: Any) -> dict[str, int]:
    """Check every held ISIN's mapping not yet checked against its newest official close."""
    due = due_for_check(conn, sorted(held_isins(conn)))
    if not due:
        return {"checked": 0, "verified": 0, "blocked": 0, "no_evidence": 0}

    frame = download(sorted({ticker for _, ticker, _, _ in due}), period="5d")
    counts = {"checked": len(due), "verified": 0, "blocked": 0, "no_evidence": 0}
    if frame is None or getattr(frame, "empty", True):
        # The provider did not answer at all: nothing is recorded, so the next run retries.
        counts["no_evidence"] = len(due)
        return counts

    for isin, ticker, on, official in due:
        provider = closes_by_date(frame, ticker).get(on)
        if provider is None or official <= 0:
            # The provider has no close for this ticker on D: the status stands, and the
            # mapping waits for the next official close rather than being asked every run.
            record_no_evidence(conn, isin, on, f"{ticker}: no provider close on {on}")
            counts["no_evidence"] += 1
            continue
        gap = abs(provider / official - 1)
        if gap <= TOLERANCE:
            record_check(conn, isin, on, VERIFIED, provider, official, None)
            counts["verified"] += 1
        else:
            note = f"{ticker} closed {provider} on {on}; official close {official}"
            record_check(conn, isin, on, BLOCKED, provider, official, note)
            counts["blocked"] += 1
            logger.warning("quote_mapping_blocked", extra={"isin": isin, "note": note})
    return counts
