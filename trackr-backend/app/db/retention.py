"""Retention policy -- 5 years by default, suspended by a legal hold.

SEBI Reg. 19 requires five years for advisory records; DPDP requires one year for access
logs. The longer obligation governs, so the default is five. Configurable per entity type
because that is what ``docs/COMPLIANCE.md`` asks for, not because shortening it is a good
idea.

This module REPORTS what is due for deletion. It does not delete. Automatic destruction
of client records on a timer is exactly the kind of thing that should require a human to
look at a list first.
"""

from __future__ import annotations

from datetime import date
from typing import Any

DEFAULT_RETAIN_YEARS = 5


def retain_years(conn: Any, entity_type: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT retain_years FROM retention_policy WHERE entity_type = %s", (entity_type,)
        )
        row = cur.fetchone()
        return row[0] if row else DEFAULT_RETAIN_YEARS


def cutoff(conn: Any, entity_type: str, as_of: date) -> date:
    """The date before which records of this type are past retention."""
    years = retain_years(conn, entity_type)
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:
        # 29 February. Step back a day rather than forward, so the cutoff never lands
        # later than intended and destroys something a day early.
        return as_of.replace(month=2, day=28, year=as_of.year - years)


def due_for_deletion(conn: Any, as_of: date) -> list[dict[str, Any]]:
    """Investors whose records are past retention AND not under legal hold.

    A held investor is never listed, at any age. Where a dispute has been raised, records
    are kept until it resolves or SEBI directs otherwise -- a cleanup job must not be able
    to quietly destroy evidence in a live matter.

    RESEARCH ACTIVITY COUNTS AS ACTIVITY. This used to measure inactivity from the last
    transaction date alone, which made an investor who reads filings, writes notes and revises
    a thesis every week -- but has not traded in six years -- look dormant and appear on a
    deletion report. Research is now folded into ``last_activity``.

    That is deliberately the CONSERVATIVE half of BE-Q05 and not an answer to it. Keeping an
    active researcher off a deletion report needs no policy decision: this function only
    reports, a human acts on the list, and a false positive there is the expensive direction.
    What still needs a decision is which retention CATEGORY and which START EVENT govern
    research records -- see ``app/db/workspace_privacy.readiness_blockers``, which refuses to let
    research inherit the five-year investor default by omission.
    """
    research_available = _research_activity_available(conn)
    research_clause = (
        """,
                   (SELECT max(latest) FROM (
                        SELECT max(recorded_at) AS latest FROM note WHERE investor_id = i.id
                        UNION ALL
                        SELECT max(recorded_at) FROM question_answer WHERE investor_id = i.id
                        UNION ALL
                        SELECT max(recorded_at) FROM thesis WHERE investor_id = i.id
                        UNION ALL
                        SELECT max(recorded_at) FROM research_source WHERE investor_id = i.id
                        UNION ALL
                        SELECT max(recorded_at) FROM thesis_event WHERE investor_id = i.id
                        UNION ALL
                        SELECT max(created_at) FROM task WHERE investor_id = i.id
                   ) r) AS last_research"""
        if research_available
        else ", NULL::timestamptz AS last_research"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT i.id, i.code, i.created_at,
                   (SELECT max(t.txn_date) FROM transaction t
                    JOIN account a ON a.id = t.account_id
                    WHERE a.investor_id = i.id) AS last_transaction
                   {research_clause}
            FROM investor i
            WHERE NOT EXISTS (
                SELECT 1 FROM legal_hold h
                WHERE h.investor_id = i.id AND h.released_at IS NULL
            )
            """
        )
        columns = [c.name for c in cur.description]
        rows = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]

    limit = cutoff(conn, "investor", as_of)
    due = []
    for row in rows:
        # Retention runs from the LAST interaction, not the first: an active client's
        # early records stay for as long as the relationship does.
        candidates = [d for d in (row["last_transaction"], row["created_at"].date()) if d]
        research = row.get("last_research")
        if research is not None:
            candidates.append(research.date())
        last = max(candidates)
        if last < limit:
            due.append(
                {
                    **row,
                    "retained_until": limit,
                    "last_activity": last,
                    # Which kind of activity set the clock. A reviewer deciding whether to act on
                    # a row needs to know whether "last active 2019" means "stopped trading" or
                    # "stopped using the product".
                    "last_activity_source": (
                        "research"
                        if research is not None and research.date() == last
                        else "transaction"
                        if row["last_transaction"] == last
                        else "investor_created"
                    ),
                }
            )
    return due


def _research_activity_available(conn: Any) -> bool:
    """Does this database have the workspace research tables?

    One catalogue read, so ``due_for_deletion`` works unchanged on every database that has no
    research schema -- which is all of them today -- without the caller having to know.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'note')"
        )
        return bool(cur.fetchone()[0])
