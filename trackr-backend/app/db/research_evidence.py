"""External evidence storage and reads: adapter sources, revised filings, dated events (BE 29/34).

Same contract as ``app/db/workspace.py``: every function takes a connection already scoped by
``enter_workspace`` for one investor. Raw SQL, append-only, no ledger, no financial table.

IDEMPOTENT BY CONSTRUCTION. A source's dedupe key is (adapter, origin item, content hash); a
fact's and an event's keys extend it. A repeated refresh over the same periods finds every key
and writes nothing -- the summary says so. Nothing is ever updated or deleted:

* same origin item, SAME bytes   -> duplicate, no-op;
* same origin item, NEW bytes    -> a new source that REVISES the previous head (055); each
  fact in the same series supersedes the revised source's current fact, with the revision as
  its citation. Unrelated sources never supersede each other; two differing values from two
  different filings remain a CONFLICT for the engine to report;
* extracted values               -> UNVERIFIED facts only if the metric is an approved registry
  entry (BE-Q03); otherwise reported UNKNOWN / METRIC_NOT_APPROVED and not stored;
* extracted events               -> UNVERIFIED research_event rows with exact identity, or
  UNKNOWN when identity is incomplete. A claimed (rumoured) date never fills a date field.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from app.db import research as rs
from app.db import workspace as ws
from app.engine import research_rules as rr
from app.research import sources as src
from app.research.errors import WorkspaceError, conflict, invalid, not_found
from app.research.registry import check_fact_against_registry

OP_REFRESH = "source.refresh"
OP_EVENT_VERIFY = "event.verify"

_rows = ws._rows
_one = ws._one
_uuid = ws._uuid

EVENT_NOT_VERIFIABLE = "EVENT_NOT_VERIFIABLE"
STALE_PREDECESSOR = "STALE_PREDECESSOR"


def _source_key(adapter_id: str, origin_item_id: str, sha: str | None) -> str:
    return f"adapter:{adapter_id}:item:{origin_item_id}:{sha or 'metadata'}"


def _origin_head(conn: Any, investor_id: str, adapter_id: str, origin_item_id: str
                 ) -> dict[str, Any] | None:
    """The latest retrieval of this origin item that nothing has revised yet."""
    return _one(conn, """
        SELECT s.id, s.content_sha256, s.isin FROM research_source s
         WHERE s.investor_id = %s AND s.adapter_id = %s AND s.origin_item_id = %s
           AND NOT EXISTS (SELECT 1 FROM research_source r WHERE r.investor_id = s.investor_id
                             AND r.revises_source_id = s.id)
         ORDER BY s.recorded_at DESC LIMIT 1
        """, (investor_id, adapter_id, origin_item_id))


def _fact_head_in_source(conn: Any, investor_id: str, source_id: str, f: src.ExtractedFact
                         ) -> str | None:
    row = _one(conn, """
        SELECT f.id FROM research_fact f
         WHERE f.investor_id = %s AND f.source_id = %s AND f.metric = %s AND f.basis = %s
           AND f.unit = %s AND f.cadence = %s AND f.period_start = %s AND f.period_end = %s
           AND NOT EXISTS (SELECT 1 FROM research_fact n WHERE n.investor_id = f.investor_id
                             AND n.supersedes_id = f.id)
        """, (investor_id, source_id, f.metric, f.basis, f.unit, f.cadence, f.period_start,
              f.period_end))
    return row["id"] if row else None


def _exists(conn: Any, table: str, investor_id: str, key: str) -> str | None:
    row = _one(conn, f"SELECT id FROM {table} WHERE investor_id = %s AND dedupe_key = %s",
               (investor_id, key))
    return row["id"] if row else None


def record_refresh(
    conn: Any,
    investor_id: str,
    actor_uid: str,
    isin: str,
    adapter: src.Adapter,
    prepared: list[src.Prepared],
    unknowns: list[dict[str, Any]],
    retained: dict[str, dict[str, Any]],
    registry: rr.MetricRegistry,
    key: str,
    payload_digest: str,
    periods: list[str],
) -> tuple[dict[str, Any], int]:
    """Store one bounded refresh atomically. ``retained`` maps origin_item_id to the verified
    document row (and extraction pin) the router stored on the financial connection."""
    isin = ws.require_unlocked(conn, investor_id, isin)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_REFRESH, isin, key, payload_digest)
    if receipt is not None:
        return {**receipt.result, "replayed": True}, 200

    summary: dict[str, Any] = {
        "adapter_id": adapter.adapter_id, "synthetic": adapter.synthetic,
        "isin": isin, "periods": list(periods),
        "sources": [], "facts": [], "events": [], "unknowns": list(unknowns),
        "duplicates": [], "revisions": [],
    }
    for p in prepared:
        it = p.item
        sha = it.content_sha256
        skey = _source_key(adapter.adapter_id, it.origin_item_id, sha)
        existing = _exists(conn, "research_source", investor_id, skey)
        if existing is not None:
            summary["duplicates"].append({"origin_item_id": it.origin_item_id,
                                          "source_id": existing})
            source_id = existing
            revised = None
        else:
            head = _origin_head(conn, investor_id, adapter.adapter_id, it.origin_item_id)
            revised = head["id"] if head is not None and head["content_sha256"] != sha else None
            if head is not None and head["isin"] not in ws.isin_family(conn, isin):
                summary["unknowns"].append({"origin_item_id": it.origin_item_id,
                                            "code": "ORIGIN_ITEM_OF_ANOTHER_COMPANY"})
                continue
            doc = retained.get(it.origin_item_id) if p.store_bytes else None
            url = rs.check_source_url(it.origin_url)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO research_source (investor_id, isin, source, source_type,
                        source_url, publication_date, period, retrieved_at, document_reference,
                        document_id, content_sha256, dedupe_key, adapter_id, origin_item_id,
                        revises_source_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (investor_id, isin, it.title, it.source_type, url, it.publication_date,
                     it.period_label or "UNSTATED", it.retrieved_at,
                     f"{adapter.adapter_id}:{it.origin_item_id}",
                     doc["id"] if doc else None, sha, skey, adapter.adapter_id,
                     it.origin_item_id, revised),
                )
                source_id = str(cur.fetchone()[0])
                extraction = doc.get("extraction") if doc else None
                if extraction is not None:
                    cur.execute(
                        """
                        INSERT INTO research_source_extraction
                          (investor_id, source_id, isin, status, unavailable_reason,
                           extractor_id, extractor_version, normalization, content_sha256,
                           text_sha256, text_document_id, page_count)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (investor_id, source_id, isin, extraction["status"],
                         extraction["unavailable_reason"], extraction["extractor_id"],
                         extraction["extractor_version"], extraction["normalization"], sha,
                         extraction["text_sha256"], extraction["text_document_id"],
                         extraction["page_count"]),
                    )
            summary["sources"].append({"origin_item_id": it.origin_item_id,
                                       "source_id": source_id,
                                       "retained": doc is not None,
                                       "publication_date": it.publication_date.isoformat()
                                       if it.publication_date else None})
            if revised:
                summary["revisions"].append({"source_id": source_id,
                                             "revises_source_id": revised})
        summary["unknowns"] += p.unknowns
        _record_facts(conn, investor_id, isin, source_id, skey, revised, it, registry, summary)
        _record_events(conn, investor_id, isin, source_id, adapter.adapter_id, it, summary)

    summary["conflicts"] = _conflicts(conn, investor_id, isin,
                                      sorted({f["metric"] for f in summary["facts"]}))
    ws.audit_write(conn, actor_uid, OP_REFRESH, "investor", investor_id,
                   sources=len(summary["sources"]), facts=len(summary["facts"]),
                   events=len(summary["events"]), unknowns=len(summary["unknowns"]))
    ws.record_receipt(conn, investor_id, OP_REFRESH, isin, key, payload_digest,
                      _receipt_result(summary), actor_uid)
    return summary, 201


def _receipt_result(summary: dict[str, Any]) -> dict[str, Any]:
    """Receipts hold identifiers and strings only."""
    return {"adapter_id": summary["adapter_id"], "isin": summary["isin"],
            "source_ids": [s["source_id"] for s in summary["sources"]],
            "fact_ids": [f["fact_id"] for f in summary["facts"]],
            "event_ids": [e["event_id"] for e in summary["events"]],
            "duplicate_source_ids": [d["source_id"] for d in summary["duplicates"]],
            "unknown_codes": [u["code"] for u in summary["unknowns"]]}


def _record_facts(conn: Any, investor_id: str, isin: str, source_id: str, skey: str,
                  revised: str | None, it: src.RetrievedItem, registry: rr.MetricRegistry,
                  summary: dict[str, Any]) -> None:
    for f in it.facts:
        base = {"origin_item_id": it.origin_item_id, "metric": f.metric, "locator": f.locator}
        if f.period_start is None or f.period_end is None:
            summary["unknowns"].append({**base, "code": "PERIOD_UNKNOWN"})
            continue
        check = check_fact_against_registry(registry, f.metric, f.basis, f.unit, f.cadence)
        if check is None:
            summary["unknowns"].append({**base, "code": "METRIC_NOT_APPROVED"})
            continue
        if check.value_type != "NUMERIC":
            summary["unknowns"].append({**base, "code": "VALUE_TYPE_NOT_SUPPORTED"})
            continue
        try:
            value = rs.parse_decimal_string(f.value)
        except WorkspaceError:
            summary["unknowns"].append({**base, "code": "VALUE_NOT_DECIMAL"})
            continue
        fkey = (f"{skey}:fact:{f.metric}:{f.basis}:{f.unit}:{f.cadence}:"
                f"{f.period_start.isoformat()}:{f.period_end.isoformat()}")
        existing = _exists(conn, "research_fact", investor_id, fkey)
        if existing is not None:
            continue
        supersedes = _fact_head_in_source(conn, investor_id, revised, f) if revised else None
        body = {"source_id": source_id, "metric": f.metric, "basis": f.basis, "unit": f.unit,
                "period_start": f.period_start, "period_end": f.period_end,
                "cadence": f.cadence}
        fact_id = rs._insert_fact(conn, investor_id, isin, body, value,
                                  check.registry_version, "UNVERIFIED", None, None, supersedes,
                                  fkey)
        summary["facts"].append({"fact_id": fact_id, "metric": f.metric, "locator": f.locator,
                                 "supersedes_id": supersedes, "verification": "UNVERIFIED"})


def _event_key(adapter_id: str, origin_event_id: str) -> str:
    return f"adapter:{adapter_id}:event:{origin_event_id}"


def _record_events(conn: Any, investor_id: str, isin: str, source_id: str, adapter_id: str,
                   it: src.RetrievedItem, summary: dict[str, Any]) -> None:
    for e in it.events:
        base = {"origin_item_id": it.origin_item_id, "origin_event_id": e.origin_event_id,
                "locator": e.locator}
        if e.event_type not in src.EVENT_TYPES:
            summary["unknowns"].append({**base, "code": "EVENT_TYPE_UNRECOGNISED"})
            continue
        status = e.status
        if status not in src.EVENT_STATUSES:
            summary["unknowns"].append({**base, "code": "EVENT_STATUS_UNRECOGNISED"})
            continue
        if it.source_type in src.CLAIM_ONLY_SOURCE_TYPES:
            status = "CLAIMED"  # a news report of a date is a claim about a date
        # A claimed, imprecise or unknown date never populates the date field.
        event_date, precision = e.event_date, e.date_precision
        if status == "CLAIMED":
            event_date, precision = None, "UNKNOWN"
        elif precision != "EXACT" or event_date is None:
            event_date = None
            precision = precision if precision in ("IMPRECISE", "UNKNOWN") else "UNKNOWN"
        # Results and earnings calls are about a period; without one they have no identity
        # (055 and 057 CHECKs) and are reported UNKNOWN rather than stored.
        if e.event_type in ("FINANCIAL_RESULTS", "EARNINGS_CALL") and (
                e.period_start is None or e.period_end is None):
            summary["unknowns"].append({**base, "code": "EVENT_PERIOD_UNKNOWN"})
            continue
        if (e.period_start is None) != (e.period_end is None):
            summary["unknowns"].append({**base, "code": "EVENT_PERIOD_UNKNOWN"})
            continue
        ekey = _event_key(adapter_id, e.origin_event_id)
        if _exists(conn, "research_event", investor_id, ekey) is not None:
            continue
        reschedules = None
        if e.reschedules_origin_event_id:
            reschedules = _exists(conn, "research_event", investor_id,
                                  _event_key(adapter_id, e.reschedules_origin_event_id))
            if reschedules is None:
                summary["unknowns"].append({**base, "code": "RESCHEDULED_EVENT_NOT_HELD"})
                continue
        if status == "CANCELLED" and reschedules is None:
            summary["unknowns"].append({**base, "code": "CANCELLATION_TARGET_UNKNOWN"})
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_event (investor_id, isin, source_id, event_type, status,
                    event_date, date_precision, period_start, period_end, description, locator,
                    verification, reschedules_id, dedupe_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'UNVERIFIED',%s,%s) RETURNING id
                """,
                (investor_id, isin, source_id, e.event_type, status, event_date, precision,
                 e.period_start, e.period_end, e.description, e.locator, reschedules, ekey),
            )
            event_id = str(cur.fetchone()[0])
        summary["events"].append({"event_id": event_id, "event_type": e.event_type,
                                  "status": status, "date_precision": precision,
                                  "reschedules_id": reschedules})


def _conflicts(conn: Any, investor_id: str, isin: str, metrics: list[str]
               ) -> list[dict[str, Any]]:
    """Current differing values from different sources for one series. Reported, never
    resolved: the adapter does not pick a winner."""
    if not metrics:
        return []
    family = ws.isin_family(conn, isin)
    rows = _rows(conn, f"SELECT {rs._FACT_COLUMNS} FROM research_fact f WHERE "
                       "f.investor_id = %s AND f.isin = ANY(%s::text[]) AND "
                       "f.metric = ANY(%s::text[])", (investor_id, family, metrics))
    out = []
    for dto in rs._annotate_facts(rows, rows):
        if dto["conflicts_with"]:
            out.append({"fact_id": dto["id"], "conflicts_with": dto["conflicts_with"],
                        "metric": dto["metric"]})
    return out


# =========================================================================================
# events: read and verify
# =========================================================================================

_EVENT_COLUMNS = """
e.id, e.isin, e.source_id, e.event_type, e.status, e.event_date, e.date_precision,
e.period_start, e.period_end, e.description, e.locator, e.verification, e.verified_by,
e.verification_reference, e.supersedes_id, e.reschedules_id, e.recorded_at, e.seq,
s.source_type, s.source_url, s.publication_date, s.retrieved_at, s.adapter_id
"""


def _event_dto(r: dict[str, Any], successors: dict[str, str], moved_by: dict[str, list[str]]
               ) -> dict[str, Any]:
    return {
        "id": r["id"], "isin": r["isin"], "source_id": r["source_id"],
        "event_type": r["event_type"], "status": r["status"],
        "event_date": r["event_date"].isoformat() if r["event_date"] else None,
        "date_precision": r["date_precision"],
        "period_start": r["period_start"].isoformat() if r["period_start"] else None,
        "period_end": r["period_end"].isoformat() if r["period_end"] else None,
        "description": r["description"], "locator": r["locator"],
        "verification": r["verification"],
        "verification_reference": r["verification_reference"],
        "supersedes_id": r["supersedes_id"], "superseded_by": successors.get(r["id"]),
        "reschedules_id": r["reschedules_id"],
        "rescheduled_by": sorted(moved_by.get(r["id"], [])),
        "source_type": r["source_type"],
        "publication_date": r["publication_date"].isoformat(),
        "retrieved_at": r["retrieved_at"].isoformat(),
        "recorded_at": r["recorded_at"].isoformat(),
        # An event is classified only once a person verified its identity (BE-V06).
        "classification_verified": r["verification"] == "VERIFIED",
    }


def list_events(conn: Any, investor_id: str, family: list[str],
                cutoff: datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
    rows = _rows(conn, f"""
        SELECT {_EVENT_COLUMNS} FROM research_event e
          JOIN research_source s ON s.investor_id = e.investor_id AND s.id = e.source_id
         WHERE e.investor_id = %s AND e.isin = ANY(%s::text[])
           AND (%s::timestamptz IS NULL OR e.recorded_at <= %s::timestamptz)
         ORDER BY e.seq DESC LIMIT %s
        """, (investor_id, family, cutoff, cutoff, limit))
    successors = {r["supersedes_id"]: r["id"] for r in rows if r["supersedes_id"]}
    moved: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["reschedules_id"]:
            moved[r["reschedules_id"]].append(r["id"])
    return [_event_dto(r, successors, moved) for r in rows]


def verify_event(conn: Any, investor_id: str, actor_uid: str, event_id: str, reference: str,
                 expected_event_id: str, key: str, payload_digest: str
                 ) -> tuple[dict[str, Any], int]:
    """A person rechecked the event's exact identity against its source: append a VERIFIED
    copy attributed to them. A CLAIMED event cannot be verified -- it is a claim, not an event
    -- and neither can one without an exact date unless it is a cancellation."""
    event_id = _uuid(event_id, "event")
    row = _one(conn, f"""
        SELECT {_EVENT_COLUMNS} FROM research_event e
          JOIN research_source s ON s.investor_id = e.investor_id AND s.id = e.source_id
         WHERE e.investor_id = %s AND e.id = %s""", (investor_id, event_id))
    if row is None:
        raise not_found("event")
    ws.require_unlocked(conn, investor_id, row["isin"])
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_EVENT_VERIFY, event_id, key, payload_digest)
    if receipt is not None:
        return _event_by_id(conn, investor_id, receipt.result["event_id"]), 200
    if _uuid(expected_event_id, "event") != event_id:
        raise conflict(STALE_PREDECESSOR, "expected_event_id does not name this event")
    if _one(conn, "SELECT 1 AS x FROM research_event WHERE investor_id = %s AND "
                  "supersedes_id = %s", (investor_id, event_id)):
        raise conflict(STALE_PREDECESSOR, "this event has already been rechecked; reload")
    if row["status"] == "CLAIMED":
        raise invalid("a claimed date is a claim, not an event; it cannot be verified",
                      {"event": EVENT_NOT_VERIFIABLE}, code=EVENT_NOT_VERIFIABLE)
    if row["date_precision"] != "EXACT" and row["status"] != "CANCELLED":
        raise invalid("an event without an exact published date cannot be verified",
                      {"event": EVENT_NOT_VERIFIABLE}, code=EVENT_NOT_VERIFIABLE)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO research_event (investor_id, isin, source_id, event_type, status,
                event_date, date_precision, period_start, period_end, description, locator,
                verification, verified_by, verification_reference, supersedes_id,
                reschedules_id, dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'VERIFIED',%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, row["isin"], row["source_id"], row["event_type"], row["status"],
             row["event_date"], row["date_precision"], row["period_start"], row["period_end"],
             row["description"], row["locator"], actor_uid, reference, event_id,
             row["reschedules_id"], ws.dedupe_key(OP_EVENT_VERIFY, event_id, key)),
        )
        new_id = str(cur.fetchone()[0])
    ws.audit_write(conn, actor_uid, OP_EVENT_VERIFY, "research_event", new_id,
                   supersedes=event_id)
    ws.record_receipt(conn, investor_id, OP_EVENT_VERIFY, event_id, key, payload_digest,
                      {"event_id": new_id}, actor_uid)
    return _event_by_id(conn, investor_id, new_id), 201


def _event_by_id(conn: Any, investor_id: str, event_id: str) -> dict[str, Any]:
    row = _one(conn, f"""
        SELECT {_EVENT_COLUMNS} FROM research_event e
          JOIN research_source s ON s.investor_id = e.investor_id AND s.id = e.source_id
         WHERE e.investor_id = %s AND e.id = %s""", (investor_id, event_id))
    if row is None:
        raise not_found("event")
    return _event_dto(row, {}, {})


# =========================================================================================
# readers for the three source tools
# =========================================================================================


def held_sources(conn: Any, investor_id: str, family: list[str],
                 cutoff: datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
    rows = _rows(conn, """
        SELECT s.id, s.publication_date, s.recorded_at, s.source_type,
               s.adapter_id, s.origin_item_id, s.revises_source_id
          FROM research_source s
         WHERE s.investor_id = %s AND s.isin = ANY(%s::text[])
           AND (%s::timestamptz IS NULL OR s.recorded_at <= %s::timestamptz)
         ORDER BY s.publication_date DESC, s.recorded_at DESC LIMIT %s
        """, (investor_id, family, cutoff, cutoff, limit))
    by_id = ws.sources_by_id(conn, investor_id, [r["id"] for r in rows])
    revised_by = {r["revises_source_id"]: r["id"] for r in rows if r["revises_source_id"]}
    out = []
    for r in rows:
        dto = dict(by_id[r["id"]])
        dto["adapter_id"] = r["adapter_id"]
        dto["revises_source_id"] = r["revises_source_id"]
        dto["revised_by"] = revised_by.get(r["id"])
        out.append(dto)
    return out


def held_facts(conn: Any, investor_id: str, family: list[str], metric: str | None,
               period_start: date | None, period_end: date | None,
               cutoff: datetime | None = None, limit: int = 400) -> list[dict[str, Any]]:
    """Every held fact (history included) with verification, successors and conflicts."""
    rows = _rows(conn, f"""
        SELECT {rs._FACT_COLUMNS}, s.source_type FROM research_fact f
          JOIN research_source s ON s.investor_id = f.investor_id AND s.id = f.source_id
         WHERE f.investor_id = %s AND f.isin = ANY(%s::text[])
           AND (%s::text IS NULL OR f.metric = %s::text)
           AND (%s::date IS NULL OR f.period_start >= %s::date)
           AND (%s::date IS NULL OR f.period_end <= %s::date)
           AND (%s::timestamptz IS NULL OR f.recorded_at <= %s::timestamptz)
         ORDER BY f.period_end DESC, f.seq DESC LIMIT %s
        """, (investor_id, family, metric, metric, period_start, period_start, period_end,
              period_end, cutoff, cutoff, limit))
    types = {r["id"]: r["source_type"] for r in rows}
    out = []
    for dto in rs._annotate_facts(rows, rows):
        dto["source_type"] = types[dto["id"]]
        out.append(dto)
    return out
