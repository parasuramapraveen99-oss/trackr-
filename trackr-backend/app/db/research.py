"""Research repository: thesis versions and events (session 11), sources and retained text
(session 12), sourced facts (session 13), evaluation receipts and overrides (session 15).

Same contract as ``app/db/workspace.py`` -- every function takes a connection already scoped
to one investor by ``enter_workspace``; writes lock the tenant, check the idempotency
receipt, then write rows + audit + receipt in one transaction. Content is gated through
``workspace.require_unlocked`` / ``visible_isins``.

NOTHING HERE IS A FINANCIAL RECORD. A fact is what a source said; an evaluation is a
deterministic comparison of those statements with the investor's own stated conditions; an
override is a person's recorded decision. None of them writes the ledger, a holding or a
price, and none implies a trade.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.db import workspace as ws
from app.engine import research_rules as rr
from app.engine import thesis_versions as tv
from app.research import canonical
from app.research.errors import (
    BLOCKING_QUESTION_OPEN,
    COMMITMENT_CONFLICT,
    EVALUATION_NOT_TRIGGERED,
    EVALUATION_STALE,
    FACT_VALUE_INVALID,
    INVALID_RULE,
    METRIC_NOT_APPROVED,
    NO_COMMITMENT_IN_FORCE,
    SOURCE_NOT_FOUND,
    STALE_PREDECESSOR,
    VERSION_CONFLICT,
    conflict,
    invalid,
    not_found,
    ritual_required,
)
from app.research.registry import check_fact_against_registry, identity_scope

OP_THESIS_CREATE = "thesis.create"
OP_THESIS_VERSION = "thesis.version"
OP_THESIS_EVENT = "thesis.event"
OP_THESIS_OVERRIDE = "thesis.override"
OP_SOURCE_CREATE = "source.create"
OP_SOURCE_UPLOAD = "source.upload"
OP_FACT_CREATE = "fact.create"
OP_FACT_VERIFY = "fact.verify"
OP_FACT_REVISE = "fact.revise"
OP_EVALUATION = "evaluation.record"

IST = rr.IST
_DECIMAL = re.compile(r"^-?[0-9]{1,14}(\.[0-9]{1,6})?$")

_rows = ws._rows
_one = ws._one
_uuid = ws._uuid


def _now() -> datetime:
    return datetime.now(UTC)


# =========================================================================================
# theses (session 11)
# =========================================================================================

_THESIS_COLUMNS = """
t.id, t.isin, t.version, t.status, t.formed_on, t.version_on, t.recorded_at, t.actor_uid,
t.argument, t.anti_thesis, t.pre_mortem, t.intended_holding_period, t.variables, t.exits,
t.draft_reasons, t.seq
"""


def _version_record(row: dict[str, Any]) -> tv.VersionRecord:
    return tv.VersionRecord(id=row["id"], isin=row["isin"], version=row["version"],
                            status=row["status"], formed_on=row["formed_on"],
                            version_on=row["version_on"], recorded_at=row["recorded_at"])


def _branch_rows(conn: Any, investor_id: str, isins: list[str]) -> list[dict[str, Any]]:
    return _rows(conn, f"SELECT {_THESIS_COLUMNS} FROM thesis t "
                       "WHERE t.investor_id = %s AND t.isin = ANY(%s::text[]) "
                       "ORDER BY t.isin, t.version",
                 (investor_id, isins))


def select_in_force(
    conn: Any, investor_id: str, isin: str, cutoff: datetime
) -> tuple[tv.CommitmentSelection, list[dict[str, Any]]]:
    """In-force commitment across the verified family's per-ISIN branches at ``cutoff``."""
    family = ws.isin_family(conn, isin)
    rows = _branch_rows(conn, investor_id, family)
    branches: dict[str, list[tv.VersionRecord]] = defaultdict(list)
    for row in rows:
        branches[row["isin"]].append(_version_record(row))
    return tv.select_commitment(branches, cutoff), rows


def _events_for(conn: Any, investor_id: str, thesis_ids: list[str]) -> dict[str, list[dict]]:
    rows = _rows(
        conn,
        """
        SELECT id, thesis_id, kind, conviction, reason, evaluation_id, actor_uid, occurred_on,
               recorded_at, seq
          FROM thesis_event WHERE investor_id = %s AND thesis_id = ANY(%s::uuid[])
         ORDER BY seq
        """,
        (investor_id, thesis_ids),
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        row.pop("seq")
        out[row["thesis_id"]].append(row)
    return out


def _latest_evaluations(conn: Any, investor_id: str, thesis_ids: list[str]) -> dict[str, dict]:
    rows = _rows(
        conn,
        """
        SELECT DISTINCT ON (thesis_id) id, thesis_id, state, evaluated_at, evidence_cutoff
          FROM trigger_evaluation WHERE investor_id = %s AND thesis_id = ANY(%s::uuid[])
         ORDER BY thesis_id, evaluated_at DESC, id
        """,
        (investor_id, thesis_ids),
    )
    return {r["thesis_id"]: r for r in rows}


def _thesis_links(
    conn: Any, investor_id: str, thesis_ids: list[str], visible: set[str]
) -> dict[str, list[dict]]:
    rows = _rows(
        conn,
        """
        SELECT l.id, l.from_note_id, l.thesis_id, l.relation, l.recorded_at, n.isin
          FROM note_link l JOIN note n ON n.investor_id = l.investor_id AND n.id = l.from_note_id
         WHERE l.investor_id = %s AND l.thesis_id = ANY(%s::uuid[])
         ORDER BY l.recorded_at, l.id
        """,
        (investor_id, thesis_ids),
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.pop("isin") in visible:
            out[row["thesis_id"]].append(row)
    return out


def _display_status(row: dict[str, Any], latest_eval: dict | None, events: list[dict]) -> str:
    """NEEDS_REVIEW is a READ projection, never stored: an ACTIVE version whose latest
    stored evaluation receipt is TRIGGERED and which has no human REVIEWED/override event
    recorded after that receipt."""
    if row["status"] != tv.STATUS_ACTIVE or not latest_eval:
        return row["status"]
    if latest_eval["state"] != rr.TRIGGERED:
        return row["status"]
    after = [e for e in events if e["kind"] in ("REVIEWED", "OVERRIDE_EXIT_TRIGGER")
             and e["recorded_at"] >= latest_eval["evaluated_at"]]
    return row["status"] if after else "NEEDS_REVIEW"


def hydrate_theses(
    conn: Any, investor_id: str, rows: list[dict[str, Any]], visible: set[str] | None = None
) -> list[dict[str, Any]]:
    if not rows:
        return []
    visible = visible if visible is not None else ws.visible_isins(conn, investor_id)
    ids = [r["id"] for r in rows]
    events = _events_for(conn, investor_id, ids)
    evaluations = _latest_evaluations(conn, investor_id, ids)
    links = _thesis_links(conn, investor_id, ids, visible)
    latest_by_isin = {
        r["isin"]: r["version"]
        for r in _rows(conn, "SELECT isin, max(version) AS version FROM thesis "
                             "WHERE investor_id = %s AND isin = ANY(%s::text[]) GROUP BY isin",
                       (investor_id, sorted({r["isin"] for r in rows})))
    }
    out = []
    for row in rows:
        own_events = events.get(row["id"], [])
        latest_eval = evaluations.get(row["id"])
        out.append({
            "id": row["id"],
            "isin": row["isin"],
            "version": row["version"],
            "status": row["status"],
            "display_status": _display_status(row, latest_eval, own_events),
            "formed_on": row["formed_on"],
            "version_on": row["version_on"],
            "recorded_at": row["recorded_at"],
            "actor_uid": row["actor_uid"],
            "argument": row["argument"],
            "anti_thesis": row["anti_thesis"],
            "pre_mortem": row["pre_mortem"],
            "intended_holding_period": row["intended_holding_period"],
            "variables": row["variables"],
            "exits": row["exits"],
            "draft_reasons": [
                {"field": r.split(":", 1)[0], "code": r.split(":", 1)[1] if ":" in r else r}
                for r in (row["draft_reasons"] or [])
            ],
            "events": own_events,
            "links": links.get(row["id"], []),
            "latest_evaluation": latest_eval,
            "is_latest": latest_by_isin.get(row["isin"]) == row["version"],
        })
    return out


def thesis_row(conn: Any, investor_id: str, thesis_id: str) -> dict[str, Any]:
    """Exact thesis version row, gated on its company."""
    thesis_id = _uuid(thesis_id, "thesis")
    row = _one(conn, f"SELECT {_THESIS_COLUMNS} FROM thesis t "
                     "WHERE t.investor_id = %s AND t.id = %s", (investor_id, thesis_id))
    if row is None:
        raise not_found("thesis")
    if row["isin"] not in ws.visible_isins(conn, investor_id):
        raise ritual_required()
    return row


def thesis_by_id(conn: Any, investor_id: str, thesis_id: str) -> dict[str, Any]:
    return hydrate_theses(conn, investor_id, [thesis_row(conn, investor_id, thesis_id)])[0]


def list_theses(
    conn: Any, investor_id: str, isin: str, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    """Every retained version across the family's branches, newest receipt first."""
    isin = ws.require_unlocked(conn, investor_id, isin)
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "theses", {"isin": isin})
    after = canonical.decode_cursor(cursor, scope)
    family = ws.isin_family(conn, isin)
    rows = _rows(
        conn,
        f"""
        SELECT {_THESIS_COLUMNS} FROM thesis t
         WHERE t.investor_id = %s AND t.isin = ANY(%s::text[])
           AND (%s::bigint IS NULL OR t.seq < %s::bigint)
         ORDER BY t.seq DESC LIMIT %s
        """,
        (investor_id, family, after[0] if after else None, after[0] if after else None,
         size + 1),
    )
    return ws._page(conn, rows, size, scope,
                    lambda page: hydrate_theses(conn, investor_id, page),
                    key=lambda r: [r["seq"]])


def _open_blocking(conn: Any, investor_id: str, family: list[str]) -> list[str]:
    return [r["id"] for r in _rows(
        conn,
        """
        SELECT q.id FROM question q
         WHERE q.investor_id = %s AND q.isin = ANY(%s::text[]) AND q.blocking
           AND NOT EXISTS (SELECT 1 FROM question_answer a
                            WHERE a.investor_id = q.investor_id AND a.question_id = q.id)
        """,
        (investor_id, family),
    )]


def _parse_thesis_rules(body: dict[str, Any]) -> tuple[tuple[tv.Rule, ...], tuple[tv.Rule, ...]]:
    try:
        variables = tv.parse_rules(body["variables"], "variables")
        exits = tv.parse_rules(body["exits"], "exits")
    except tv.RuleSyntaxError as exc:
        raise invalid("a variable or exit rule is malformed", exc.reasons,
                      code=INVALID_RULE) from exc
    return variables, exits


def _publish(
    conn: Any, investor_id: str, actor_uid: str, isin: str, body: dict[str, Any],
    supplied_id: str | None, operation: str, scope: str, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Shared by new-branch and new-version publication. Tenant lock is held on entry."""
    receipt = ws.check_replay(conn, investor_id, operation, scope, key, payload_digest)
    if receipt is not None:
        return _publish_response(conn, investor_id, receipt.result["thesis_id"]), 200

    variables, exits = _parse_thesis_rules(body)
    family = ws.isin_family(conn, isin)
    try:
        tv.assert_publishable(_open_blocking(conn, investor_id, family))
    except tv.BlockingQuestionsOpenError as exc:
        raise conflict(BLOCKING_QUESTION_OPEN,
                       "answer your blocking questions before saving a thesis") from exc

    existing = [_version_record(r) for r in _branch_rows(conn, investor_id, [isin])]
    try:
        version = tv.next_version(existing, body["expected_version"], supplied_id)
        formed_on = tv.formation_date(existing, body["formed_on"])
        classification = tv.classify(body["intent"], body, variables, exits,
                                     branch_exists=bool(existing))
    except tv.VersionConflictError as exc:
        raise conflict(VERSION_CONFLICT, str(exc)) from exc
    if body["version_on"] < formed_on:
        raise invalid("version_on cannot precede formed_on", {"version_on": "BEFORE_FORMED"})

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO thesis (investor_id, isin, version, status, formed_on, version_on,
                                actor_uid, argument, anti_thesis, pre_mortem,
                                intended_holding_period, variables, exits, draft_reasons,
                                dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, isin, version, classification.status, formed_on, body["version_on"],
             actor_uid, body["argument"], body["anti_thesis"], body["pre_mortem"],
             body["intended_holding_period"],
             json.dumps([r.as_dict() for r in variables]),
             json.dumps([r.as_dict() for r in exits]),
             [r.encoded() for r in classification.draft_reasons],
             ws.dedupe_key(operation, scope, key)),
        )
        thesis_id = str(cur.fetchone()[0])
    ws.audit_write(conn, actor_uid, operation, "thesis", thesis_id, version=version,
                   status=classification.status)
    ws.record_receipt(conn, investor_id, operation, scope, key, payload_digest,
                      {"thesis_id": thesis_id}, actor_uid)
    return _publish_response(conn, investor_id, thesis_id), 201


def _publish_response(conn: Any, investor_id: str, thesis_id: str) -> dict[str, Any]:
    thesis = thesis_by_id(conn, investor_id, thesis_id)
    return {"thesis": thesis, "validation": {"draft_reasons": thesis["draft_reasons"]}}


def create_thesis(
    conn: Any, investor_id: str, actor_uid: str, isin: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """A new branch for this ISIN. ``expected_version`` must be 0."""
    isin = ws.require_unlocked(conn, investor_id, isin)
    ws.lock_tenant(conn, investor_id)
    return _publish(conn, investor_id, actor_uid, isin, body, None, OP_THESIS_CREATE, isin,
                    key, payload_digest)


def create_thesis_version(
    conn: Any, investor_id: str, actor_uid: str, thesis_id: str, body: dict[str, Any],
    key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Append the next complete snapshot. The URL thesis must be the branch's latest."""
    row = thesis_row(conn, investor_id, thesis_id)
    ws.lock_tenant(conn, investor_id)
    return _publish(conn, investor_id, actor_uid, row["isin"], body, row["id"],
                    OP_THESIS_VERSION, row["id"], key, payload_digest)


def record_thesis_event(
    conn: Any, investor_id: str, actor_uid: str, thesis_id: str, body: dict[str, Any],
    key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Human CONVICTION or REVIEWED, with a reason and a stated date. No computed score."""
    row = thesis_row(conn, investor_id, thesis_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_THESIS_EVENT, row["id"], key,
                              payload_digest)
    if receipt is not None:
        return _event_by_id(conn, investor_id, receipt.result["event_id"]), 200
    if body["kind"] not in ("CONVICTION", "REVIEWED"):
        raise invalid("kind must be CONVICTION or REVIEWED; overrides have their own route",
                      {"kind": "ENUM"})
    if body["kind"] == "CONVICTION" and body.get("conviction") is None:
        raise invalid("a CONVICTION event states a conviction", {"conviction": "REQUIRED"})
    latest = _one(conn, "SELECT id FROM thesis WHERE investor_id = %s AND isin = %s "
                        "ORDER BY version DESC LIMIT 1", (investor_id, row["isin"]))
    if latest is None or latest["id"] != row["id"]:
        raise conflict(STALE_PREDECESSOR, "record events against the latest thesis version")
    event_id = _insert_event(conn, investor_id, actor_uid, row["id"], body["kind"],
                             body.get("conviction"), body["reason"], None, body["occurred_on"],
                             ws.dedupe_key(OP_THESIS_EVENT, row["id"], key))
    ws.audit_write(conn, actor_uid, OP_THESIS_EVENT, "thesis_event", event_id,
                   kind=body["kind"])
    ws.record_receipt(conn, investor_id, OP_THESIS_EVENT, row["id"], key, payload_digest,
                      {"event_id": event_id}, actor_uid)
    return _event_by_id(conn, investor_id, event_id), 201


def _insert_event(
    conn: Any, investor_id: str, actor_uid: str, thesis_id: str, kind: str,
    conviction: str | None, reason: str, evaluation_id: str | None, occurred_on: date,
    dkey: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO thesis_event (investor_id, thesis_id, kind, conviction, reason,
                                      evaluation_id, actor_uid, occurred_on, dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, thesis_id, kind, conviction, reason, evaluation_id, actor_uid,
             occurred_on, dkey),
        )
        return str(cur.fetchone()[0])


def _event_by_id(conn: Any, investor_id: str, event_id: str) -> dict[str, Any]:
    row = _one(
        conn,
        "SELECT id, thesis_id, kind, conviction, reason, evaluation_id, actor_uid, occurred_on, "
        "recorded_at FROM thesis_event WHERE investor_id = %s AND id = %s",
        (investor_id, event_id),
    )
    if row is None:
        raise not_found("thesis event")
    return row


# =========================================================================================
# sources (session 12)
# =========================================================================================

#: Query parameters that mark a URL as a signed, expiring or credential-bearing link. Such a
#: URL is a live credential today and a dead link tomorrow; it is never stored. Matched by
#: exact name OR by suffix, so vendor-prefixed variants (x-amz-..., x-goog-...) are caught.
_SIGNED_PARAM_NAMES = {"sig", "se", "sv", "expires", "key", "apikey", "api_key", "auth"}
_SIGNED_PARAM_SUFFIXES = ("signature", "credential", "token", "security-token")
_REFERENCE_SCHEMES = ("isbn:", "doi:", "urn:", "trackr-document:")


def check_source_url(url: str) -> str:
    """A public canonical URL or a reference identifier; never a signed or credentialed URL.
    Storing a URL grants no permission to fetch it, and nothing here fetches it."""
    candidate = url.strip()
    if candidate.lower().startswith(_REFERENCE_SCHEMES):
        return candidate
    parts = urlsplit(candidate)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise invalid("source_url must be an http(s) URL or an isbn:/doi:/urn: reference",
                      {"source_url": "SCHEME"})
    if parts.username or parts.password:
        raise invalid("source_url must not carry credentials", {"source_url": "CREDENTIALS"})
    params = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if params & _SIGNED_PARAM_NAMES or any(p.endswith(_SIGNED_PARAM_SUFFIXES) for p in params):
        raise invalid("source_url looks like a signed or expiring link; cite the public URL",
                      {"source_url": "SIGNED_URL"})
    return candidate


def check_provenance(body: dict[str, Any], now: datetime) -> None:
    retrieved = body["retrieved_at"]
    if retrieved.tzinfo is None:
        raise invalid("retrieved_at must include a UTC offset", {"retrieved_at": "NAIVE"})
    if retrieved > now + timedelta(minutes=5):
        raise invalid("retrieved_at is in the future", {"retrieved_at": "FUTURE"})
    if body["publication_date"] > retrieved.astimezone(IST).date():
        raise invalid("a source cannot be published after it was retrieved (Asia/Kolkata)",
                      {"publication_date": "AFTER_RETRIEVAL"})


def source_row(conn: Any, investor_id: str, source_id: str) -> dict[str, Any]:
    source_id = _uuid(source_id, "source")
    sources = ws.sources_by_id(conn, investor_id, [source_id])
    if source_id not in sources:
        raise not_found("source")
    source = sources[source_id]
    if source["isin"] not in ws.visible_isins(conn, investor_id):
        raise ritual_required()
    return source


def extraction_row(conn: Any, investor_id: str, source_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        """
        SELECT status, unavailable_reason, extractor_id, extractor_version, normalization,
               content_sha256, text_sha256, text_document_id, page_count
          FROM research_source_extraction WHERE investor_id = %s AND source_id = %s
        """,
        (investor_id, source_id),
    )


def list_sources(
    conn: Any, investor_id: str, isin: str, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    isin = ws.require_unlocked(conn, investor_id, isin)
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "sources", {"isin": isin})
    after = canonical.decode_cursor(cursor, scope)
    family = ws.isin_family(conn, isin)
    rows = _rows(
        conn,
        """
        SELECT id, publication_date, recorded_at FROM research_source
         WHERE investor_id = %s AND isin = ANY(%s::text[])
           AND (%s::date IS NULL
                OR (publication_date, recorded_at, id) < (%s::date, %s::timestamptz, %s::uuid))
         ORDER BY publication_date DESC, recorded_at DESC, id DESC
         LIMIT %s
        """,
        (investor_id, family, after[0] if after else None, after[0] if after else None,
         after[1] if after else None, after[2] if after else None, size + 1),
    )

    def hydrate(page: list[dict]) -> list[dict]:
        by_id = ws.sources_by_id(conn, investor_id, [r["id"] for r in page])
        return [by_id[r["id"]] for r in page]

    return ws._page(conn, rows, size, scope, hydrate,
                    key=lambda r: [r["publication_date"].isoformat(),
                                   r["recorded_at"].isoformat(), r["id"]])


def create_source(
    conn: Any,
    investor_id: str,
    actor_uid: str,
    isin: str,
    body: dict[str, Any],
    operation: str,
    key: str,
    payload_digest: str,
    verified_document: dict[str, Any] | None,
    extraction: dict[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    """Insert a source, its extraction pin (when bytes were read) and audit, atomically.

    ``verified_document`` is supplied by the router after it checked -- on the financial
    connection, with the store -- that the document belongs to this investor and that its
    retained bytes hash to ``sha256``. A client-declared hash is never proof.
    """
    isin = ws.require_unlocked(conn, investor_id, isin)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, operation, isin, key, payload_digest)
    if receipt is not None:
        return source_row(conn, investor_id, receipt.result["source_id"]), 200
    check_provenance(body, _now())
    url = check_source_url(body["source_url"])
    document_id = verified_document["id"] if verified_document else None
    content_sha = verified_document["sha256"] if verified_document else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO research_source (investor_id, isin, source, source_type, source_url,
                                         publication_date, period, retrieved_at,
                                         document_reference, document_id, content_sha256,
                                         dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, isin, body["source"], body["source_type"], url,
             body["publication_date"], body["period"], body["retrieved_at"],
             body["document_reference"], document_id, content_sha,
             ws.dedupe_key(operation, isin, key)),
        )
        source_id = str(cur.fetchone()[0])
        if extraction is not None:
            cur.execute(
                """
                INSERT INTO research_source_extraction
                  (investor_id, source_id, isin, status, unavailable_reason, extractor_id,
                   extractor_version, normalization, content_sha256, text_sha256,
                   text_document_id, page_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (investor_id, source_id, isin, extraction["status"],
                 extraction["unavailable_reason"], extraction["extractor_id"],
                 extraction["extractor_version"], extraction["normalization"], content_sha,
                 extraction["text_sha256"], extraction["text_document_id"],
                 extraction["page_count"]),
            )
    ws.audit_write(conn, actor_uid, operation, "research_source", source_id,
                   retained=document_id is not None,
                   extraction=extraction["status"] if extraction else "METADATA_ONLY")
    ws.record_receipt(conn, investor_id, operation, isin, key, payload_digest,
                      {"source_id": source_id}, actor_uid)
    return source_row(conn, investor_id, source_id), 201


def peek_receipt(
    conn: Any, investor_id: str, operation: str, scope: str, key: str, payload_digest: str
) -> ws.Receipt | None:
    """Replay check without writing -- lets the upload route answer a retry before it
    touches storage again."""
    ws.lock_tenant(conn, investor_id)
    return ws.check_replay(conn, investor_id, operation, scope, key, payload_digest)


# =========================================================================================
# facts (session 13)
# =========================================================================================

_FACT_COLUMNS = """
f.id, f.isin, f.source_id, f.metric, f.basis, f.unit, f.period_start, f.period_end,
f.cadence, f.numeric_value, f.boolean_value, f.text_value, f.verification, f.verified_by,
f.verification_reference, f.supersedes_id, f.registry_version, f.recorded_at, f.seq
"""


def parse_decimal_string(value: str) -> Decimal:
    """A finite decimal string that fits NUMERIC(20,6) EXACTLY. Checked before PostgreSQL
    sees it, because NUMERIC(20,6) would silently round a seventh fractional digit."""
    if not _DECIMAL.match(value):
        raise invalid("numeric_value must be a plain decimal string with at most 14 integer "
                      "and 6 fractional digits", {"numeric_value": FACT_VALUE_INVALID},
                      code=FACT_VALUE_INVALID)
    try:
        return Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - regex excludes
        raise invalid("numeric_value is not a decimal", {"numeric_value": FACT_VALUE_INVALID},
                      code=FACT_VALUE_INVALID) from exc


def _fact_dto(row: dict[str, Any]) -> dict[str, Any]:
    numeric = row["numeric_value"]
    return {
        "id": row["id"],
        "isin": row["isin"],
        "source_id": row["source_id"],
        "metric": row["metric"],
        "basis": row["basis"],
        "unit": row["unit"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "cadence": row["cadence"],
        "numeric_value": format(numeric, "f") if numeric is not None else None,
        "boolean_value": row["boolean_value"],
        "text_value": row["text_value"],
        "verification": row["verification"],
        "verified_by": row["verified_by"],
        "verification_reference": row["verification_reference"],
        "supersedes_id": row["supersedes_id"],
        "registry_version": row["registry_version"],
        "recorded_at": row["recorded_at"],
    }


def _annotate_facts(rows: list[dict[str, Any]], all_rows: list[dict[str, Any]]) -> list[dict]:
    """Add superseded_by and conflicts_with, computed only among comparable rows.

    Comparable = same series (metric, basis, unit, cadence, exact period). A consolidated
    and a standalone figure are different series and never conflict. Only CURRENT heads
    (not superseded) can conflict, and only with a different value from a DIFFERENT source;
    identical values from the same origin are not independent corroboration either.
    """
    successor = {r["supersedes_id"]: r["id"] for r in all_rows if r["supersedes_id"]}

    def series(r: dict) -> tuple:
        return (r["metric"], r["basis"], r["unit"], r["cadence"], r["period_start"],
                r["period_end"])

    def value(r: dict) -> Any:
        return (r["numeric_value"], r["boolean_value"], r["text_value"])

    heads: dict[tuple, list[dict]] = defaultdict(list)
    for r in all_rows:
        if r["id"] not in successor:
            heads[series(r)].append(r)
    out = []
    for r in rows:
        dto = _fact_dto(r)
        dto["superseded_by"] = successor.get(r["id"])
        dto["conflicts_with"] = sorted(
            other["id"] for other in heads.get(series(r), [])
            if r["id"] not in successor and other["id"] != r["id"]
            and other["source_id"] != r["source_id"] and value(other) != value(r)
        )
        out.append(dto)
    return out


def list_facts(
    conn: Any, investor_id: str, isin: str, metric: str | None, period_start: date | None,
    period_end: date | None, cursor: str | None, limit: int | None,
) -> dict[str, Any]:
    """Every fact row, superseded history included, with verification and conflicts shown.
    Nothing is averaged, preferred or hidden."""
    isin = ws.require_unlocked(conn, investor_id, isin)
    size = canonical.clamp_limit(limit)
    filters = {"isin": isin, "metric": metric,
               "period_start": period_start.isoformat() if period_start else None,
               "period_end": period_end.isoformat() if period_end else None}
    scope = canonical.scope_digest(investor_id, "facts", filters)
    after = canonical.decode_cursor(cursor, scope)
    family = ws.isin_family(conn, isin)
    params = {"inv": investor_id, "fam": family, "metric": metric, "ps": period_start,
              "pe": period_end}
    where = """
        f.investor_id = %(inv)s AND f.isin = ANY(%(fam)s::text[])
        AND (%(metric)s::text IS NULL OR f.metric = %(metric)s::text)
        AND (%(ps)s::date IS NULL OR f.period_start >= %(ps)s::date)
        AND (%(pe)s::date IS NULL OR f.period_end <= %(pe)s::date)
    """
    everything = _rows(conn, f"SELECT {_FACT_COLUMNS} FROM research_fact f WHERE {where}",
                       params)
    rows = _rows(
        conn,
        f"""
        SELECT {_FACT_COLUMNS} FROM research_fact f WHERE {where}
           AND (%(k0)s::date IS NULL OR (f.period_end, f.seq) < (%(k0)s::date, %(k1)s::bigint))
         ORDER BY f.period_end DESC, f.seq DESC LIMIT %(lim)s
        """,
        {**params, "k0": after[0] if after else None, "k1": after[1] if after else None,
         "lim": size + 1},
    )
    return ws._page(conn, rows, size, scope,
                    lambda page: _annotate_facts(page, everything),
                    key=lambda r: [r["period_end"].isoformat(), r["seq"]])


def fact_row(conn: Any, investor_id: str, fact_id: str) -> dict[str, Any]:
    fact_id = _uuid(fact_id, "fact")
    row = _one(conn, f"SELECT {_FACT_COLUMNS} FROM research_fact f "
                     "WHERE f.investor_id = %s AND f.id = %s", (investor_id, fact_id))
    if row is None:
        raise not_found("fact")
    if row["isin"] not in ws.visible_isins(conn, investor_id):
        raise ritual_required()
    return row


def _fact_response(conn: Any, investor_id: str, fact_id: str) -> dict[str, Any]:
    row = fact_row(conn, investor_id, fact_id)
    family = ws.isin_family(conn, row["isin"])
    everything = _rows(conn, f"SELECT {_FACT_COLUMNS} FROM research_fact f "
                             "WHERE f.investor_id = %s AND f.isin = ANY(%s::text[]) "
                             "AND f.metric = %s", (investor_id, family, row["metric"]))
    return _annotate_facts([row], everything)[0]


def _validate_fact(
    conn: Any, investor_id: str, isin: str, body: dict[str, Any], registry: rr.MetricRegistry
) -> tuple[Decimal | None, str]:
    values = [body.get("numeric_value"), body.get("boolean_value"), body.get("text_value")]
    if sum(v is not None for v in values) != 1:
        raise invalid("exactly one of numeric_value, boolean_value, text_value is required",
                      {"value": "EXACTLY_ONE"}, code=FACT_VALUE_INVALID)
    if body["period_end"] < body["period_start"]:
        raise invalid("period_end precedes period_start", {"period_end": "BEFORE_START"})
    check = check_fact_against_registry(registry, body["metric"], body["basis"], body["unit"],
                                        body["cadence"])
    if check is None:
        raise invalid("this metric/basis/unit/cadence is not an approved registry entry "
                      "(BE-Q03)", {"metric": METRIC_NOT_APPROVED}, code=METRIC_NOT_APPROVED)
    numeric = None
    if body.get("numeric_value") is not None:
        numeric = parse_decimal_string(body["numeric_value"])
    expected = {"NUMERIC": "numeric_value", "BOOLEAN": "boolean_value",
                "TEXT": "text_value"}.get(check.value_type)
    if expected is None or body.get(expected) is None:
        raise invalid(f"this metric takes a {check.value_type.lower()} value",
                      {"value": "TYPE_MISMATCH"}, code=FACT_VALUE_INVALID)
    source = _one(conn, "SELECT isin FROM research_source WHERE investor_id = %s AND id = %s",
                  (investor_id, _uuid(body["source_id"], "source")))
    if source is None or source["isin"] != isin:
        raise invalid("source_id must be one of your sources for this company",
                      {"source_id": SOURCE_NOT_FOUND}, code=SOURCE_NOT_FOUND)
    return numeric, check.registry_version


def _insert_fact(
    conn: Any, investor_id: str, isin: str, body: dict[str, Any], numeric: Decimal | None,
    registry_version: str, verification: str, verified_by: str | None,
    verification_reference: str | None, supersedes_id: str | None, dkey: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO research_fact (investor_id, isin, source_id, metric, basis, unit,
                                       period_start, period_end, cadence, numeric_value,
                                       boolean_value, text_value, verification, verified_by,
                                       verification_reference, supersedes_id,
                                       registry_version, dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, isin, body["source_id"], body["metric"], body["basis"], body["unit"],
             body["period_start"], body["period_end"], body["cadence"], numeric,
             body.get("boolean_value"), body.get("text_value"), verification, verified_by,
             verification_reference, supersedes_id, registry_version, dkey),
        )
        return str(cur.fetchone()[0])


def create_fact(
    conn: Any, investor_id: str, actor_uid: str, isin: str, body: dict[str, Any],
    registry: rr.MetricRegistry, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """A human transcription. Always UNVERIFIED -- FactCreate cannot claim verification."""
    isin = ws.require_unlocked(conn, investor_id, isin)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_FACT_CREATE, isin, key, payload_digest)
    if receipt is not None:
        return _fact_response(conn, investor_id, receipt.result["fact_id"]), 200
    numeric, version = _validate_fact(conn, investor_id, isin, body, registry)
    fact_id = _insert_fact(conn, investor_id, isin, body, numeric, version, "UNVERIFIED",
                           None, None, None, ws.dedupe_key(OP_FACT_CREATE, isin, key))
    ws.audit_write(conn, actor_uid, OP_FACT_CREATE, "research_fact", fact_id)
    ws.record_receipt(conn, investor_id, OP_FACT_CREATE, isin, key, payload_digest,
                      {"fact_id": fact_id}, actor_uid)
    return _fact_response(conn, investor_id, fact_id), 201


def _require_head(conn: Any, investor_id: str, row: dict[str, Any], expected_fact_id: str
                  ) -> None:
    if _uuid(expected_fact_id, "fact") != row["id"]:
        raise conflict(STALE_PREDECESSOR, "expected_fact_id does not name this fact")
    successor = _one(conn, "SELECT id FROM research_fact WHERE investor_id = %s "
                           "AND supersedes_id = %s", (investor_id, row["id"]))
    if successor is not None:
        raise conflict(STALE_PREDECESSOR, "this fact has already been superseded; reload")


def verify_fact(
    conn: Any, investor_id: str, actor_uid: str, fact_id: str, reference: str,
    expected_fact_id: str, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """A person rechecked the exact stored fact against its source: append a VERIFIED copy
    attributed to them, superseding it. The value cannot change on the way (migration 049)."""
    row = fact_row(conn, investor_id, fact_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_FACT_VERIFY, row["id"], key,
                              payload_digest)
    if receipt is not None:
        return _fact_response(conn, investor_id, receipt.result["fact_id"]), 200
    _require_head(conn, investor_id, row, expected_fact_id)
    body = {k: row[k] for k in ("source_id", "metric", "basis", "unit", "period_start",
                                "period_end", "cadence", "boolean_value", "text_value")}
    new_id = _insert_fact(conn, investor_id, row["isin"], body, row["numeric_value"],
                          row["registry_version"] or "unrecorded", "VERIFIED", actor_uid,
                          reference, row["id"], ws.dedupe_key(OP_FACT_VERIFY, row["id"], key))
    ws.audit_write(conn, actor_uid, OP_FACT_VERIFY, "research_fact", new_id,
                   supersedes=row["id"])
    ws.record_receipt(conn, investor_id, OP_FACT_VERIFY, row["id"], key, payload_digest,
                      {"fact_id": new_id}, actor_uid)
    return _fact_response(conn, investor_id, new_id), 201


def revise_fact(
    conn: Any, investor_id: str, actor_uid: str, fact_id: str, body: dict[str, Any],
    expected_fact_id: str, registry: rr.MetricRegistry, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Same-series, same-source UNVERIFIED replacement. A different filing is independent
    evidence, created with POST /facts -- never a silent supersession."""
    row = fact_row(conn, investor_id, fact_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_FACT_REVISE, row["id"], key,
                              payload_digest)
    if receipt is not None:
        return _fact_response(conn, investor_id, receipt.result["fact_id"]), 200
    _require_head(conn, investor_id, row, expected_fact_id)
    numeric, version = _validate_fact(conn, investor_id, row["isin"], body, registry)
    same_series = all(body[k] == row[k] for k in ("metric", "basis", "unit", "cadence",
                                                  "period_start", "period_end"))
    if not same_series:
        raise invalid("a revision stays in the same series (metric, basis, unit, cadence, "
                      "period)", {"series": "CHANGED"})
    if _uuid(body["source_id"], "source") != row["source_id"]:
        raise invalid("a revision cites the same source; record another filing as a new fact",
                      {"source_id": "DIFFERENT_SOURCE"})
    new_id = _insert_fact(conn, investor_id, row["isin"], body, numeric, version,
                          "UNVERIFIED", None, None, row["id"],
                          ws.dedupe_key(OP_FACT_REVISE, row["id"], key))
    ws.audit_write(conn, actor_uid, OP_FACT_REVISE, "research_fact", new_id,
                   supersedes=row["id"])
    ws.record_receipt(conn, investor_id, OP_FACT_REVISE, row["id"], key, payload_digest,
                      {"fact_id": new_id}, actor_uid)
    return _fact_response(conn, investor_id, new_id), 201


# =========================================================================================
# evaluations and overrides (session 15)
# =========================================================================================


def _fact_candidates(
    conn: Any, investor_id: str, family: list[str], metrics: list[str], cutoff: datetime
) -> list[rr.FactCandidate]:
    """Facts RECORDED by the cutoff for the referenced metrics across the family. Later
    rows (including later supersessions) cannot influence an earlier cutoff."""
    if not metrics:
        return []
    rows = _rows(
        conn,
        f"""
        SELECT {_FACT_COLUMNS}, s.publication_date, s.retrieved_at
          FROM research_fact f
          JOIN research_source s ON s.investor_id = f.investor_id AND s.id = f.source_id
         WHERE f.investor_id = %s AND f.isin = ANY(%s::text[]) AND f.metric = ANY(%s::text[])
           AND f.recorded_at <= %s
         ORDER BY f.id
        """,
        (investor_id, family, metrics, cutoff),
    )
    return [_candidate(r) for r in rows]


def calculation_facts(
    conn: Any, investor_id: str, fact_ids: list[str], cutoff: datetime | None = None,
) -> tuple[list[rr.FactCandidate], dict[str, str]]:
    """The exact facts a calculator was asked to use (BE 27), plus which of them already have
    a successor. READ only. With ``cutoff``, a fact or successor recorded later does not exist.

    Ids that do not parse or do not exist simply come back absent; the pure engine names each
    one FACT_NOT_FOUND. Facts of another company are returned so the engine can refuse them
    by name (ISIN_MISMATCH) -- the calculator response carries ids and codes, never their
    values, for a fact outside the family."""
    ids = []
    for f in fact_ids:
        try:
            ids.append(_uuid(f, "fact"))
        except Exception:  # noqa: BLE001 - an unparseable id is FACT_NOT_FOUND, not a 422
            continue
    if not ids:
        return [], {}
    rows = _rows(
        conn,
        f"""
        SELECT {_FACT_COLUMNS}, s.publication_date, s.retrieved_at
          FROM research_fact f
          JOIN research_source s ON s.investor_id = f.investor_id AND s.id = f.source_id
         WHERE f.investor_id = %s AND f.id = ANY(%s::uuid[])
           AND (%s::timestamptz IS NULL OR f.recorded_at <= %s::timestamptz)
        """,
        (investor_id, ids, cutoff, cutoff),
    )
    successors = _rows(
        conn,
        """
        SELECT supersedes_id AS old, id AS new FROM research_fact
         WHERE investor_id = %s AND supersedes_id = ANY(%s::uuid[])
           AND (%s::timestamptz IS NULL OR recorded_at <= %s::timestamptz)
        """,
        (investor_id, ids, cutoff, cutoff),
    )
    return [_candidate(r) for r in rows], {str(s["old"]): str(s["new"]) for s in successors}


def _candidate(r: dict[str, Any]) -> rr.FactCandidate:
    return rr.FactCandidate(
        id=r["id"], isin=r["isin"], metric=r["metric"], basis=r["basis"], unit=r["unit"],
        cadence=r["cadence"], period_start=r["period_start"], period_end=r["period_end"],
        numeric_value=r["numeric_value"], boolean_value=r["boolean_value"],
        text_value=r["text_value"], verification=r["verification"], source_id=r["source_id"],
        publication_date=r["publication_date"], retrieved_at=r["retrieved_at"],
        recorded_at=r["recorded_at"], supersedes_id=r["supersedes_id"],
    )


def _rules_of(row: dict[str, Any]) -> tuple[tuple[tv.Rule, ...], tuple[tv.Rule, ...]]:
    return (tv.parse_rules(row["variables"], "variables"), tv.parse_rules(row["exits"], "exits"))


def resolve_commitment(
    conn: Any, investor_id: str, thesis_id: str, cutoff: datetime
) -> dict[str, Any]:
    """The URL thesis must be exactly the ACTIVE commitment in force at ``cutoff``."""
    row = thesis_row(conn, investor_id, thesis_id)
    selection, _ = select_in_force(conn, investor_id, row["isin"], cutoff)
    if selection.state == "CONFLICT":
        raise conflict(COMMITMENT_CONFLICT,
                       "verified aliases hold more than one active commitment; resolve them "
                       "before evaluating")
    if selection.state == "NONE":
        raise conflict(NO_COMMITMENT_IN_FORCE, "no active commitment is in force at this cutoff")
    if selection.thesis.id != row["id"]:
        raise conflict(STALE_PREDECESSOR,
                       f"thesis {selection.thesis.id} is the commitment in force; evaluate it")
    return row


def compute_evaluation(
    conn: Any,
    investor_id: str,
    thesis: dict[str, Any],
    cutoff: datetime,
    registry: rr.MetricRegistry,
    calendar_provider: Any,
) -> tuple[rr.Evaluation, dict[str, Any]]:
    """Assemble pinned inputs from this connection's view and run the pure engine.

    Returns the evaluation and the definition bodies (registry, calendar, identity) that a
    receipt must retain for replay.
    """
    family = ws.isin_family(conn, thesis["isin"])
    variables, exits = _rules_of(thesis)
    metrics = sorted({r.metric for r in (*variables, *exits)})
    facts = _fact_candidates(conn, investor_id, family, metrics, cutoff)
    calendar = calendar_provider.calendar_for(family, cutoff)
    identity = identity_scope(family)
    evaluation = rr.evaluate(thesis["id"], thesis["status"], variables, exits, facts, cutoff,
                             registry, calendar, identity, canonical.digest)
    definitions = {
        "METRIC_REGISTRY": (registry.version, registry.canonical()),
        "CALENDAR": (calendar.version, calendar.canonical()),
        "IDENTITY_SCOPE": (identity.version, identity.canonical()),
    }
    return evaluation, definitions


def check_cutoff(cutoff: datetime) -> datetime:
    if cutoff.tzinfo is None:
        raise invalid("evidence_cutoff must include a UTC offset", {"evidence_cutoff": "NAIVE"})
    if cutoff > _now():
        raise invalid("evidence_cutoff cannot be in the future; a receipt cannot know it",
                      {"evidence_cutoff": "FUTURE"})
    return cutoff


def _record_definitions(conn: Any, definitions: dict[str, tuple[str, dict]]) -> dict[str, str]:
    digests: dict[str, str] = {}
    with conn.cursor() as cur:
        for kind, (version, body) in definitions.items():
            body_digest = canonical.digest(body)
            cur.execute(
                "INSERT INTO research_definition (digest, kind, version, body) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (digest) DO NOTHING",
                (body_digest, kind, version, canonical.canonical_json(body)),
            )
            digests[kind] = body_digest
    return digests


def record_evaluation(
    conn: Any,
    investor_id: str,
    thesis_id: str,
    cutoff: datetime,
    registry: rr.MetricRegistry,
    calendar_provider: Any,
    key: str,
    payload_digest: str,
    requested_by: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Engine-capability transaction: receipt + every considered fact + definitions + audit.

    ``requested_by`` is the authenticated person who asked for the receipt. It is recorded
    in the audit row and the idempotency receipt for traceability only: the engine
    connection sets no workspace actor (it is not a person), and nothing about the result
    depends on who asked.

    Replays a committed receipt before recomputing, so a retry never produces a second
    receipt with a newer engine or registry. New evidence is a new receipt, never an UPDATE.
    """
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_EVALUATION, _uuid(thesis_id, "thesis"),
                              key, payload_digest)
    if receipt is not None:
        return evaluation_by_id(conn, investor_id, receipt.result["evaluation_id"]), 200
    thesis = resolve_commitment(conn, investor_id, thesis_id, check_cutoff(cutoff))
    evaluation, definitions = compute_evaluation(conn, investor_id, thesis, cutoff, registry,
                                                 calendar_provider)
    digests = _record_definitions(conn, definitions)
    body = rr.evaluation_dict(evaluation)
    manifest = {
        "schema_version": rr.INPUT_SCHEMA_VERSION,
        "engine_version": evaluation.engine_version,
        "thesis_id": thesis["id"],
        "thesis_version": str(thesis["version"]),
        "evidence_cutoff": body["evidence_cutoff"],
        "registry_digest": digests["METRIC_REGISTRY"],
        "calendar_digest": digests["CALENDAR"],
        "identity_digest": digests["IDENTITY_SCOPE"],
        "registry_version": evaluation.registry_version,
        "calendar_version": evaluation.calendar_version,
        "windows": {r["rule_id"] + "@" + r["kind"]: r["required_windows"]
                    for r in (*body["variables"], *body["exits"])},
        "fact_ids": list(evaluation.considered_fact_ids),
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trigger_evaluation (investor_id, thesis_id, evidence_cutoff,
                                            engine_version, input_digest, state, rule_results,
                                            dedupe_key, replay_manifest)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, thesis["id"], cutoff, evaluation.engine_version,
             evaluation.input_digest, evaluation.state,
             json.dumps([*body["variables"], *body["exits"]]),
             ws.dedupe_key(OP_EVALUATION, thesis["id"], key), json.dumps(manifest)),
        )
        evaluation_id = str(cur.fetchone()[0])
        for fact_id in evaluation.considered_fact_ids:
            cur.execute("INSERT INTO evaluation_fact (investor_id, evaluation_id, fact_id) "
                        "VALUES (%s, %s, %s)", (investor_id, evaluation_id, fact_id))
    ws.audit_write(conn, requested_by, OP_EVALUATION, "trigger_evaluation", evaluation_id,
                   state=evaluation.state, facts=len(evaluation.considered_fact_ids))
    ws.record_receipt(conn, investor_id, OP_EVALUATION, thesis["id"], key, payload_digest,
                      {"evaluation_id": evaluation_id}, requested_by)
    return evaluation_by_id(conn, investor_id, evaluation_id), 201


def _stored_evaluation(row: dict[str, Any]) -> dict[str, Any]:
    results = row["rule_results"] or []
    return {
        "id": row["id"],
        "thesis_id": row["thesis_id"],
        "state": row["state"],
        "variables": [r for r in results if r.get("kind") == "VARIABLE"],
        "exits": [r for r in results if r.get("kind") == "EXIT"],
        "evidence_cutoff": row["evidence_cutoff"],
        "evaluated_at": row["evaluated_at"],
        "engine_version": row["engine_version"],
        "input_digest": row["input_digest"],
        "replay_manifest": row["replay_manifest"],
        "fact_ids": row.get("fact_ids") or [],
    }


_EVALUATION_SQL = """
SELECT e.id, e.thesis_id, e.state, e.rule_results, e.evidence_cutoff, e.evaluated_at,
       e.engine_version, e.input_digest, e.replay_manifest,
       ARRAY(SELECT ef.fact_id::text FROM evaluation_fact ef
              WHERE ef.investor_id = e.investor_id AND ef.evaluation_id = e.id
              ORDER BY ef.fact_id) AS fact_ids
  FROM trigger_evaluation e
"""


def evaluation_by_id(conn: Any, investor_id: str, evaluation_id: str) -> dict[str, Any]:
    evaluation_id = _uuid(evaluation_id, "evaluation")
    row = _one(conn, _EVALUATION_SQL + " WHERE e.investor_id = %s AND e.id = %s",
               (investor_id, evaluation_id))
    if row is None:
        raise not_found("evaluation")
    thesis_row(conn, investor_id, row["thesis_id"])  # gate
    return _stored_evaluation(row)


def list_evaluations(
    conn: Any, investor_id: str, thesis_id: str, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    """Every stored receipt for this thesis, TRIGGERED/UNKNOWN/CONFLICT included."""
    thesis = thesis_row(conn, investor_id, thesis_id)
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "evaluations", {"thesis": thesis["id"]})
    after = canonical.decode_cursor(cursor, scope)
    rows = _rows(
        conn,
        _EVALUATION_SQL + """
         WHERE e.investor_id = %s AND e.thesis_id = %s
           AND (%s::timestamptz IS NULL OR (e.evaluated_at, e.id) < (%s::timestamptz, %s::uuid))
         ORDER BY e.evaluated_at DESC, e.id DESC LIMIT %s
        """,
        (investor_id, thesis["id"], after[0] if after else None, after[0] if after else None,
         after[1] if after else None, size + 1),
    )
    return ws._page(conn, rows, size, scope, lambda page: [_stored_evaluation(r) for r in page],
                    key=lambda r: [r["evaluated_at"].isoformat(), r["id"]])


def replay_evaluation(conn: Any, investor_id: str, evaluation_id: str) -> dict[str, Any]:
    """Re-run a stored receipt from its PINNED inputs only -- the manifest's definition
    bodies, the thesis row it named and the exact fact rows -- never a fresh live query.

    Reports VERIFIED (same digest and state), DRIFT (inputs no longer reproduce it) or
    UNAVAILABLE (a pinned definition, fact or engine is gone). Never rewrites the receipt.
    """
    stored = evaluation_by_id(conn, investor_id, evaluation_id)
    manifest = stored["replay_manifest"]
    if not manifest:
        return {"status": "UNAVAILABLE", "reasons": ["NO_REPLAY_MANIFEST"]}
    if stored["engine_version"] != rr.ENGINE_VERSION:
        return {"status": "UNAVAILABLE", "reasons": ["ENGINE_RETIRED"]}
    bodies = {
        r["digest"]: r["body"]
        for r in _rows(conn, "SELECT digest, body FROM research_definition "
                             "WHERE digest = ANY(%s::text[])",
                       ([manifest["registry_digest"], manifest["calendar_digest"],
                         manifest["identity_digest"]],))
    }
    missing = [k for k in ("registry_digest", "calendar_digest", "identity_digest")
               if manifest[k] not in bodies]
    if missing:
        return {"status": "UNAVAILABLE", "reasons": [f"DEFINITION_MISSING:{m}" for m in missing]}
    registry = rr.MetricRegistry.from_canonical(bodies[manifest["registry_digest"]])
    calendar = rr.ReportingCalendar.from_canonical(bodies[manifest["calendar_digest"]])
    identity_body = bodies[manifest["identity_digest"]]
    identity = rr.IdentityScope(tuple(identity_body["isins"]), identity_body["version"])
    thesis = _one(conn, f"SELECT {_THESIS_COLUMNS} FROM thesis t "
                        "WHERE t.investor_id = %s AND t.id = %s",
                  (investor_id, stored["thesis_id"]))
    fact_ids = manifest["fact_ids"]
    rows = _rows(
        conn,
        f"""
        SELECT {_FACT_COLUMNS}, s.publication_date, s.retrieved_at
          FROM research_fact f
          JOIN research_source s ON s.investor_id = f.investor_id AND s.id = f.source_id
         WHERE f.investor_id = %s AND f.id = ANY(%s::uuid[])
        """,
        (investor_id, fact_ids),
    )
    if thesis is None or len(rows) != len(fact_ids):
        return {"status": "UNAVAILABLE", "reasons": ["PINNED_ROW_MISSING"]}
    cutoff = stored["evidence_cutoff"]
    variables, exits = _rules_of(thesis)
    rerun = rr.evaluate(thesis["id"], "ACTIVE", variables, exits, [_candidate(r) for r in rows],
                        cutoff, registry, calendar, identity, canonical.digest)
    reasons = []
    if rerun.input_digest != stored["input_digest"]:
        reasons.append("INPUT_DIGEST_MISMATCH")
    if rerun.state != stored["state"]:
        reasons.append("STATE_MISMATCH")
    return {"status": "DRIFT" if reasons else "VERIFIED", "reasons": reasons}


def _semantic_trigger(rule_results: list[dict[str, Any]]) -> dict[str, Any]:
    """What an override is about: each MET exit's rule, windows and proving facts."""
    return {
        r["rule_id"]: {"windows": r["required_windows"], "fact_ids": sorted(r["fact_ids"])}
        for r in rule_results if r.get("kind") == "EXIT" and r["state"] == rr.MET
    }


def override_trigger(
    conn: Any,
    investor_id: str,
    actor_uid: str,
    thesis_id: str,
    evaluation_id: str,
    reason: str,
    occurred_on: date,
    registry: rr.MetricRegistry,
    calendar_provider: Any,
    key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Record a human decision to hold through a TRIGGERED exit, against that exact receipt.

    Freshness is SEMANTIC: re-evaluate now and require the same MET exits over the same
    required windows and proving facts, under the same registry and calendar definitions.
    A different wall-clock cutoff alone never makes a receipt stale. The receipt stays
    TRIGGERED afterwards; nothing here clears a trigger, changes conviction, retires the
    thesis or writes a transaction.
    """
    row = thesis_row(conn, investor_id, thesis_id)
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_THESIS_OVERRIDE, row["id"], key,
                              payload_digest)
    if receipt is not None:
        return _event_by_id(conn, investor_id, receipt.result["event_id"]), 200
    stored = evaluation_by_id(conn, investor_id, evaluation_id)
    if stored["thesis_id"] != row["id"]:
        raise not_found("evaluation")
    if stored["state"] != rr.TRIGGERED:
        raise conflict(EVALUATION_NOT_TRIGGERED, "only a TRIGGERED evaluation can be overridden")
    now = _now()
    thesis = resolve_commitment(conn, investor_id, row["id"], now)
    fresh, definitions = compute_evaluation(conn, investor_id, thesis, now, registry,
                                            calendar_provider)
    fresh_body = rr.evaluation_dict(fresh)
    manifest = stored["replay_manifest"] or {}
    same_definitions = (
        manifest.get("registry_digest") == canonical.digest(definitions["METRIC_REGISTRY"][1])
        and manifest.get("calendar_digest") == canonical.digest(definitions["CALENDAR"][1])
    )
    stored_trigger = _semantic_trigger([*stored["variables"], *stored["exits"]])
    fresh_trigger = _semantic_trigger([*fresh_body["variables"], *fresh_body["exits"]])
    if not same_definitions or not stored_trigger or stored_trigger != fresh_trigger:
        raise conflict(EVALUATION_STALE,
                       "the evidence or definitions behind this trigger have changed; record a "
                       "new evaluation and review it before overriding")
    event_id = _insert_event(conn, investor_id, actor_uid, row["id"], "OVERRIDE_EXIT_TRIGGER",
                             None, reason, stored["id"], occurred_on,
                             ws.dedupe_key(OP_THESIS_OVERRIDE, row["id"], key))
    ws.audit_write(conn, actor_uid, OP_THESIS_OVERRIDE, "thesis_event", event_id,
                   evaluation=stored["id"])
    ws.record_receipt(conn, investor_id, OP_THESIS_OVERRIDE, row["id"], key, payload_digest,
                      {"event_id": event_id}, actor_uid)
    return _event_by_id(conn, investor_id, event_id), 201


def evaluate_readonly(
    conn: Any, investor_id: str, thesis_id: str, cutoff: datetime,
    registry: rr.MetricRegistry, calendar_provider: Any,
) -> dict[str, Any]:
    """READ: compute an Evaluation with no receipt, no children and no fact writes."""
    thesis = resolve_commitment(conn, investor_id, thesis_id, check_cutoff(cutoff))
    evaluation, _ = compute_evaluation(conn, investor_id, thesis, cutoff, registry,
                                       calendar_provider)
    return {**rr.evaluation_dict(evaluation), "id": None, "evaluated_at": None,
            "replay_manifest": None, "fact_ids": list(evaluation.considered_fact_ids)}

