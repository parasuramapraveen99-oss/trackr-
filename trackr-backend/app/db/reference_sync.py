"""Durable, shared public-data work. A crashed worker never loses an import request."""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, timedelta
from typing import Any

from app.db.reference_notice_outcome import cleared_notice_clause
from app.ingest.nsdl import SearchPage


def enqueue(conn, kind: str, key: str, payload: dict, *, refresh: bool = False) -> None:
    """Queue (or, with ``refresh``, re-request) a task.

    A refresh replaces the request but keeps the ``snapshot_*`` keys a REPLAY records about
    the ledger it last saw (app.reference_worker). They are the baseline that limits the
    next replay's invalidation to the days a change can reach; dropping them made every
    import delete and requeue the investor's whole snapshot history.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO reference_sync_task(kind, task_key, payload)
               VALUES (%s, %s, %s::jsonb)
               ON CONFLICT(kind, task_key) DO UPDATE SET
                   payload = CASE WHEN %s THEN
                                  COALESCE((SELECT jsonb_object_agg(e.key, e.value)
                                            FROM jsonb_each(reference_sync_task.payload) e
                                            WHERE left(e.key, 9) = 'snapshot_'), '{}'::jsonb)
                                  || EXCLUDED.payload
                                  ELSE reference_sync_task.payload END,
                   state = CASE WHEN %s THEN 'pending' ELSE reference_sync_task.state END,
                   available_at = CASE WHEN %s THEN now()
                                       ELSE reference_sync_task.available_at END
               WHERE %s""",
            (kind, key, json.dumps(payload), refresh, refresh, refresh, refresh),
        )


def schedule(conn, today: date | None = None) -> None:
    """Coalesce uploads; shared issuer/month tasks prevent per-investor scraping."""
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.account_id::text, min(t.txn_date),
                      array_agg(DISTINCT t.isin), r.requested_at, r.scheduled_at
               FROM reference_sync_import r JOIN transaction t ON t.account_id = r.account_id
               WHERE t.source != 'CORP_ACTION'
                 AND (r.scheduled_at IS NULL OR r.requested_at > r.scheduled_at
                      OR r.scheduled_at::date < %s)
               GROUP BY r.account_id, r.requested_at, r.scheduled_at
               ORDER BY r.account_id""",
            (today,),
        )
        accounts = cur.fetchall()
    for account_id, start, isins, requested, scheduled in accounts:
        changed = scheduled is None or requested > scheduled
        dependencies = []
        for isin in sorted({isin for isin in isins if isin.startswith("IN")}):
            # Exact ISIN, never an issuer prefix: NSDL's offset pagination is unstable
            # (observed 2026-09-29: INE423A reported 684 rows, a full walk returned only
            # 413 distinct), so a prefix traversal can never reconcile. A full-ISIN
            # query is one page. A new daily key makes the observation date explicit.
            key = f"{isin}:{today.isoformat()}"
            enqueue(conn, "NSDL", key, {"query": isin, "page": 1, "seen": []})
            dependencies.append((account_id, "NSDL", key))
        cursor = start.replace(day=1)
        while cursor <= today:
            end = min(cursor.replace(day=calendar.monthrange(cursor.year, cursor.month)[1]), today)
            key = f"{cursor.isoformat()}:{end.isoformat()}"
            enqueue(conn, "NSE", key, {"start": cursor.isoformat(), "end": end.isoformat()})
            dependencies.append((account_id, "NSE", key))
            cursor = end + timedelta(days=1)
        enqueue(conn, "REPLAY", account_id, {"account_id": account_id}, refresh=changed)
        dependencies.append((account_id, "REPLAY", account_id))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO reference_sync_dependency(account_id, kind, task_key)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                dependencies,
            )
            cur.execute(
                "UPDATE reference_sync_import SET scheduled_at = clock_timestamp() "
                "WHERE account_id = %s AND requested_at = %s",
                (account_id, requested),
            )
    conn.commit()


def store_nsdl_page(conn, page: SearchPage, prefix: str) -> int:
    """Never delete absent securities or turn a failed search into an inactive record.

    An unchanged record is not rewritten: identity lookups repeat daily per held ISIN, and
    each rewrite of a shared table invalidated every investor's cached analytics report
    (migration 093). ``observed_at`` is therefore when the current content was observed;
    every distinct record stays in ``security_reference_history``."""
    matched = [r for r in page.records if r.isin.startswith(prefix)]
    with conn.cursor() as cur:
        for record in matched:
            raw = json.dumps(record.raw, sort_keys=True)
            # Issuer searches include debt/preference series. Keep their exact registry
            # observations without registering them as equities in the portfolio master.
            if record.instrument.upper() == "EQUITY SHARES":
                cur.execute(
                    """INSERT INTO security(isin, name) VALUES (%s,%s)
                       ON CONFLICT(isin) DO UPDATE
                           SET name = COALESCE(security.name, EXCLUDED.name)
                       WHERE security.name IS NULL AND EXCLUDED.name IS NOT NULL""",
                    (record.isin, record.issuer),
                )
            cur.execute(
                """INSERT INTO security_reference
                       (isin, issuer, description, instrument, status, source_url,
                        response_sha256, raw_payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT(isin) DO UPDATE SET issuer=EXCLUDED.issuer,
                       description=EXCLUDED.description, instrument=EXCLUDED.instrument,
                       status=EXCLUDED.status, source_url=EXCLUDED.source_url,
                       response_sha256=EXCLUDED.response_sha256,
                       raw_payload=EXCLUDED.raw_payload, observed_at=now()
                   WHERE (security_reference.issuer, security_reference.description,
                          security_reference.instrument, security_reference.status,
                          security_reference.raw_payload)
                       IS DISTINCT FROM (EXCLUDED.issuer, EXCLUDED.description,
                          EXCLUDED.instrument, EXCLUDED.status, EXCLUDED.raw_payload)""",
                (
                    record.isin,
                    record.issuer,
                    record.description,
                    record.instrument,
                    record.status,
                    page.url,
                    page.sha256,
                    raw,
                ),
            )
            cur.execute(
                """INSERT INTO security_reference_history
                       (isin, record_sha256, source_url, raw_payload) VALUES (%s,%s,%s,%s::jsonb)
                   ON CONFLICT DO NOTHING""",
                (record.isin, hashlib.sha256(raw.encode()).hexdigest(), page.url, raw),
            )
    return len(matched)


def _entitled_notices(conn, investor_id: str, rows: list[tuple]) -> list[dict[str, Any]]:
    """Notice questions worth asking: only where the investor held shares the day before
    the ex-date. A rights issue or buyback reaches only holders on that date, so with none
    held there is nothing to take part in and Trackr+ answers it itself (ADANIENT's
    Nov-2025 rights, asked of an account that had sold out in May 2023). Each question
    carries the company and the shares held, so it can be answered without looking up an
    ISIN. A notice without a readable date is kept: it cannot be checked, so it is asked."""
    from datetime import datetime

    from app import securities
    from app.db.repository import load_transactions
    from app.engine.holdings import quantity_held_before

    if not rows:
        return []
    transactions = load_transactions(conn, investor_id=investor_id)
    meta = securities.lookup(conn, sorted({r[0] for r in rows}))
    out = []
    for isin, subject, ex_date in rows:
        held = None
        try:
            on = datetime.strptime(ex_date, "%d-%b-%Y").date()
        except (TypeError, ValueError):
            on = None
        if on is not None:
            held = quantity_held_before(transactions, isin, on)
            if held <= 0:
                continue
        info = meta.get(isin) or {}
        out.append(
            {
                "isin": isin,
                "related_isin": None,
                "reason": subject,
                "ex_date": ex_date,
                "name": info.get("name"),
                "symbol": info.get("symbol"),
                "held": str(held) if held is not None else None,
            }
        )
    return out


def status(conn, investor_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT t.state, count(*), max(t.last_success_at), max(t.updated_at)
               FROM reference_sync_task t JOIN (
                   SELECT DISTINCT d.kind, d.task_key FROM reference_sync_dependency d
                   JOIN account a ON a.id=d.account_id WHERE a.investor_id=%s
               ) d USING(kind, task_key) WHERE t.kind <> 'NSE' GROUP BY t.state""",
            (investor_id,),
        )
        states = cur.fetchall()
        cur.execute(
            """SELECT count(*) FROM reference_sync_import r JOIN account a ON a.id=r.account_id
               WHERE a.investor_id=%s AND
                   (r.scheduled_at IS NULL OR r.requested_at > r.scheduled_at)""",
            (investor_id,),
        )
        unscheduled = cur.fetchone()[0]
        # Is the automatic gap fix still to run for this investor? While it is, a holding
        # "left out" may be about to be fixed, so the screen says "checking" instead of
        # asking the investor to type in a purchase (app.db.gap_resolution). A task in
        # retry has already run the exchange fixes once and waits only on AI research,
        # so it does not hold the screen in "checking" for the 30 minutes it waits.
        cur.execute(
            """SELECT EXISTS (SELECT 1 FROM reference_sync_task t JOIN account a
                   ON a.id::text = t.task_key
               WHERE a.investor_id=%s AND t.kind IN ('REPLAY','GAP')
                   AND t.state IN ('pending','running'))""",
            (investor_id,),
        )
        checking_gaps = bool(cur.fetchone()[0]) or unscheduled > 0
        cur.execute(
            """SELECT DISTINCT s.old_isin, s.current_isin, s.verification::text
               FROM security_alias s JOIN transaction t
                   ON t.isin IN (s.old_isin,s.current_isin)
               JOIN account a ON a.id=t.account_id
               WHERE a.investor_id=%s AND s.verification != 'VERIFIED'""",
            (investor_id,),
        )
        aliases = [{"isin": r[0], "related_isin": r[1], "reason": r[2]} for r in cur.fetchall()]
        # Only notices that could have touched a holding: ex-date on or after the account's
        # first trade in that ISIN. ABFRL's May-2025 demerger was flagged for an account
        # that first bought on 2025-08-05 -- no entitlement, so nothing to review.
        # A notice the investor has already answered with a clearing outcome (migration
        # 067) is not unresolved. The ex-date travels with each item so the screen can
        # send the same identity back when the investor answers.
        cur.execute(
            """SELECT DISTINCT n.isin,n.subject,n.ex_date FROM reference_notice n JOIN (
                   SELECT t.isin, min(t.txn_date) AS first_txn FROM transaction t
                   JOIN account a ON a.id=t.account_id WHERE a.investor_id=%s
                   GROUP BY t.isin
               ) h ON h.isin=n.isin
               WHERE (n.ex_date !~ '^\\d{2}-[A-Za-z]{3}-\\d{4}$'
                  OR to_date(n.ex_date, 'DD-Mon-YYYY') >= h.first_txn)
                 AND NOT """
            + cleared_notice_clause(),
            (investor_id, investor_id),
        )
        aliases.extend(_entitled_notices(conn, investor_id, cur.fetchall()))
        cur.execute(
            """SELECT DISTINCT t.isin,r.status FROM transaction t
               JOIN account a ON a.id=t.account_id
               LEFT JOIN security_reference r ON r.isin=t.isin
               LEFT JOIN security_alias s ON s.old_isin=t.isin
                   AND s.verification='VERIFIED' AND s.effective_date <= current_date
               WHERE a.investor_id=%s AND s.old_isin IS NULL
                   AND (r.isin IS NULL OR upper(r.status) != 'ACTIVE')""",
            (investor_id,),
        )
        aliases.extend(
            {
                "isin": r[0],
                "related_isin": None,
                "reason": (
                    f"NSDL status: {r[1]}; replacement identity is not established"
                    if r[1]
                    else "Exact NSDL record has not been observed"
                ),
            }
            for r in cur.fetchall()
        )
        cur.execute(
            "SELECT DISTINCT i.isin,i.reason FROM reference_account_issue i "
            "JOIN account a ON a.id=i.account_id WHERE a.investor_id=%s",
            (investor_id,),
        )
        aliases.extend({"isin": r[0], "related_isin": None, "reason": r[1]} for r in cur.fetchall())
    missing_reference = [
        {
            "kind": kind,
            "reason_code": "REFERENCE_DATA_MISSING",
            "details": payload.get("reference_missing", []),
        }
        for kind, payload in conn.execute(
            "SELECT DISTINCT t.kind,t.payload FROM reference_sync_task t "
            "JOIN reference_sync_dependency d USING(kind,task_key) "
            "JOIN account a ON a.id=d.account_id "
            "WHERE a.investor_id=%s AND (t.payload ? 'reference_missing' "
            "OR (t.kind='NSE' AND t.state <> 'done'))",
            (investor_id,),
        ).fetchall()
    ]
    counts = {row[0]: row[1] for row in states}
    active = counts.get("pending", 0) + counts.get("running", 0) + unscheduled
    retry = counts.get("retry", 0)
    last_success = max((row[2] for row in states if row[2]), default=None)
    latest = max((row[3] for row in states if row[3]), default=None)
    phase = (
        "retrying"
        if retry
        else "processing"
        if active
        else "needs_evidence"
        if aliases or missing_reference
        else "idle"
    )
    return {
        "state": phase,
        "pending": active,
        "retrying": retry,
        "completed": counts.get("done", 0),
        "last_success_at": last_success,
        "updated_at": latest,
        "unresolved": aliases,
        "checking_gaps": checking_gaps,
        "missing_reference": missing_reference,
    }
