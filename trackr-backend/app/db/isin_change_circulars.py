"""Turns NSE "Change in ISIN" circulars into DRAFT ``security_alias`` rows.

WHAT THIS MODULE OWNS THAT ``app.ingest.isin_change_feed`` DOES NOT. That module fetches
and parses only -- no database. This one holds the connection: it checks and updates the
circuit breaker in ``feed_poll_state``, resolves a circular's symbol against ``security``
(the inference the circular itself never states -- see that module's docstring), calls
``app.db.security_alias.add_alias`` to write the actual identity assertion, and records
every circular seen (whether or not it produced an alias) in ``isin_change_circular``.
Same split as ``app.ingest.corporate_action_feed``/``refresh_corporate_action_feed`` and
``parse_dividend_feed``/``app.db.dividend_events.refresh_dividend_feed``.

WHY THE CIRCUIT BREAKER LIVES HERE AND NOT IN THE INGEST MODULE. NSE's circulars API is
confirmed live but unofficial and undocumented, with no stability guarantee --
``app.ingest.isin_change_feed``'s own docstring says so. A circuit breaker that skips a
poll needs to remember across separate weekly cron invocations that a run keeps failing,
and that memory is exactly what a database row is for. ``feed_poll_state`` is generic on
purpose: neither ``corporate_action_feed`` nor ``dividend_events`` has a breaker of its
own today, and either can start using this same table without a further migration.

WHAT NEVER HAPPENS HERE. Nothing this module writes is ever ``VERIFIED`` -- every alias
this feed creates lands ``DRAFT``, inert until a human calls ``verify_alias`` or
``reconcile_after_alias`` (``app.db.security_alias``) actually proves the merged ledger
holds. This module's only judgment call is the symbol join deciding which security an
alias is even ATTEMPTED for; the identity assertion itself, and its promotion, stay
exactly where migration 027 already put them.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from app.db.security_alias import SecurityAlias, SecurityAliasError, add_alias
from app.engine.corporate_actions import Verification
from app.ingest.isin_change_feed import (
    CircularListing,
    IsinChangeCircularFormatError,
    IsinChangeFeedFetchFailed,
    IsinChangePdfExtractionError,
    ParsedCircular,
    extract_pdf_text,
    nse_circular_list_feed,
    parse_circular_listing,
    parse_isin_change_circular,
)

logger = logging.getLogger("isin_change_circulars")

FEED_NAME = "nse_isin_change_circulars"

#: Consecutive failed polls before the breaker opens and a poll is skipped entirely
#: (zero HTTP calls) rather than attempted and caught.
_BREAKER_FAILURE_THRESHOLD = 5

#: How long a tripped breaker stays open. One week: matches this feed's own poll cadence,
#: so a persistent outage costs at most one missed cycle of retrying before trying again.
_BREAKER_COOLDOWN_DAYS = 7


def _get_poll_state(conn: Any, feed: str) -> tuple[int, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT consecutive_failures, breaker_open_until FROM feed_poll_state "
            "WHERE feed = %s",
            (feed,),
        )
        row = cur.fetchone()
    if row is None:
        return 0, None
    return row[0], row[1]


def _record_attempt(conn: Any, feed: str, *, succeeded: bool) -> None:
    """Upsert this poll's outcome. On success, resets the failure count and closes the
    breaker unconditionally -- one success is enough to trust the endpoint again, the
    same "self-correcting" posture ``refresh_corporate_action_feed`` already documents
    for its own retry loop, just applied to the feed's reachability rather than one
    candidate's price evidence."""
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        if succeeded:
            cur.execute(
                """
                INSERT INTO feed_poll_state
                    (feed, consecutive_failures, last_attempt_at, last_success_at,
                     breaker_open_until)
                VALUES (%s, 0, %s, %s, NULL)
                ON CONFLICT (feed) DO UPDATE SET
                    consecutive_failures = 0,
                    last_attempt_at = EXCLUDED.last_attempt_at,
                    last_success_at = EXCLUDED.last_success_at,
                    breaker_open_until = NULL
                """,
                (feed, now, now),
            )
        else:
            cur.execute(
                """
                INSERT INTO feed_poll_state
                    (feed, consecutive_failures, last_attempt_at)
                VALUES (%s, 1, %s)
                ON CONFLICT (feed) DO UPDATE SET
                    consecutive_failures = feed_poll_state.consecutive_failures + 1,
                    last_attempt_at = EXCLUDED.last_attempt_at,
                    breaker_open_until = CASE
                        WHEN feed_poll_state.consecutive_failures + 1 >= %s
                        THEN EXCLUDED.last_attempt_at + interval '%s days'
                        ELSE feed_poll_state.breaker_open_until
                    END
                """,
                (feed, now, _BREAKER_FAILURE_THRESHOLD, _BREAKER_COOLDOWN_DAYS),
            )
    conn.commit()


def _resolve_old_isin(conn: Any, symbol: str, new_isin: str) -> tuple[str | None, str | None]:
    """The symbol join -- the inference NSE's circular never states outright. Returns
    ``(old_isin, skip_reason)``, exactly one of which is set.

    Excludes ``new_isin`` itself from the candidates: a re-poll after the alias already
    exists would otherwise find ``security`` already carrying the new ISIN under this
    symbol (a later sync may have added it) and wrongly treat that as the OLD one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT isin FROM security WHERE upper(symbol) = upper(%s) AND isin != %s",
            (symbol, new_isin),
        )
        rows = cur.fetchall()
    if not rows:
        return None, "symbol_unknown"
    if len(rows) > 1:
        return None, "multiple_isins_for_symbol"
    return rows[0][0], None


def _ensure_new_security_row(conn: Any, old_isin: str, new_isin: str) -> None:
    """Carry the old security's row forward under ``new_isin``.

    ``security_alias.current_isin`` has a ``REFERENCES security(isin)`` (027) -- the
    schema's own way of saying an alias can only point at a security that exists.
    Since ``new_isin`` is, definitionally, one this codebase has never seen before, that
    row has to be created here or ``add_alias`` cannot succeed at all. This is NOT the
    "always create a minimal row" option declined during planning: it only ever fires
    once the symbol join has already found ``old_isin`` -- i.e. for a company already
    tracked -- so a circular for a symbol nobody holds still stops at
    ``skip_reason='symbol_unknown'`` and creates nothing.

    ``ON CONFLICT DO NOTHING``: a later NSDL sync or another alias source may have
    already created this row with better data (name, sector); this must never overwrite
    it, only guarantee it exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO security (isin, symbol, name, exchange, asset_class, sector) "
            "SELECT %s, symbol, name, exchange, asset_class, sector FROM security "
            "WHERE isin = %s "
            "ON CONFLICT (isin) DO NOTHING",
            (new_isin, old_isin),
        )


def _upsert_circular(
    conn: Any,
    listing: CircularListing,
    parsed: ParsedCircular | None,
    *,
    old_isin: str | None,
    alias_created: bool,
    skip_reason: str | None,
    pdf_sha256: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO isin_change_circular
                (circular_number, circular_ref, circular_date, subject_raw, symbol,
                 new_isin, remarks_raw, reason_class, reason_confidence, wef_date,
                 ex_date, source_url, old_isin, alias_created, skip_reason, pdf_sha256,
                 attempts, next_retry_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    1, now() + interval '1 hour')
            ON CONFLICT (circular_number) DO UPDATE SET
                symbol=EXCLUDED.symbol, new_isin=EXCLUDED.new_isin,
                remarks_raw=EXCLUDED.remarks_raw, ex_date=EXCLUDED.ex_date,
                wef_date=EXCLUDED.wef_date, source_url=EXCLUDED.source_url,
                old_isin=EXCLUDED.old_isin, alias_created=EXCLUDED.alias_created,
                skip_reason=EXCLUDED.skip_reason, pdf_sha256=EXCLUDED.pdf_sha256,
                attempts=isin_change_circular.attempts+1,
                next_retry_at=EXCLUDED.next_retry_at
            WHERE NOT isin_change_circular.alias_created
            """,
            (
                listing.circular_number,
                listing.circular_ref,
                listing.circular_date,
                listing.subject,
                parsed.symbol if parsed else None,
                parsed.new_isin if parsed else None,
                parsed.remarks_raw if parsed else None,
                parsed.reason_class if parsed else None,
                parsed.reason_confidence if parsed else None,
                parsed.wef_date if parsed else None,
                parsed.ex_date if parsed else None,
                listing.pdf_url,
                old_isin,
                alias_created,
                skip_reason,
                pdf_sha256,
            ),
        )
    conn.commit()


def refresh_isin_change_feed(
    conn: Any,
    start: date,
    end: date,
    source: Any | None = None,
    actor_uid: str | None = None,
    max_documents: int = 20,
) -> dict[str, Any]:
    """Poll NSE's "Change in ISIN" circulars for ``[start, end]`` and write a DRAFT
    ``security_alias`` row for every one whose symbol resolves to exactly one known
    security. Never raises -- a fetch, parse, or PDF-extraction failure is logged and
    counted toward the circuit breaker, exactly the log-and-continue posture
    ``refresh_corporate_action_feed`` already documents for its own feed.

    ``isin_change_circular.reason_class``/``reason_confidence`` are written NULL. A
    model-backed classifier of the circular's Remarks text used to fill them; it was
    removed, and nothing downstream ever consulted it -- an alias has always rested on
    the symbol join and ``add_alias``'s structural gates, never on a classification.
    """
    failures_before, breaker_open_until = _get_poll_state(conn, FEED_NAME)
    now = datetime.now(UTC)
    if breaker_open_until is not None and breaker_open_until > now:
        logger.warning(
            "isin-change feed breaker open until %s (%d consecutive failures) -- "
            "skipping poll, no request attempted",
            breaker_open_until, failures_before,
        )
        return {
            "fetched": 0, "aliases_created": 0, "skipped": 0,
            "breaker_open": True, "error": None,
        }

    feed = source or nse_circular_list_feed()
    try:
        payload = feed.list_circulars(start, end)
        listings = parse_circular_listing(payload)
    except (IsinChangeFeedFetchFailed, IsinChangeCircularFormatError) as exc:
        logger.warning("isin-change circulars list failed for %s..%s: %s", start, end, exc)
        _record_attempt(conn, FEED_NAME, succeeded=False)
        return {
            "fetched": 0, "aliases_created": 0, "skipped": 0,
            "breaker_open": False, "error": str(exc),
        }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT circular_number FROM isin_change_circular "
            "WHERE alias_created OR next_retry_at > now()"
        )
        already_seen = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT circular_number, circular_ref, circular_date, subject_raw, source_url "
            "FROM isin_change_circular WHERE NOT alias_created "
            "AND (next_retry_at IS NULL OR next_retry_at <= now()) ORDER BY fetched_at"
        )
        retry_listings = [CircularListing(*row) for row in cur.fetchall()]
    by_number = {row.circular_number: row for row in retry_listings + listings}
    eligible = [r for r in by_number.values() if r.circular_number not in already_seen]
    new_listings = eligible[:max_documents]

    aliases_created = 0
    skipped = 0
    for listing in new_listings:
        try:
            pdf_bytes = feed.fetch_pdf(listing.pdf_url)
            pdf_text = extract_pdf_text(pdf_bytes)
            parsed = parse_isin_change_circular(pdf_text)
        except (
            IsinChangeFeedFetchFailed,
            IsinChangeCircularFormatError,
            IsinChangePdfExtractionError,
        ) as exc:
            # One circular's own failure does not abort the run -- recorded with no
            # parsed fields so it still shows up in the review queue as unresolved,
            # same "log-and-continue, this row lands in the exception pile" posture
            # refresh_corporate_action_feed already applies per-row.
            logger.warning(
                "isin-change circular %s could not be parsed: %s",
                listing.circular_number, exc,
            )
            try:
                _upsert_circular(
                    conn, listing, None, old_isin=None, alias_created=False,
                    skip_reason=f"parse_failed: {exc}",
                )
            except psycopg.errors.UniqueViolation:
                conn.rollback()
            skipped += 1
            continue

        old_isin, skip_reason = _resolve_old_isin(conn, parsed.symbol, parsed.new_isin)
        alias_created = False
        if old_isin is not None:
            try:
                # A SAVEPOINT, not a bare pair of statements: if add_alias's structural
                # gates reject the pair, _ensure_new_security_row's insert must be undone
                # with it -- otherwise a cross-issuer or cyclic pair still leaves behind
                # a `security` row for the new ISIN, wrongly carrying the OLD security's
                # symbol, even though no identity link was ever established.
                with conn.transaction():
                    _ensure_new_security_row(conn, old_isin, parsed.new_isin)
                    add_alias(
                        conn,
                        SecurityAlias(
                            old_isin=old_isin,
                            current_isin=parsed.new_isin,
                            effective_date=parsed.ex_date,
                            source=f"NSE circular {listing.circular_ref}",
                            source_url=listing.pdf_url,
                            verification=Verification.DRAFT,
                            notes=parsed.remarks_raw,
                        ),
                        actor_uid=actor_uid,
                    )
                alias_created = True
            except SecurityAliasError as exc:
                # The structural gates in 027 still have the final word -- a self-alias,
                # cross-issuer pair, or cycle refuses here exactly as it would for a
                # hand-entered alias, and the refusal is what skip_reason records. The
                # savepoint above has already undone both statements.
                skip_reason = str(exc)
            except psycopg.errors.UniqueViolation:
                # Already on file (an earlier run's poll, or a hand-entered alias) --
                # benign and expected on a re-fetched or overlapping circular.
                skip_reason = "alias_already_exists"

        try:
            _upsert_circular(
                conn, listing, parsed, old_isin=old_isin,
                alias_created=alias_created, skip_reason=skip_reason,
                pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            )
        except psycopg.errors.UniqueViolation:
            conn.rollback()

        if alias_created:
            aliases_created += 1
        else:
            skipped += 1

    _record_attempt(conn, FEED_NAME, succeeded=True)
    result = {
        "fetched": len(new_listings),
        "aliases_created": aliases_created,
        "skipped": skipped,
        "breaker_open": False,
        "error": None,
    }
    if len(eligible) > len(new_listings):
        result["remaining"] = len(eligible) - len(new_listings)
    return result
