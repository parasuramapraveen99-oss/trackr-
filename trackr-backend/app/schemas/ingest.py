"""DTOs for /ingest/*. Staging review/promote payloads.

The inbound-email webhook body is NOT modeled here: Mailgun and SendGrid post
multipart/form-data and Postmark posts differently-cased JSON, so there is no one
Pydantic body FastAPI could validate the same way across all three -- see
app/ingest/webhook_providers.py, which reads ``Request`` directly per provider and
produces the same internal shape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class StageResultOut(BaseModel):
    """One adapter's ``ParseResult`` after staging. Mirrors ``db.imports.stage_batch``'s
    return value -- see docs/BACKEND_REUSE_AND_GAP_ANALYSIS.md section 2."""

    #: Null when the attachment was refused before any batch existed -- today only an
    #: identity mismatch (app.db.identity), where the document named an owner who is not
    #: this account's and nothing was stored or inserted. ``notes`` carries the reason.
    #: Same reasoning as FileOutcomeOut.batch_id below, which is nullable for the
    #: equivalent "failed before a batch existed" case on the multi-file upload path.
    batch_id: str | None
    parsed: int
    rejected: int
    sha256: str
    document_id: str | None
    notes: list[str]
    #: Rows written to holding_snapshot (app.db.holdings.record_snapshot), 0 for every
    #: adapter except cas_pdf -- see app.db.imports.stage_batch. Not a ledger count and
    #: not gated on review/promote: holding_snapshot is a checking dataset, not the
    #: transaction ledger.
    declared_holdings: int = 0


class BatchSummaryOut(BaseModel):
    """One row of GET /ingest/batches -- enough to find a batch again and decide what
    to do with it; the row-level detail is review_batch's job."""

    batch_id: str
    filename: str | None
    adapter: str
    status: str
    row_count: int
    rejected_count: int
    uploaded_at: datetime


class RejectionOut(BaseModel):
    row_number: int
    reason: str


class NeedsReviewOut(BaseModel):
    row_number: int
    confidence: str


class ReviewBatchOut(BaseModel):
    """What a human reads before deciding to promote. Mirrors
    ``db.imports.review_batch``'s return value."""

    status: str
    adapter: str
    filename: str | None
    row_count: int
    rejected_count: int
    notes: list[str] | None
    uploaded_at: datetime
    staged: int
    will_insert: int
    will_skip_as_duplicate: int
    rejections: list[RejectionOut]
    needs_review: list[NeedsReviewOut]


class PromoteResultOut(BaseModel):
    """Mirrors ``engine.types.ImportSummary``."""

    inserted: int
    skipped_duplicates: int
    rejected: list[dict]


class DiscardRequest(BaseModel):
    reason: str


class PromoteRequest(BaseModel):
    """Optional body for POST /ingest/stage/{batch_id}/promote.

    Absent (or false) is the ordinary case and matches every caller that existed before
    this field: rows a parser vouched for promote with no ceremony. Setting it true says
    "I have looked at the rows no parser could verify, and I accept them" -- which is
    recorded as a count in the promotion's audit entry.
    """

    acknowledge_uncertain: bool = False


class FileOutcomeOut(BaseModel):
    """What happened to ONE file in a mixed multi-file upload (POST /ingest/files).

    Deliberately not ``StageResultOut``: a file can now fail before staging exists at all
    (wrong extension, wrong contents, locked PDF), so ``batch_id`` is optional and the
    interesting fields are the two sentences a person reads -- ``reason`` (what is wrong)
    and ``hint`` (what to do). A rejected file leaves no batch, no document and no rows.
    """

    filename: str
    ok: bool
    #: What the bytes actually are (``pdf``/``xlsx``/``text``/...), from magic numbers --
    #: not from the extension, which is what a renamed file lies about.
    container: str
    #: What the document is (``tradebook_csv``, ``contract_note_pdf``, ``cas_pdf``, ...).
    document: str
    adapter: str | None = None
    batch_id: str | None = None
    parsed: int = 0
    rejected: int = 0
    reason: str | None = None
    hint: str | None = None
    #: True only when the file is a PDF we could not unlock -- the fix is its password,
    #: not a different file, and the UI asks for it.
    encrypted: bool = False
    #: True when a password was typed for this upload and it did not open this file.
    #: Nothing was saved; the UI says the password is wrong and asks again.
    wrong_password: bool = False
    notes: list[str] = []


class FilesUploadOut(BaseModel):
    """POST /ingest/files. Per-file outcomes plus the counts a banner needs."""

    files: list[FileOutcomeOut]
    accepted: int
    rejected: int
    #: Batches that staged rows and are ready to promote. The caller promotes them with
    #: the existing POST /ingest/stage/{id}/promote -- this route never writes to the
    #: ledger itself (docs/INGESTION.md, "Staging: why nothing auto-commits").
    staged_batch_ids: list[str]
