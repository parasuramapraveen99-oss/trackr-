"""Deferred Mailgun extraction, validated promotion, and portfolio continuation.

The worker uses the same document archive, adapters, staging and promotion as uploads.
No handler commits financial work: the drain commits outcome + ledger + events together.
Portfolio continuation is the existing bounded cycle, which owns its own checkpoints.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.db import audit, consent, crypto, documents, imports
from app.db import email_ingestion as db
from app.db.identity import IdentityMismatchError
from app.ingest.cas_pdf import CasStatementError
from app.ingest.email_documents import BlockedDocument, select
from app.ingest.webhook_providers import _from_raw_mime, unwrap_attachments
from app.jobs import queue


def _lock_consent(conn: Any, investor_id: str) -> None:
    # A withdrawal racing this transaction waits, or is observed before any financial
    # write. Do not process an old queued grant after it has been withdrawn.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM client_consent WHERE investor_id = %s AND withdrawn_at IS NULL "
            "AND purpose = %s FOR SHARE",
            (investor_id, consent.PURPOSE_PORTFOLIO_TRACKING),
        )
        cur.fetchall()
    consent.require_consent(conn, investor_id)


def extract_delivery(conn: Any, store: Any, payload: dict) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.*, i.code FROM email_delivery d JOIN investor i ON i.id = d.investor_id "
            "WHERE d.id = %s FOR UPDATE OF d",
            (payload["delivery_id"],),
        )
        delivery = db.row_dict(cur, cur.fetchone())
    if not delivery or delivery["status"] == "ARCHIVED":
        return {}
    investor_id = str(delivery["investor_id"])
    _lock_consent(conn, investor_id)
    attempt = delivery["extraction_attempts"] + 1
    if delivery["payload_enc"] is not None:
        plaintext = crypto.decrypt(delivery["payload_enc"])
    else:
        source = documents.document_for_parse(conn, str(delivery["source_document_id"]))
        plaintext = store.get(source["storage_key"]).decode()
    snapshot = json.loads(plaintext)
    if snapshot.get("raw_mime"):
        message = _from_raw_mime(snapshot["raw_mime"].encode(), to=delivery["recipient"])
        attachments = message.attachments
    else:
        attachments = unwrap_attachments(snapshot.get("attachments", []))
    source = documents.store_document(
        conn,
        store,
        investor_id=investor_id,
        investor_code=delivery["code"],
        doctype="EMAIL_SOURCE",
        content=plaintext.encode(),
        received=datetime.now(UTC).date(),
        filename=f"{delivery['id']}.json",
        content_type="application/json",
        parse_status="EMAIL_SOURCE",
    )
    limit = get_settings().ingest_max_attachments_per_delivery
    # Preserve a finite manifest failure as an attachment outcome, with the entire
    # original still archived. Valid siblings before the limit continue independently.
    if len(attachments) > limit:
        attachments = attachments[:limit] + [{"extraction_error": "Attachment limit exceeded"}]
    if not attachments:
        attachments = [{"extraction_error": "Delivery contains no extractable attachments"}]
    if not snapshot.get("raw_mime"):
        try:
            declared = int(snapshot.get("declared_attachments", 0))
        except (TypeError, ValueError):
            declared = -1
        if declared != len(snapshot.get("attachments", [])):
            attachments.append({"extraction_error": "Provider attachment count is inconsistent"})
    for ordinal, attachment in enumerate(attachments):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO email_attachment "
                "(delivery_id, investor_id, ordinal, extraction_attempt) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (delivery_id, extraction_attempt, ordinal) "
                "DO UPDATE SET ordinal = EXCLUDED.ordinal RETURNING id",
                (delivery["id"], investor_id, ordinal, attempt),
            )
            attachment_id = str(cur.fetchone()[0])
        if attachment.get("extraction_error"):
            db.outcome(conn, attachment_id, "NEEDS_ATTENTION", attachment["extraction_error"])
            continue
        try:
            content = base64.b64decode(attachment.get("content_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            db.outcome(conn, attachment_id, "NEEDS_ATTENTION", "Attachment encoding is invalid")
            continue
        meta = attachment.get("email_meta") or {
            "subject": snapshot.get("subject", ""),
            "from": snapshot.get("sender", ""),
            "date": snapshot.get("date", ""),
            "message_id": snapshot.get("message_id", ""),
        }
        stored = documents.store_document(
            conn,
            store,
            investor_id=investor_id,
            investor_code=delivery["code"],
            doctype="EMAIL_ATTACHMENT",
            content=content,
            received=datetime.now(UTC).date(),
            filename=attachment.get("filename", ""),
            account_id=None,
            content_type=attachment.get("content_type") or "application/octet-stream",
            source_message_id=meta.get("message_id"),
            email_meta=meta,
        )
        document_id = str(stored["id"])
        document = documents.document_for_parse(conn, document_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE email_attachment SET document_id = %s WHERE id = %s",
                (document_id, attachment_id),
            )
        if document["parse_status"] in ("RESEARCH_ONLY", "EMAIL_SOURCE"):
            db.outcome(
                conn, attachment_id, "UNSUPPORTED", "Document is reserved for another purpose"
            )
            continue
        queue.enqueue(
            conn,
            db.KIND_DOCUMENT,
            {"attachment_id": attachment_id},
            investor_id=investor_id,
            max_attempts=8,
            work_key=f"email-attachment:{attachment_id}",
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE email_delivery SET status = 'ARCHIVED', source_document_id = %s, "
            "payload_enc = NULL, archived_at = now(), last_error = NULL, "
            "extraction_attempts = %s WHERE id = %s",
            (source["id"], attempt, delivery["id"]),
        )
    return {"attachments": len(attachments)}


def parse_document(conn: Any, store: Any, payload: dict) -> dict:
    attachment_id = payload["attachment_id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM email_attachment WHERE id = %s", (attachment_id,))
        attachment = db.row_dict(cur, cur.fetchone())
    if not attachment or attachment["status"] in ("IMPORTED", "DUPLICATE", "IGNORED"):
        return {}
    investor_id = str(attachment["investor_id"])
    # Same lock order as mandate updates: investor, then attachment/account/mandate.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM investor WHERE id = %s FOR SHARE", (investor_id,))
        cur.fetchone()
        cur.execute("SELECT * FROM email_attachment WHERE id = %s FOR UPDATE", (attachment_id,))
        attachment = db.row_dict(cur, cur.fetchone())
    if not attachment or attachment["status"] in ("IMPORTED", "DUPLICATE", "IGNORED"):
        return {}
    try:
        with conn.transaction():
            _lock_consent(conn, investor_id)
            document = documents.document_for_parse(conn, str(attachment["document_id"]))
            if not document or str(document["investor_id"]) != investor_id:
                raise BlockedDocument("NEEDS_ATTENTION", "Original document is unavailable")
            if document["parse_status"] in ("RESEARCH_ONLY", "EMAIL_SOURCE"):
                raise BlockedDocument("UNSUPPORTED", "Document is reserved for another purpose")
            content = store.get(document["storage_key"])
            selected = select(conn, investor_id, content, document["filename"] or "")
            imports.lock_promotion_account(conn, selected.account_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE document SET doctype = %s, account_id = %s, content_type = %s "
                    "WHERE id = %s AND investor_id = %s AND doctype = 'EMAIL_ATTACHMENT'",
                    (
                        selected.doctype,
                        selected.account_id,
                        selected.content_type,
                        document["id"],
                        investor_id,
                    ),
                )
                cur.execute(
                    "SELECT id FROM account WHERE id = %s AND investor_id = %s FOR SHARE",
                    (selected.account_id, investor_id),
                )
                if not cur.fetchone():
                    raise BlockedDocument("NEEDS_ATTENTION", "Destination account no longer exists")
                cur.execute("SELECT code FROM investor WHERE id = %s", (investor_id,))
                investor_code = cur.fetchone()[0]
                cur.execute(
                    "SELECT id, status FROM import_batch WHERE account_id = %s "
                    "AND sha256 = %s AND status <> 'DISCARDED' FOR UPDATE",
                    (
                        selected.account_id,
                        document["sha256"]
                        if "sha256" in document
                        else imports.content_hash(content),
                    ),
                )
                existing = cur.fetchone()
            if existing and existing[1] == "PROMOTED":
                db.outcome(
                    conn,
                    attachment_id,
                    "DUPLICATE",
                    "This document is already imported",
                    str(existing[0]),
                )
                return {}
            if existing:
                result = imports.reparse_batch(conn, store, str(existing[0]), selected.adapter)
            else:
                result = imports.stage_batch(
                    conn,
                    selected.adapter,
                    content,
                    investor_id,
                    selected.account_id,
                    filename=document["filename"] or "",
                    store=store,
                    investor_code=investor_code,
                    doctype=selected.doctype,
                    content_type=selected.content_type,
                    delivery_id=str(attachment["delivery_id"]),
                )
            batch_id = str(result["batch_id"])
            if result["rejected"]:
                detail = f"{result['rejected']} row(s) need attention; no automatic trade import"
                state = "NEEDS_ATTENTION"
                if result.get("fno_awaiting_master"):
                    db.request_refresh(conn, investor_id)
                    if result["rejected"] == result["fno_awaiting_master"]:
                        state = "WAITING_REFERENCE"
                        detail = "Waiting for the exchange contract master; retry is automatic"
            elif selected.doctype == "CAS" and result.get("declared_holdings", 0):
                db.request_refresh(conn, investor_id)
                state, detail = "IMPORTED", "Validated CAS holdings saved for reconciliation"
            elif not result["parsed"]:
                state, detail = "NEEDS_ATTENTION", "No validated trades were found in this document"
            else:
                active = db.mandate(conn, investor_id, lock=True)
                if (
                    not get_settings().email_auto_promote_enabled
                    or not active
                    or active["policy_version"] != db.POLICY_VERSION
                ):
                    state, detail = (
                        "NEEDS_ATTENTION",
                        "Parsed and staged; automatic import is disabled",
                    )
                elif not selected.verified or selected.validation_problem:
                    state, detail = (
                        "NEEDS_ATTENTION",
                        (selected.validation_problem or "Document owner is not fully verified"),
                    )
                elif overlap_problem := db.overlap_problem(conn, batch_id, selected.account_id):
                    state, detail = "NEEDS_ATTENTION", overlap_problem
                else:
                    # Reuse all existing promotion gates, precedence, security creation
                    # and ledger dedupe. The service actor is never a forged human id.
                    summary = imports.promote_batch(conn, batch_id, db.SERVICE_ACTOR)
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE import_batch SET promotion_origin = 'EMAIL_AUTOMATION', "
                            "email_mandate_id = %s WHERE id = %s",
                            (active["id"], batch_id),
                        )
                    audit.record(
                        conn,
                        "EMAIL_AUTO_PROMOTED",
                        "import_batch",
                        batch_id,
                        db.SERVICE_ACTOR,
                        {"mandate_id": str(active["id"]), "policy_version": db.POLICY_VERSION},
                    )
                    db.request_refresh(conn, investor_id)
                    state, detail = (
                        "IMPORTED",
                        f"{summary.inserted} trade(s) imported automatically",
                    )
            documents.mark_parse_status(
                conn, str(document["id"]), "PARSED" if state == "IMPORTED" else state
            )
            db.outcome(conn, attachment_id, state, detail, batch_id)
            return result
    except BlockedDocument as exc:
        db.outcome(conn, attachment_id, exc.status, str(exc))
    except (IdentityMismatchError, CasStatementError, imports.UnreviewedRowsError) as exc:
        db.outcome(conn, attachment_id, "NEEDS_ATTENTION", str(exc))
    except consent.ConsentMissingError:
        db.outcome(
            conn, attachment_id, "NEEDS_ATTENTION", "Portfolio processing consent is inactive"
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE document d SET parse_status = a.status, parsed_at = now() "
            "FROM email_attachment a WHERE a.id = %s AND d.id = a.document_id "
            "AND d.investor_id = a.investor_id "
            "AND d.parse_status NOT IN ('PARSED', 'RESEARCH_ONLY', 'EMAIL_SOURCE')",
            (attachment_id,),
        )
    return {}


def refresh_portfolio(conn: Any, store: Any, payload: dict) -> dict:
    from app.post_import import run_cycle

    investor_id = payload["investor_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT requested_revision FROM email_refresh_state WHERE investor_id = %s",
            (investor_id,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    revision = row[0]
    result = run_cycle(conn, get_settings(), budget_seconds=150, include_sectors=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id FROM email_attachment a JOIN import_batch b ON b.id = a.batch_id "
            "WHERE a.investor_id = %s AND a.status = 'WAITING_REFERENCE' "
            "AND b.status = 'STAGED' AND b.rejected_count = 0 FOR UPDATE OF a",
            (investor_id,),
        )
        ready = [str(r[0]) for r in cur.fetchall()]
        for attachment_id in ready:
            queue.enqueue(
                conn,
                db.KIND_DOCUMENT,
                {"attachment_id": attachment_id},
                investor_id=investor_id,
                max_attempts=8,
                work_key=f"email-attachment:{attachment_id}",
            )
            cur.execute(
                "UPDATE email_attachment SET status = 'PENDING', updated_at = now() WHERE id = %s",
                (attachment_id,),
            )
        # A new import before this lock makes another bounded cycle necessary. An
        # import after it waits until the drain has completed/rescheduled this job.
        cur.execute(
            "SELECT requested_revision FROM email_refresh_state WHERE investor_id = %s FOR UPDATE",
            (investor_id,),
        )
        latest = cur.fetchone()[0]
        if result.get("busy") or result.get("more_work") or latest != revision:
            return {"reschedule_seconds": 60 if result.get("progress") else 300}
        cur.execute(
            "UPDATE email_refresh_state SET completed_revision = %s WHERE investor_id = %s",
            (revision, investor_id),
        )
    return {"refreshed": True}


def record_failure(conn: Any, kind: str, payload: dict, detail: str) -> None:
    if kind == db.KIND_DOCUMENT:
        db.outcome(conn, payload["attachment_id"], "FAILED", detail)
    elif kind == db.KIND_DELIVERY:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE email_delivery SET status = 'FAILED', last_error = %s WHERE id = %s",
                (detail[:1000], payload["delivery_id"]),
            )


def reconcile_failures(conn: Any) -> None:
    """Surface jobs abandoned on their final lease in the investor's activity feed."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE email_delivery d SET status = 'FAILED', last_error = j.last_error "
            "FROM job j WHERE j.kind = 'email_delivery' AND j.status = 'FAILED' "
            "AND j.payload->>'delivery_id' = d.id::text AND d.status = 'PENDING' "
            "AND NOT EXISTS (SELECT 1 FROM job active WHERE active.kind = j.kind "
            "AND active.payload = j.payload AND active.status IN ('PENDING', 'RUNNING'))"
        )
        cur.execute(
            "SELECT a.id FROM email_attachment a JOIN job j "
            "ON j.payload->>'attachment_id' = a.id::text "
            "WHERE j.kind = 'email_document' AND j.status = 'FAILED' AND a.status = 'PENDING' "
            "AND NOT EXISTS (SELECT 1 FROM job active WHERE active.kind = j.kind "
            "AND active.payload = j.payload AND active.status IN ('PENDING', 'RUNNING'))"
        )
        ids = [str(r[0]) for r in cur.fetchall()]
    for attachment_id in ids:
        db.outcome(
            conn, attachment_id, "FAILED", "Worker stopped before completing this attachment"
        )
