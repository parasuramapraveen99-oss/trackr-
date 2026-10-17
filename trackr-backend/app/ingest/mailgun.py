"""Mailgun's fast admission path: bounded I/O, authentication, encrypted DB spool.

No MIME recursion, PDF opening, object-storage calls, or ledger writes occur here.
"""

from __future__ import annotations

import base64
import email.utils
import json
import re
import uuid
from typing import Any

from fastapi import HTTPException, Request

from app.db import email_ingestion as db
from app.db import forwarding_confirmation
from app.ingest import webhook
from app.ingest.webhook_providers import _verify_mailgun


async def accept(request: Request, conn: Any, settings: Any) -> list[dict]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.ingest_max_attachment_bytes:
            raise HTTPException(413, "Inbound delivery exceeds the configured byte limit")

    async def receive() -> dict:
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    bounded = Request(request.scope, receive)
    form = await bounded.form(max_part_size=settings.ingest_max_attachment_bytes)
    await _verify_mailgun(bounded, settings)
    recipient = email.utils.parseaddr(str(form.get("recipient", "")))[1].lower()
    local, sep, domain = recipient.partition("@")
    if not sep or domain != settings.ingest_domain.lower().rstrip("."):
        raise webhook.UnroutableAddressError("Invalid forwarding recipient")
    token = re.fullmatch(r"mail-([0-9a-f]{32})", local)
    broker = doctype = "documents"
    if token:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT investor_id FROM email_inbox WHERE token = %s", (str(uuid.UUID(token[1])),)
            )
            row = cur.fetchone()
        if not row:
            raise webhook.NoSuchInvestorError("Unknown forwarding inbox")
        investor_id = str(row[0])
    else:
        routed = webhook.parse_to_address(recipient)
        investor_id, _ = webhook.resolve_investor(conn, routed.investor_code)
        broker, doctype = routed.broker, routed.doctype

    sender = email.utils.parseaddr(str(form.get("from") or form.get("sender", "")))[1].lower()
    if sender == forwarding_confirmation.GMAIL_FORWARDING_SENDER:
        code = forwarding_confirmation.extract_code(str(form.get("body-plain", "")))
        # Raw-MIME mode has no body-plain: only the small setup message is opened here.
        if code is None and form.get("body-mime"):
            from app.ingest.webhook_providers import _from_raw_mime

            message = _from_raw_mime(str(form["body-mime"]).encode(), to=recipient)
            code = forwarding_confirmation.extract_code(message.body_text)
        if code:
            forwarding_confirmation.record_confirmation_code(
                conn, investor_id, broker, doctype, code
            )
        conn.commit()
        return []

    if not token:
        # Legacy addresses remain registered entry points. This is only an intake
        # scope check; the worker still detects each attachment's actual type/account.
        webhook.resolve_account(conn, investor_id, broker, doctype)
        if (broker, doctype) not in webhook._ADAPTER_FACTORIES:
            raise webhook.UnsupportedDocumentError("Unknown legacy forwarding address")

    attachments = []
    for name, upload in form.multi_items():
        if name.startswith("attachment-") and hasattr(upload, "read"):
            attachments.append(
                {
                    "filename": upload.filename or "",
                    "content_type": upload.content_type or "application/octet-stream",
                    "content_b64": base64.b64encode(await upload.read()).decode("ascii"),
                }
            )
    headers = {}
    try:
        headers = {
            str(k).lower(): str(v) for k, v in json.loads(str(form.get("message-headers", "[]")))
        }
    except (ValueError, TypeError):
        pass
    snapshot = {
        "recipient": recipient,
        "sender": sender,
        "subject": str(form.get("subject", "")),
        "message_id": headers.get("message-id", str(form.get("Message-Id", ""))),
        "date": headers.get("date", ""),
        "body_text": str(form.get("body-plain", "")),
        "raw_mime": str(form.get("body-mime", "")),
        "attachments": attachments,
        "declared_attachments": str(form.get("attachment-count", len(attachments))),
    }
    await form.close()
    try:
        result = db.receive(
            conn, investor_id, recipient, snapshot, settings.ingest_max_spooled_bytes_per_investor
        )
    except db.SpoolCapacityError as exc:
        raise HTTPException(503, str(exc)) from None
    # This is deliberately BEFORE the HTTP response. Dependency teardown is too late.
    conn.commit()
    return [
        {
            "batch_id": None,
            "parsed": 0,
            "rejected": 0,
            "document_id": None,
            "sha256": result["sha256"],
            "notes": [f"delivery {result['delivery_id']} durably accepted; processing queued"],
        }
    ]
