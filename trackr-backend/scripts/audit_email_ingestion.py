"""Offline observations for docs/audits/EMAIL_INGESTION_AUDIT_2026_10_16.md.

Run from trackr-backend: .venv/bin/python -m scripts.audit_email_ingestion
No mailbox, network, real database, settings file, or financial documents are used.
This reports current behavior; it is not a production readiness test.
"""

from __future__ import annotations

import asyncio
import json
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import psycopg
from fastapi import Depends, FastAPI
from starlette.requests import Request

from app.database import get_conn
from app.ingest.webhook_providers import (
    _from_raw_mime,
    _translate_postmark,
    _translate_sendgrid,
)

RECIPIENT = "c001-zerodha-contractnote@ingest.example.com"


def request(body: bytes, content_type: str) -> Request:
    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", content_type.encode())],
        },
        receive,
    )


async def observe_recipients() -> None:
    data = urlencode(
        {
            "to": "Investor <investor@gmail.com>",
            "envelope": json.dumps({"to": [RECIPIENT], "from": "broker@example.com"}),
        }
    ).encode()
    message = await _translate_sendgrid(request(data, "application/x-www-form-urlencoded"))
    print(f"SendGrid: expected envelope {RECIPIENT}; actual {message.to}")

    data = json.dumps(
        {
            "To": "Investor <investor@gmail.com>",
            "OriginalRecipient": RECIPIENT,
            "From": "broker@example.com",
            "Attachments": [],
        }
    ).encode()
    message = await _translate_postmark(request(data, "application/json"))
    print(f"Postmark: expected envelope {RECIPIENT}; actual {message.to}")


def observe_mime() -> None:
    root = EmailMessage()
    root.set_content("Documents")
    root.add_attachment(
        b"%PDF-shallow", maintype="application", subtype="pdf", filename="shallow.pdf"
    )
    nested = EmailMessage()
    nested.set_content("Documents")
    nested.add_attachment(b"%PDF-deep", maintype="application", subtype="pdf", filename="deep.pdf")
    for _ in range(5):
        wrapper = EmailMessage()
        wrapper.make_mixed()
        wrapper.attach(nested)
        nested = wrapper
    root.attach(nested)
    message = _from_raw_mime(root.as_bytes(), to=RECIPIENT)
    print(
        "MIME: expected shallow.pdf and deep.pdf accounted for; actual",
        [a["filename"] for a in message.attachments],
    )


async def observe_commit_order() -> None:
    events: list[str] = []

    class SimulatedConnection:
        def commit(self) -> None:
            events.append("commit failed")
            raise RuntimeError("simulated commit failure")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    # Exercise the real get_conn/connect lifecycle, wrapping its generator in an async
    # dependency to avoid threadpool requirements in a restricted audit environment.
    async def lifecycle():
        generator = get_conn()
        try:
            yield next(generator)
        finally:
            next(generator, None)

    app = FastAPI()

    @app.post("/probe")
    async def probe(conn=Depends(lifecycle)):
        return {"accepted": True}

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            events.append(f"HTTP {message['status']}")
        if message["type"] == "http.response.body":
            events.append("response sent")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/probe",
        "raw_path": b"/probe",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "http_version": "1.1",
    }
    with (
        patch("app.database.get_settings", return_value=SimpleNamespace(database_url="unused")),
        patch.object(psycopg, "connect", return_value=SimulatedConnection()),
    ):
        try:
            await app(scope, receive, send)
        except RuntimeError as exc:
            if str(exc) != "simulated commit failure":
                raise
    print("get_conn lifecycle (simulated DB):", " -> ".join(events))


async def main() -> None:
    await observe_recipients()
    observe_mime()
    await observe_commit_order()


if __name__ == "__main__":
    asyncio.run(main())
