"""Document persistence -- the pointer, the provenance, and the audit trail.

The bytes live in object storage; this module holds the row that knows where. Split that
way because the two have different failure modes and different lifecycles, and because a
database that stored megabytes of PDF would make every backup and restore worse for no
benefit.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.db import audit
from app.storage.base import DEFAULT_URL_TTL_SECONDS
from app.storage.keys import content_hash, investor_prefix, object_key


def find_by_hash(conn: Any, sha256: str, investor_id: str) -> dict[str, Any] | None:
    """This investor's document with these bytes, if they already sent them.

    Scoped to the investor deliberately. Searched globally, the second investor to send
    identical bytes -- a joint account's contract note, an identical NIL statement -- was
    handed the first one's row: their document id, their storage_key, and no record of the
    second upload at all. See migration 020.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, investor_id, storage_key, doctype, byte_size, received_at "
            "FROM document WHERE sha256 = %s AND investor_id = %s",
            (sha256, investor_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c.name for c in cur.description], row, strict=True))


def store_document(
    conn: Any,
    store: Any,
    investor_id: str,
    investor_code: str,
    doctype: str,
    content: bytes,
    received: date,
    filename: str = "",
    content_type: str = "application/pdf",
    account_id: str | None = None,
    source_message_id: str | None = None,
    actor_uid: str | None = None,
    email_meta: dict[str, Any] | None = None,
    parse_status: str | None = None,
) -> dict[str, Any]:
    """Put the bytes in storage and record the pointer. Idempotent on content.

    ``parse_status`` is None for every financial caller, which keeps the column default
    ('UNPARSED') exactly as before. Research uploads pass 'RESEARCH_ONLY' so the row is
    created in that state atomically -- never inserted UNPARSED and relabelled, which would
    leave a window in which the parse queue could pick up a research file. It is applied
    ONLY to a newly inserted row: an existing same-investor document found by hash keeps
    its own doctype and parse status untouched (session 12, BE-V04).

    Re-storing an identical document FOR THE SAME INVESTOR returns the existing row and
    does not duplicate it. That is not an optimisation: email forwarding delivers the same
    statement more than once, and a client uploading their history sends overlapping files
    by necessity. Another investor sending the same bytes gets their own row and their own
    object -- see find_by_hash and migration 020.

    Order matters. The object is written FIRST, then the row. The reverse order can leave a
    row pointing at nothing, which looks like data loss; this order can at worst leave an
    object with no row, which a prefix sweep finds. Prefer the recoverable failure.
    """
    digest = content_hash(content)

    existing = find_by_hash(conn, digest, investor_id)
    if existing is not None:
        # find_by_hash returns the raw row, so ``id`` is a psycopg UUID object, not the
        # string the callers (and StageResultOut) expect. Stringify it before returning.
        #
        # ``sha256`` and ``byte_size`` are added explicitly so BOTH return paths of this
        # function carry the same keys. They did not: find_by_hash does not select sha256,
        # so a caller that read ``result["sha256"]`` worked on a fresh document and raised
        # KeyError the first time the same bytes arrived twice -- which for an email
        # forwarding path is not an edge case but the expected second delivery.
        return {
            **existing,
            "id": str(existing["id"]),
            "sha256": digest,
            "byte_size": existing.get("byte_size", len(content)),
            "already_present": True,
        }

    key = object_key(investor_code, doctype, received, content, filename)
    store.put(key, content, content_type)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document (investor_id, account_id, doctype, storage_key, sha256,
                                  byte_size, content_type, filename, source_message_id,
                                  received_at, email_meta, parse_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s, COALESCE(%s, 'UNPARSED'))
            RETURNING id
            """,
            (
                investor_id,
                account_id,
                doctype,
                key,
                digest,
                len(content),
                content_type,
                filename or None,
                source_message_id,
                received,
                json.dumps(email_meta) if email_meta else None,
                parse_status,
            ),
        )
        document_id = str(cur.fetchone()[0])

    audit.record(
        conn,
        audit.ACTION_DOCUMENT_STORED,
        "document",
        document_id,
        actor_uid,
        {"doctype": doctype, "bytes": len(content), "storage_key": key},
    )
    return {
        "id": document_id,
        "storage_key": key,
        "sha256": digest,
        "byte_size": len(content),
        "already_present": False,
    }


def documents_for_investor(conn: Any, investor_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, doctype, storage_key, sha256, byte_size, filename, received_at, "
            "stored_at, parse_status FROM document WHERE investor_id = %s "
            "ORDER BY received_at DESC, stored_at DESC",
            (investor_id,),
        )
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]


def signed_url_for(
    conn: Any,
    store: Any,
    document_id: str,
    ttl_seconds: int = DEFAULT_URL_TTL_SECONDS,
    actor_uid: str | None = None,
) -> str:
    """Issue a short-lived URL, and audit the issuance.

    Audited HERE, at issuance, not when the URL is followed. Following it happens at the
    storage provider, which this system may never hear about -- so the moment access is
    granted is the only moment it can honestly be recorded.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT storage_key, investor_id FROM document WHERE id = %s", (document_id,))
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no such document: {document_id}")
        key, investor_id = row[0], str(row[1])

    audit.record(
        conn,
        audit.ACTION_DOCUMENT_ACCESSED,
        "document",
        document_id,
        actor_uid,
        {"investor_id": investor_id, "ttl_seconds": ttl_seconds},
    )
    return store.signed_url(key, ttl_seconds)


def delete_artifacts_for_investor(
    conn: Any,
    store: Any,
    investor_id: str,
    investor_code: str,
    actor_uid: str | None = None,
) -> int:
    """Remove an investor's stored OBJECTS. Leaves the `document` rows for the caller's cascade.

    OBJECTS FIRST, AND ONLY OBJECTS. An erasure that drops the rows and leaves the PDFs is not
    an erasure -- the file holding the PAN and the full holdings would outlive the record of it,
    and nothing left in the system would know it was there. Deleting objects first means a
    failure part-way leaves rows pointing at missing objects, which is visible and re-runnable,
    rather than orphaned files nobody can find.

    WHY THE ROW DELETE MOVED OUT. This function used to delete the `document` rows too, and that
    broke the moment research existed: `research_source.document_id` references
    `document (investor_id, id)` with no ON DELETE action, so deleting a document row while a
    research source cites it fails with a foreign-key violation -- verified against PostgreSQL,
    not theorised. `privacy.erase_investor` now lets the single `DELETE FROM investor` cascade
    remove the document rows and the research rows together; NO ACTION constraints are checked
    at end of statement, so no intermediate orphan is ever visible. The alternative fixes --
    nulling the research reference, or relabelling a document's parse state -- would rewrite an
    immutable research record to make a deletion convenient.

    STORAGE AND POSTGRES ARE SEPARATE FAILURE DOMAINS, and that asymmetry is the reason for this
    ordering rather than a preference. A database rollback cannot un-delete an object. So if the
    caller's transaction later fails, these objects are already gone and their rows are still
    present -- which is a visible, retryable state, and `workspace_privacy.record_pending_artifacts`
    is how a partial object deletion stays discoverable. The opposite order would leave files
    with no index, which nothing can find and nobody can prove are gone.

    A ``delete_prefix`` store deletes the whole tenant prefix in one call. A MISSING investor
    code would make that prefix dangerously broad, so it is refused rather than normalised.
    """
    if hasattr(store, "delete_prefix") and not (investor_code or "").strip():
        raise ValueError(
            f"investor {investor_id} has no code, so no tenant storage prefix can be built. "
            "Refusing to call delete_prefix with an empty prefix: that would delete every "
            "investor's objects."
        )

    documents = documents_for_investor(conn, investor_id)

    if hasattr(store, "delete_prefix"):
        store.delete_prefix(investor_prefix(investor_code))
    else:
        # Object by object, tolerating an object that is already gone. A retry of a half-finished
        # erasure must be able to complete rather than failing on the ones it removed last time,
        # and a missing object is the outcome this function wants anyway.
        missing: list[str] = []
        for doc in documents:
            try:
                store.delete(doc["storage_key"])
            except FileNotFoundError:
                missing.append(doc["storage_key"])
        if missing:
            audit.record(
                conn,
                audit.ACTION_DOCUMENT_DELETED,
                "investor",
                investor_id,
                actor_uid,
                {"already_absent": len(missing)},
            )

    if documents:
        audit.record(
            conn,
            audit.ACTION_DOCUMENT_DELETED,
            "investor",
            investor_id,
            actor_uid,
            {"documents_deleted": len(documents)},
        )
    return len(documents)


def delete_documents_for_investor(
    conn: Any,
    store: Any,
    investor_id: str,
    investor_code: str,
    actor_uid: str | None = None,
) -> int:
    """Deprecated: deletes objects AND `document` rows. Use ``delete_artifacts_for_investor``.

    Kept because callers outside the privacy workflow may still expect the combined behaviour,
    and removing it would be a silent API break. **It cannot be used on a database that holds
    research**: `research_source.document_id` will refuse the row delete. Erasure must go through
    ``privacy.erase_investor``, which deletes objects here and lets one cascading statement
    remove the rows.
    """
    removed = delete_artifacts_for_investor(conn, store, investor_id, investor_code, actor_uid)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM document WHERE investor_id = %s", (investor_id,))
    return removed


# --- parse_status: the archive's state machine -------------------------------------
#
# ``document.parse_status`` has existed since migration 003 and, until the job queue, was
# read once and written by nothing -- an inert column. It is what makes "store every
# forwarded document now, decide which are worth opening later" a real state rather than a
# comment: a bulk forward lands 500 UNPARSED rows in seconds, and reconciliation then
# picks which of them to open first. See docs/BACKFILL_PIPELINE.md section 6.

#: Stored, not yet opened. The state a bulk-forwarded document lands in.
PARSE_UNPARSED = "UNPARSED"
#: Opened, and its rows staged for review. NOT "in the ledger" -- promotion is separate.
PARSE_PARSED = "PARSED"
#: Opened and refused. ``parsed_at`` is still set: the attempt happened, and the reason is
#: on the job row. A document is never silently forgotten.
PARSE_FAILED = "FAILED"


def mark_parse_status(conn: Any, document_id: str, status: str) -> None:
    """Record that a document has been opened, or has failed to open.

    ``parsed_at`` is stamped for both outcomes, because it answers "when did we last try",
    which is the question asked when a document is missing from a ledger. A FAILED row
    with a timestamp is diagnosable; a row that silently stays UNPARSED forever is not.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document SET parse_status = %s, parsed_at = now() WHERE id = %s",
            (status, document_id),
        )


def unparsed_for_investor(
    conn: Any, investor_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    """Stored-but-never-opened documents, oldest email first.

    Ordered by the ORIGINAL message's date where we have one (``email_meta->>'date'``),
    falling back to ``received_at``. That ordering matters for cost basis: replaying a
    ledger in the order the trades happened keeps FIFO lots forming in the right sequence,
    whereas replaying in the order someone happened to forward their mailbox does not.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, doctype, storage_key, filename, received_at, email_meta, account_id
              FROM document
             WHERE investor_id = %s AND parse_status = %s
             ORDER BY COALESCE(email_meta->>'date', received_at::text), stored_at
             LIMIT %s
            """,
            (investor_id, PARSE_UNPARSED, limit),
        )
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]


def document_for_parse(conn: Any, document_id: str) -> dict[str, Any] | None:
    """Everything the drain needs to reopen one stored document, and nothing more.

    Deliberately returns no bytes and no PAN: the worker fetches the bytes from object
    storage by ``storage_key`` and re-derives the PAN itself, so neither ever travels
    through a job payload. See app/jobs/queue.py's module docstring.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, investor_id, account_id, doctype, storage_key, content_type,
                   filename, parse_status
              FROM document WHERE id = %s
            """,
            (document_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [c.name for c in cur.description]
        return dict(zip(columns, row, strict=True))
