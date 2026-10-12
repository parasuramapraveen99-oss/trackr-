"""The adapter that runs the extraction pipeline.

Composes the five modules that do the actual work, and owns none of the decisions:

    bytes -> extract_text -> redaction.redact_for_llm -> llm_extract.structure
          -> llm_verify.verify -> ParseResult

Every step can refuse, and a refusal here is always a ``ParseResult`` with rejections
rather than an exception, because that is the contract ``stage_batch`` consumes and it is
how a person ends up reading a reason instead of a 500.

Where the boundaries sit
------------------------
Only ``redaction`` decides what may be transmitted. Only ``llm_verify`` decides what is
true. This module decides neither -- it wires them in the one order that is safe and
converts failures into the shape the staging layer expects.

``identity()`` deliberately reads the PAN and UCC from the **raw** document text, locally,
before redaction. That is the tenant check (docs/INGESTION.md, "Tenant isolation"), it
never leaves the process, and the values it reads are precisely the ones redaction strips
before anything is sent. Both facts are true at once and neither weakens the other.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.ai.llm import LlmResponseError, LlmUnavailable
from app.ingest import llm_extract, llm_verify, redaction
from app.ingest.base import DocumentIdentity, ParseResult, Rejection
from app.ingest.zerodha_contract_note import (
    PdfTextExtractionError,
    extract_text,
    parse_identity,
)


def _text_of(content: bytes, password: str) -> str:
    """Bytes -> text, however the bytes arrived. Same shortcut every adapter here takes:
    anything not starting with %PDF- is decoded directly, which is what lets tests feed
    text without building a PDF."""
    if content[:5] != b"%PDF-":
        return content.decode("utf-8")
    return extract_text(content, password)


class LlmDocumentAdapter:
    """A broker document no deterministic parser handles.

    Produces rows at CONFIDENCE_UNCERTAIN or CONFIDENCE_DERIVED depending on how much
    ``llm_verify`` could prove, and never above -- a transcription is never worth more
    than a read. ``app.db.imports.promote_batch`` refuses the uncertain ones without an
    explicit acknowledgement, so nothing here can reach the ledger unattended.

    ``client`` is injected rather than built from Settings: this module stays free of
    configuration, and every test runs offline against a fake. A None client means the
    feature is off, and the adapter says so instead of pretending to parse.
    """

    name = "llm_document"

    def __init__(self, password: str = "", client: Any = None, today: date | None = None):
        self.password = password
        self._client = client
        self._today = today

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        if self._client is None:
            return ParseResult(
                rejections=[
                    Rejection(0, "LLM-assisted parsing is not enabled on this deployment")
                ]
            )

        try:
            text = _text_of(content, self.password)
        except PdfTextExtractionError as exc:
            return ParseResult(rejections=[Rejection(0, str(exc))])
        except UnicodeDecodeError:
            return ParseResult(rejections=[Rejection(0, "not a readable document")])

        try:
            redacted = redaction.redact_for_llm(text)
        except redaction.NothingToSendError as exc:
            # The egress gate. Nothing was transmitted, and the reason names the document
            # rather than the model -- from the uploader's side this is "we could not find
            # a trade table in this", which is the truth.
            return ParseResult(rejections=[Rejection(0, f"nothing to extract: {exc}")])
        except redaction.RedactionError as exc:
            # Fail-closed. A redaction bug costs an import, never a disclosure.
            return ParseResult(rejections=[Rejection(0, str(exc))])

        try:
            payload = llm_extract.structure(self._client, redacted.text)
        except LlmUnavailable as exc:
            # Transport. Degrades to exactly the behaviour this document had before the
            # feature existed: no rows, and a reason a person can retry against.
            return ParseResult(rejections=[Rejection(0, f"extraction unavailable: {exc}")])
        except LlmResponseError as exc:
            return ParseResult(rejections=[Rejection(0, f"unusable extraction: {exc}")])

        verified = llm_verify.verify(
            payload, redacted.text, account_id=account_id, today=self._today
        )

        notes = list(verified.notes)
        notes.append(
            # Counts, never content. The redacted payload is still document content and is
            # deliberately not recorded anywhere -- this line is what a reviewer reads to
            # know that transmission happened and roughly what shape it had.
            f"sent {redacted.kept_lines} line(s) of redacted text to the extraction "
            f"service, withholding {redacted.dropped_lines}; masked "
            f"{sum(redacted.masked.values())} identifier(s)"
        )
        return ParseResult(rows=verified.rows, rejections=verified.rejections, notes=notes)

    def identity(self, content: bytes) -> DocumentIdentity:
        """The client this document names -- see ``app.ingest.base.IdentifyingAdapter``.

        Read from the RAW text, locally, before redaction and independently of it. The
        tenant check has to happen whether or not the document is ever transmitted, and
        the identifiers it needs are exactly the ones redaction removes.
        """
        try:
            return parse_identity(_text_of(content, self.password))
        except (PdfTextExtractionError, UnicodeDecodeError):
            return DocumentIdentity()
