"""DB-layer CRUD for the ``dividend_event`` reference table -- migration 029.

Nothing here writes to ``transaction``, and deliberately there is no function that could.
The corporate-action module has ``apply_known_actions_for_account`` because a bonus is
credited to every holder of record unconditionally, which makes eligibility the only open
question; a dividend's open question is RECEIPT, which no feed behind this table answers.
So this module stops at maintained reference data, and the path to a ``DIVIDEND`` ledger
row stays the existing ``import_batch``/``promote`` flow with a human or a credit advice
in it. The absence of an apply function here is the design, not an omission to fill in
later -- see migration 029's header.

THE ONE NON-OBVIOUS RULE IN THIS MODULE is ``match_unspecified``'s: how a candidate whose
source did not say "interim" or "final" is reconciled against an event already on file
that does. Read that docstring before changing anything about uniqueness.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from time import monotonic
from typing import Any

from app.db import audit
from app.engine.corporate_actions import Verification
from app.engine.dividend_events import DividendEvent, DividendKind, amounts_agree

logger = logging.getLogger("dividend_events")

_SELECT_COLUMNS = (
    "id, isin, ex_date, record_date, payment_date, amount_per_share, kind::text, "
    "source, source_url, corroborated_by, corroborated_url, verification::text, notes"
)


def _row_to_event(row: tuple) -> DividendEvent:
    (
        id_, isin, ex_date, record_date, payment_date, amount_per_share, kind,
        source, source_url, corroborated_by, corroborated_url, verification, notes,
    ) = row
    return DividendEvent(
        id=str(id_),
        isin=isin,
        ex_date=ex_date,
        record_date=record_date,
        payment_date=payment_date,
        amount_per_share=amount_per_share,
        kind=DividendKind(kind),
        source=source,
        source_url=source_url,
        corroborated_by=corroborated_by,
        corroborated_url=corroborated_url,
        verification=Verification(verification),
        notes=notes or "",
    )


class DividendEventDateError(ValueError):
    """A candidate's dates cannot be true together -- a record or payment date before the
    ex-date, which in practice means a misread or transposed field rather than an unusual
    corporate timetable.

    Raised in Python as well as enforced by migration 029's CHECK constraints, so a caller
    that wants to land the candidate in ``Exceptions`` with a readable note can catch this
    instead of taking a ``CheckViolation`` and rolling back a whole statement.
    """


def validate_dates(event: DividendEvent) -> None:
    """Raise ``DividendEventDateError`` if the dates are impossible. Mirrors the table's
    own constraints; see that class's docstring for why both exist."""
    if event.record_date is not None and event.record_date < event.ex_date:
        raise DividendEventDateError(
            f"record date {event.record_date} precedes ex-date {event.ex_date}"
        )
    if event.payment_date is not None and event.payment_date < event.ex_date:
        raise DividendEventDateError(
            f"payment date {event.payment_date} precedes ex-date {event.ex_date}"
        )


def add_dividend_event(
    conn: Any, event: DividendEvent, actor_uid: str | None = None
) -> str:
    """Insert one declared dividend. Raises ``psycopg.errors.UniqueViolation`` on a
    repeat of ``(isin, ex_date, kind)``, which callers treat as benign -- the same posture
    ``refresh_corporate_action_feed`` already takes for a re-fetched notice."""
    validate_dates(event)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dividend_event (
                isin, ex_date, record_date, payment_date, amount_per_share, kind,
                source, source_url, corroborated_by, corroborated_url, verification, notes
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                event.isin, event.ex_date, event.record_date, event.payment_date,
                event.amount_per_share, event.kind.value,
                event.source, event.source_url,
                event.corroborated_by, event.corroborated_url,
                event.verification.value, event.notes or None,
            ),
        )
        event_id = str(cur.fetchone()[0])

    audit.record(
        conn,
        audit.ACTION_DIVIDEND_EVENT_ADDED,
        "dividend_event",
        event_id,
        actor_uid,
        {
            "isin": event.isin,
            "ex_date": event.ex_date.isoformat(),
            "kind": event.kind.value,
            "amount_per_share": str(event.amount_per_share),
            "source": event.source,
        },
    )
    return event_id


def list_dividend_events(
    conn: Any,
    isins: list[str] | None = None,
    verification: Verification | None = None,
    ex_date_from: date | None = None,
    ex_date_to: date | None = None,
) -> list[DividendEvent]:
    """Declared dividends on file, optionally filtered. Ordered by ex-date then ISIN so a
    caller rendering a calendar does not have to re-sort.

    No ``resulting_isin`` clause, unlike ``list_corporate_actions``: a dividend has no
    second security. An ISIN change is handled where it belongs -- at the read boundary,
    through ``app.db.security_alias`` -- not by matching two columns here.
    """
    query = f"SELECT {_SELECT_COLUMNS} FROM dividend_event"
    clauses: list[str] = []
    params: list[Any] = []
    if isins:
        clauses.append("isin = ANY(%s)")
        params.append(isins)
    if verification is not None:
        clauses.append("verification = %s")
        params.append(verification.value)
    if ex_date_from is not None:
        clauses.append("ex_date >= %s")
        params.append(ex_date_from)
    if ex_date_to is not None:
        clauses.append("ex_date <= %s")
        params.append(ex_date_to)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ex_date, isin"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [_row_to_event(row) for row in cur.fetchall()]


def get_dividend_event(conn: Any, event_id: str) -> DividendEvent | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_SELECT_COLUMNS} FROM dividend_event WHERE id = %s", (event_id,))
        row = cur.fetchone()
        return _row_to_event(row) if row else None


def list_pending_dividend_events(conn: Any) -> list[DividendEvent]:
    """Every ``DRAFT`` row -- the ``Exceptions`` surface.

    Unlike ``list_pending_corporate_actions``, this is expected to be LONG rather than
    short, and that is not a sign of a broken feed. A dividend reaches ``VERIFIED`` only
    once a second source agrees (``check_cross_sourced``), so every dividend seen by just
    one feed so far sits here legitimately, waiting. Any UI over this must say so, or it
    reads as a backlog of failures.
    """
    return list_dividend_events(conn, verification=Verification.DRAFT)


def match_unspecified(
    conn: Any, isin: str, ex_date: date
) -> list[DividendEvent]:
    """Events already on file for this ``(isin, ex_date)``, whatever their kind.

    WHY THIS EXISTS, AND WHY IT RETURNS A LIST RATHER THAN ONE EVENT. The table is unique
    on ``(isin, ex_date, kind)``, and one of the two feeds cannot supply a kind:
    ``Ticker.dividends`` carries only an ex-date and an amount. So a yfinance candidate
    arrives as ``UNSPECIFIED`` while the NSE notice for the SAME dividend may already be
    on file as ``INTERIM``. Inserting the candidate as-is would create a second row for
    one real event -- and worse, it would mean the two sources never corroborate each
    other, silently defeating the only verification a dividend has.

    So an unspecified candidate must be matched, not inserted blindly. The rule, and the
    reason it is fail-closed:

      - EXACTLY ONE event on file for that (isin, ex_date): the candidate is about that
        event. ``corroborate`` it.
      - NOTHING on file: insert it as ``UNSPECIFIED``. A later classified notice for the
        same ex-date will find it through this same lookup.
      - MORE THAN ONE event on file (an interim and a special sharing an ex-date): the
        candidate is AMBIGUOUS. Which one did this source mean? Nothing here can tell, and
        picking either would attach corroborating evidence to a dividend it may not
        describe. Corroborate NEITHER, and leave a note -- a wrong corroboration is worse
        than a missing one, because it converts DRAFT into VERIFIED on false evidence.

    That last branch is the fail-closed case and the reason this returns every match
    rather than a single row: the caller must be able to SEE that there was more than one.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM dividend_event WHERE isin = %s AND ex_date = %s "
            "ORDER BY kind",
            (isin, ex_date),
        )
        return [_row_to_event(row) for row in cur.fetchall()]


def corroborate_dividend_event(
    conn: Any,
    event_id: str,
    source: str,
    source_url: str,
    amount_per_share: Decimal,
    actor_uid: str | None = None,
) -> Verification:
    """Record that a second, distinct source read this event -- and decide what that does
    to its verification. Returns the resulting state.

    IDEMPOTENT against a REPEAT of the same corroboration, not only against the original
    declarer repeating itself. Four outcomes, in the order checked:

      - A human's ``DISPUTED`` rejection stands. Automation never revisits it, the same
        contract ``refresh_corporate_action_feed`` gives ``DISPUTED`` corporate actions.
      - The "second" source IS the original declarer -> no-op. Two reads of one feed are
        one observation.
      - The "second" source is the SAME source THAT ALREADY CORROBORATED THIS, reporting
        THE SAME AMOUNT again -> also a no-op: no write, no audit entry. This is the case
        a daily yfinance or NSE re-run hits on every ordinary day once an event is already
        VERIFIED, and without this check it would re-execute the UPDATE and write a fresh
        ``DIVIDEND_EVENT_CORROBORATED`` audit row on every single run forever -- an
        observation repeated is not a new fact, and an audit trail exists to record STATE
        TRANSITIONS, not to be re-stamped every time nothing happened.
      - Otherwise this is a genuinely new observation: it AGREES on the amount ->
        ``corroborated_by``/``corroborated_url`` are set and the event is promoted to
        ``VERIFIED`` (may now produce a projection, still never a ledger row); or it
        DISAGREES -> ``DISPUTED``, with both readings written into ``notes`` -- not a
        second row, not an average, not "prefer the newer": one fact, two contradictory
        claims, and no basis in this module for choosing. A human reads the filing.

    Never downgrades a ``VERIFIED`` event that a human promoted -- a manual
    ``verify_dividend_event`` leaves ``corroborated_by`` ``None``, and a later re-run of
    the ORIGINAL declaring source still hits the "same source" no-op above, never the
    corroboration branch, so it can never be misreported as a fresh corroboration.
    """
    event = get_dividend_event(conn, event_id)
    if event is None:
        raise LookupError(f"no dividend_event {event_id}")
    if event.verification is Verification.DISPUTED:
        return Verification.DISPUTED  # a human's rejection stands
    if source == event.source:
        return event.verification  # not a second observation
    if source == event.corroborated_by and amounts_agree(amount_per_share, event.amount_per_share):
        return event.verification  # the same corroboration, reported again -- nothing new

    if not amounts_agree(amount_per_share, event.amount_per_share):
        restated = _restated_agreement(conn, event, source, amount_per_share)
        if restated is not None:
            _verify_restated(conn, event, source, source_url, amount_per_share, restated, actor_uid)
            return Verification.VERIFIED
        note = (
            f"{event.notes}\nAMOUNT_CONFLICT: {event.source} reports "
            f"{event.amount_per_share}/share, {source} reports {amount_per_share}/share"
        ).strip()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dividend_event SET verification = %s, notes = %s WHERE id = %s",
                (Verification.DISPUTED.value, note, event_id),
            )
        audit.record(
            conn, audit.ACTION_DIVIDEND_EVENT_DISPUTED, "dividend_event", event_id, actor_uid,
            {
                "isin": event.isin,
                "reason": "AMOUNT_CONFLICT",
                "reported": str(amount_per_share),
                "on_file": str(event.amount_per_share),
                "reported_by": source,
                "reported_url": source_url,
            },
        )
        return Verification.DISPUTED

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dividend_event
               SET corroborated_by = %s, corroborated_url = %s, verification = %s
             WHERE id = %s
            """,
            (source, source_url, Verification.VERIFIED.value, event_id),
        )
    audit.record(
        conn, audit.ACTION_DIVIDEND_EVENT_CORROBORATED, "dividend_event", event_id, actor_uid,
        {"isin": event.isin, "corroborated_by": source, "amount_per_share": str(amount_per_share)},
    )
    return Verification.VERIFIED


def _restated_agreement(
    conn: Any, event: DividendEvent, source: str, amount_per_share: Decimal
) -> tuple[Decimal, Decimal, Any] | None:
    """``(declared, yahoo, factor)`` when Yahoo's reading, multiplied back by the VERIFIED
    bonuses/splits dated after the dividend, agrees with the other source's as-declared
    reading; else ``None`` (owner decision 2026-10-13, docs/ENGINE_SPEC.md).

    Yahoo divides past dividends by later share-count actions; NSE does not. Only Yahoo is
    restated, and only when it is exactly one of the two sources -- two other sources
    disagreeing is a real conflict, never explained away by a ratio."""
    from app.db.corporate_actions import list_corporate_actions
    from app.db.security_alias import verified_map
    from app.engine.dividend_events import later_share_multiplier, restated_amount
    from app.ingest.corporate_action_yfinance import YFINANCE_SOURCE_NAME

    if (source == YFINANCE_SOURCE_NAME) == (event.source == YFINANCE_SOURCE_NAME):
        return None
    if source == YFINANCE_SOURCE_NAME:
        yahoo, declared = amount_per_share, event.amount_per_share
    else:
        yahoo, declared = event.amount_per_share, amount_per_share

    # The issuer's whole ISIN family: a split often changes the ISIN, and the action may
    # be filed under the old identifier while the dividend sits under the new one.
    aliases = verified_map(conn)
    terminal = aliases.get(event.isin, event.isin)
    family = {event.isin, terminal} | {old for old, new in aliases.items() if new == terminal}
    actions = list_corporate_actions(conn, isins=sorted(family), verification=Verification.VERIFIED)
    factor = later_share_multiplier(actions, event.ex_date)
    if factor == 1 or not amounts_agree(restated_amount(yahoo, factor), declared):
        return None
    return declared, yahoo, factor


def _verify_restated(
    conn: Any,
    event: DividendEvent,
    source: str,
    source_url: str,
    amount_per_share: Decimal,
    restated: tuple[Decimal, Decimal, Any],
    actor_uid: str | None,
) -> None:
    """VERIFIED at the AS-DECLARED amount. When Yahoo created the row, its stored amount is
    Yahoo's scaled figure, which would project a fraction of the real dividend -- so the
    declared amount replaces it, and the note keeps Yahoo's raw reading and the factor."""
    declared, yahoo, factor = restated
    note = (
        f"{event.notes or ''}\nRESTATED: Yahoo reported {yahoo}/share, scaled by later "
        f"bonuses/splits (x{factor}); as declared {declared}/share"
    ).strip()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dividend_event
               SET corroborated_by = %s, corroborated_url = %s, verification = %s,
                   amount_per_share = %s, notes = %s
             WHERE id = %s
            """,
            (source, source_url, Verification.VERIFIED.value, declared, note, event.id),
        )
    audit.record(
        conn, audit.ACTION_DIVIDEND_EVENT_CORROBORATED, "dividend_event", event.id, actor_uid,
        {
            "isin": event.isin,
            "corroborated_by": source,
            "amount_per_share": str(amount_per_share),
            "restated": {"yahoo": str(yahoo), "factor": str(factor), "declared": str(declared)},
        },
    )


def _reported_amount(diff: dict, notes: str, reporter: str) -> Decimal | None:
    """The disputing source's amount: from the audit row, or -- for disputes recorded
    before the audit row carried it -- from the ``AMOUNT_CONFLICT`` line this module
    itself wrote ("<source> reports <amount>/share"). None when neither has it."""
    import re

    if diff.get("reported") is not None:
        return Decimal(diff["reported"])
    found = re.findall(rf"{re.escape(reporter)} reports ([0-9]+(?:\.[0-9]+)?)/share", notes)
    return Decimal(found[-1]) if found else None


def recheck_automatic_disputes(conn: Any) -> dict[str, int]:
    """Re-run the restated comparison on every dividend that AUTOMATION disputed for an
    amount conflict -- never one a human disputed (``"manual": true`` in its audit row).

    Why this exists: a dividend often lands before the bonus or split that explains
    Yahoo's figure is recorded (a new user's history is swept month by month), so the
    comparison that disputed it had no factor to apply. The original readings are in the
    dispute's audit row; nothing is re-fetched."""
    from app.ingest.corporate_action_yfinance import YFINANCE_SOURCE_NAME

    counts = {"rechecked": 0, "verified": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id::text, a.diff_json
              FROM dividend_event d
              JOIN LATERAL (
                    SELECT diff_json FROM audit_log
                     WHERE entity_type = 'dividend_event' AND entity_id = d.id
                       AND action = %s
                     ORDER BY ts DESC LIMIT 1
                   ) a ON true
             WHERE d.verification = 'DISPUTED'
            """,
            (audit.ACTION_DIVIDEND_EVENT_DISPUTED,),
        )
        rows = cur.fetchall()
    for event_id, diff in rows:
        if not diff or diff.get("manual") or diff.get("reason") != "AMOUNT_CONFLICT":
            continue
        counts["rechecked"] += 1
        event = get_dividend_event(conn, event_id)
        reporter = diff["reported_by"]
        reported = _reported_amount(diff, event.notes or "", reporter)
        if reported is None:
            continue  # the reading cannot be recovered exactly -- leave it DISPUTED
        restated = _restated_agreement(conn, event, reporter, reported)
        if restated is None:
            continue
        # Disputes recorded before the reporter's URL was kept: Yahoo's is derived from
        # the symbol exactly as discovery builds it; anything else is NSE's filings page.
        url = diff.get("reported_url")
        if not url and reporter == YFINANCE_SOURCE_NAME:
            row = conn.execute(
                "SELECT symbol FROM security WHERE isin = %s", (event.isin,)
            ).fetchone()
            if row and row[0]:
                url = f"https://finance.yahoo.com/quote/{row[0]}.NS/history"
        url = url or _NSE_SOURCE_URL
        _verify_restated(conn, event, reporter, url, reported, restated, None)
        counts["verified"] += 1
    return counts


def verify_dividend_event(
    conn: Any, event_id: str, actor_uid: str | None = None
) -> None:
    """A human promotes an event to ``VERIFIED`` by hand.

    Deliberately does NOT require ``check_cross_sourced`` to pass. A person can read the
    company's own filing, which is a stronger and differently-originated piece of evidence
    than two redistributors agreeing -- see that guard's docstring on why their agreement
    is corroboration rather than proof. This is the one path that can verify a
    single-sourced dividend, and it is a human taking responsibility for it.

    Unlike ``verify_corporate_action``, this triggers NO retroactive ledger sweep: there
    is nothing to sweep, because a verified declaration still has no receipt behind it.
    Verification here unlocks a projection, and nothing else.

    Refuses a ``DISPUTED`` row, as ``dispute_dividend_event``'s docstring promises and as
    ``verify_corporate_action`` enforces for ``corporate_action``: a human's rejection is
    terminal, so no later run -- automated or manual -- may quietly turn it into evidence.
    """
    from app.ingest.action_source import ActionConflictError

    event = get_dividend_event(conn, event_id)
    if event is None:
        return  # no such row -- nothing to promote
    if event.verification is Verification.DISPUTED:
        raise ActionConflictError("a disputed dividend declaration is terminal", "DISPUTED")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dividend_event SET verification = %s WHERE id = %s AND verification <> %s",
            (Verification.VERIFIED.value, event_id, Verification.VERIFIED.value),
        )
        if cur.rowcount == 0:
            return
    audit.record(
        conn, audit.ACTION_DIVIDEND_EVENT_VERIFIED, "dividend_event", event_id, actor_uid,
        {"isin": event.isin, "manual": True},
    )


def dispute_dividend_event(
    conn: Any, event_id: str, reason: str, actor_uid: str | None = None
) -> None:
    """Mark an event rejected. Terminal as far as automation is concerned -- no feed run
    revisits a ``DISPUTED`` row, the same contract ``corporate_action`` has."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dividend_event SET verification = %s, notes = %s WHERE id = %s",
            (Verification.DISPUTED.value, reason, event_id),
        )
    audit.record(
        conn, audit.ACTION_DIVIDEND_EVENT_DISPUTED, "dividend_event", event_id, actor_uid,
        {"reason": reason, "manual": True},
    )


def update_dividend_event_dates(
    conn: Any,
    event_id: str,
    record_date: date | None = None,
    payment_date: date | None = None,
) -> None:
    """Fill in a record or payment date a later fetch supplied and an earlier one did not.

    Only ever fills a NULL -- ``COALESCE`` on the existing value, so a date already on
    file is never overwritten by a later source. A contradiction between two sources about
    a date is not silently resolved here; it is the same class of problem as an amount
    conflict and belongs in ``notes`` and ``DISPUTED``, not in a last-writer-wins update.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dividend_event
               SET record_date  = COALESCE(record_date, %s),
                   payment_date = COALESCE(payment_date, %s)
             WHERE id = %s
            """,
            (record_date, payment_date, event_id),
        )


__all__ = [
    "OUTCOME_AMBIGUOUS",
    "OUTCOME_CLASSIFIED",
    "OUTCOME_CONFLICT",
    "OUTCOME_CORROBORATED",
    "OUTCOME_INSERTED",
    "OUTCOME_UNCHANGED",
    "DividendEventDateError",
    "add_dividend_event",
    "corroborate_dividend_event",
    "discover_dividends_from_yfinance",
    "dispute_dividend_event",
    "get_dividend_event",
    "list_dividend_events",
    "list_pending_dividend_events",
    "match_unspecified",
    "refresh_dividend_feed",
    "update_dividend_event_dates",
    "validate_dates",
    "upsert_declared_dividend",
    "verify_dividend_event",
]


# ===========================================================================
# MERGE AND DISCOVERY -- where the two sources actually meet.
# ===========================================================================

#: Outcomes ``upsert_declared_dividend`` can report. Returned rather than logged-and-
#: forgotten so a refresh run can tell a human what actually happened, and so the
#: ambiguous case is COUNTED rather than silently doing nothing.
OUTCOME_INSERTED = "inserted"
OUTCOME_CORROBORATED = "corroborated"
OUTCOME_CLASSIFIED = "classified"
OUTCOME_CONFLICT = "conflict"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_UNCHANGED = "unchanged"

_NSE_SOURCE_NAME = "NSE corporate filings"
_NSE_SOURCE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


def _classify_and_corroborate(
    conn: Any,
    event: DividendEvent,
    candidate: DividendEvent,
    actor_uid: str | None,
) -> str:
    """An UNSPECIFIED event on file turns out to have a name, supplied by a source that
    also read its amount.

    WHY CHANGING ``kind`` IS LEGITIMATE HERE even though it is part of the event's
    identity. The row was always THIS dividend; what was missing was its label, because
    ``Ticker.dividends`` does not carry one. An NSE notice naming it "Interim" is better
    information about the same fact, not a different fact -- so this is a reference-data
    correction, audited like any other, and not an identity change. It is safe against
    collision because the caller has already established that no row of the target kind
    exists for this (isin, ex_date): that lookup is what makes this branch reachable.

    The amount is still checked. Better information about the label does not license
    accepting a contradictory number.

    IDEMPOTENT against a repeated disagreeing candidate, the same principle
    ``corroborate_dividend_event`` now applies: once a row is ``DISPUTED``, automation
    does not re-dispute it on every subsequent run of the same feed -- without this, an
    unresolved conflict would grow a fresh duplicate line onto ``notes`` and write a fresh
    audit entry every single day until a human resolved it.
    """
    if event.verification is Verification.DISPUTED:
        return OUTCOME_CONFLICT  # a human's rejection stands; do not re-dispute it daily

    restated = None
    if not amounts_agree(candidate.amount_per_share, event.amount_per_share):
        # Yahoo's row often arrives first carrying its scaled figure; the NSE notice that
        # labels it may still agree once later bonuses/splits are multiplied back.
        restated = _restated_agreement(conn, event, candidate.source, candidate.amount_per_share)
    if restated is None and not amounts_agree(candidate.amount_per_share, event.amount_per_share):
        note = (
            f"{event.notes}\nAMOUNT_CONFLICT: {event.source} reports "
            f"{event.amount_per_share}/share, {candidate.source} reports "
            f"{candidate.amount_per_share}/share"
        ).strip()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dividend_event SET verification = %s, notes = %s WHERE id = %s",
                (Verification.DISPUTED.value, note, event.id),
            )
        audit.record(
            conn, audit.ACTION_DIVIDEND_EVENT_DISPUTED, "dividend_event", event.id, actor_uid,
            {
                "isin": event.isin,
                "reason": "AMOUNT_CONFLICT",
                "reported_by": candidate.source,
                "reported": str(candidate.amount_per_share),
                "reported_url": candidate.source_url,
            },
        )
        return OUTCOME_CONFLICT

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dividend_event
               SET kind = %s,
                   record_date = COALESCE(record_date, %s),
                   payment_date = COALESCE(payment_date, %s),
                   corroborated_by = %s,
                   corroborated_url = %s,
                   verification = %s
             WHERE id = %s
            """,
            (
                candidate.kind.value, candidate.record_date, candidate.payment_date,
                candidate.source, candidate.source_url,
                Verification.VERIFIED.value, event.id,
            ),
        )
    audit.record(
        conn, audit.ACTION_DIVIDEND_EVENT_CORROBORATED, "dividend_event", event.id, actor_uid,
        {
            "isin": event.isin,
            "kind_was": event.kind.value,
            "kind_now": candidate.kind.value,
            "corroborated_by": candidate.source,
        },
    )
    if restated is not None:
        # Labelled and verified above; now record the as-declared amount and why.
        _verify_restated(
            conn, event, candidate.source, candidate.source_url, candidate.amount_per_share,
            restated, actor_uid,
        )
    return OUTCOME_CLASSIFIED


def _corroboration_outcome(before: DividendEvent, after: DividendEvent) -> str:
    """Classify what a ``corroborate_dividend_event`` call actually did, from a real
    before/after comparison -- never inferred from a single snapshot's fields, which is
    exactly what made the outcome misreportable.

    AUDIT RECORDS DESCRIBE STATE TRANSITIONS, NOT REPEATED OBSERVATIONS OF THE SAME STATE.
    The two failure modes this replaces, both now impossible by construction:

      - A repeat corroboration from the SAME corroborating source, reporting the SAME
        amount again (the ordinary case on every subsequent daily run once an event is
        VERIFIED) used to be reported as ``OUTCOME_CORROBORATED`` on every single run,
        because the check was "is corroborated_by set now" rather than "did corroborated_by
        CHANGE just now". ``corroborate_dividend_event`` itself is now idempotent for this
        case (no write at all), so ``before == after`` and this correctly falls through to
        UNCHANGED.
      - A re-run of the ORIGINAL declaring source against an event a HUMAN had already
        verified by hand (``corroborated_by`` left ``None`` by ``verify_dividend_event``)
        used to be misread as a fresh corroboration because ``corroborated_by is None`` was
        checked against the state BEFORE this call, not against what this call itself did.
        ``before.corroborated_by`` and ``after.corroborated_by`` are both still ``None``
        here, so the diff correctly reports UNCHANGED.
    """
    became_disputed = (
        before.verification is not Verification.DISPUTED
        and after.verification is Verification.DISPUTED
    )
    if became_disputed:
        return OUTCOME_CONFLICT
    if before.corroborated_by != after.corroborated_by or before.verification != after.verification:
        return OUTCOME_CORROBORATED
    return OUTCOME_UNCHANGED


def upsert_declared_dividend(
    conn: Any, candidate: DividendEvent, actor_uid: str | None = None
) -> str:
    """Land one declared-dividend candidate, merging it with whatever is already on file.

    THE WHOLE CROSS-SOURCE DESIGN LIVES HERE, so it lives in exactly one place rather than
    being re-derived by each feed. Five cases, and the last one is the one that matters:

    1. An event of the SAME (isin, ex_date, kind) is on file -> corroborate it. A different
       source agreeing promotes it to VERIFIED; the same source repeating itself changes
       nothing, because two reads of one feed are one observation.
    2. The candidate NAMES a kind and an UNSPECIFIED event sits at that (isin, ex_date) ->
       the candidate is that event, better labelled. Classify and corroborate it, rather
       than inserting a second row for one dividend. This is the NSE-arrives-second case.
    3. The candidate is UNSPECIFIED and exactly ONE event sits at that (isin, ex_date) ->
       corroborate that one, whatever its kind. This is the yfinance-arrives-second case,
       and without it the two feeds would never corroborate each other at all, because
       ``Ticker.dividends`` cannot supply a kind.
    4. Nothing is on file -> insert, DRAFT. One source is not enough to project from.
    5. The candidate is UNSPECIFIED and SEVERAL events sit at that (isin, ex_date) -- an
       interim and a special declared together -> AMBIGUOUS. Do nothing at all, and say so.
       Attaching this source's agreement to one of them would promote it to VERIFIED on
       evidence that may describe the other, and a wrong corroboration is worse than a
       missing one because it converts DRAFT into a number the product will show.

    Never raises ``UniqueViolation`` for the ordinary repeat case: case 1 resolves it by
    lookup first, which is what lets a caller commit per row without a rollback in the
    common path.

    THE RETURNED OUTCOME IS A REAL BEFORE/AFTER STATE DIFF (``_corroboration_outcome``),
    not an inference from a single snapshot. It used to be the latter, and that made
    ``OUTCOME_CORROBORATED`` reportable on runs that corroborated nothing: a repeat of an
    already-corroborating source changed nothing on disk but still looked, from one
    snapshot, indistinguishable from a fresh corroboration. A caller aggregating these
    outcomes across a refresh run (``refresh_dividend_feed``) now gets an honest count of
    what actually changed, not of how many times a feed happened to run.
    """
    validate_dates(candidate)
    existing = match_unspecified(conn, candidate.isin, candidate.ex_date)

    same_kind = next((e for e in existing if e.kind is candidate.kind), None)
    if same_kind is not None:
        corroborate_dividend_event(
            conn, same_kind.id, candidate.source, candidate.source_url,
            candidate.amount_per_share, actor_uid=actor_uid,
        )
        update_dividend_event_dates(
            conn, same_kind.id, candidate.record_date, candidate.payment_date
        )
        after = get_dividend_event(conn, same_kind.id)
        return _corroboration_outcome(same_kind, after)

    if candidate.kind is not DividendKind.UNSPECIFIED:
        unspecified = [e for e in existing if e.kind is DividendKind.UNSPECIFIED]
        if len(unspecified) == 1:
            return _classify_and_corroborate(conn, unspecified[0], candidate, actor_uid)
    elif len(existing) == 1:
        before = existing[0]
        corroborate_dividend_event(
            conn, before.id, candidate.source, candidate.source_url,
            candidate.amount_per_share, actor_uid=actor_uid,
        )
        after = get_dividend_event(conn, before.id)
        return _corroboration_outcome(before, after)
    elif len(existing) > 1:
        return OUTCOME_AMBIGUOUS

    add_dividend_event(conn, candidate, actor_uid=actor_uid)
    return OUTCOME_INSERTED


def refresh_dividend_feed(
    conn: Any,
    start: date,
    end: date,
    source: Any | None = None,
    actor_uid: str | None = None,
    jev_client: Any = None,
) -> dict[str, Any]:
    """Fetch NSE's notices for ``[start, end]`` and land every dividend among them.

    Same posture as ``refresh_corporate_action_feed``, deliberately, down to the
    per-row commit: without it, a later row's error would roll back every earlier row's
    already-successful work in the same run, because Postgres aborts the whole transaction
    until rolled back and rollback undoes everything since the last commit. Log-and-
    continue only actually isolates one bad row if each row's work is committed first.

    A fetch or format failure raises. A row that fails is rolled back and logged, the
    other rows still commit, and the run raises ``ActionIngestionError`` at the end.

    ``jev_client`` is passed straight through to ``parse_dividend_feed`` -- an
    ``app.ai.jev.JevClient`` or ``None``, constructed by the caller from ``Settings`` so
    this module needs no Settings import of its own. See that function's docstring for
    what it is used for and what ``None`` (the ordinary state absent a configured key)
    preserves unchanged.

    NOTE what this does NOT do, unlike its corporate-action counterpart: there is no
    retry loop over pending ``DRAFT`` rows. A DRAFT corporate action is waiting for a PRICE
    that a later bhavcopy will supply, so retrying it is productive. A DRAFT dividend is
    waiting for a SECOND SOURCE, and re-reading the same feed cannot produce one -- the
    thing that promotes it is the other feed running, which is its own entry point.
    """
    from app.ingest.corporate_action_feed import (
        CorporateActionFeedFetchFailed,
        CorporateActionFeedFormatError,
        nse_corporate_action_feed,
        parse_dividend_feed,
    )

    feed = source or nse_corporate_action_feed()
    try:
        payload = feed.fetch(start, end)
    except CorporateActionFeedFetchFailed as exc:
        logger.warning("dividend feed fetch failed for %s..%s: %s", start, end, exc)
        raise

    try:
        rows = parse_dividend_feed(payload, jev_client=jev_client)
    except CorporateActionFeedFormatError as exc:
        logger.warning("dividend feed format changed for %s..%s: %s", start, end, exc)
        raise

    counts: dict[str, int] = {}
    for row in rows:
        if conn.execute("SELECT 1 FROM security WHERE isin=%s", (row.isin,)).fetchone() is None:
            # The feed is all-market: a dividend for a security nobody here holds is
            # expected, not a failure -- the same SECURITY_NOT_REGISTERED posture as
            # refresh_corporate_action_feed. dividend_event.isin is a foreign key to
            # security, so it cannot be stored. Counting it as failed made every month of
            # the reference sweep raise, so no month ever reached coverage.
            counts["unregistered"] = counts.get("unregistered", 0) + 1
            continue
        candidate = DividendEvent(
            isin=row.isin,
            ex_date=row.ex_date,
            amount_per_share=row.amount_per_share,
            kind=DividendKind(row.kind),
            record_date=row.record_date,
            source=_NSE_SOURCE_NAME,
            source_url=_NSE_SOURCE_URL,
            verification=Verification.DRAFT,
            notes=row.raw_subject,
        )
        try:
            outcome = upsert_declared_dividend(conn, candidate, actor_uid=actor_uid)
            conn.commit()
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception:
            conn.rollback()
            logger.warning(
                "dividend candidate failed for isin=%s ex_date=%s -- skipped, not aborting "
                "the run", row.isin, row.ex_date, exc_info=True,
            )
            counts["failed"] = counts.get("failed", 0) + 1

    if counts.get("failed"):
        from app.ingest.action_source import ActionIngestionError
        raise ActionIngestionError(f"{counts['failed']} dividend observations failed processing")
    return {"fetched": len(rows), **counts}


def discover_dividends_from_yfinance(
    conn: Any,
    isins: list[str] | None = None,
    source: Any | None = None,
    actor_uid: str | None = None,
    rights_reference: str | None = None,
    after: str | None = None,
    time_budget_seconds: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> dict[str, Any]:
    """``Ticker.dividends`` for every ISIN this system holds -- a second, independent
    source whose agreement with an NSE notice is what verifies a dividend. Not the only
    historical source: NSE's notices API also serves historical windows, read month by
    month through ``app.db.reference_sync.schedule``.

    Occasional, not daily: a ticker's whole dividend history comes back in one call and
    does not change day to day. ``isins`` defaults to every ISIN ever held
    (``app.db.prices.held_isins``), never Yahoo's universe, so no call is spent on a
    security nobody here owns.

    Every candidate is UNSPECIFIED, because the kind is genuinely absent from the data --
    see ``app.ingest.corporate_action_yfinance``'s dividend section on why inferring it
    from the month or the size would be an invention. ``upsert_declared_dividend`` case 3
    is what lets such a candidate corroborate an NSE notice anyway.

    The retired side of a VERIFIED alias is skipped without a record: the current ISIN
    covers its history. One bad ISIN or candidate never discards the rest: an ISIN with
    no usable symbol, a failed lookup, or a candidate that cannot be stored is recorded
    as a REJECTED observation and the run continues; ``ActionBatchFailed`` is raised at the end if
    anything was rejected. Good rows are committed either way. ``rights_reference`` is
    copied onto those observations; when ``source`` is omitted the real Yahoo source is
    built here and the reference is ``YAHOO_DATA_RIGHTS_REFERENCE``.

    BOUNDED AND RESUMABLE. One Yahoo call per held ISIN grows with every user's holdings,
    and a serverless caller is killed at a hard limit. ISINs are walked in sorted order,
    only those after ``after``; once ``time_budget_seconds`` is spent the sweep stops and
    returns ``next_after`` (the last ISIN it finished) so the caller can continue from
    there -- a budget alone would restart from the same first ISINs every week and never
    reach the rest. ``next_after`` is None when the sweep reached the end. When the run
    raises ``ActionBatchFailed``, the same cursor rides on it as ``next_after``.
    """
    from app.config import get_settings
    from app.db.corporate_actions import (
        current_identifiers,
        record_yahoo_rejection,
        yahoo_symbol_rejection,
    )
    from app.db.prices import held_isins
    from app.ingest.action_source import ActionBatchFailed
    from app.ingest.corporate_action_yfinance import (
        YFINANCE_SOURCE_NAME,
        YfinanceSource,
        discover_dividend_candidates,
    )
    from app.securities import lookup as lookup_securities

    yf_source = source
    if yf_source is None:
        yf_source = YfinanceSource()
        rights_reference = get_settings().yahoo_data_rights_reference
    target_isins = sorted(
        set(current_identifiers(conn, isins if isins is not None else held_isins(conn)))
    )
    if after is not None:
        target_isins = [i for i in target_isins if i > after]
    meta = lookup_securities(conn, target_isins)

    counts: dict[str, int] = {}
    rejected: list[str] = []
    next_after: str | None = None
    last_done: str | None = None
    began = clock()

    for isin in target_isins:
        # At least one ISIN per call, so a cursor always moves and "finished" (None) is
        # never reported for a sweep that did nothing.
        if (
            time_budget_seconds is not None
            and last_done is not None
            and clock() - began > time_budget_seconds
        ):
            next_after = last_done
            break
        last_done = isin
        symbol = (meta.get(isin) or {}).get("symbol")
        reason_code = yahoo_symbol_rejection(meta, isin)
        if reason_code is not None:
            rejected.append(record_yahoo_rejection(
                conn, isin=isin, symbol=symbol, classification="DIVIDEND",
                reason_code=reason_code, reason=None, rights_reference=rights_reference,
            ))
            continue
        try:
            candidates = discover_dividend_candidates(yf_source, isin, symbol)
        except Exception as exc:
            rejected.append(record_yahoo_rejection(
                conn, isin=isin, symbol=symbol, classification="DIVIDEND",
                reason_code="SOURCE_LOOKUP_FAILED",
                reason=f"{type(exc).__name__}: {str(exc)[:200]}",
                rights_reference=rights_reference,
            ))
            continue

        for candidate in candidates:
            event = DividendEvent(
                isin=candidate.isin,
                ex_date=candidate.ex_date,
                amount_per_share=candidate.amount_per_share,
                kind=DividendKind.UNSPECIFIED,
                source=YFINANCE_SOURCE_NAME,
                source_url=candidate.source_url,
                verification=Verification.DRAFT,
                notes=f"Discovered via yfinance dividend history for {symbol}.NS.",
            )
            try:
                outcome = upsert_declared_dividend(conn, event, actor_uid=actor_uid)
                conn.commit()
                counts[outcome] = counts.get(outcome, 0) + 1
            except Exception as exc:
                conn.rollback()
                counts["failed"] = counts.get("failed", 0) + 1
                rejected.append(record_yahoo_rejection(
                    conn, isin=isin, symbol=symbol, classification="DIVIDEND",
                    reason_code="UNEXPECTED_ERROR", reason=type(exc).__name__,
                    rights_reference=rights_reference,
                    detail={"ex_date": candidate.ex_date.isoformat(),
                            "amount_per_share": str(candidate.amount_per_share)},
                ))

    if rejected:
        failure = ActionBatchFailed(rejected, [])
        failure.next_after = next_after
        raise failure
    return {**counts, "next_after": next_after}
