"""The worker: claim jobs, open the documents they name, stage the rows.

RUNNABLE TWO WAYS, AND THE MODULE IS THE IMPLEMENTATION::

    python -m app.jobs.drain --budget-seconds 300     # a laptop, a cron, a CI runner
    POST /ops/jobs/drain                              # a thin HTTP wrapper over drain_once

That is deliberate and is the single decision that keeps hosting a deployment choice
rather than an architectural one. Parsing a PDF is CPU, and Vercel's Hobby plan includes
4 CPU-hours per month after which the feature *pauses for thirty days* rather than
billing -- so the ability to run the identical code somewhere unmetered, today on the
developer's own machine, is what makes the free tier safe. See
docs/BACKFILL_PIPELINE.md sections 4.0-4.2.

WHY THE COMMIT PATTERN LOOKS THE WAY IT DOES. ``app.db.connection.connect`` commits once
at the end of its context and rolls back on any exception, which is right for a request
and wrong for a batch: one poison document would roll back the forty that succeeded
before it. So this module commits per job, and commits the CLAIM first:

    claim -> COMMIT          <- attempts++ and the lease are now durable
    work  -> COMMIT          <- or rollback, then record the failure, then COMMIT

Committing the claim first is not a detail. If the claim rolled back with the work, the
``attempts`` increment would roll back too, and a document that reliably crashes the
process would be retried forever instead of landing in FAILED with its reason.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid as _uuid
from typing import Any

from app.db import documents, imports
from app.db.identity import IdentityMismatchError
from app.ingest import file_validation, webhook
from app.ingest.cas_pdf import CasStatementError
from app.jobs import email as email_jobs
from app.jobs import queue
from app.jobs.queue import KIND_PARSE_DOCUMENT
from app.storage.base import ObjectNotFoundError

logger = logging.getLogger(__name__)

#: How many jobs one claim takes. Small on purpose -- a claim holds a lease, and a worker
#: that claims fifty and then dies leaves fifty leases to expire instead of one.
DEFAULT_CLAIM_BATCH = 5

#: Stop claiming new work with less than this left in the budget, so the last job claimed
#: has room to finish inside the caller's own limit rather than being cut off mid-parse.
_HEADROOM_SECONDS = 20


class PermanentJobError(Exception):
    """This job can never succeed. Retrying wastes the queue and, worse, hides the real
    reason behind an attempts counter."""


def _document_id(payload: dict[str, Any]) -> str:
    """The payload's document id, PROVEN to be a uuid before any SQL sees it.

    Not defensive noise. A malformed id reaching Postgres raises
    ``InvalidTextRepresentation``, which aborts the whole transaction -- and in Postgres an
    aborted transaction refuses every subsequent statement, including the ``queue.fail``
    that was supposed to record why this job failed. The result is a job stuck in RUNNING
    with no reason attached, retried once per lease expiry. Validating here turns that into
    an ordinary permanent failure with a sentence on the row.
    """
    raw = payload.get("document_id")
    if not raw:
        raise PermanentJobError("payload carries no document_id")
    try:
        return str(_uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PermanentJobError(f"document_id is not a uuid: {raw!r}") from exc


def _parse_document(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Reopen one stored document and stage it.

    Everything needed is re-derived from ids: the bytes come from object storage, the PAN
    from ``investor_secret`` via ``webhook.adapter_for``. Nothing sensitive was carried in
    the payload -- see app/jobs/queue.py.

    Raises:
        PermanentJobError: no retry can succeed -- the document row is gone, its bytes are
            gone, or no adapter exists for what it is.
        IdentityMismatchError: the document belongs to another investor. Permanent, and
            handled by the caller so the refusal is audited by app.db.identity as usual.
    """
    document_id = _document_id(payload)
    document = documents.document_for_parse(conn, document_id)
    if document is None:
        # ON DELETE CASCADE from investor: an erasure between enqueue and drain is the
        # ordinary way this happens, and it is a success for the erasure, not an error.
        raise PermanentJobError(f"document {document_id} no longer exists")

    investor_id = str(document["investor_id"])
    account_id = payload.get("account_id") or document["account_id"]
    if not account_id:
        raise PermanentJobError(f"document {document_id} has no account to stage into")

    broker = str(payload.get("broker", ""))
    doctype = str(payload.get("doctype", ""))

    try:
        content = store.get(document["storage_key"])
    except ObjectNotFoundError as exc:
        raise PermanentJobError(f"stored object missing: {document['storage_key']}") from exc

    # A forwarded PDF opens with no password or with one the investor saved, exactly as
    # typed. Locked with none of them is permanent until they save the right one -- retrying
    # cannot change that, and parsing it anyway would only report "no trades".
    password = ""
    if content[:5] == b"%PDF-":
        unlock = webhook.unlock_with_saved_passwords(conn, investor_id, content)
        if unlock.outcome == file_validation.UNLOCK_WRONG_PASSWORD:
            raise PermanentJobError(
                "This forwarded file is locked and none of your saved passwords open it. "
                "Upload it once on the Import page and enter its password; forwarded copies "
                "will open by themselves after that."
            )
        if unlock.outcome == file_validation.UNLOCK_DAMAGED:
            raise PermanentJobError("This forwarded file is damaged or not a complete PDF.")
        password = unlock.password

    try:
        adapter = webhook.adapter_for(conn, investor_id, broker, doctype, password)
    except webhook.UnsupportedDocumentError as exc:
        raise PermanentJobError(str(exc)) from exc

    result = imports.stage_batch(
        conn,
        adapter,
        content,
        investor_id=investor_id,
        account_id=str(account_id),
        filename=str(payload.get("filename") or document["filename"] or ""),
        store=store,
        investor_code=str(payload.get("investor_code", "")),
        actor_uid=None,
        doctype=str(document["doctype"] or doctype.upper()),
        content_type=str(document["content_type"] or "application/pdf"),
        delivery_id=payload.get("delivery_id"),
    )
    documents.mark_parse_status(conn, str(document_id), documents.PARSE_PARSED)
    return result


#: kind -> handler. A job whose kind is not here is a permanent failure rather than a
#: crash: it means a deploy removed a handler while work was still queued, and the row
#: should say so instead of being retried three times against nothing.
def _butler_tick(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Butler's deterministic pass (BT 05) -- the same durable worker, not a second scheduler."""
    from app.jobs.butler import handle_job

    return handle_job(conn, store, payload)


def _hosted_tick(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """BT-P02 hosted report/reflection jobs (RP 01) -- the same durable queue, one more kind."""
    from app.jobs.hosted import handle_job

    return handle_job(conn, store, payload)


def _monitor_tick(conn: Any, store: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """BE 36-37 monitor due occurrences -- the same durable queue, one more kind. Records
    blocked occurrences only while BE-Q01 is open; never queues model work."""
    from app.jobs.monitors import handle_job

    return handle_job(conn, store, payload)


def _security_sectors(conn, store, payload):
    from app.db.sectors import refresh_sectors
    from app.post_import import _isins_missing_sector

    missing = _isins_missing_sector(conn)
    return refresh_sectors(conn, missing) if missing else {"unclassified": []}


_HANDLERS = {
    KIND_PARSE_DOCUMENT: _parse_document,
    "butler_tick": _butler_tick,
    "security_sectors": _security_sectors,
    "hosted_job_tick": _hosted_tick,
    "monitor_tick": _monitor_tick,
    "email_delivery": email_jobs.extract_delivery,
    "email_document": email_jobs.parse_document,
    "email_portfolio_refresh": email_jobs.refresh_portfolio,
}


def drain_once(
    conn: Any,
    store: Any,
    *,
    limit: int = 500,
    budget_seconds: float = 240.0,
    claim_batch: int = DEFAULT_CLAIM_BATCH,
    investor_id: str | None = None,
) -> dict[str, Any]:
    """Work the queue until it is empty, the budget is spent, or ``limit`` is reached.

    Returns a summary rather than logging and forgetting, because Vercel's Hobby plan
    retains one hour of runtime logs -- a drain that failed overnight leaves no trace by
    morning. The counts here are what an ops call reads back, and the per-job reasons
    live on the ``job`` rows, where retention is ours.

    Never raises for a bad document. A parse failure is recorded against its job and the
    loop continues: one malformed PDF must not stall the other four hundred and ninety-nine.
    """
    started = time.monotonic()
    done = failed = 0
    permanent = 0
    fno_awaiting = 0

    while done + failed < limit:
        remaining = budget_seconds - (time.monotonic() - started)
        if remaining <= _HEADROOM_SECONDS:
            break

        batch = min(claim_batch, limit - (done + failed))
        jobs = queue.claim(conn, batch, investor_id=investor_id)
        email_jobs.reconcile_failures(conn)
        # THE CLAIM IS COMMITTED BEFORE ANY WORK. See the module docstring.
        conn.commit()
        if not jobs:
            break

        for job in jobs:
            handler = _HANDLERS.get(job.kind)
            try:
                if not queue.lock_claim(conn, job):
                    conn.rollback()
                    continue
                if handler is None:
                    raise PermanentJobError(f"no handler for job kind {job.kind!r}")
                outcome = handler(conn, store, job.payload)
            except imports.DuplicateUploadError:
                # Already staged. Not a failure: re-forwarding a page you already sent is
                # explicitly supported, and `sha256` UNIQUE is what makes it a no-op.
                conn.rollback()
                queue.complete(conn, job.id, claim_token=job.claim_token)
                conn.commit()
                done += 1
            except (PermanentJobError, IdentityMismatchError, CasStatementError) as exc:
                conn.rollback()
                # ORDER IS LOAD-BEARING. Record the job's own failure FIRST and commit it,
                # then attempt the document flag as a separate transaction. Reversed, any
                # error in the best-effort write aborts the transaction and Postgres then
                # refuses the queue.fail as well -- leaving a RUNNING job with no reason,
                # re-tried once per lease expiry. A try/except cannot rescue that, because
                # the transaction is already poisoned by the time it is caught.
                changed = queue.fail(
                    conn, job.id, str(exc), permanent=True, claim_token=job.claim_token
                )
                if changed:
                    email_jobs.record_failure(conn, job.kind, job.payload, str(exc))
                conn.commit()
                _mark_document_failed(conn, job.payload)
                permanent += 1
                failed += 1
                logger.warning("job %s permanently failed: %s", job.id, exc)
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the loop
                conn.rollback()
                changed = queue.fail(
                    conn,
                    job.id,
                    f"{type(exc).__name__}: {exc}",
                    permanent=False,
                    claim_token=job.claim_token,
                )
                if changed and job.attempts >= job.max_attempts:
                    email_jobs.record_failure(
                        conn,
                        job.kind,
                        job.payload,
                        "Processing failed after retries; original retained",
                    )
                conn.commit()
                failed += 1
                logger.warning("job %s failed (retryable): %s", job.id, exc, exc_info=True)
            else:
                if isinstance(outcome, dict) and "reschedule_seconds" in outcome:
                    queue.reschedule(conn, job, outcome["reschedule_seconds"])
                else:
                    queue.complete(conn, job.id, claim_token=job.claim_token)
                conn.commit()
                done += 1
                if isinstance(outcome, dict) and outcome.get("fno_awaiting_master"):
                    fno_awaiting += outcome["fno_awaiting_master"]

    # An emailed contract note whose F&O rows wait on a contract-master day: close that
    # gap now, as the upload routes do through the post-import cycle (owner amendment,
    # 8 Oct 2026). Every job above is already committed; a failure here is logged and
    # retried by the next cycle, never raised into the drain.
    fno_heal: dict[str, Any] | None = None
    if fno_awaiting:
        try:
            fno_heal = imports.heal_fno_awaiting_master(conn)
        except Exception:  # noqa: BLE001
            conn.rollback()
            logger.warning("drain_fno_heal_failed", exc_info=True)
            fno_heal = {"error": "heal failed; retried by the next cycle"}

    return {
        "done": done,
        "failed": failed,
        "permanent": permanent,
        "pending": queue.pending_count(conn),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "fno_heal": fno_heal,
    }


def _mark_document_failed(conn: Any, payload: dict[str, Any]) -> None:
    """Flag the document as attempted-and-refused. Best effort, in its OWN transaction.

    Called only after the job's failure has already been committed, so nothing here can
    lose the real reason. It still rolls back explicitly on error: leaving a poisoned
    transaction behind would break the NEXT job in the drain loop, turning one bad document
    into a stalled queue -- the exact failure this module exists to prevent.
    """
    try:
        document_id = _document_id(payload)
    except PermanentJobError:
        return  # nothing to flag; the job row already says why
    try:
        documents.mark_parse_status(conn, document_id, documents.PARSE_FAILED)
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("could not flag document %s as FAILED", document_id, exc_info=True)


def main(argv: list[str] | None = None) -> int:
    """``python -m app.jobs.drain``. The same ``drain_once`` the HTTP route calls.

    Builds its own connection and store from Settings rather than taking them as
    arguments, so running the worker needs nothing but the environment the app already
    needs. Refuses to invent a store: with ``STORAGE_BACKEND`` unset or wrong,
    ``get_store`` raises and this exits non-zero instead of silently writing somewhere
    that is not the archive.
    """
    parser = argparse.ArgumentParser(description="Drain the Trackr+ job queue.")
    parser.add_argument("--budget-seconds", type=float, default=600.0)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--claim-batch", type=int, default=DEFAULT_CLAIM_BATCH)
    parser.add_argument("--investor-id", default=None, help="drain one investor's work only")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--watch", action="store_true", help="keep polling the durable queue")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Imported here, not at module scope: `python -m app.jobs.drain` must not require the
    # web layer's settings to merely import this module (the HTTP route already has them).
    from app.config import get_settings
    from app.db.connection import connect
    from app.storage.factory import get_store

    settings = get_settings()
    store = get_store(settings)

    if args.poll_seconds <= 0 or args.claim_batch <= 0:
        parser.error("poll-seconds and claim-batch must be positive")
    while True:
        with connect(settings.database_url) as conn:
            summary = drain_once(
                conn,
                store,
                limit=args.limit,
                budget_seconds=args.budget_seconds,
                claim_batch=args.claim_batch,
                investor_id=args.investor_id,
            )
        logger.info(
            "drained: %(done)s done, %(failed)s failed (%(permanent)s permanent), "
            "%(pending)s still pending, %(elapsed_seconds)ss",
            summary,
        )
        if not args.watch:
            break
        time.sleep(args.poll_seconds)
    # Waiting for a scheduled retry/continuation is ordinary idle work, not failure.
    return 1 if summary["done"] == 0 and summary["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
