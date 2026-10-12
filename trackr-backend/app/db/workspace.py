"""Workspace research repository: companies, the question ritual, notes, questions, answers
and the Home feed (sessions 08-10, BLUEPRINT_BACKEND.md section 7.2).

EVERY FUNCTION HERE TAKES A CONNECTION ALREADY SCOPED BY
``app/db/workspace_context.py::enter_workspace`` to one investor (and, for writes, one
authenticated human). Row-level security (migration 045) then confines every statement to
that tenant; the explicit ``investor_id = %s`` predicates below are the belt to that braces
and keep each query readable on its own. Neither is the ownership check -- that happened in
``require_owned_investor`` before any of this ran.

NOTHING HERE IS A FINANCIAL RECORD. No function reads or writes the ledger, a holding, a
price or a transaction. The optional position on a company page is assembled by the router
from the existing engine on the existing financial connection.

THREE RULES EVERY WRITE FOLLOWS, in this order, inside one transaction:

1. **Take the tenant lock first** (``lock_tenant``), in its own statement, before reading any
   state the write depends on. Migration 044's insert guard takes the same lock; taking it
   up front means a waiter reads the committed winner under READ COMMITTED rather than a
   stale snapshot.
2. **Check the idempotency receipt next** (``check_replay``), BEFORE any stale-predecessor or
   version check. A retry of a request that already committed must replay its original
   result even if later writes have since made its preconditions false -- otherwise a
   network retry turns a success into a spurious 409.
3. **Write rows, audit and receipt together.** The caller's connection commits them as one
   unit or not at all; deferred constraints (an empty ritual) fail at COMMIT, which the
   router surfaces as an error rather than a success.

THE CONTENT GATE (``require_unlocked``) is the single definition of "may this investor see
research content for this company": the company is linked, and a ritual with at least one
saved question exists anywhere in its VERIFIED identity family. Aggregate reads filter by
``unlocked_isins`` instead of raising.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from app.db import audit
from app.research import canonical
from app.research.errors import (
    COMPANY_NOT_LINKED,
    IDEMPOTENCY_CONFLICT,
    KIND_MISMATCH,
    QUESTION_IMMUTABLE,
    SOURCE_NOT_FOUND,
    STALE_PREDECESSOR,
    UNKNOWN_SECURITY,
    WorkspaceError,
    conflict,
    invalid,
    not_found,
    ritual_required,
)
from app.research.returns import originals_due

# --- operation names (ws_receipt.operation) ------------------------------------------------
OP_COMPANY_LINK = "company.link"
OP_RITUAL = "ritual.create"
OP_NOTE_CREATE = "note.create"
OP_NOTE_REVISE = "note.revise"
OP_NOTE_LINK = "note.link"
OP_QUESTION_CREATE = "question.create"
OP_ANSWER = "answer.create"

#: audit_log actions for research writes. Entity ids and counts only -- never prose.
ACTION_RESEARCH_WRITE = "RESEARCH_WRITE"

NOTE_KINDS_WRITABLE = ("OBSERVATION", "INTERPRETATION", "THESIS_NOTE", "LITERATURE", "DECISION")
LINK_RELATIONS = ("READS", "SUPPORTS", "CONTRADICTS", "CITES")

#: Home feed bounds. Collections are bounded and say so; nothing is silently dropped.
HOME_OPEN_QUESTIONS = 20
HOME_RETURNS = 50


# =========================================================================================
# plumbing: rows, lock, receipts, audit
# =========================================================================================


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value


def _rows(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [{k: _plain(v) for k, v in row.items()} for row in cur.fetchall()]


def _one(conn: Any, sql: str, params: Any = None) -> dict[str, Any] | None:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _uuid(value: str, what: str) -> str:
    """Canonical UUID text or a scoped 404 -- a malformed id is simply not one of yours."""
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise not_found(what) from exc


def lock_tenant(conn: Any, investor_id: str) -> None:
    """The same transaction-scoped advisory lock migration 044's insert guard takes."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))", (investor_id,))


@dataclass(frozen=True)
class Receipt:
    status_code: int
    result: dict[str, Any]


def check_replay(
    conn: Any, investor_id: str, operation: str, scope: str, key: str, payload_digest: str
) -> Receipt | None:
    """The committed receipt for this key, if any. A different payload is 409.

    Called AFTER ``lock_tenant`` so a concurrent twin of this request is either fully
    committed (and found here) or still waiting behind us.
    """
    row = _one(
        conn,
        """
        SELECT payload_digest, status_code, result FROM ws_receipt
         WHERE investor_id = %s AND operation = %s AND scope = %s AND idem_key = %s
        """,
        (investor_id, operation, scope, key),
    )
    if row is None:
        return None
    if row["payload_digest"] != payload_digest:
        raise conflict(
            IDEMPOTENCY_CONFLICT,
            "this Idempotency-Key was already used for a different request",
        )
    return Receipt(status_code=row["status_code"], result=row["result"])


def record_receipt(
    conn: Any,
    investor_id: str,
    operation: str,
    scope: str,
    key: str,
    payload_digest: str,
    result: dict[str, Any],
    actor_uid: str | None,
    status_code: int = 201,
) -> None:
    """Identifiers only. ``result`` values must be strings or lists of strings."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ws_receipt (investor_id, operation, scope, idem_key, payload_digest,
                                    status_code, result, actor_uid)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, operation, scope, key, payload_digest, status_code,
             json.dumps(result), actor_uid),
        )


def dedupe_key(operation: str, scope: str, key: str, part: str = "root") -> str:
    """Row-level dedupe_key, namespaced exactly like the receipt."""
    return f"{operation}:{scope}:{key}:{part}"


def audit_write(
    conn: Any, actor_uid: str | None, operation: str, entity_type: str, entity_id: str,
    **counts: Any,
) -> None:
    """Same-transaction audit row. Operation, entity and counts -- never research prose."""
    audit.record(
        conn,
        ACTION_RESEARCH_WRITE,
        entity_type,
        entity_id,
        actor_uid,
        {"operation": operation, **{k: v for k, v in counts.items() if v is not None}},
    )


# =========================================================================================
# identity: securities, verified alias families
# =========================================================================================

_FAMILY_SQL = """
WITH RECURSIVE family(isin) AS (
  SELECT unnest(%s::text[])
  UNION
  SELECT CASE WHEN s.old_isin = f.isin THEN s.current_isin ELSE s.old_isin END
    FROM family f JOIN security_alias s
      ON (s.old_isin = f.isin OR s.current_isin = f.isin)
   WHERE s.verification = 'VERIFIED'
) SELECT isin FROM family
"""


def isin_family(conn: Any, isin: str) -> list[str]:
    """Every ISIN connected to ``isin`` through VERIFIED alias edges, including itself.

    The same graph migration 044's ``ws_same_security`` walks. DRAFT aliases are not
    followed: an unverified identity claim must not merge two research histories.
    """
    return sorted(r["isin"] for r in _rows(conn, _FAMILY_SQL, ([isin],)))


def canonical_isin(conn: Any, isin: str) -> str:
    """Terminal ISIN of the VERIFIED old->current chain starting at ``isin``."""
    row = _one(
        conn,
        """
        WITH RECURSIVE chain(isin, depth) AS (
          SELECT %s::text, 0
          UNION
          SELECT a.current_isin, c.depth + 1
            FROM chain c JOIN security_alias a
              ON a.old_isin = c.isin AND a.verification = 'VERIFIED'
           WHERE c.depth < 32
        ) SELECT isin FROM chain ORDER BY depth DESC LIMIT 1
        """,
        (isin,),
    )
    return row["isin"] if row else isin


def identities(conn: Any, isins: list[str]) -> dict[str, dict[str, Any]]:
    """Identity = {isin, canonical_isin, symbol, name}. Symbol/name are the canonical
    security's display fields (tickers are display only; the ISIN is the key)."""
    out: dict[str, dict[str, Any]] = {}
    for isin in dict.fromkeys(isins):
        canon = canonical_isin(conn, isin)
        meta = {
            r["isin"]: r
            for r in _rows(
                conn,
                "SELECT isin, symbol, name FROM security WHERE isin = ANY(%s::text[])",
                ([isin, canon],),
            )
        }
        shown = meta.get(canon) or meta.get(isin) or {}
        out[isin] = {
            "isin": isin,
            "canonical_isin": canon,
            "symbol": shown.get("symbol"),
            "name": shown.get("name"),
        }
    return out


def identity(conn: Any, isin: str) -> dict[str, Any]:
    return identities(conn, [isin])[isin]


def _valid_isin(isin: str) -> str:
    candidate = (isin or "").strip().upper()
    if len(candidate) != 12 or not candidate.startswith("IN") or not candidate.isalnum():
        raise invalid("isin must be a 12-character Indian ISIN", {"isin": "FORMAT"})
    return candidate


def search_securities(
    conn: Any, investor_id: str, q: str, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    """Existing EQ securities matching ``q`` on ISIN, symbol or name. Never creates one."""
    text = (q or "").strip()
    if not text or len(text) > 64:
        raise invalid("q is required and at most 64 characters", {"q": "RANGE"})
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "securities", {"q": text})
    after = canonical.decode_cursor(cursor, scope)
    pattern = canonical.escape_like(text)
    rows = _rows(
        conn,
        """
        SELECT isin FROM security
         WHERE asset_class = 'EQ'
           AND (isin ILIKE %s ESCAPE '\\' OR symbol ILIKE %s ESCAPE '\\'
                OR name ILIKE %s ESCAPE '\\')
           AND (%s::text IS NULL OR isin > %s::text)
         ORDER BY isin
         LIMIT %s
        """,
        (pattern, pattern, pattern, after[0] if after else None,
         after[0] if after else None, size + 1),
    )
    return _page(conn, [r["isin"] for r in rows], size, scope,
                 lambda isins: [identities(conn, isins)[i] for i in isins],
                 key=lambda isin: [isin])


def _page(
    conn: Any, keys: list[Any], size: int, scope: str, hydrate: Any, key: Any
) -> dict[str, Any]:
    more = len(keys) > size
    keys = keys[:size]
    items = hydrate(keys) if keys else []
    return {
        "items": items,
        "next_cursor": canonical.encode_cursor(scope, key(keys[-1])) if more and keys else None,
    }


# =========================================================================================
# companies and the content gate
# =========================================================================================


def company_linked(conn: Any, investor_id: str, isin: str) -> bool:
    return _one(
        conn, "SELECT 1 AS x FROM company WHERE investor_id = %s AND isin = %s",
        (investor_id, isin),
    ) is not None


def ritual_complete(conn: Any, investor_id: str, isin: str) -> bool:
    """A ritual exists for this company's VERIFIED identity family.

    Completion means questions were RECORDED (migration 044 refuses an empty ritual at
    commit), never that they were answered.
    """
    family = isin_family(conn, isin)
    return _one(
        conn,
        "SELECT 1 AS x FROM ritual WHERE investor_id = %s AND isin = ANY(%s::text[]) LIMIT 1",
        (investor_id, family),
    ) is not None


def require_linked(conn: Any, investor_id: str, isin: str) -> str:
    isin = _valid_isin(isin)
    if not company_linked(conn, investor_id, isin):
        raise WorkspaceError(404, COMPANY_NOT_LINKED, "this company is not in your research")
    return isin


def require_unlocked(conn: Any, investor_id: str, isin: str) -> str:
    """THE shared content gate. 404 if not linked; 409 RITUAL_REQUIRED if locked."""
    isin = require_linked(conn, investor_id, isin)
    if not ritual_complete(conn, investor_id, isin):
        raise ritual_required()
    return isin


def unlocked_isins(conn: Any, investor_id: str) -> set[str]:
    """Every linked company ISIN whose identity family has a ritual. Aggregate reads
    (search, Home, backlinks) filter by this set instead of raising."""
    rows = _rows(
        conn,
        """
        SELECT c.isin FROM company c
         WHERE c.investor_id = %s
           AND EXISTS (SELECT 1 FROM ritual r
                        WHERE r.investor_id = c.investor_id
                          AND ws_same_security(r.isin, c.isin))
        """,
        (investor_id,),
    )
    return {r["isin"] for r in rows}


def visible_isins(conn: Any, investor_id: str) -> set[str]:
    """ISINs whose research rows may be shown: every member of an unlocked company's
    verified family, so a note filed under a superseded ISIN stays visible."""
    visible: set[str] = set()
    for isin in unlocked_isins(conn, investor_id):
        visible.update(isin_family(conn, isin))
    return visible


def list_companies(
    conn: Any, investor_id: str, q: str | None, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    """Owned registered companies with ``ritual_complete``. No locked-content preview."""
    size = canonical.clamp_limit(limit)
    text = (q or "").strip()
    if len(text) > 64:
        raise invalid("q is at most 64 characters", {"q": "RANGE"})
    scope = canonical.scope_digest(investor_id, "companies", {"q": text})
    after = canonical.decode_cursor(cursor, scope)
    pattern = canonical.escape_like(text) if text else None
    rows = _rows(
        conn,
        """
        SELECT c.isin FROM company c JOIN security s ON s.isin = c.isin
         WHERE c.investor_id = %s
           AND (%s::text IS NULL OR c.isin ILIKE %s ESCAPE '\\'
                OR s.symbol ILIKE %s ESCAPE '\\' OR s.name ILIKE %s ESCAPE '\\')
           AND (%s::text IS NULL OR c.isin > %s::text)
         ORDER BY c.isin
         LIMIT %s
        """,
        (investor_id, pattern, pattern, pattern, pattern,
         after[0] if after else None, after[0] if after else None, size + 1),
    )
    unlocked = unlocked_isins(conn, investor_id)

    def hydrate(isins: list[str]) -> list[dict[str, Any]]:
        ids = identities(conn, isins)
        return [{**ids[i], "ritual_complete": i in unlocked} for i in isins]

    return _page(conn, [r["isin"] for r in rows], size, scope, hydrate, key=lambda i: [i])


def link_company(
    conn: Any, investor_id: str, actor_uid: str, isin: str, key: str, payload_digest: str
) -> tuple[dict[str, Any], int]:
    """Link an existing EQ security to this investor. Durable receipt even when the
    company was already linked, so a reused key for another ISIN is still detected."""
    isin = _valid_isin(isin)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_COMPANY_LINK, "-", key, payload_digest)
    if receipt is not None:
        return identity(conn, receipt.result["isin"]), 200

    security = _one(conn, "SELECT isin, asset_class::text AS asset_class FROM security "
                          "WHERE isin = %s", (isin,))
    if security is None or security["asset_class"] != "EQ":
        raise WorkspaceError(404, UNKNOWN_SECURITY,
                             "no listed equity with this ISIN is known to the system")
    already = company_linked(conn, investor_id, isin)
    if not already:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO company (investor_id, isin) VALUES (%s, %s)",
                        (investor_id, isin))
    audit_write(conn, actor_uid, OP_COMPANY_LINK, "company", investor_id,
                already_linked=already)
    record_receipt(conn, investor_id, OP_COMPANY_LINK, "-", key, payload_digest,
                   {"isin": isin}, actor_uid)
    return identity(conn, isin), 201


# =========================================================================================
# rituals and questions
# =========================================================================================

_QUESTION_SQL = """
SELECT q.id, q.isin, q.note_id, q.ritual_id, q.blocking, q.recorded_at,
       n.title AS text, n.written_on, n.seq AS note_seq,
       EXISTS (SELECT 1 FROM question_answer a
                WHERE a.investor_id = q.investor_id AND a.question_id = q.id) AS answered
  FROM question q
  JOIN note n ON n.investor_id = q.investor_id AND n.id = q.note_id
 WHERE q.investor_id = %(inv)s
"""


def _answers_for(conn: Any, investor_id: str, question_ids: list[str]) -> dict[str, list[dict]]:
    rows = _rows(
        conn,
        """
        SELECT id, question_id, answer, answered_on, recorded_at, previous_id, actor_uid, seq
          FROM question_answer
         WHERE investor_id = %s AND question_id = ANY(%s::uuid[])
         ORDER BY seq
        """,
        (investor_id, question_ids),
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["question_id"]].append(row)
    return grouped


def _latest_answer(chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The answer nothing names as its predecessor. Receipt-ordered, not by answered_on."""
    if not chain:
        return None
    named = {a["previous_id"] for a in chain if a["previous_id"]}
    tips = [a for a in chain if a["id"] not in named]
    return max(tips, key=lambda a: a["seq"]) if tips else chain[-1]


def _answer_dto(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "question_id": row["question_id"],
        "answer": row["answer"],
        "answered_on": row["answered_on"],
        "recorded_at": row["recorded_at"],
        "previous_id": row["previous_id"],
        "actor_uid": row["actor_uid"],
    }


def _reviews_for(conn: Any, investor_id: str, answer_ids: list[str]) -> dict[str, dict]:
    """The latest passing ANSWER_REVIEW proposal for each EXACT answer id.

    Keyed by answer id, so a revised answer never inherits the review of its predecessor.
    No record -> absent (the DTO shows null), never a fabricated review.
    """
    if not answer_ids:
        return {}
    rows = _rows(
        conn,
        """
        SELECT DISTINCT ON (t.answer_id)
               t.answer_id, t.id AS task_id, r.id AS result_id, r.structured_output,
               rv.decision, (a.mode = 'STORED_SAMPLE') AS sample
          FROM task t
          JOIN task_result r ON r.investor_id = t.investor_id AND r.task_id = t.id
                            AND r.outcome = 'PROPOSED' AND r.guardrail_status = 'PASS'
          JOIN task_attempt a ON a.investor_id = r.investor_id AND a.id = r.attempt_id
          LEFT JOIN task_review rv ON rv.investor_id = r.investor_id AND rv.result_id = r.id
         WHERE t.investor_id = %s AND t.purpose = 'ANSWER_REVIEW'
           AND t.answer_id = ANY(%s::uuid[])
         ORDER BY t.answer_id, r.finished_at DESC
        """,
        (investor_id, answer_ids),
    )
    # Imported here, not at module level: app/db/tasks.py imports this module.
    from app.db.tasks import entries_for
    from app.research import guardrails as gr

    out: dict[str, dict] = {}
    for row in rows:
        output = row["structured_output"] or {}
        review = (output.get("proposal") or {}).get("answer_review") or {}
        # ok[]/gap[] are cited paragraphs (BE 25); values are filled from the task's pinned
        # rows by code, exactly as the task view renders them. Never re-worded here.
        context = entries_for(conn, investor_id, row["task_id"]) if review else {}
        out[row["answer_id"]] = {
            "answer_id": row["answer_id"],
            "task_id": row["task_id"],
            "result_id": row["result_id"],
            "ok": [gr._fill(p, context)["text"] for p in review.get("ok", [])],
            "gap": [gr._fill(p, context)["text"] for p in review.get("gap", [])],
            "decision": row["decision"],
            "sample": bool(row["sample"]),
        }
    return out


def _hydrate_questions(conn: Any, investor_id: str, rows: list[dict]) -> list[dict[str, Any]]:
    answers = _answers_for(conn, investor_id, [r["id"] for r in rows])
    latest = {qid: _latest_answer(chain) for qid, chain in answers.items()}
    reviews = _reviews_for(conn, investor_id, [a["id"] for a in latest.values() if a])
    out = []
    for row in rows:
        chain = answers.get(row["id"], [])
        tip = latest.get(row["id"])
        out.append({
            "id": row["id"],
            "isin": row["isin"],
            "note_id": row["note_id"],
            "ritual_id": row["ritual_id"],
            "blocking": row["blocking"],
            "recorded_at": row["recorded_at"],
            "text": row["text"],
            "written_on": row["written_on"],
            # Derived from answer existence, never stored.
            "status": "ANSWERED" if chain else "OPEN",
            "answer_ids": [a["id"] for a in chain],
            "latest_answer": _answer_dto(tip) if tip else None,
            "review": reviews.get(tip["id"]) if tip else None,
        })
    return out


def question_by_id(conn: Any, investor_id: str, question_id: str) -> dict[str, Any]:
    """Exact question, gated on its company. Foreign or locked -> 404 / 409."""
    question_id = _uuid(question_id, "question")
    rows = _rows(conn, _QUESTION_SQL + " AND q.id = %(qid)s",
                 {"inv": investor_id, "qid": question_id})
    if not rows:
        raise not_found("question")
    if rows[0]["isin"] not in visible_isins(conn, investor_id):
        raise ritual_required()
    return _hydrate_questions(conn, investor_id, rows)[0]


def open_questions_for(conn: Any, investor_id: str, family: list[str]) -> list[dict[str, Any]]:
    """OPEN questions for a company family, blocking first, oldest first."""
    rows = _rows(
        conn,
        _QUESTION_SQL + " AND q.isin = ANY(%(fam)s::text[]) ORDER BY q.blocking DESC, n.seq",
        {"inv": investor_id, "fam": family},
    )
    return [q for q in _hydrate_questions(conn, investor_id, rows) if q["status"] == "OPEN"]


def list_questions(
    conn: Any,
    investor_id: str,
    isin: str | None,
    status: str | None,
    blocking: bool | None,
    cursor: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Page<Question>, OPEN first, then blocking first, then oldest first."""
    size = canonical.clamp_limit(limit)
    if status is not None and status not in ("OPEN", "ANSWERED"):
        raise invalid("status must be OPEN or ANSWERED", {"status": "ENUM"})
    filters = {"isin": isin, "status": status, "blocking": blocking}
    scope = canonical.scope_digest(investor_id, "questions", filters)
    after = canonical.decode_cursor(cursor, scope)

    visible = visible_isins(conn, investor_id)
    if isin is not None:
        isin = require_unlocked(conn, investor_id, isin)
        allowed = [i for i in isin_family(conn, isin) if i in visible]
    else:
        allowed = sorted(visible)

    rows = _rows(
        conn,
        """
        SELECT * FROM (
          SELECT q.id,
                 CASE WHEN EXISTS (SELECT 1 FROM question_answer a
                                    WHERE a.investor_id = q.investor_id
                                      AND a.question_id = q.id) THEN 1 ELSE 0 END AS rank,
                 CASE WHEN q.blocking THEN 0 ELSE 1 END AS brank,
                 n.seq
            FROM question q
            JOIN note n ON n.investor_id = q.investor_id AND n.id = q.note_id
           WHERE q.investor_id = %(inv)s AND q.isin = ANY(%(allowed)s::text[])
             AND (%(blocking)s::boolean IS NULL OR q.blocking = %(blocking)s::boolean)
        ) x
        WHERE (%(status)s::text IS NULL
               OR (%(status)s = 'OPEN' AND rank = 0) OR (%(status)s = 'ANSWERED' AND rank = 1))
          AND (%(k0)s::int IS NULL
               OR (rank, brank, seq) > (%(k0)s::int, %(k1)s::int, %(k2)s::bigint))
        ORDER BY rank, brank, seq
        LIMIT %(lim)s
        """,
        {"inv": investor_id, "allowed": allowed, "blocking": blocking, "status": status,
         "k0": after[0] if after else None, "k1": after[1] if after else None,
         "k2": after[2] if after else None, "lim": size + 1},
    )
    keys = [(r["id"], r["rank"], r["brank"], r["seq"]) for r in rows]

    def hydrate(page_keys: list[tuple]) -> list[dict[str, Any]]:
        ids = [k[0] for k in page_keys]
        full = _rows(conn, _QUESTION_SQL + " AND q.id = ANY(%(ids)s::uuid[])",
                     {"inv": investor_id, "ids": ids})
        by_id = {r["id"]: r for r in full}
        return _hydrate_questions(conn, investor_id, [by_id[i] for i in ids])

    return _page(conn, keys, size, scope, hydrate, key=lambda k: [k[1], k[2], k[3]])


def _insert_question(
    conn: Any, investor_id: str, actor_uid: str, isin: str, text: str, blocking: bool,
    written_on: date, ritual_id: str | None, dkey: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO note (investor_id, isin, kind, title, body, written_on, actor_uid,
                              authorship, dedupe_key)
            VALUES (%s, %s, 'QUESTION', %s, '', %s, %s, 'HUMAN', %s)
            RETURNING id
            """,
            (investor_id, isin, text, written_on, actor_uid, dkey + ":note"),
        )
        note_id = str(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO question (investor_id, isin, note_id, ritual_id, blocking)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, isin, note_id, ritual_id, blocking),
        )
        return str(cur.fetchone()[0])


def create_ritual(
    conn: Any,
    investor_id: str,
    actor_uid: str,
    isin: str,
    questions: list[dict[str, Any]],
    written_on: date,
    key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Ritual + QUESTION notes + questions + audit + receipt, atomically.

    At least one question is required here AND at commit (migration 044's deferred
    ``ritual_complete`` trigger); a failure anywhere leaves no partial unlock. A later
    ritual appends history; it never edits an earlier one.
    """
    isin = require_linked(conn, investor_id, isin)
    if not questions:
        raise invalid("a ritual needs at least one question", {"questions": "EMPTY"})
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_RITUAL, isin, key, payload_digest)
    if receipt is not None:
        return _ritual_response(conn, investor_id, receipt.result), 200

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ritual (investor_id, isin, actor_uid, dedupe_key) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (investor_id, isin, actor_uid, dedupe_key(OP_RITUAL, isin, key)),
        )
        ritual_id = str(cur.fetchone()[0])
    question_ids = [
        _insert_question(conn, investor_id, actor_uid, isin, q["text"], q["blocking"],
                         written_on, ritual_id, dedupe_key(OP_RITUAL, isin, key, f"q{i}"))
        for i, q in enumerate(questions)
    ]
    audit_write(conn, actor_uid, OP_RITUAL, "ritual", ritual_id, questions=len(question_ids))
    result = {"ritual_id": ritual_id, "question_ids": question_ids}
    record_receipt(conn, investor_id, OP_RITUAL, isin, key, payload_digest, result, actor_uid)
    return _ritual_response(conn, investor_id, result), 201


def _ritual_response(conn: Any, investor_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ids = result["question_ids"]
    rows = _rows(conn, _QUESTION_SQL + " AND q.id = ANY(%(ids)s::uuid[])",
                 {"inv": investor_id, "ids": ids})
    by_id = {r["id"]: r for r in rows}
    return {
        "ritual_id": result["ritual_id"],
        "questions": _hydrate_questions(conn, investor_id, [by_id[i] for i in ids]),
        "state": "READY",
    }


def create_question(
    conn: Any, investor_id: str, actor_uid: str, isin: str, text: str, blocking: bool,
    written_on: date, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """A standalone question on an already-unlocked company. Creates no ritual and
    changes no earlier question."""
    isin = require_unlocked(conn, investor_id, isin)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_QUESTION_CREATE, isin, key, payload_digest)
    if receipt is not None:
        return question_by_id(conn, investor_id, receipt.result["question_id"]), 200
    question_id = _insert_question(conn, investor_id, actor_uid, isin, text, blocking,
                                   written_on, None, dedupe_key(OP_QUESTION_CREATE, isin, key))
    audit_write(conn, actor_uid, OP_QUESTION_CREATE, "question", question_id)
    record_receipt(conn, investor_id, OP_QUESTION_CREATE, isin, key, payload_digest,
                   {"question_id": question_id}, actor_uid)
    return question_by_id(conn, investor_id, question_id), 201


def answer_question(
    conn: Any, investor_id: str, actor_uid: str, question_id: str, answer: str,
    answered_on: date, expected_latest_answer_id: str | None, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Append the investor's own answer. Null expected id means the first answer."""
    question = question_by_id(conn, investor_id, question_id)
    qid = question["id"]
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_ANSWER, qid, key, payload_digest)
    if receipt is not None:
        return _answer_by_id(conn, investor_id, receipt.result["answer_id"]), 200

    chain = _answers_for(conn, investor_id, [qid]).get(qid, [])
    tip = _latest_answer(chain)
    current = tip["id"] if tip else None
    expected = str(UUID(expected_latest_answer_id)) if expected_latest_answer_id else None
    if expected != current:
        raise conflict(
            STALE_PREDECESSOR,
            "the latest answer has changed; reload before revising"
            if current else "this question has no answer yet; send a first answer",
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO question_answer (investor_id, question_id, answer, actor_uid,
                                         answered_on, previous_id, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, qid, answer, actor_uid, answered_on, current,
             dedupe_key(OP_ANSWER, qid, key)),
        )
        answer_id = str(cur.fetchone()[0])
    audit_write(conn, actor_uid, OP_ANSWER, "question_answer", answer_id,
                revision=current is not None)
    record_receipt(conn, investor_id, OP_ANSWER, qid, key, payload_digest,
                   {"answer_id": answer_id}, actor_uid)
    return _answer_by_id(conn, investor_id, answer_id), 201


def _answer_by_id(conn: Any, investor_id: str, answer_id: str) -> dict[str, Any]:
    row = _one(
        conn,
        "SELECT id, question_id, answer, answered_on, recorded_at, previous_id, actor_uid, seq "
        "FROM question_answer WHERE investor_id = %s AND id = %s",
        (investor_id, answer_id),
    )
    if row is None:
        raise not_found("answer")
    return _answer_dto(row)


# =========================================================================================
# notes
# =========================================================================================

_NOTE_COLUMNS = """
n.id, n.isin, n.kind, n.title, n.body, n.written_on, n.recorded_at, n.actor_uid,
n.authorship, n.primary_source_id, n.previous_id, n.seq
"""


def sources_by_id(conn: Any, investor_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Source metadata rows, with their extraction state (session 12)."""
    if not ids:
        return {}
    rows = _rows(
        conn,
        """
        SELECT s.id, s.isin, s.source, s.source_type, s.source_url, s.publication_date,
               s.period, s.retrieved_at, s.document_reference, s.document_id,
               s.content_sha256, s.recorded_at,
               e.status AS extraction_status, e.unavailable_reason, e.extractor_id,
               e.extractor_version, e.normalization, e.text_sha256, e.page_count
          FROM research_source s
          LEFT JOIN research_source_extraction e
            ON e.investor_id = s.investor_id AND e.source_id = s.id
         WHERE s.investor_id = %s AND s.id = ANY(%s::uuid[])
        """,
        (investor_id, ids),
    )
    return {r["id"]: source_dto(r) for r in rows}


def source_dto(row: dict[str, Any]) -> dict[str, Any]:
    status = row.get("extraction_status")
    return {
        "id": row["id"],
        "isin": row["isin"],
        "source": row["source"],
        "source_type": row["source_type"],
        "source_url": row["source_url"],
        "publication_date": row["publication_date"],
        "period": row["period"],
        "retrieved_at": row["retrieved_at"],
        "document_reference": row["document_reference"],
        "document_id": row["document_id"],
        "content_sha256": row["content_sha256"],
        "recorded_at": row["recorded_at"],
        "content": {
            # METADATA_ONLY: nothing was read. Never claim otherwise.
            "state": status or "METADATA_ONLY",
            "unavailable_reason": row.get("unavailable_reason"),
            "extractor_id": row.get("extractor_id"),
            "extractor_version": row.get("extractor_version"),
            "normalization": row.get("normalization"),
            "text_sha256": row.get("text_sha256"),
            "page_count": row.get("page_count"),
        },
    }


def _chains(conn: Any, investor_id: str, note_ids: list[str]) -> dict[str, list[str]]:
    """Full revision chain (oldest first, by receipt sequence) for each note id."""
    if not note_ids:
        return {}
    rows = _rows(
        conn,
        """
        WITH RECURSIVE up(start, id, previous_id) AS (
          SELECT n.id, n.id, n.previous_id FROM note n
           WHERE n.investor_id = %(inv)s AND n.id = ANY(%(ids)s::uuid[])
          UNION ALL
          SELECT up.start, p.id, p.previous_id FROM up
            JOIN note p ON p.investor_id = %(inv)s AND p.id = up.previous_id
        ),
        down(start, id) AS (
          SELECT n.id, n.id FROM note n
           WHERE n.investor_id = %(inv)s AND n.id = ANY(%(ids)s::uuid[])
          UNION ALL
          SELECT down.start, c.id FROM down
            JOIN note c ON c.investor_id = %(inv)s AND c.previous_id = down.id
        ),
        members AS (SELECT start, id FROM up UNION SELECT start, id FROM down)
        SELECT m.start, m.id, n.seq FROM members m
          JOIN note n ON n.investor_id = %(inv)s AND n.id = m.id
         ORDER BY m.start, n.seq
        """,
        {"inv": investor_id, "ids": note_ids},
    )
    chains: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        chains[row["start"]].append(row["id"])
    return chains


def _links(
    conn: Any, investor_id: str, note_ids: list[str], visible: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """Outgoing and incoming links for each note, with the far end gated.

    A link whose other end is a note, source or thesis of a LOCKED or foreign company is
    dropped entirely -- no title, no id, no count -- so backlinks cannot leak.
    """
    if not note_ids:
        return {}
    rows = _rows(
        conn,
        """
        SELECT l.id, l.from_note_id, l.to_note_id, l.source_id, l.thesis_id, l.relation,
               l.recorded_at, l.actor_uid,
               fn.isin AS from_isin, tn.isin AS to_isin, s.isin AS source_isin,
               t.isin AS thesis_isin
          FROM note_link l
          JOIN note fn ON fn.investor_id = l.investor_id AND fn.id = l.from_note_id
          LEFT JOIN note tn ON tn.investor_id = l.investor_id AND tn.id = l.to_note_id
          LEFT JOIN research_source s ON s.investor_id = l.investor_id AND s.id = l.source_id
          LEFT JOIN thesis t ON t.investor_id = l.investor_id AND t.id = l.thesis_id
         WHERE l.investor_id = %(inv)s
           AND (l.from_note_id = ANY(%(ids)s::uuid[]) OR l.to_note_id = ANY(%(ids)s::uuid[]))
         ORDER BY l.recorded_at, l.id
        """,
        {"inv": investor_id, "ids": note_ids},
    )
    wanted = set(note_ids)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        target_isin = row["to_isin"] or row["source_isin"] or row["thesis_isin"]
        if row["from_isin"] not in visible or target_isin not in visible:
            continue
        link = {
            "id": row["id"],
            "from_note_id": row["from_note_id"],
            "to_note_id": row["to_note_id"],
            "source_id": row["source_id"],
            "thesis_id": row["thesis_id"],
            "relation": row["relation"],
            "recorded_at": row["recorded_at"],
            "actor_uid": row["actor_uid"],
        }
        if row["from_note_id"] in wanted:
            out[row["from_note_id"]].append({**link, "direction": "OUT"})
        if row["to_note_id"] in wanted:
            out[row["to_note_id"]].append({**link, "direction": "IN"})
    return out


def _accepted_from(conn: Any, investor_id: str, note_ids: list[str]) -> dict[str, dict]:
    if not note_ids:
        return {}
    rows = _rows(
        conn,
        """
        SELECT rv.accepted_note_id, rv.task_id, rv.result_id,
               (a.mode = 'STORED_SAMPLE') AS sample
          FROM task_review rv
          JOIN task_result r ON r.investor_id = rv.investor_id AND r.id = rv.result_id
          JOIN task_attempt a ON a.investor_id = r.investor_id AND a.id = r.attempt_id
         WHERE rv.investor_id = %s AND rv.decision = 'ACCEPT'
           AND rv.accepted_note_id = ANY(%s::uuid[])
        """,
        (investor_id, note_ids),
    )
    return {
        r["accepted_note_id"]: {"task_id": r["task_id"], "result_id": r["result_id"],
                                "sample": bool(r["sample"])}
        for r in rows
    }


def hydrate_notes(
    conn: Any, investor_id: str, rows: list[dict[str, Any]], visible: set[str] | None = None
) -> list[dict[str, Any]]:
    """Note = row + source + gated links + revision ids + acceptance provenance."""
    if not rows:
        return []
    visible = visible if visible is not None else visible_isins(conn, investor_id)
    ids = [r["id"] for r in rows]
    sources = sources_by_id(conn, investor_id,
                            [r["primary_source_id"] for r in rows if r["primary_source_id"]])
    chains = _chains(conn, investor_id, ids)
    links = _links(conn, investor_id, ids, visible)
    accepted = _accepted_from(conn, investor_id, ids)
    out = []
    for row in rows:
        chain = chains.get(row["id"], [row["id"]])
        out.append({
            "id": row["id"],
            "isin": row["isin"],
            "kind": row["kind"],
            "title": row["title"],
            "body": row["body"],
            "written_on": row["written_on"],
            "recorded_at": row["recorded_at"],
            "actor_uid": row["actor_uid"],
            "authorship": row["authorship"],
            "primary_source_id": row["primary_source_id"],
            "previous_id": row["previous_id"],
            "source": sources.get(row["primary_source_id"]) if row["primary_source_id"] else None,
            "links": links.get(row["id"], []),
            "revision_ids": chain,
            "latest_id": chain[-1],
            "is_latest": chain[-1] == row["id"],
            "accepted_from": accepted.get(row["id"]),
        })
    return out


def note_by_id(conn: Any, investor_id: str, note_id: str) -> dict[str, Any]:
    """Exact immutable note, gated on its company."""
    note_id = _uuid(note_id, "note")
    row = _one(conn, f"SELECT {_NOTE_COLUMNS} FROM note n WHERE n.investor_id = %s AND n.id = %s",
               (investor_id, note_id))
    if row is None:
        raise not_found("note")
    visible = visible_isins(conn, investor_id)
    if row["isin"] not in visible:
        raise ritual_required()
    return hydrate_notes(conn, investor_id, [row], visible)[0]


_LATEST_ONLY = """
NOT EXISTS (SELECT 1 FROM note s WHERE s.investor_id = n.investor_id AND s.previous_id = n.id)
"""


def list_notes(
    conn: Any, investor_id: str, isin: str, kind: str | None, history: bool,
    cursor: str | None, limit: int | None,
) -> dict[str, Any]:
    """Page<Note> for one company family, newest (written_on, seq) first.

    Latest revisions only unless ``history``. "Latest" is decided by the revision CHAIN,
    so a backdated revision still replaces its predecessor. QUESTION notes are listed only
    when asked for by kind; questions have their own route.
    """
    isin = require_unlocked(conn, investor_id, isin)
    size = canonical.clamp_limit(limit)
    if kind is not None and kind not in (*NOTE_KINDS_WRITABLE, "QUESTION"):
        raise invalid("unknown note kind", {"kind": "ENUM"})
    scope = canonical.scope_digest(investor_id, "notes",
                                   {"isin": isin, "kind": kind, "history": history})
    after = canonical.decode_cursor(cursor, scope)
    family = isin_family(conn, isin)
    rows = _rows(
        conn,
        f"""
        SELECT {_NOTE_COLUMNS} FROM note n
         WHERE n.investor_id = %(inv)s AND n.isin = ANY(%(fam)s::text[])
           AND (%(kind)s::text IS NULL AND n.kind <> 'QUESTION' OR n.kind = %(kind)s::text)
           AND (%(history)s OR {_LATEST_ONLY})
           AND (%(k0)s::date IS NULL OR (n.written_on, n.seq) < (%(k0)s::date, %(k1)s::bigint))
         ORDER BY n.written_on DESC, n.seq DESC
         LIMIT %(lim)s
        """,
        {"inv": investor_id, "fam": family, "kind": kind, "history": history,
         "k0": after[0] if after else None, "k1": after[1] if after else None,
         "lim": size + 1},
    )
    return _page(conn, rows, size, scope,
                 lambda page_rows: hydrate_notes(conn, investor_id, page_rows),
                 key=lambda r: [r["written_on"].isoformat(), r["seq"]])


def search_notes(
    conn: Any, investor_id: str, q: str, isin: str | None, kind: str | None,
    cursor: str | None, limit: int | None,
) -> dict[str, Any]:
    """Literal substring search over latest revisions of UNLOCKED companies only.

    ``%``, ``_`` and ``\\`` in ``q`` match themselves. QUESTION notes are excluded unless
    ``kind=QUESTION`` is asked for, matching ``list_notes``."""
    text = (q or "").strip()
    if not text or len(text) > 200:
        raise invalid("q is required and at most 200 characters", {"q": "RANGE"})
    if kind is not None and kind not in (*NOTE_KINDS_WRITABLE, "QUESTION"):
        raise invalid("unknown note kind", {"kind": "ENUM"})
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "note-search",
                                   {"q": text, "isin": isin, "kind": kind})
    after = canonical.decode_cursor(cursor, scope)
    visible = visible_isins(conn, investor_id)
    if isin is not None:
        allowed = [i for i in isin_family(conn, _valid_isin(isin)) if i in visible]
    else:
        allowed = sorted(visible)
    pattern = canonical.escape_like(text)
    rows = _rows(
        conn,
        f"""
        SELECT {_NOTE_COLUMNS} FROM note n
         WHERE n.investor_id = %(inv)s AND n.isin = ANY(%(allowed)s::text[])
           AND (%(kind)s::text IS NULL AND n.kind <> 'QUESTION' OR n.kind = %(kind)s::text)
           AND {_LATEST_ONLY}
           AND (n.title ILIKE %(p)s ESCAPE '\\' OR n.body ILIKE %(p)s ESCAPE '\\')
           AND (%(k0)s::date IS NULL OR (n.written_on, n.seq) < (%(k0)s::date, %(k1)s::bigint))
         ORDER BY n.written_on DESC, n.seq DESC
         LIMIT %(lim)s
        """,
        {"inv": investor_id, "allowed": allowed, "kind": kind, "p": pattern,
         "k0": after[0] if after else None, "k1": after[1] if after else None,
         "lim": size + 1},
    )
    return _page(conn, rows, size, scope,
                 lambda page_rows: hydrate_notes(conn, investor_id, page_rows, visible),
                 key=lambda r: [r["written_on"].isoformat(), r["seq"]])


def _check_source(conn: Any, investor_id: str, isin: str, source_id: str | None) -> None:
    if source_id is None:
        return
    row = _one(conn, "SELECT isin FROM research_source WHERE investor_id = %s AND id = %s",
               (investor_id, _uuid(source_id, "source")))
    if row is None or row["isin"] != isin:
        raise invalid("primary_source_id must be one of your sources for this company",
                      {"primary_source_id": SOURCE_NOT_FOUND}, code=SOURCE_NOT_FOUND)


def _validate_link_target(
    conn: Any, investor_id: str, from_note_id: str | None, link: dict[str, Any],
    visible: set[str],
) -> dict[str, Any]:
    """Exactly one target, owned and unlocked. Returns the normalised link."""
    relation = link.get("relation")
    if relation not in LINK_RELATIONS:
        raise invalid("unknown link relation", {"relation": "ENUM"})
    targets = {k: link.get(k) for k in ("to_note_id", "source_id", "thesis_id")}
    present = [k for k, v in targets.items() if v is not None]
    if len(present) != 1:
        raise invalid("a link needs exactly one target", {"links": "ONE_TARGET"})
    field = present[0]
    target = _uuid(targets[field], "link target")
    if relation == "CONTRADICTS" and field != "to_note_id":
        raise invalid("CONTRADICTS links two notes", {"links": "CONTRADICTS_NOTE"})
    if field == "to_note_id" and target == from_note_id:
        raise invalid("a note cannot link to itself", {"links": "SELF"})
    table = {"to_note_id": "note", "source_id": "research_source", "thesis_id": "thesis"}[field]
    row = _one(conn, f"SELECT isin FROM {table} WHERE investor_id = %s AND id = %s",
               (investor_id, target))
    if row is None:
        raise not_found("link target")
    if row["isin"] not in visible:
        raise ritual_required()
    return {"relation": relation, "to_note_id": None, "source_id": None, "thesis_id": None,
            field: target}


def _insert_link(
    conn: Any, investor_id: str, actor_uid: str, from_note_id: str, link: dict[str, Any],
    dkey: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO note_link (investor_id, from_note_id, to_note_id, source_id, thesis_id,
                                   relation, actor_uid, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (investor_id, from_note_id, link["to_note_id"], link["source_id"],
             link["thesis_id"], link["relation"], actor_uid, dkey),
        )
        return str(cur.fetchone()[0])


def _insert_note(
    conn: Any, investor_id: str, actor_uid: str, isin: str, body: dict[str, Any],
    previous_id: str | None, dkey: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO note (investor_id, isin, kind, title, body, written_on, actor_uid,
                              authorship, primary_source_id, previous_id, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'HUMAN', %s, %s, %s) RETURNING id
            """,
            (investor_id, isin, body["kind"], body["title"], body["body"], body["written_on"],
             actor_uid, body["primary_source_id"], previous_id, dkey),
        )
        return str(cur.fetchone()[0])


def _prepare_note(conn: Any, investor_id: str, isin: str, body: dict[str, Any]) -> None:
    if body["kind"] not in NOTE_KINDS_WRITABLE:
        raise invalid("QUESTION notes are created through the question routes",
                      {"kind": "USE_QUESTION_ROUTE"})
    if body["kind"] == "OBSERVATION" and body["primary_source_id"] is None:
        raise invalid("an OBSERVATION must cite a primary source",
                      {"primary_source_id": "SOURCE_REQUIRED"}, code="SOURCE_REQUIRED")
    _check_source(conn, investor_id, isin, body["primary_source_id"])


def create_note(
    conn: Any, investor_id: str, actor_uid: str, isin: str, body: dict[str, Any], key: str,
    payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Human note + requested links + audit + receipt in one transaction."""
    isin = require_unlocked(conn, investor_id, isin)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_NOTE_CREATE, isin, key, payload_digest)
    if receipt is not None:
        return note_by_id(conn, investor_id, receipt.result["note_id"]), 200
    _prepare_note(conn, investor_id, isin, body)
    visible = visible_isins(conn, investor_id)
    links = [_validate_link_target(conn, investor_id, None, link, visible)
             for link in body["links"]]
    note_id = _insert_note(conn, investor_id, actor_uid, isin, body, None,
                           dedupe_key(OP_NOTE_CREATE, isin, key))
    for i, link in enumerate(links):
        _insert_link(conn, investor_id, actor_uid, note_id, link,
                     dedupe_key(OP_NOTE_CREATE, isin, key, f"l{i}"))
    audit_write(conn, actor_uid, OP_NOTE_CREATE, "note", note_id, links=len(links))
    record_receipt(conn, investor_id, OP_NOTE_CREATE, isin, key, payload_digest,
                   {"note_id": note_id}, actor_uid)
    return note_by_id(conn, investor_id, note_id), 201


def revise_note(
    conn: Any, investor_id: str, actor_uid: str, note_id: str, body: dict[str, Any],
    expected_latest_id: str, key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Append a revision to the chain's actual latest row. Old rows are never touched."""
    base = note_by_id(conn, investor_id, note_id)
    lock_tenant(conn, investor_id)
    # Replay first: a retry must return its original revision even though that revision
    # has made its own expected_latest_id stale.
    receipt = check_replay(conn, investor_id, OP_NOTE_REVISE, base["id"], key, payload_digest)
    if receipt is not None:
        return note_by_id(conn, investor_id, receipt.result["note_id"]), 200
    if base["kind"] == "QUESTION" or body["kind"] == "QUESTION":
        raise conflict(QUESTION_IMMUTABLE,
                       "a question cannot be revised; record a new question instead")
    if body["kind"] != base["kind"]:
        raise conflict(KIND_MISMATCH, "a revision keeps the kind of the note it revises")
    latest = _chains(conn, investor_id, [base["id"]])[base["id"]][-1]
    if _uuid(expected_latest_id, "note") != latest:
        raise conflict(STALE_PREDECESSOR, "this note has a newer revision; reload first")
    _prepare_note(conn, investor_id, base["isin"], body)
    visible = visible_isins(conn, investor_id)
    links = [_validate_link_target(conn, investor_id, None, link, visible)
             for link in body["links"]]
    new_id = _insert_note(conn, investor_id, actor_uid, base["isin"], body, latest,
                          dedupe_key(OP_NOTE_REVISE, base["id"], key))
    for i, link in enumerate(links):
        _insert_link(conn, investor_id, actor_uid, new_id, link,
                     dedupe_key(OP_NOTE_REVISE, base["id"], key, f"l{i}"))
    audit_write(conn, actor_uid, OP_NOTE_REVISE, "note", new_id, previous=latest,
                links=len(links))
    record_receipt(conn, investor_id, OP_NOTE_REVISE, base["id"], key, payload_digest,
                   {"note_id": new_id}, actor_uid)
    return note_by_id(conn, investor_id, new_id), 201


def link_note(
    conn: Any, investor_id: str, actor_uid: str, note_id: str, link: dict[str, Any],
    key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Append one dated relationship from an exact note. Neither end is modified."""
    base = note_by_id(conn, investor_id, note_id)
    lock_tenant(conn, investor_id)
    receipt = check_replay(conn, investor_id, OP_NOTE_LINK, base["id"], key, payload_digest)
    if receipt is None:
        visible = visible_isins(conn, investor_id)
        normalised = _validate_link_target(conn, investor_id, base["id"], link, visible)
        link_id = _insert_link(conn, investor_id, actor_uid, base["id"], normalised,
                               dedupe_key(OP_NOTE_LINK, base["id"], key))
        audit_write(conn, actor_uid, OP_NOTE_LINK, "note_link", link_id)
        record_receipt(conn, investor_id, OP_NOTE_LINK, base["id"], key, payload_digest,
                       {"link_id": link_id}, actor_uid)
        status = 201
    else:
        link_id, status = receipt.result["link_id"], 200
    row = _one(
        conn,
        "SELECT id, from_note_id, to_note_id, source_id, thesis_id, relation, recorded_at, "
        "actor_uid FROM note_link WHERE investor_id = %s AND id = %s",
        (investor_id, link_id),
    )
    return {**row, "direction": "OUT"}, status


# =========================================================================================
# Home: open questions, returns, reviews
# =========================================================================================


def home(conn: Any, investor_id: str, as_of: date) -> dict[str, Any]:
    """Deterministic Home feed from owned, unlocked, immutable records only.

    Returns are exact-date calendar anniversaries (6/12/24 months, clamped; see
    ``app/research/returns.py``) of ORIGINAL writing -- a chain's first note, a thesis's
    formation, an override's occurred_on -- plus recorded contradictions with both sides.
    No GET here writes anything.
    """
    visible = visible_isins(conn, investor_id)
    allowed = sorted(visible)
    notices: list[dict[str, str]] = []

    questions = list_questions(conn, investor_id, None, "OPEN", None, None,
                               HOME_OPEN_QUESTIONS)

    due = originals_due(as_of)
    items: list[dict[str, Any]] = []
    for reason, originals in due.items():
        if not originals:
            continue
        for row in _rows(
            conn,
            """
            SELECT n.id, n.isin, n.kind, n.written_on FROM note n
             WHERE n.investor_id = %s AND n.isin = ANY(%s::text[]) AND n.previous_id IS NULL
               AND n.kind <> 'QUESTION' AND n.written_on = ANY(%s::date[])
             ORDER BY n.written_on, n.seq
            """,
            (investor_id, allowed, originals),
        ):
            items.append({"kind": "NOTE", "reason": reason, "note_id": row["id"],
                          "isin": row["isin"], "original_date": row["written_on"],
                          "content_ref": row["id"]})
        for row in _rows(
            conn,
            """
            SELECT DISTINCT ON (t.isin) t.id, t.isin, t.formed_on FROM thesis t
             WHERE t.investor_id = %s AND t.isin = ANY(%s::text[]) AND t.formed_on = ANY(%s::date[])
             ORDER BY t.isin, t.version
            """,
            (investor_id, allowed, originals),
        ):
            items.append({"kind": "THESIS", "reason": reason, "thesis_id": row["id"],
                          "isin": row["isin"], "original_date": row["formed_on"],
                          "content_ref": row["id"]})
        for row in _rows(
            conn,
            """
            SELECT e.id, t.isin, e.occurred_on FROM thesis_event e
              JOIN thesis t ON t.investor_id = e.investor_id AND t.id = e.thesis_id
             WHERE e.investor_id = %s AND t.isin = ANY(%s::text[])
               AND e.kind = 'OVERRIDE_EXIT_TRIGGER' AND e.occurred_on = ANY(%s::date[])
             ORDER BY e.occurred_on, e.seq
            """,
            (investor_id, allowed, originals),
        ):
            items.append({"kind": "OVERRIDE", "reason": reason, "event_id": row["id"],
                          "isin": row["isin"], "original_date": row["occurred_on"],
                          "content_ref": row["id"]})

    # Contradictions: both sides must be visible; both texts stay exactly as written.
    for row in _rows(
        conn,
        """
        SELECT l.id, l.from_note_id, l.to_note_id, l.recorded_at,
               a.isin AS from_isin, a.written_on AS from_written_on,
               b.isin AS to_isin, b.written_on AS to_written_on
          FROM note_link l
          JOIN note a ON a.investor_id = l.investor_id AND a.id = l.from_note_id
          JOIN note b ON b.investor_id = l.investor_id AND b.id = l.to_note_id
         WHERE l.investor_id = %s AND l.relation = 'CONTRADICTS'
           AND a.isin = ANY(%s::text[]) AND b.isin = ANY(%s::text[])
         ORDER BY l.recorded_at DESC, l.id
         LIMIT %s
        """,
        (investor_id, allowed, allowed, HOME_RETURNS + 1),
    ):
        items.append({"kind": "CONTRADICTION", "reason": "CONTRADICTION", "link_id": row["id"],
                      "note_id": row["from_note_id"], "other_note_id": row["to_note_id"],
                      "isin": row["from_isin"], "original_date": row["from_written_on"],
                      "other_date": row["to_written_on"], "content_ref": row["id"]})

    # One item per underlying record: joins must never make one piece of writing look like two.
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in items:
        marker = (item["kind"], item["content_ref"])
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    if len(unique) > HOME_RETURNS:
        notices.append({"code": "RETURNS_TRUNCATED",
                        "message": f"showing the first {HOME_RETURNS} returned items"})
        unique = unique[:HOME_RETURNS]

    # Pending task reviews (BE 21-22) are filled by the router from app/db/tasks.py on the
    # same connection; this module must not import the task repository (it imports us).
    return {
        "as_of": as_of,
        "open_questions": questions,
        "returns": unique,
        "reviews": [],
        "notices": notices,
    }
