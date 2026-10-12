"""Grievance intake and the 90-day clock.

`docs/COMPLIANCE.md`: DPDP sets a **90-day ceiling** on grievance redressal. A ceiling is
not a target — 89 days is compliant and terrible — so this module reports on a warning
horizon well before the deadline rather than only at breach.

The clock itself is a generated column in the database, not a value this code computes. A
deadline the application sets is a deadline the application can set wrongly, and the one
record that must not be wrong is the one evidencing that a complaint was handled in time.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Any

from app.db import audit

#: DPDP ceiling. Stated here for readers; the database computes the actual deadline.
REDRESSAL_CEILING_DAYS = 90

#: Surface a grievance as "due soon" with three weeks left. Anything that needs a document
#: from a depository or a broker's compliance desk will take longer than the days left if
#: it is only noticed at day 85.
WARNING_HORIZON_DAYS = 21

STATUS_OPEN = "OPEN"
STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
STATUS_RESOLVED = "RESOLVED"
STATUS_ESCALATED = "ESCALATED"

OPEN_STATUSES = (STATUS_OPEN, STATUS_ACKNOWLEDGED, STATUS_ESCALATED)

ACTION_RAISED = "GRIEVANCE_RAISED"
ACTION_ACKNOWLEDGED = "GRIEVANCE_ACKNOWLEDGED"
ACTION_RESOLVED = "GRIEVANCE_RESOLVED"
ACTION_ESCALATED = "GRIEVANCE_ESCALATED"


class GrievanceNotFoundError(LookupError):
    pass


class AlreadyResolvedError(ValueError):
    """A resolved grievance is closed. Reopening would rewrite the redressal record."""


def new_reference(raised: date | None = None) -> str:
    """An externally quotable reference: ``GRV-2026-8F3A21``.

    Given to the complainant instead of the internal uuid, so a support conversation never
    needs to expose a database identifier. Random rather than sequential: a sequence tells
    anyone who receives one how many complaints there have been.
    """
    year = (raised or date.today()).year
    return f"GRV-{year}-{secrets.token_hex(3).upper()}"


def raise_grievance(
    conn: Any,
    subject: str,
    channel: str,
    detail: str | None = None,
    investor_id: str | None = None,
    actor_uid: str | None = None,
) -> dict[str, Any]:
    """Record a complaint and start the clock. Returns the row including ``due_at``.

    ``investor_id`` is optional on purpose. A complaint can arrive from someone who is not
    yet an investor, or whom nobody has matched to a record yet, and a grievance route that
    can only accept complaints from known clients is not an intake route.
    """
    reference = new_reference()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO grievance (investor_id, reference, channel, subject, detail)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, reference, status, raised_at, due_at
            """,
            (investor_id, reference, channel, subject, detail),
        )
        row = dict(zip([c.name for c in cur.description], cur.fetchone(), strict=True))

    audit.record(
        conn,
        ACTION_RAISED,
        "grievance",
        str(row["id"]),
        actor_uid,
        {
            "reference": reference,
            "channel": channel,
            "investor_id": investor_id,
            "due_at": row["due_at"].isoformat(),
        },
    )
    return row


def acknowledge(conn: Any, grievance_id: str, actor_uid: str | None = None) -> None:
    """Mark it seen. Does not stop the clock -- only resolution does that."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE grievance SET status = 'ACKNOWLEDGED', acknowledged_at = now()
            WHERE id = %s AND status = 'OPEN'
            """,
            (grievance_id,),
        )
        if cur.rowcount == 0:
            raise GrievanceNotFoundError(f"no open grievance {grievance_id}")
    audit.record(conn, ACTION_ACKNOWLEDGED, "grievance", grievance_id, actor_uid)


def resolve(
    conn: Any, grievance_id: str, resolution: str, actor_uid: str | None = None
) -> dict[str, Any]:
    """Close it, recording how. Returns whether it landed inside the ceiling.

    The database refuses a resolution that is empty or dated before the complaint, so the
    two ways of faking a compliant record -- closing with nothing, and backdating -- are
    both impossible rather than discouraged.
    """
    if not resolution or not resolution.strip():
        raise ValueError("a grievance cannot be resolved without recording how")

    with conn.cursor() as cur:
        cur.execute("SELECT status, due_at FROM grievance WHERE id = %s", (grievance_id,))
        row = cur.fetchone()
        if row is None:
            raise GrievanceNotFoundError(grievance_id)
        if row[0] == STATUS_RESOLVED:
            raise AlreadyResolvedError(grievance_id)
        due_at = row[1]

        cur.execute(
            """
            UPDATE grievance
            SET status = 'RESOLVED', resolved_at = now(), resolution = %s
            WHERE id = %s
            RETURNING resolved_at
            """,
            (resolution, grievance_id),
        )
        resolved_at = cur.fetchone()[0]

    within = resolved_at <= due_at
    audit.record(
        conn,
        ACTION_RESOLVED,
        "grievance",
        grievance_id,
        actor_uid,
        {"within_ceiling": within, "days_taken": None},
    )
    return {"resolved_at": resolved_at, "due_at": due_at, "within_ceiling": within}


def escalate(conn: Any, grievance_id: str, reason: str, actor_uid: str | None = None) -> None:
    """Escalate. Does NOT extend the deadline -- the ceiling is statutory, not internal."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE grievance SET status = 'ESCALATED' WHERE id = %s AND status <> 'RESOLVED'",
            (grievance_id,),
        )
        if cur.rowcount == 0:
            raise GrievanceNotFoundError(f"no unresolved grievance {grievance_id}")
    audit.record(conn, ACTION_ESCALATED, "grievance", grievance_id, actor_uid, {"reason": reason})


def _select_open(conn: Any, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, reference, investor_id, subject, channel, status,
                   raised_at, due_at, (due_at - now()) AS time_left
            FROM grievance
            WHERE status = ANY(%s) AND {where}
            ORDER BY due_at
            """,
            (list(OPEN_STATUSES), *params),
        )
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]


def open_grievances(conn: Any, investor_id: str | None = None) -> list[dict[str, Any]]:
    """Everything unresolved, soonest deadline first."""
    if investor_id:
        return _select_open(conn, "investor_id = %s", (investor_id,))
    return _select_open(conn, "TRUE", ())


def due_soon(conn: Any, horizon_days: int = WARNING_HORIZON_DAYS) -> list[dict[str, Any]]:
    """Unresolved and inside the warning horizon. What a weekly review should read.

    Deliberately not "overdue only". By the time something is overdue the obligation has
    already been missed, and no alert can undo that.
    """
    return _select_open(conn, "due_at <= now() + %s", (timedelta(days=horizon_days),))


def overdue(conn: Any) -> list[dict[str, Any]]:
    """Past the 90-day ceiling and still unresolved. Should always be empty."""
    return _select_open(conn, "due_at < now()", ())


def days_remaining(due_at: datetime, now: datetime | None = None) -> int:
    """Whole days left. Negative once the ceiling has passed."""
    return (due_at - (now or datetime.now(due_at.tzinfo))).days
