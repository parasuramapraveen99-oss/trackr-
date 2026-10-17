"""POST /ingest/csv, POST /ingest/email, GET /ingest/stage/{batch_id},
POST /ingest/stage/{batch_id}/promote, POST /ingest/stage/{batch_id}/discard.

Everything here is a thin HTTP layer over ``app/db/imports.py``'s already-tested staging
state machine (``stage_batch`` / ``review_batch`` / ``promote_batch`` / ``discard_batch``)
and, for Mailgun, the durable admission path in ``app/ingest/mailgun.py``. No adapter
writes to the ledger. Promotion is a separate attributed operation, through human
review or the investor-authorized policy in ``app/jobs/email.py``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from app.auth.dependencies import (
    assert_owned_investor,
    get_current_user,
    require_owned_batch,
)
from app.config import Settings, get_settings
from app.database import get_conn
from app.db import consent, crypto, document_passwords, imports, repository
from app.db.identity import IdentityMismatchError
from app.db.imports import DuplicateUploadError
from app.ingest import file_validation, webhook, webhook_providers
from app.ingest.cas_pdf import CasPdfAdapter, CasStatementError
from app.ingest.file_validation import FileVerdict
from app.ingest.llm_adapter import LlmDocumentAdapter
from app.ingest.webhook_providers import WebhookNotConfiguredError
from app.ingest.zerodha_contract_note import ZerodhaContractNoteAdapter
from app.ingest.zerodha_contract_note_xlsx import ZerodhaContractNoteXlsxAdapter
from app.ingest.zerodha_funds_statement import ZerodhaFundsStatementAdapter
from app.ingest.zerodha_tradebook import ZerodhaTradebookAdapter
from app.schemas.ingest import (
    BatchSummaryOut,
    DiscardRequest,
    FilesUploadOut,
    PromoteRequest,
    PromoteResultOut,
    ReviewBatchOut,
    StageResultOut,
)
from app.storage.factory import get_store
from app.storage.keys import content_hash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])

#: adapter query param -> factory. Only "zerodha_tradebook" has a real parser; the
#: openapi.yaml contract also lists "generic_csv", which does not exist yet -- CLAUDE.md:
#: never invent a parser to fill a contract slot.
_CSV_ADAPTERS = {"zerodha_tradebook": ZerodhaTradebookAdapter}

#: adapter query param -> factory for the XLSX upload path. Contract notes exported
#: from Console as a spreadsheet -- see app.ingest.zerodha_contract_note_xlsx's module
#: docstring for the two real shapes this parses and why "custom date range" is
#: something Console does when the file is downloaded, not a control in this app.
_XLSX_ADAPTERS = {
    "zerodha_contract_note_xlsx": ZerodhaContractNoteXlsxAdapter,
    # The Console P&L export's "Other Debits and Credits" tab -- the only
    # routinely available document carrying EVIDENCE OF PAYMENT for a dividend
    # rather than a declaration. Both NSE and yfinance stop at "declared per
    # share"; this is what lets a dividend reach the ledger at all. See that
    # module's docstring and docs/INGESTION.md Adapter 6c.
    "zerodha_funds_statement": ZerodhaFundsStatementAdapter,
}


#: adapter name -> factory for the PDF path. Unlike the two above this one takes the
#: password that opens the file, so it is constructed through ``_build_adapter`` rather
#: than called directly -- see ``_unlock`` for where that password comes from.
_PDF_ADAPTERS = {"zerodha_contract_note": ZerodhaContractNoteAdapter}


def _llm_client(settings: Settings | None) -> Any:
    """A OpenRouter client, or None when the feature is off.

    None is the ordinary state. Two separate facts have to be true -- a key is configured
    AND transmission is permitted -- and app/config.py keeps them as separate fields on
    purpose: a signed Data Processing Agreement is not the same fact as a credential.
    """
    if settings is None or not settings.llm_parsing_enabled or not settings.openrouter_api_key:
        return None
    from app.ai.llm import LlmClient

    return LlmClient(settings.openrouter_api_key, settings.openrouter_model)


def _build_adapter(
    name: str,
    pdf_password: str = "",
    settings: Settings | None = None,
    broker_accounts: dict[str, str] | None = None,
) -> Any:
    """One adapter instance by name, across every registry (CSV/XLSX/PDF) -- the same
    construction every upload endpoint already does, factored out so reparse_stage can
    build the exact adapter a fresh upload would.

    ``settings`` is optional so every existing caller and test keeps working; only the LLM
    adapter needs it, and without it that adapter is built clientless and says so rather
    than pretending to parse. ``broker_accounts`` is what a CAS is matched against: a
    statement whose demat account matches none of them is refused, never attributed to
    whichever account happened to be selected.
    """
    if name in _CSV_ADAPTERS:
        return _CSV_ADAPTERS[name]()
    if name in _XLSX_ADAPTERS:
        return _XLSX_ADAPTERS[name]()
    if name in _PDF_ADAPTERS:
        return _PDF_ADAPTERS[name](password=pdf_password)
    if name == CasPdfAdapter.name:
        # HOLDINGS AND DISCOVERY ONLY. A CAS carries no price -- a depository records
        # securities movement, not trades -- so it can never produce a Transaction, and
        # cas_pdf rejects a movement it cannot explain rather than inventing one. What it
        # writes is holding_snapshot, which is the only completeness proof available: every
        # ISIN and quantity across EVERY broker on one PAN. stage_batch already branches on
        # parse_declared_holdings, so nothing downstream needed changing -- only this
        # registration, which is what made a written parser reachable.
        return CasPdfAdapter(password=pdf_password, broker_accounts=broker_accounts)
    if name == LlmDocumentAdapter.name:
        return LlmDocumentAdapter(password=pdf_password, client=_llm_client(settings))
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"no adapter for {name!r}")


#: What each adapter's output is recorded as in ``document`` -- the metadata pair
#: ``stage_batch`` takes. One table instead of a literal at each call site, so the PDF
#: path cannot repeat the "every PDF stored as a TRADEBOOK/text-csv" mistake that
#: stage_batch's own docstring documents.
_DOCTYPES: dict[str, tuple[str, str]] = {
    "zerodha_tradebook": ("TRADEBOOK", "text/csv"),
    "zerodha_contract_note_xlsx": (
        "CONTRACT_NOTE",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "zerodha_contract_note": ("CONTRACT_NOTE", "application/pdf"),
    CasPdfAdapter.name: ("CAS", "application/pdf"),
    # Documents no deterministic parser claimed. The doctype is deliberately generic:
    # what this file IS was never established -- only that it carried a trade table.
    LlmDocumentAdapter.name: ("DOCUMENT", "application/pdf"),
}


def _saved_passwords(conn: Any, investor_id: str) -> list[tuple[str, str]]:
    """What to try on a locked file before asking: the investor's PAN (both cases), then
    each statement password they saved -- decrypted, for opening files only.

    A password we hold but cannot decrypt is a server fault (wrong or missing
    PORTFOLIO_ENCRYPTION_KEY) -- said as one, rather than quietly trying nothing and telling
    the investor their file is locked when the real problem is ours.
    """
    try:
        return document_passwords.unlock_candidates(conn, investor_id)
    except Exception as exc:  # noqa: BLE001 - crypto/key errors
        logger.error(
            "could not decrypt saved file passwords for investor %s", investor_id, exc_info=True
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Your saved file passwords could not be read: the server's encryption key is "
            "misconfigured. Nothing was imported.",
        ) from exc


def _unlock(
    content: bytes, saved: list[tuple[str, str]], typed: tuple[str, str] | None = None
) -> file_validation.Unlock:
    """Open one uploaded file: no password, then the password just typed (if any), then
    each saved one -- every one exactly as given. Not a PDF: nothing to unlock."""
    if content[:5] != b"%PDF-":
        return file_validation.Unlock(file_validation.UNLOCK_OPENED)
    candidates = ([typed] if typed else []) + saved
    return file_validation.unlock_pdf(content, candidates)


def _stored_original(conn: Any, store: Any, batch_id: str) -> bytes:
    """The bytes a batch was staged from -- what a re-parse has to open again."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.storage_key FROM import_batch b JOIN document d ON d.id = b.document_id "
            "WHERE b.id = %s",
            (batch_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"batch {batch_id} has no stored document to re-parse",
        )
    return store.get(row[0])


def _reject_invalid_files(
    uploads: list[tuple[str, bytes]], adapter: str, pdf_password: str = ""
) -> None:
    """Tier 1 + Tier 2, enforced for a single-adapter upload route. Raises 422 naming
    every bad file, or returns having found none.

    ALL-OR-NOTHING, unlike the duplicate handling below it. A duplicate is a fact about
    one file and the other nine are still worth staging; a wrong file means the person
    picked the wrong things and half-importing their selection leaves them guessing which
    half. Nothing is staged until every file is one this adapter can read.
    """
    problems = []
    for filename, content in uploads:
        verdict = file_validation.verdict_for_adapter(
            file_validation.classify(content, filename, pdf_password), adapter
        )
        if not verdict.ok:
            problems.append(f"{verdict.filename}: {verdict.reason} {verdict.hint or ''}".strip())
    if problems:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Nothing was imported. " + " ".join(problems),
        )


def _investor_code(conn: Any, investor_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such investor: {investor_id}")
    return row[0]


def _heal_fno_in_background(
    conn: Any, background_tasks: BackgroundTasks, settings: Settings, results: list[dict]
) -> None:
    """A staged F&O row waiting on a contract-master day is a gap public data closes: run
    the post-import cycle now (app.post_import), which backfills that day and re-resolves
    the row before anyone has to promote. Commit first -- see promote_stage for why a
    background cycle must never wait on this request's uncommitted rows."""
    if any(r.get("fno_awaiting_master") for r in results):
        conn.commit()
        from app.post_import import run_cycle_in_own_connection

        background_tasks.add_task(run_cycle_in_own_connection, settings)


def _duplicate_result(exc: DuplicateUploadError) -> dict[str, Any]:
    """A re-uploaded file is an answer, not an error: name the batch it already is, so a
    multi-file upload survives one stale file instead of failing wholesale (and, with
    this router's one-connection-per-request commit, taking every successfully staged
    file in the same request down with it)."""
    return {
        "batch_id": exc.batch_id,
        "parsed": 0,
        "rejected": 0,
        "sha256": exc.sha256,
        "document_id": None,
        "notes": [f"identical file already staged as batch {exc.batch_id}"],
    }


def _refused_result(exc: IdentityMismatchError, content: bytes) -> dict[str, Any]:
    """A document that names somebody else. No batch, because nothing was staged and
    nothing was stored -- see app.db.identity.

    Shaped like ``_duplicate_result`` and for the same reason: one refused file in a
    multi-file upload must not take the request's other, legitimate files down with it.
    The distinction the person needs is in the note -- a duplicate is "we already have
    this", a refusal is "this is not this account's document".

    ``str(exc)`` is already redacted at source (app.db.identity builds the message
    through crypto.redact_pan), so it can be shown to the uploader as-is.
    """
    return {
        "batch_id": None,
        "parsed": 0,
        "rejected": 0,
        "sha256": content_hash(content),
        "document_id": None,
        "notes": [f"refused: {exc}"],
    }


@router.post(
    "/csv",
    response_model=list[StageResultOut],
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_csv(
    investor_id: str = Form(...),
    account_id: str = Form(...),
    adapter: str = Form(...),
    files: list[UploadFile] = File(...),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Multi-file by design -- Console caps each tradebook download at 365 days, so a
    full history is several files. Each is staged independently; merge-and-dedupe
    happens at promotion, via the same ``dedupe_key`` unique index every import uses.

    One file failing never fails the request: a duplicate comes back as its own result
    entry pointing at the existing batch, and any other per-file error is reported in
    its entry rather than aborting the files that staged fine.

    VALIDATION IS THE EXCEPTION, and deliberately: every file is checked against the
    declared adapter -- by extension and by content -- before anything stages, and a
    mismatch refuses the whole request (see ``_reject_invalid_files``). Callers that want
    per-file routing rather than a declared slot should use POST /ingest/files."""
    factory = _CSV_ADAPTERS.get(adapter)
    if factory is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no adapter for {adapter!r}; supported: {sorted(_CSV_ADAPTERS)}",
        )

    assert_owned_investor(conn, current_user, investor_id)
    if not repository.account_belongs_to(conn, account_id, investor_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no such account for this investor: {account_id}",
        )
    investor_code = _investor_code(conn, investor_id)
    consent.require_consent(conn, investor_id)

    uploads = [(upload.filename or "", await upload.read()) for upload in files]
    _reject_invalid_files(uploads, adapter)

    store = get_store(settings)
    results = []
    for filename, content in uploads:
        try:
            results.append(
                imports.stage_batch(
                    conn,
                    factory(),
                    content,
                    investor_id=investor_id,
                    account_id=account_id,
                    filename=filename,
                    store=store,
                    investor_code=investor_code,
                    actor_uid=str(current_user["id"]),
                )
            )
        except DuplicateUploadError as exc:
            results.append(_duplicate_result(exc))
        except IdentityMismatchError as exc:
            results.append(_refused_result(exc, content))
    return results


@router.post(
    "/xlsx",
    response_model=list[StageResultOut],
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_xlsx(
    background_tasks: BackgroundTasks,
    investor_id: str = Form(...),
    account_id: str = Form(...),
    adapter: str = Form(...),
    files: list[UploadFile] = File(...),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """A client's own contract-note spreadsheet, uploaded directly -- the historical-
    backfill path: Console's bulk contract-note export covers a chosen date range,
    which is how "upload my last three financial years" actually works here (there is
    no in-app date-range picker, and none is needed -- the range is whatever was
    downloaded). Same multi-file, per-file-duplicate-handling shape as /ingest/csv, and
    the same all-or-nothing validation in front of it. No password handling: these
    workbooks aren't encrypted.
    """
    factory = _XLSX_ADAPTERS.get(adapter)
    if factory is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no adapter for {adapter!r}; supported: {sorted(_XLSX_ADAPTERS)}",
        )

    assert_owned_investor(conn, current_user, investor_id)
    if not repository.account_belongs_to(conn, account_id, investor_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no such account for this investor: {account_id}",
        )
    investor_code = _investor_code(conn, investor_id)
    consent.require_consent(conn, investor_id)

    uploads = [(upload.filename or "", await upload.read()) for upload in files]
    _reject_invalid_files(uploads, adapter)

    instance = factory()
    store = get_store(settings)
    results = []
    doctype, content_type = _DOCTYPES[adapter]
    for filename, content in uploads:
        try:
            results.append(
                imports.stage_batch(
                    conn,
                    instance,
                    content,
                    investor_id=investor_id,
                    account_id=account_id,
                    filename=filename,
                    store=store,
                    investor_code=investor_code,
                    actor_uid=str(current_user["id"]),
                    doctype=doctype,
                    content_type=content_type,
                )
            )
        except DuplicateUploadError as exc:
            results.append(_duplicate_result(exc))
        except IdentityMismatchError as exc:
            results.append(_refused_result(exc, content))
    _heal_fno_in_background(conn, background_tasks, settings, results)
    return results


def _rejected_outcome(verdict: FileVerdict) -> dict[str, Any]:
    """A file that never reached an adapter. No batch, no document, no rows -- and two
    sentences saying why and what to do instead."""
    return {
        "filename": verdict.filename,
        "ok": False,
        "container": verdict.container,
        "document": verdict.document,
        "adapter": None,
        "batch_id": None,
        "parsed": 0,
        "rejected": 0,
        "reason": verdict.reason,
        "hint": verdict.hint,
        "encrypted": verdict.encrypted,
        "wrong_password": False,
        "notes": verdict.notes,
    }


@router.post(
    "/files",
    response_model=FilesUploadOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_files(
    background_tasks: BackgroundTasks,
    investor_id: str = Form(...),
    account_id: str = Form(...),
    files: list[UploadFile] = File(...),
    password: str | None = Form(default=None),
    password_source: str | None = Form(default=None),
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Drop a pile of broker files in and let the server sort them.

    WHY A SEPARATE ROUTE. ``/ingest/csv`` and ``/ingest/xlsx`` each take ONE declared
    adapter for the whole request, which is right for the import screen where the person
    has already said what they are uploading, and wrong for onboarding, where the honest
    instruction is "give us whatever your broker gave you". A first-time user has a folder
    with tradebook CSVs, contract-note XLSXs and PDFs in it and no reason to know which is
    which -- so the classification (app.ingest.file_validation) picks the adapter per file
    instead of asking.

    PER-FILE OUTCOMES, NOT ALL-OR-NOTHING. The opposite of ``_reject_invalid_files``, and
    deliberately: there is no wrong slot to be in here, so one unreadable file is a fact
    about that file. Nine good files still import and the tenth comes back with a sentence.

    STAGING ONLY. Like every other route in this module, nothing reaches the ledger here.
    The caller promotes the returned ``staged_batch_ids`` with
    ``POST /ingest/stage/{id}/promote`` -- the second explicit, attributed action that
    docs/INGESTION.md ("Staging: why nothing auto-commits") requires. Routing files
    automatically is a convenience; committing them automatically would not be.
    """
    assert_owned_investor(conn, current_user, investor_id)
    if not repository.account_belongs_to(conn, account_id, investor_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no such account for this investor: {account_id}",
        )
    investor_code = _investor_code(conn, investor_id)
    consent.require_consent(conn, investor_id)

    # A password typed just now, for a locked file. Tried on these files only; saved for
    # its source only once it has actually opened one of them -- a wrong password is never
    # stored (migration 082).
    typed: tuple[str, str] | None = None
    if password:
        if password_source not in document_passwords.SOURCES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"password_source must be one of {list(document_passwords.SOURCES)}",
            )
        typed = (password_source, password)
    saved = _saved_passwords(conn, investor_id)
    typed_saved = False
    store = get_store(settings)

    outcomes: list[dict[str, Any]] = []
    staged_batch_ids: list[str] = []
    staged_results: list[dict[str, Any]] = []

    for upload in files:
        filename = upload.filename or ""
        content = await upload.read()
        unlock = _unlock(content, saved, typed)
        password_used = unlock.password
        if (
            typed is not None
            and not typed_saved
            and unlock.outcome == file_validation.UNLOCK_OPENED
            and unlock.password == typed[1]
            and unlock.password
        ):
            try:
                document_passwords.save(
                    conn, investor_id, typed[0], typed[1], actor_uid=str(current_user["id"])
                )
            except crypto.EncryptionNotConfiguredError as exc:
                # The password was right; we just cannot store it safely. Say that, rather
                # than a bare 500 the investor would read as "your password failed".
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "That password opened your file, but the server cannot save it: its "
                    "encryption key is not configured. Nothing was imported.",
                ) from exc
            typed_saved = True
        verdict = file_validation.classify(content, filename, password_used)
        if verdict.encrypted and typed is not None:
            # They typed a password and it opened nothing -- the one answer they need is
            # that THIS password is wrong, so they can fix it and try again.
            outcomes.append(
                _rejected_outcome(verdict)
                | {
                    "reason": "That password didn't open this file. Check it and try again.",
                    "hint": None,
                    "wrong_password": True,
                }
            )
            continue
        if not verdict.ok or verdict.adapter is None:
            # No deterministic parser claimed this file. Where the LLM path is enabled it
            # gets one attempt; where it is not, this is the dead end it has always been.
            #
            # Classification itself is NOT reconsidered here -- the model never routes,
            # per docs/INGESTION.md ("never by sniffing message bodies"). The file is
            # handed to a transcriber, and app/ingest/redaction.py refuses to transmit
            # anything that does not carry a trade table, so an unrelated document a
            # client attached by mistake still goes nowhere.
            #
            # A LOCKED PDF NEVER GOES TO THE MODEL. The model cannot open it either, and
            # handing it over turned "password-protected" into "no trades found -- check
            # the date range", which hid the PAN prompt the browser shows on `encrypted`.
            if verdict.encrypted or _llm_client(settings) is None:
                outcomes.append(_rejected_outcome(verdict))
                continue
            verdict = replace(verdict, ok=True, adapter=LlmDocumentAdapter.name)

        doctype, content_type = _DOCTYPES[verdict.adapter]
        try:
            # ONE SAVEPOINT PER FILE. A failure staging this file rolls back only this
            # file; the files already staged in this request keep their batches and their
            # ids reach the browser (2026-10-08: one 500 lost every staged batch id). The
            # two expected refusals are caught INSIDE the savepoint so what they wrote
            # (the identity-refusal audit row) is kept, exactly as before.
            refusal: Exception | None = None
            with conn.transaction():
                try:
                    result = imports.stage_batch(
                        conn,
                        _build_adapter(
                            verdict.adapter,
                            password_used,
                            settings,
                            webhook.investor_broker_accounts(conn, investor_id),
                        ),
                        content,
                        investor_id=investor_id,
                        account_id=account_id,
                        filename=filename,
                        store=store,
                        investor_code=investor_code,
                        actor_uid=str(current_user["id"]),
                        doctype=doctype,
                        content_type=content_type,
                    )
                except (IdentityMismatchError, DuplicateUploadError) as exc:
                    refusal = exc
            if refusal is not None:
                raise refusal
        except IdentityMismatchError as exc:
            # The file is the right kind and perfectly readable -- it just is not this
            # account's. ok=False: unlike a duplicate, nothing about this file has been
            # or will be imported, and saying otherwise would be a lie on the screen.
            # The hint names the two ways it happens, because both are one-click fixes
            # and neither is "send a different file".
            outcomes.append(
                {
                    "filename": filename,
                    "ok": False,
                    "container": verdict.container,
                    "document": verdict.document,
                    "adapter": verdict.adapter,
                    "batch_id": None,
                    "parsed": 0,
                    "rejected": 0,
                    "reason": str(exc),
                    "hint": (
                        "This document belongs to a different person or account. Check "
                        "the account selected above, and that this investor's PAN in "
                        "Settings matches the one printed on the document."
                    ),
                    "encrypted": False,
                    "notes": [],
                }
            )
            continue
        except CasStatementError as exc:
            # A CAS that cannot be tied to exactly one of this investor's accounts, or whose
            # holdings do not reproduce its own totals. Raised inside the savepoint before
            # anything is stored, so nothing is left behind.
            outcomes.append(
                {
                    "filename": filename,
                    "ok": False,
                    "container": verdict.container,
                    "document": verdict.document,
                    "adapter": verdict.adapter,
                    "batch_id": None,
                    "parsed": 0,
                    "rejected": 0,
                    "reason": str(exc),
                    "hint": None,
                    "encrypted": False,
                    "notes": [],
                }
            )
            continue
        except DuplicateUploadError as exc:
            # Not an error and not a rejection: re-sending a file we already hold is the
            # normal cost of "upload everything" (and of email forwarding, which is why
            # dedupe_key exists at all). Say so plainly and move on. If that earlier
            # batch is still STAGED -- an upload whose response never reached the
            # browser -- hand its id back so the retry resumes it instead of saying
            # "already imported" about trades that are not in the ledger.
            existing = imports.batch_summary(conn, exc.batch_id)
            # CAS balances were recorded during staging; they never need trade promotion.
            is_cas = verdict.adapter == CasPdfAdapter.name
            pending = (
                not is_cas
                and existing is not None
                and existing["status"] == imports.STATUS_STAGED
            )
            if pending:
                staged_batch_ids.append(exc.batch_id)
            outcomes.append(
                {
                    "filename": filename,
                    "ok": True,
                    "container": verdict.container,
                    "document": verdict.document,
                    "adapter": verdict.adapter,
                    "batch_id": exc.batch_id,
                    "parsed": existing["row_count"] if pending else 0,
                    "rejected": existing["rejected_count"] if pending else 0,
                    "reason": None,
                    "hint": None,
                    "encrypted": False,
                    "notes": [
                        "CAS balances already processed — no ledger promotion is needed."
                        if is_cas
                        else (
                            "Already uploaded and still waiting for review — resumed, nothing "
                            "was staged twice."
                            if pending
                            else "Already imported earlier — nothing was added twice."
                        )
                    ],
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one file must not fail the whole upload
            logger.warning("ingest_file_stage_failed %s", filename, exc_info=True)
            outcomes.append(
                {
                    "filename": filename,
                    "ok": False,
                    "container": verdict.container,
                    "document": verdict.document,
                    "adapter": verdict.adapter,
                    "batch_id": None,
                    "parsed": 0,
                    "rejected": 0,
                    "reason": f"this file could not be staged ({type(exc).__name__})",
                    "hint": "The other files were not affected. Try this one again.",
                    "encrypted": False,
                    "notes": [],
                }
            )
            continue

        staged_results.append(result)
        # F&O rows waiting on a contract-master day resolve in the background cycle, so
        # their batch is promotable even when nothing has resolved yet.
        awaiting = result.get("fno_awaiting_master", 0)
        if result["parsed"] > 0 or awaiting:
            staged_batch_ids.append(result["batch_id"])
        notes = list(result.get("notes") or [])
        if awaiting:
            notes.append(
                f"{awaiting} F&O trade(s) are waiting for the exchange's F&O contract list "
                "for their day; it is being fetched and they will resolve automatically."
            )
        elif result["parsed"] == 0 and verdict.adapter != CasPdfAdapter.name:
            # The file was the right kind and still yielded nothing. That is a real
            # answer -- an empty date range, or a note whose every row was rejected --
            # and it must not read as success on the screen. Not for a CAS: it never
            # carries trades, and its own notes already say what it recorded.
            notes.append(
                "We recognised this file but found no trades in it. "
                "Check the date range you exported."
            )
        outcomes.append(
            {
                "filename": filename,
                "ok": True,
                "container": verdict.container,
                "document": verdict.document,
                "adapter": verdict.adapter,
                "batch_id": result["batch_id"],
                "parsed": result["parsed"],
                "rejected": result["rejected"],
                "reason": None,
                "hint": None,
                "encrypted": False,
                "notes": notes,
            }
        )

    _heal_fno_in_background(conn, background_tasks, settings, staged_results)
    return {
        "files": outcomes,
        "accepted": sum(1 for o in outcomes if o["ok"]),
        "rejected": sum(1 for o in outcomes if not o["ok"]),
        "staged_batch_ids": staged_batch_ids,
    }


@router.get("/batches", response_model=list[BatchSummaryOut])
def list_batches(
    investor_id: str,
    batch_status: Literal["ALL", "STAGED", "PROMOTED", "DISCARDED"] = Query(
        default="STAGED", alias="status"
    ),
    conn: Any = Depends(get_conn),
    _current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """The batches that exist for one investor -- what makes a batch id findable after a
    page refresh, instead of knowable only to whoever kept the upload response around.
    Owner-scoped like everything else under the investor; defaults to STAGED because
    that is the state a reviewer needs back."""
    assert_owned_investor(conn, _current_user, investor_id)
    return imports.list_batches(conn, investor_id, None if batch_status == "ALL" else batch_status)


def _reject_if_oversized(request: Request, settings: Settings) -> None:
    """Refuse an oversized request from ``Content-Length`` alone, before FastAPI reads
    the body into memory at all -- let alone before any provider translator
    base64-decodes an attachment out of it."""
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > settings.ingest_max_attachment_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"request exceeds INGEST_MAX_ATTACHMENT_BYTES ({settings.ingest_max_attachment_bytes})",
        )


@router.post("/email/mime", response_model=list[StageResultOut], status_code=status.HTTP_200_OK)
@router.post("/email", response_model=list[StageResultOut], status_code=status.HTTP_200_OK)
async def ingest_email(
    request: Request,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """Provider-authenticated intake. Mailgun commits a complete encrypted delivery and
    pending extraction job, then returns 200; all expensive work happens in the worker.
    The /mime alias selects Mailgun's raw-MIME delivery mode. Legacy providers retain
    their attachment archive path, with an explicit commit before acknowledgement.
    """
    # The dependency-injected settings, not app.main's module-level get_settings() --
    # app.main's auth-failure handlers read this back for their log line, since a
    # registered @app.exception_handler is called by Starlette directly and never goes
    # through FastAPI's dependency injection (so it would otherwise see the real
    # process-wide Settings even under a test's dependency_overrides).
    request.state.settings = settings
    if not settings.email_forwarding_enabled:
        # Switched off (EMAIL_FORWARDING_ENABLED). 406 is the providers' "rejected, do not
        # retry", so a delivery is refused once rather than queued or retried for days.
        raise HTTPException(status.HTTP_406_NOT_ACCEPTABLE, "Email forwarding is turned off.")
    _reject_if_oversized(request, settings)
    if settings.ingest_webhook_provider is None:
        raise WebhookNotConfiguredError()
    if settings.ingest_webhook_provider == "mailgun":
        from app.ingest.mailgun import accept

        return await accept(request, conn, settings)
    message = await webhook_providers.verify_and_translate(request, settings)
    store = get_store(settings)
    results = webhook.handle_inbound_email(
        conn,
        store,
        message.to,
        message.attachments,
        sender=message.sender,
        body_text=message.body_text,
        max_attachments=settings.ingest_max_attachments_per_delivery,
    )
    conn.commit()
    return results


@router.get(
    "/stage/{batch_id}",
    response_model=ReviewBatchOut,
    dependencies=[Depends(require_owned_batch)],
)
def review_stage(batch_id: str, conn: Any = Depends(get_conn)) -> dict[str, Any]:
    """What a human reads before deciding. Predicts what promotion will do, including
    which rows are already in the ledger, rather than reporting it afterwards."""
    result = imports.review_batch(conn, batch_id)
    result["needs_review"] = [
        {
            "row_number": r["row_number"],
            "confidence": (
                str(r["confidence"]) if isinstance(r["confidence"], Decimal) else r["confidence"]
            ),
        }
        for r in result["needs_review"]
    ]
    return result


@router.post(
    "/stage/{batch_id}/promote",
    response_model=PromoteResultOut,
    dependencies=[Depends(require_owned_batch)],
)
def promote_stage(
    batch_id: str,
    background_tasks: BackgroundTasks,
    body: PromoteRequest | None = None,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """The second explicit, attributed action. Writes staged rows to the ledger --
    the only place this router writes a transaction.

    409 when the batch holds rows no parser could verify and the caller has not said to
    take them. The message names the count, because "3 rows need your confirmation" is
    something a person can act on and "promotion refused" is not.
    """
    try:
        summary = imports.promote_batch(
            conn,
            batch_id,
            actor_uid=current_user["id"],
            acknowledge_uncertain=bool(body and body.acknowledge_uncertain),
        )
    except imports.UnreviewedRowsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    if summary.inserted or imports.fno_rows_awaiting_master(conn, batch_id):
        # COMMIT FIRST. ``get_conn`` commits at teardown, and FastAPI tears a request's
        # yield-dependencies down only AFTER its background tasks finish -- so the cycle
        # below, on its own connection, would wait on this request's uncommitted rows
        # while this request waited on it: a hang, holding the worker's advisory lock, with
        # the import never scheduled (seen live 2026-10-08: screen stuck on "processing").
        conn.commit()
        # New ledger rows: price their ISINs and sweep their corporate actions now, not at
        # tomorrow's cron (owner decision 2026-10-08) -- and fetch any F&O contract-master
        # day this batch's F&O rows are waiting on. Runs after the response is sent;
        # the import screen continues it via POST /investors/{id}/sync. See app.post_import.
        #
        # On Vercel a background task holds the response until it ends, so a long cycle
        # made this promote answer 5xx after its trades were committed (2026-10-13). The
        # cycle is handed to another request; only if that cannot start does it run here.
        from app.continuation import start_cycle_elsewhere

        if not start_cycle_elsewhere(settings):
            from app.post_import import run_cycle_in_own_connection

            background_tasks.add_task(run_cycle_in_own_connection, settings)
    return {
        "inserted": summary.inserted,
        "skipped_duplicates": summary.skipped_duplicates,
        "rejected": summary.rejected,
    }


@router.post(
    "/stage/{batch_id}/reparse",
    response_model=StageResultOut,
    dependencies=[Depends(require_owned_batch)],
)
def reparse_stage(
    batch_id: str,
    background_tasks: BackgroundTasks,
    conn: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Re-run the original adapter against a STAGED batch's stored bytes, replacing
    its rows -- the retry path for a document that failed for a reason since fixed.
    Re-uploading the same file cannot do this -- stage_batch's duplicate check is a
    permanent per-account sha256 lookup that just points back at this same batch
    forever. See app.db.imports.reparse_batch.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT adapter, investor_id FROM import_batch WHERE id = %s",
            (batch_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such batch: {batch_id}")
    adapter_name, batch_investor_id = row[0], str(row[1])
    # A locked file stays locked on re-parse -- opening the stored original with the
    # investor's saved passwords is what makes this the retry path it claims to be, rather
    # than one that silently fails for exactly the documents most likely to need it.
    store = get_store(settings)
    unlock = _unlock(
        _stored_original(conn, store, batch_id), _saved_passwords(conn, batch_investor_id)
    )
    if unlock.outcome != file_validation.UNLOCK_OPENED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This file is locked and none of your saved passwords open it. Upload it again "
            "and enter its password when asked."
            if unlock.outcome == file_validation.UNLOCK_WRONG_PASSWORD
            else "The stored file is damaged or not a complete PDF.",
        )
    instance = _build_adapter(
        adapter_name,
        unlock.password,
        settings,
        webhook.investor_broker_accounts(conn, batch_investor_id),
    )

    try:
        result = imports.reparse_batch(
            conn, store, batch_id, instance, actor_uid=str(current_user["id"])
        )
    except IdentityMismatchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except CasStatementError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    _heal_fno_in_background(conn, background_tasks, settings, [result])
    return result


@router.post(
    "/stage/{batch_id}/discard",
    dependencies=[Depends(require_owned_batch)],
)
def discard_stage(
    batch_id: str,
    body: DiscardRequest,
    conn: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Reject a batch without promoting it. The staged rows are kept, not deleted --
    what was rejected and why is the evidence that review happened."""
    imports.discard_batch(conn, batch_id, body.reason, actor_uid=current_user["id"])
    return {"status": "discarded"}
