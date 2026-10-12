"""Early notification for demerger/merger/amalgamation/capital-reduction NSE notices
touching a tracked symbol -- NOT an alias source, NOT a corporate action, NOT a queue.

WHY THIS EXISTS, AND WHY IT IS DELIBERATELY THIN. ``app.ingest.corporate_action_feed``'s
own parser already SEES these notices in the same JSON payload it fetches every run --
and correctly refuses to classify them (``_DISQUALIFYING_TERMS``: "a qualifier changes
what the ratio means"). That refusal is right: a scheme of arrangement's swap ratio and
cost apportionment vary per scheme and must never be guessed. But refusing to classify a
notice and refusing to tell anyone about it are two different things, and today this
system does the second by accident -- a disqualified row is simply dropped on the floor,
so the gap between "the company filed the apportionment letter" and "a human knows to go
read it" depends entirely on someone noticing on their own.

This module closes that gap with the smallest feature that does it: re-read the SAME
payload ``app.ingest.corporate_action_feed`` already fetches, pull out the rows THAT
parser correctly discards, and if one names a symbol this system tracks, log it. Nothing
more.

WHAT THIS IS NOT, three times over, because each is a real thing a similar feature could
easily drift into and none of them are wanted here:
  - NOT a ``security_alias`` source. It writes no row anywhere. An identity claim still
    needs real evidence, exactly as migration 027 requires.
  - NOT ``app.engine.demerger``. It computes no ratio, no cost split, no swap. Those still
    need a human reading the actual scheme document -- which is the whole point, this
    just gets them there sooner.
  - NOT a queue. There is no ``isin_change_circular``-shaped table here and no review
    command reads from one. The notification IS the log line. If a persisted,
    reviewable queue is ever wanted for this, that is a separate, later decision --
    the smaller feature this module implements does not default to it.

REUSES ``app.ingest.corporate_action_feed``'s ALREADY-ACCEPTED NSE session/fetch
machinery (``nse_corporate_action_feed``, ``CorporateActionFeedFetchFailed``) rather than
adding a second, less-vetted third-party dependency of its own -- this feature carries no
new ToS exposure beyond what that module's docstring already accepts. ``_REQUIRED_FIELDS``
and the exDate parsing below are small enough to duplicate locally rather than import
that module's private names across a module boundary neither module otherwise depends on.

NO CIRCUIT BREAKER, NO THROTTLE, ON PURPOSE. One request a day, not the PDF-per-circular
volume Phase 1's feed makes -- a failed fetch logs and returns, the next day's run tries
again, matching ``refresh_corporate_action_feed``'s own "log-and-continue" posture at a
tenth the request rate that posture was written for.

NO DE-DUPLICATION, ON PURPOSE. Overlapping poll windows can log the same notice on two
consecutive days. A human seeing the same warning twice is a minor nuisance, not a
correctness bug -- adding a persisted "already notified" table to prevent it would be the
queue this module deliberately declines to become, for a problem this small.

ON BY DEFAULT since 2026-09-22 (``Settings.demerger_watch_enabled``,
``app/config.py``), where the measurement that justified the default is recorded. The
flag stays, so a deployment that does not want the warnings can still set
``DEMERGER_WATCH_ENABLED=false`` without a code change.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.ingest.corporate_action_feed import (
    CorporateActionFeedFetchFailed,
    CorporateActionFeedFormatError,
    nse_corporate_action_feed,
)

logger = logging.getLogger("demerger_watch")

#: The same four fields every row of NSE's corporates-corporateActions feed has carried
#: so far -- duplicated from corporate_action_feed.py's own _REQUIRED_FIELDS rather than
#: imported, so this module has no dependency on that module's private names.
_REQUIRED_FIELDS = {"isin", "symbol", "exDate", "subject"}

#: The specific subset of corporate_action_feed.py's own _DISQUALIFYING_TERMS that name
#: a demerger/merger/amalgamation/scheme/capital-reduction event -- deliberately
#: narrower than that full list, which also excludes NCRPS, preference shares,
#: debentures, warrants and partly-paid shares for reasons that have nothing to do with
#: a company-level restructuring.
#:
#: "Capital reduction"/"reduction of capital" was added here on the same audit that
#: added it to _DISQUALIFYING_TERMS (confirmed live, two independent 3-year pulls) --
#: unlike that list, widening THIS one is a pure notification-only addition: a capital
#: reduction changing share count/value with no one aware is exactly the failure mode
#: this module exists to shrink, and there is no safety cost to widening a log line.
_DEMERGER_SUBJECT_RE = re.compile(
    r"demerger|amalgamation|scheme of arrangement|\bmerger\b|"
    r"reduction of capital|capital reduction",
    re.IGNORECASE,
)


def _parse_nse_date(raw: str) -> date | None:
    """Duplicated from corporate_action_feed.py's identical helper -- "-" is NSE's own
    routine value for an ex-date not yet fixed, which is expected here too and must not
    raise."""
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class DemergerNotice:
    """One NSE notice this system will never auto-apply -- see module docstring. Kept
    only long enough to log; nothing here persists it."""

    isin: str
    symbol: str
    subject: str
    ex_date: date | None


def find_demerger_notices(payload: bytes) -> list[DemergerNotice]:
    """The same JSON payload ``parse_corporate_action_feed`` reads, filtered to the rows
    THAT parser deliberately discards as disqualified.

    Raises ``CorporateActionFeedFormatError`` on the same conditions that parser raises
    on (not JSON, not an array) -- one format contract for two readers of the same feed.
    A row missing a required field is skipped rather than raised on: that is
    ``parse_corporate_action_feed``'s format guarantee to police, not this pass's;
    duplicating its raise here would mean one malformed row breaks a feature that has
    nothing to do with fixing it.
    """
    try:
        records = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorporateActionFeedFormatError(f"not valid JSON: {exc}") from exc
    if not isinstance(records, list):
        raise CorporateActionFeedFormatError(
            f"expected a JSON array, got {type(records).__name__}"
        )

    notices: list[DemergerNotice] = []
    for record in records:
        if not isinstance(record, dict) or not _REQUIRED_FIELDS <= set(record):
            continue
        isin = str(record["isin"]).strip()
        symbol = str(record["symbol"]).strip()
        subject = str(record["subject"]).strip()
        if not isin or not subject or not _DEMERGER_SUBJECT_RE.search(subject):
            continue
        notices.append(
            DemergerNotice(
                isin=isin, symbol=symbol, subject=subject,
                ex_date=_parse_nse_date(str(record.get("exDate", "")).strip()),
            )
        )
    return notices


def check_demerger_watch(conn: Any, source: Any = None, today: date | None = None) -> int:
    """Fetch a day's worth of NSE corporate-actions notices, log one warning per
    demerger/merger/amalgamation/scheme/capital-reduction notice whose symbol this
    system tracks, and return how many were logged.

    Never raises on a routine fetch or parse failure -- this feature going dark must
    never take anything else down with it, the same posture every other feed in this
    codebase already takes. Callers are expected to check
    ``Settings.demerger_watch_enabled`` before calling this at all; the function itself
    does not gate on it, so a test or a manual run is never blocked by the flag.
    """
    today = today or date.today()
    feed = source or nse_corporate_action_feed()
    try:
        payload = feed.fetch(today - timedelta(days=1), today)
        notices = find_demerger_notices(payload)
    except (CorporateActionFeedFetchFailed, CorporateActionFeedFormatError) as exc:
        logger.warning("demerger watch: feed unavailable, skipping this run: %s", exc)
        return 0

    if not notices:
        return 0

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM security WHERE symbol IS NOT NULL")
        tracked = {row[0] for row in cur.fetchall()}

    logged = 0
    for notice in notices:
        if notice.symbol not in tracked:
            continue
        logger.warning(
            "DEMERGER WATCH: %s (%s) -- %r. NOTIFICATION ONLY: no security_alias row, "
            "no corporate action, nothing auto-applied. Go read the scheme document.",
            notice.symbol, notice.isin, notice.subject,
        )
        logged += 1
    return logged
