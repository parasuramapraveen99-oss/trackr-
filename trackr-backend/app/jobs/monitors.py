"""The monitor tick: record due occurrences, blocked honestly (BE 36-37).

RUNNABLE THE SAME TWO WAYS AS THE BUTLER WORKER, because it is one more job kind on the same
durable queue rather than a second scheduler::

    python -m app.jobs.monitors                     # one pass over every eligible investor
    job kind ``monitor_tick`` via app/jobs/drain.py  # enqueued like any other job

It is an internal authenticated job function, not an HTTP endpoint: it runs on the SCHEDULER
workspace capability, which can insert a due occurrence and nothing a person authors.

WHAT ONE PASS DOES, per investor and schedule, in its own transaction: take the tenant lock,
compute what is due at the controlled clock (``app/research/monitor.py``), and append the missing
occurrences. While BE-Q01 is open every occurrence is BLOCKED_EXECUTION_POLICY,
SUPERSEDED_THESIS or BLOCKED_EVENT with a reason; there is no QUEUED path, no task, no model call,
no network fetch and no notification. A repeated pass, two overlapping workers or a retry after a
crash converge on the same rows (043/057 uniqueness plus the tenant lock).

CAPABILITY REVOCATION stops new occurrences (the owner no longer holds Workspace) and deletes
nothing already recorded.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.database import workspace_connection
from app.db import monitors as mon
from app.db.connection import WorkspaceCapability
from app.db.migrate import monitor_schema_ready
from app.db.workspace_context import enter_workspace
from app.jobs.butler import eligible_investors
from app.research.source_policy import LIVE_SOURCE_POLICY, SourcePolicy

logger = logging.getLogger(__name__)

KIND_MONITOR_TICK = "monitor_tick"
SCHEDULER = WorkspaceCapability.SCHEDULER


def tick_investor(investor_id: str, *, now: datetime | None = None,
                  policy: SourcePolicy = LIVE_SOURCE_POLICY, connection: Any = None
                  ) -> dict[str, Any]:
    """One pass for one investor on a SCHEDULER connection, committing per schedule.

    ``now`` drives the logical clock (tests drive it explicitly, including a repeated tick);
    ``connection`` lets a test supply a fixture-owned scheduler connection.
    """
    clock = now if now is not None else datetime.now(UTC)
    if connection is not None:
        return _tick(connection, investor_id, clock, policy)
    with workspace_connection(SCHEDULER) as conn:
        return _tick(conn, investor_id, clock, policy)


def _tick(conn: Any, investor_id: str, now: datetime, policy: SourcePolicy) -> dict[str, Any]:
    totals = {"schedules": 0, "recorded": 0, "blocked_execution_policy": 0,
              "superseded_thesis": 0, "blocked_event": 0, "failed": 0}
    with enter_workspace(conn, SCHEDULER, investor_id):
        ids = mon.schedule_ids(conn, investor_id)
    conn.commit()
    for schedule_id in ids:
        try:
            with enter_workspace(conn, SCHEDULER, investor_id):
                counts = mon.tick_schedule(conn, investor_id, schedule_id, now, policy)
            conn.commit()
            totals["schedules"] += 1
            for k, n in counts.items():
                totals[k] += n
        except Exception:
            conn.rollback()
            totals["failed"] += 1
            logger.exception("monitor tick failed for schedule %s of investor %s",
                             schedule_id, investor_id)
    outcome = "PARTIAL" if totals["failed"] else "OK"
    return {"investor_id": investor_id, "outcome": outcome, **totals}


def tick(fin_conn: Any, investor_ids: list[str] | None = None,
         policy: SourcePolicy = LIVE_SOURCE_POLICY, now: datetime | None = None
         ) -> list[dict[str, Any]]:
    """One pass over every owned investor. A revoked owner is reported, never ticked."""
    if not monitor_schema_ready(fin_conn):
        return [{"investor_id": None, "outcome": "SCHEMA_NOT_READY"}]
    results = []
    for investor_id, allowed in eligible_investors(fin_conn, investor_ids):
        if not allowed:
            results.append({"investor_id": investor_id, "outcome": "SKIPPED_REVOKED"})
            continue
        try:
            results.append(tick_investor(investor_id, now=now, policy=policy))
        except Exception:
            logger.exception("monitor tick failed for investor %s", investor_id)
            results.append({"investor_id": investor_id, "outcome": "FAILED"})
    return results


def handle_job(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """``app/jobs/drain.py`` handler for ``monitor_tick``. Payload: {"investor_id"?: uuid}."""
    from app.db.butler import parse_uuid

    investor = payload.get("investor_id")
    ids = [parse_uuid(investor)] if investor else None
    if investor and ids == [None]:
        from app.jobs.drain import PermanentJobError

        raise PermanentJobError(f"investor_id is not a uuid: {investor!r}")
    results = tick(conn, ids)
    return {"ticks": len(results),
            "outcomes": sorted({str(r.get("outcome")) for r in results})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one monitor tick pass.")
    parser.add_argument("--investor", action="append", help="limit to this investor id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from app.db.connection import connect, database_url

    with connect(database_url()) as fin_conn:
        results = tick(fin_conn, args.investor)
    for r in results:
        print(r)
    return 0 if all(r.get("outcome") in ("OK", "SKIPPED_REVOKED", "SCHEMA_NOT_READY")
                    for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
