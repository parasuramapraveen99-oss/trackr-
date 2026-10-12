"""The Butler worker: durable deterministic events, due checkpoints and coverage (BT 05).

docs/BUTLER_ARCHITECTURE.md sections 6 and 8. RUNNABLE THE SAME TWO WAYS AS THE EXISTING
WORKER, because it IS the existing worker's job kind rather than a second scheduler::

    python -m app.jobs.butler                      # one pass over every eligible investor
    job kind ``butler_tick`` via app/jobs/drain.py  # enqueued like any other job

AT-LEAST-ONCE IN, EXACTLY ONE LOGICAL EFFECT OUT. Every saved note, source, thesis version,
answer, evaluation, retraction and review writes its outbox row in the same transaction
(051's ``butler_emit`` trigger). This worker claims a row with a leased claim, COMMITS the
claim, does the work, and commits the effect together with any inbox item. Migration 051 makes
a second effect for the same event impossible and refuses an effect from a stale lease, and
every inbox item carries a dedupe key over investor + subscription + kind + record version +
rule version. A crash before the claim commits leaves nothing; a crash after it leaves a lease
that expires and a later attempt that produces the one effect; a crash after the effect leaves
a finished event. Duplicate ticks and concurrent workers race to the same constraints.

WHAT IT NEVER DOES. No model call, no network fetch, no email/push/SMS, no provider key, no
billable retry. It never writes a note, answer, thesis, decision or transaction. It never turns
"nothing was fetched" into "nothing happened": no external feed exists, so every followed
company carries an explicit coverage card saying only workspace-saved documents are checked.
Capability revocation or a paused follow stops NEW cards; nothing already recorded is deleted.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from typing import Any

import psycopg

from app.database import workspace_connection
from app.db import butler as bd
from app.db.capabilities import has_capability
from app.db.connection import WorkspaceCapability
from app.db.migrate import butler_schema_ready
from app.db.workspace import lock_tenant
from app.db.workspace_context import enter_workspace
from app.research import butler_policy as bp
from app.research.source_policy import ALLOWED, LIVE_SOURCE_POLICY, USE_METADATA, SourcePolicy

logger = logging.getLogger(__name__)

KIND_BUTLER_TICK = "butler_tick"
ENGINE = WorkspaceCapability.ENGINE

#: Events that are receipts of the investor's own activity. Counted on read by the REVIEW_DUE
#: card; no card of their own -- Butler does not interrupt every note edit.
_ACTIVITY_ONLY = frozenset({"USER_NOTE_SAVED", "QUESTION_ANSWERED", "USER_REVIEW_REQUESTED"})


class _Suppressed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _presentation(conn: Any, investor_id: str, now: datetime) -> dict[str, Any]:
    pref = bd.current_preference(conn, investor_id)
    if pref is None:
        # No timezone chosen: nothing is inferred. Quiet hours cannot apply without a zone, so
        # items are visible on capture and the digest counts UTC days.
        return {"tz": UTC, "quiet_start": bp.DEFAULT_QUIET_START,
                "quiet_end": bp.DEFAULT_QUIET_START, "digest": bp.DEFAULT_DIGEST_LIMIT,
                "timezone": None}
    return {"tz": bp.zone(pref["timezone"]), "quiet_start": pref["quiet_start"],
            "quiet_end": pref["quiet_end"], "digest": pref["digest_limit"],
            "timezone": pref["timezone"]}


def _deliver_after(conn: Any, investor_id: str, now: datetime, priority: str,
                   pres: dict[str, Any]) -> datetime:
    start, end = bp.local_day_bounds(now, pres["tz"])
    today = bd.items_presented_between(conn, investor_id, start, end)
    return bp.deliver_after(now, priority, today, pres["tz"], pres["quiet_start"],
                            pres["quiet_end"], pres["digest"])


def _create(
    conn: Any, investor_id: str, now: datetime, pres: dict[str, Any], *, template_id: str,
    isin: str, subscription_id: str | None, issue_key: str, evidence_version: str,
    args: dict[str, str], dedupe: str, source_id: str | None = None,
    thesis_id: str | None = None, evaluation_id: str | None = None,
    supersedes: str | None = None, cooldown: bool = False,
) -> str:
    """Insert one template item unless it already exists or an unchanged issue is cooling
    down. Returns the item id; raises _Suppressed with a fixed reason otherwise."""
    existing = bd.item_by_dedupe(conn, investor_id, dedupe)
    if existing is not None:
        return existing
    previous = bd.latest_issue_item(conn, investor_id, issue_key)
    if previous is not None and cooldown and previous["evidence_version"] == evidence_version \
            and now - previous["recorded_at"] < bp.UNCHANGED_ISSUE_COOLDOWN:
        raise _Suppressed("UNCHANGED_ISSUE_COOLDOWN")
    if supersedes is None and previous is not None and not previous["superseded"] \
            and previous["evidence_version"] != evidence_version:
        supersedes = previous["id"]
    template = bp.CARDS[template_id]
    return bd.insert_item(conn, investor_id, {
        "card_kind": template.card_kind, "priority": template.priority, "isin": isin,
        "subscription_id": subscription_id, "template_id": template_id,
        "template_version": template.version, "rule_version": bp.RULE_VERSION,
        "issue_key": issue_key, "evidence_version": evidence_version, "args": args,
        "source_id": source_id, "thesis_id": thesis_id, "evaluation_id": evaluation_id,
        "supersedes_item_id": supersedes,
        "deliver_after": _deliver_after(conn, investor_id, now, template.priority, pres),
        "dedupe_key": dedupe,
    })


def _subscription_for(subs: list[dict[str, Any]], isin: str) -> dict[str, Any]:
    """The follow covering this ISIN's verified family. Paused/ended stop new work."""
    matching = [s for s in subs if isin in s["family"]]
    active = [s for s in matching if s["state"] == "ACTIVE"]
    if active:
        return active[0]
    if any(s["state"] == "PAUSED" for s in matching):
        raise _Suppressed("SUBSCRIPTION_PAUSED")
    raise _Suppressed("NOT_FOLLOWED")


def _origin(source: dict[str, Any]) -> str:
    """One filing redistributed by several vendors is ONE origin. Content hash when the bytes
    were retained; otherwise the canonical URL, case-folded, without a trailing slash."""
    if source.get("content_sha256"):
        return f"sha256:{source['content_sha256']}"
    return "url:" + (source.get("source_url") or "").strip().lower().rstrip("/")


def _process(
    conn: Any, investor_id: str, event: dict[str, Any], now: datetime, policy: SourcePolicy,
    subs: list[dict[str, Any]], pres: dict[str, Any],
) -> str:
    kind = event["event_kind"]
    rv = bp.RULE_VERSION
    if kind in _ACTIVITY_ONLY:
        raise _Suppressed("ACTIVITY_RECEIPT_ONLY")

    if kind == "SOURCE_VERSION_ADDED":
        # BT 10 kill switch: source-derived cards stop, recorded as a SUPPRESSED effect with a
        # named reason. The source row, memory and every other card are untouched, and the
        # status route reports the switch -- an outage never reads as "nothing new".
        from app.config import get_settings

        if not get_settings().butler_source_events_enabled:
            raise _Suppressed("SOURCE_INGESTION_DISABLED")
        sub = _subscription_for(subs, event["isin"])
        source = bd.source_row(conn, investor_id, event["record_id"])
        if source is None:
            raise _Suppressed("RECORD_UNAVAILABLE")
        if policy.verdict(source["source_type"], USE_METADATA) != ALLOWED:
            raise _Suppressed("SOURCE_RIGHTS_NOT_ALLOWED")
        origin = _origin(source)
        if bd.prior_origin_item(conn, investor_id, sub["id"], origin):
            raise _Suppressed("DUPLICATE_ORIGIN")
        item = _create(
            conn, investor_id, now, pres, template_id="NEW_MATERIAL", isin=event["isin"],
            subscription_id=sub["id"], issue_key=f"source:{source['id']}",
            evidence_version=source["id"], args={"source_id": source["id"], "origin": origin},
            dedupe=f"new_material:{sub['id']}:{source['id']}:{rv}", source_id=source["id"],
        )
        if source["extraction_status"] == "UNAVAILABLE":
            _create(
                conn, investor_id, now, pres, template_id="COVERAGE_TEXT_UNAVAILABLE",
                isin=event["isin"], subscription_id=sub["id"],
                issue_key=f"text_unavailable:{source['id']}", evidence_version=source["id"],
                args={"source_id": source["id"]},
                dedupe=f"text_unavailable:{sub['id']}:{source['id']}:{rv}",
                source_id=source["id"],
            )
        return item

    if kind == "THESIS_VERSION_ADDED":
        sub = _subscription_for(subs, event["isin"])
        old, new = bd.thesis_pair(conn, investor_id, event["record_id"])
        if new is None:
            raise _Suppressed("RECORD_UNAVAILABLE")
        if old is None:
            raise _Suppressed("FIRST_VERSION")
        forgotten = bd.forgotten_refs(conn, investor_id)
        if {("THESIS", old["id"]), ("THESIS", new["id"])} & forgotten:
            raise _Suppressed("MEMORY_FORGOTTEN")
        return _create(
            conn, investor_id, now, pres, template_id="THESIS_CHANGED", isin=new["isin"],
            subscription_id=sub["id"], issue_key=f"thesis:{new['isin']}",
            evidence_version=new["id"],
            args={"old_thesis_id": old["id"], "new_thesis_id": new["id"]},
            dedupe=f"thesis_changed:{sub['id']}:{new['id']}:{rv}", thesis_id=new["id"],
        )

    if kind == "EVALUATION_RECORDED":
        sub = _subscription_for(subs, event["isin"])
        old, new = bd.evaluation_pair(conn, investor_id, event["record_id"])
        if new is None:
            raise _Suppressed("RECORD_UNAVAILABLE")
        previous = old["state"] if old else "NONE"
        if previous == new["state"] or (old is None and new["state"] == "NOT_TRIGGERED"):
            raise _Suppressed("UNCHANGED_STATE")
        return _create(
            conn, investor_id, now, pres, template_id="RULE_STATE_CHANGED", isin=new["isin"],
            subscription_id=sub["id"], issue_key=f"rule:{new['thesis_id']}",
            evidence_version=f"{new['state']}:{new['id']}",
            args={"evaluation_id": new["id"], "thesis_id": new["thesis_id"],
                  "thesis_version": str(new["version"]), "state": new["state"],
                  "previous_state": previous},
            dedupe=f"rule_state:{sub['id']}:{new['id']}:{rv}", thesis_id=new["thesis_id"],
            evaluation_id=new["id"], cooldown=True,
        )

    if kind == "SOURCE_RETRACTED":
        retraction = bd._one(
            conn, "SELECT id, source_id, isin, reason_code FROM butler_source_retraction "
                  "WHERE investor_id = %s AND id = %s", (investor_id, event["record_id"]))
        if retraction is None:
            raise _Suppressed("RECORD_UNAVAILABLE")
        cited = bd._one(
            conn, "SELECT i.id FROM butler_inbox_item i WHERE i.investor_id = %s "
                  "AND i.source_id = %s AND NOT EXISTS (SELECT 1 FROM butler_inbox_item n "
                  "WHERE n.investor_id = i.investor_id AND n.supersedes_item_id = i.id) "
                  "ORDER BY i.seq DESC LIMIT 1",
            (investor_id, retraction["source_id"]))
        matching = [s for s in subs if retraction["isin"] in s["family"]]
        if cited is None and not matching:
            raise _Suppressed("NOT_FOLLOWED")
        # A correction is shown even for a paused follow when an earlier card cited the source:
        # leaving a withdrawn claim looking current would be worse than an interruption.
        return _create(
            conn, investor_id, now, pres, template_id="SOURCE_CORRECTION",
            isin=retraction["isin"], subscription_id=matching[0]["id"] if matching else None,
            issue_key=f"correction:{retraction['source_id']}",
            evidence_version=retraction["id"],
            args={"source_id": retraction["source_id"], "retraction_id": retraction["id"]},
            dedupe=f"correction:{retraction['id']}:{rv}", source_id=retraction["source_id"],
            supersedes=cited["id"] if cited else None,
        )

    raise _Suppressed("UNHANDLED_EVENT_KIND")


def _scheduled_cards(
    conn: Any, investor_id: str, now: datetime, subs: list[dict[str, Any]], pres: dict[str, Any],
) -> tuple[int, int]:
    """Due checkpoints and coverage cards for every ACTIVE company follow. (created, skipped)"""
    created = skipped = 0
    rv = bp.RULE_VERSION
    for sub in subs:
        if sub["state"] != "ACTIVE":
            continue
        try:
            before = bd.item_by_dedupe(conn, investor_id, f"coverage_no_feed:{sub['id']}:{rv}")
            _create(conn, investor_id, now, pres, template_id="COVERAGE_NO_FEED",
                    isin=sub["isin"], subscription_id=sub["id"],
                    issue_key=f"coverage:{sub['id']}", evidence_version="no-feed",
                    args={"subscription_id": sub["id"]},
                    dedupe=f"coverage_no_feed:{sub['id']}:{rv}", cooldown=True)
            created += before is None
        except _Suppressed:
            skipped += 1
        # A checkpoint needs BOTH an explicit cadence and an explicit timezone. Neither is
        # inferred; without them nothing is scheduled and the subscription says why.
        if not sub["checkpoint_days"] or pres["timezone"] is None:
            continue
        tz = pres["tz"]
        anchor = sub["checkpoint_set_at"].astimezone(tz).date()
        due = bp.checkpoint_due_on(anchor, sub["checkpoint_days"], now.astimezone(tz).date())
        if due is None:
            continue
        dedupe = f"review_due:{sub['id']}:{due.isoformat()}:{sub['checkpoint_days']}:{rv}"
        if bd.item_by_dedupe(conn, investor_id, dedupe):
            continue
        start, end = bd.checkpoint_window(sub, pres["timezone"], due)
        counts = bd.activity_counts(conn, investor_id, sub["family"], start, end)
        _create(
            conn, investor_id, now, pres, template_id="REVIEW_DUE", isin=sub["isin"],
            subscription_id=sub["id"], issue_key=f"checkpoint:{sub['id']}",
            evidence_version=due.isoformat(),
            args={"subscription_id": sub["id"], "due_on": due.isoformat(),
                  "window_start": start.astimezone(tz).date().isoformat(),
                  "window_end": end.astimezone(tz).date().isoformat(),
                  **{k: str(v) for k, v in counts.items()}},
            dedupe=dedupe,
        )
        created += 1
    return created, skipped


def tick_investor(
    investor_id: str, *, now: datetime | None = None, policy: SourcePolicy = LIVE_SOURCE_POLICY,
    workspace_allowed: bool = True, connection: Any = None, notebook: bool = False,
) -> dict[str, Any]:
    """One pass for one investor on an ENGINE connection. Commits per step.

    ``notebook`` is whether 053 is applied (resolved by the caller on the financial connection,
    which may read schema_migrations). When true and the investor has ACTIVE notebook consent,
    the pass also builds episodes and deterministic pattern revisions (BT 13) -- no model.

    ``connection`` lets a test supply a fixture-owned engine connection; production opens its
    own. ``workspace_allowed`` is the owner's CURRENT capability, resolved by the caller.
    """
    # The receipt's start is the WALL clock -- 051 refuses a tick that "finished" before it
    # started. A controlled ``now`` only drives the logical decisions below.
    started = datetime.now(UTC)
    counts = {"processed": 0, "cards": 0, "suppressed": 0, "failed": 0}

    def clock() -> datetime:
        # A controlled clock (tests, replays) stays fixed; production reads the wall clock.
        # Leases are the database's own clock either way (051's butler_guard).
        return now if now is not None else datetime.now(UTC)

    if connection is not None:
        return _tick(connection, investor_id, started, clock, policy, workspace_allowed,
                     counts, notebook)
    with workspace_connection(ENGINE) as conn:
        return _tick(conn, investor_id, started, clock, policy, workspace_allowed, counts,
                     notebook)


def _notebook_step(conn: Any, investor_id: str, now: datetime) -> dict[str, Any] | None:
    """BT 13's deterministic notebook pass, in its own transaction. Consent revoked mid-pass
    rolls the whole step back (053 refuses the writes); nothing partial lands."""
    from app.db import butler_episodes as bep

    try:
        with enter_workspace(conn, ENGINE, investor_id):
            lock_tenant(conn, investor_id)
            result = bep.notebook_pass(conn, investor_id, now)
        conn.commit()
        return result
    except bep.ConsentChanged:
        conn.rollback()
        return {"state": "CONSENT_CHANGED", "episodes": 0, "revisions": 0}
    except Exception:
        conn.rollback()
        logger.exception("butler notebook pass failed for investor %s", investor_id)
        return {"state": "FAILED", "episodes": 0, "revisions": 0}


def _tick(conn: Any, investor_id: str, started: datetime, clock: Any, policy: SourcePolicy,
          workspace_allowed: bool, counts: dict[str, int], notebook: bool = False
          ) -> dict[str, Any]:
    if not workspace_allowed:
        with enter_workspace(conn, ENGINE, investor_id):
            bd.record_tick(conn, investor_id, started, "SKIPPED_REVOKED", counts, None)
        conn.commit()
        return {"investor_id": investor_id, "outcome": "SKIPPED_REVOKED", **counts}

    with enter_workspace(conn, ENGINE, investor_id):
        events = bd.pending_events(conn, investor_id, clock(), bp.TICK_EVENT_BATCH)
    conn.commit()
    for event in events:
        attempt = int(event["attempts"]) + 1
        try:
            with enter_workspace(conn, ENGINE, investor_id):
                claim_id = bd.claim_event(conn, investor_id, event["id"], attempt)
            conn.commit()
        except psycopg.errors.RaiseException:
            conn.rollback()  # another worker holds it, or it finished meanwhile
            continue
        try:
            with enter_workspace(conn, ENGINE, investor_id):
                lock_tenant(conn, investor_id)
                if attempt > bp.MAX_EVENT_ATTEMPTS:
                    bd.record_effect(conn, investor_id, event["id"], claim_id, "FAILED",
                                     "MAX_ATTEMPTS")
                    counts["failed"] += 1
                else:
                    subs = bd.company_subscriptions(conn, investor_id)
                    pres = _presentation(conn, investor_id, clock())
                    try:
                        item_id = _process(conn, investor_id, event, clock(), policy, subs, pres)
                        bd.record_effect(conn, investor_id, event["id"], claim_id,
                                         "CARD_CREATED", "CARD_CREATED", item_id)
                        counts["cards"] += 1
                    except _Suppressed as s:
                        bd.record_effect(conn, investor_id, event["id"], claim_id, "SUPPRESSED",
                                         s.reason)
                        counts["suppressed"] += 1
            conn.commit()
            counts["processed"] += 1
        except Exception:
            conn.rollback()
            counts["failed"] += 1
            logger.exception("butler event %s failed for investor %s", event["id"], investor_id)

    outcome = "OK"
    try:
        with enter_workspace(conn, ENGINE, investor_id):
            lock_tenant(conn, investor_id)
            subs = bd.company_subscriptions(conn, investor_id)
            created, _ = _scheduled_cards(conn, investor_id, clock(), subs,
                                          _presentation(conn, investor_id, clock()))
            counts["cards"] += created
        conn.commit()
    except Exception:
        conn.rollback()
        outcome = "PARTIAL"
        logger.exception("butler scheduled cards failed for investor %s", investor_id)
    notebook_result = _notebook_step(conn, investor_id, clock()) if notebook else None
    if notebook_result is not None and notebook_result["state"] == "FAILED":
        outcome = "PARTIAL"
    if counts["failed"]:
        outcome = "PARTIAL"
    with enter_workspace(conn, ENGINE, investor_id):
        status = bd.worker_status(conn, investor_id, clock())
        bd.record_tick(conn, investor_id, started, outcome, counts, status["oldest_pending_at"])
    conn.commit()
    return {"investor_id": investor_id, "outcome": outcome, **counts,
            **({"notebook": notebook_result} if notebook_result is not None else {})}


def eligible_investors(fin_conn: Any, investor_ids: list[str] | None = None
                       ) -> list[tuple[str, bool]]:
    """(investor_id, owner currently holds Workspace) for every owned investor. Revoked owners
    are returned too, so their tick records SKIPPED_REVOKED instead of silently vanishing."""
    with fin_conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, owner_user_id::text FROM investor WHERE owner_user_id IS NOT NULL "
            "AND (%s::uuid[] IS NULL OR id = ANY(%s::uuid[])) ORDER BY id",
            (investor_ids, investor_ids),
        )
        rows = cur.fetchall()
    return [(inv, has_capability(fin_conn, owner, "workspace")) for inv, owner in rows]


def tick(fin_conn: Any, investor_ids: list[str] | None = None,
         policy: SourcePolicy = LIVE_SOURCE_POLICY) -> list[dict[str, Any]]:
    """One pass over every eligible investor. One investor's failure never stops the others."""
    if not butler_schema_ready(fin_conn):
        # 051 not applied: say so, never a traceback per investor.
        return [{"investor_id": None, "outcome": "SCHEMA_NOT_READY"}]
    from app.db.migrate import notebook_schema_ready

    notebook = notebook_schema_ready(fin_conn)
    results = []
    for investor_id, allowed in eligible_investors(fin_conn, investor_ids):
        try:
            results.append(tick_investor(investor_id, policy=policy, workspace_allowed=allowed,
                                         notebook=notebook))
        except Exception:
            logger.exception("butler tick failed for investor %s", investor_id)
            results.append({"investor_id": investor_id, "outcome": "FAILED"})
    return results


def handle_job(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """``app/jobs/drain.py`` handler for ``butler_tick``. Payload: {"investor_id"?: uuid}."""
    investor = payload.get("investor_id")
    ids = [bd.parse_uuid(investor)] if investor else None
    if investor and ids == [None]:
        from app.jobs.drain import PermanentJobError

        raise PermanentJobError(f"investor_id is not a uuid: {investor!r}")
    results = tick(conn, ids)
    return {"ticks": len(results),
            "outcomes": sorted({str(r.get("outcome")) for r in results})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Butler worker pass.")
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
