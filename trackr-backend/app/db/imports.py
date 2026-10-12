"""Staged imports: stage, review, promote, discard.

The rule this module exists to enforce: **an adapter never writes to the ledger.** Parsing
produces staged rows and stops. A second explicit call, attributed to a person, promotes
them. No adapter-parsed row reaches the ledger without that human step.

Why that matters more than it looks: a parser that auto-commits is a parser whose bugs land
in a client's cost basis before anyone reads the output. Staging turns a bad parse into a
discarded batch rather than a correction exercise.

ONE NARROW, DELIBERATE EXCEPTION: ``promote_batch`` ends by sweeping the promoted
account for known corporate actions (``app.db.corporate_actions.
apply_known_actions_for_account``), which -- unlike everything above -- writes to the
ledger with no staging and no human promote click. That is not the same category of
write as an adapter's guess at a trade: it only fires for an action that already cleared
three independent, automated checks (sourced, human-VERIFIED at entry time, and its
ratio corroborated by the recorded price move -- see ``app.engine.corporate_actions.
screen``), applied to an eligibility fact (held it before the ex-date) computed from
this account's own ledger, not guessed from a file. The safety argument for why THIS
write does not need a human in the loop is documented once, at
``apply_known_actions_for_account``, rather than repeated here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app import isin as isin_rules
from app import securities as securities_master
from app.db import audit, documents, fno_reference, holdings, precedence, repository
from app.db import identity as db_identity
from app.engine.dedupe import make_dedupe_key, make_fno_dedupe_key
from app.engine.fno_types import FnoSide, FnoTradeKind
from app.engine.types import ImportSummary, Source, Transaction, TxnType
from app.ingest import fno_symbol
from app.ingest.base import (
    CONFIDENCE_DERIVED,
    CONFIDENCE_UNCERTAIN,
    REVIEW_THRESHOLD,
    FnoParsedRow,
    ParseResult,
    Rejection,
    StagedRow,
)
from app.storage.keys import content_hash

logger = logging.getLogger("imports")

ACTION_BATCH_STAGED = "IMPORT_BATCH_STAGED"
ACTION_BATCH_PROMOTED = "IMPORT_BATCH_PROMOTED"
ACTION_BATCH_DISCARDED = "IMPORT_BATCH_DISCARDED"
ACTION_BATCH_REPARSED = "IMPORT_BATCH_REPARSED"
ACTION_ROWS_EXCLUDED = "IMPORT_ROWS_EXCLUDED"

#: Rows at or BELOW this need an explicit acknowledgement before they reach the ledger.
#:
#: Deliberately CONFIDENCE_UNCERTAIN (0.5) and NOT ``REVIEW_THRESHOLD`` (0.9), which is
#: the advisory flag ``review_batch`` uses. Contract-note rows score CONFIDENCE_DERIVED
#: (0.8) because charges are apportioned rather than read, and they promote today with no
#: ceremony -- gating on 0.9 would block every contract note this system has ever parsed.
#: 0.5 is reserved for rows a parser could not vouch for at all.
PROMOTE_FLOOR = CONFIDENCE_UNCERTAIN

STATUS_STAGED = "STAGED"
STATUS_PROMOTED = "PROMOTED"
STATUS_DISCARDED = "DISCARDED"

#: "Is import_row ``r`` already in its ledger?" -- ``transaction`` for an equity row,
#: ``fno_trade`` for an F&O row (migration 064's ``segment``). One predicate for review and
#: promote, so the prediction and the outcome cannot disagree.
_IN_LEDGER = (
    "(CASE r.segment"
    " WHEN 'FNO' THEN EXISTS (SELECT 1 FROM fno_trade f WHERE f.dedupe_key = r.dedupe_key)"
    " ELSE EXISTS (SELECT 1 FROM transaction t WHERE t.dedupe_key = r.dedupe_key) END)"
)


class BatchNotFoundError(LookupError):
    pass


class BatchAlreadyResolvedError(ValueError):
    """Promoted or discarded batches are final. Re-promoting would double-count."""


class UnreviewedRowsError(ValueError):
    """This batch holds rows no parser could vouch for, and nobody has said to take them.

    Raised by ``promote_batch`` BEFORE anything is written, so a refused promotion leaves
    the ledger and the batch exactly as they were.

    Refusing rather than silently skipping is the whole point. A version that promoted
    the confident rows and dropped the rest would report "inserted 7" to somebody who
    uploaded 12, and the five that never made it would be discoverable only by counting.
    An import that partially happened without saying so is worse than one that refused.
    """

    def __init__(self, batch_id: str, count: int) -> None:
        self.batch_id = batch_id
        self.count = count
        super().__init__(
            f"batch {batch_id} has {count} row(s) at or below confidence "
            f"{PROMOTE_FLOOR} that no parser could verify -- promote with "
            f"acknowledge_uncertain=true to accept them"
        )


class DuplicateUploadError(ValueError):
    """This exact file has already been staged for this account."""

    def __init__(self, sha256: str, batch_id: str) -> None:
        self.sha256 = sha256
        self.batch_id = batch_id
        super().__init__(f"identical file already staged as batch {batch_id}")


def _txn_to_payload(txn: Transaction) -> dict[str, Any]:
    return {
        "isin": txn.isin,
        "txn_date": txn.txn_date.isoformat(),
        "txn_type": str(txn.txn_type),
        "quantity": str(txn.quantity),
        "price": str(txn.price),
        "charges": str(txn.charges),
        "stt": str(txn.stt),
        "account_id": txn.account_id,
        "source": str(txn.source) if txn.source else None,
        "source_ref": txn.source_ref,
    }


def _payload_to_txn(payload: dict[str, Any]) -> Transaction:
    return Transaction(
        isin=payload["isin"],
        txn_date=date.fromisoformat(payload["txn_date"]),
        txn_type=TxnType(payload["txn_type"]),
        quantity=Decimal(payload["quantity"]),
        price=Decimal(payload["price"]),
        charges=Decimal(payload.get("charges", "0")),
        stt=Decimal(payload.get("stt", "0")),
        account_id=payload.get("account_id"),
        source=Source(payload["source"]) if payload.get("source") else Source.TRADEBOOK,
        source_ref=payload.get("source_ref"),
    )


#: What a reviewer is told when a row's identifier does not survive ``app.isin``. Keyed by
#: the reason so the message names the actual damage rather than "invalid ISIN", which is
#: the one thing a person fixing the file already knows.
_ISIN_REJECTION = {
    isin_rules.Reason.NOT_A_STRING: "ISIN is not text",
    isin_rules.Reason.TOO_LONG: "ISIN field is too long to be an identifier",
    isin_rules.Reason.MISSING_IDENTIFIER: "row carries no ISIN",
    isin_rules.Reason.SHAPE: "not an ISO 6166 identifier",
    isin_rules.Reason.CHECKSUM: (
        "ISO 6166 check digit failed and no already-known security carries this ISIN"
    ),
}


def _screen_isins(conn: Any, result: ParseResult) -> ParseResult:
    """Refuse a staged row whose ISIN is damaged, before it can key anything.

    CLAUDE.md rule 2 makes the ISIN the identity of a security, so a dropped character does
    not produce an error -- it produces a *second*, phantom security that nothing will ever
    price. ``app.isin.classify`` has existed to catch that since the lineage work landed and
    was wired to nothing; this is the one place every adapter's output passes through, so it
    is the one place the check belongs.

    RUNS BEFORE THE BATCH ROW IS WRITTEN, deliberately: ``import_batch.row_count`` and
    ``rejected_count`` are taken from the ``ParseResult`` this returns, so screening
    afterwards would record counts that contradict the rows actually stored.

    THE CHECKSUM CASE IS NOT A REJECTION ON ITS OWN, per ``app.isin``'s own note: real
    reference data is occasionally dirty, and a checksum failure on a string that exactly
    matches a security this system has already accepted is evidence about the stored row,
    not grounds to discard a holding. Those rows stage at ``CONFIDENCE_DERIVED`` so
    ``review_batch`` surfaces them, and promote without further ceremony. Every other
    failure -- and a checksum failure on an identifier never seen before -- is rejected: no
    lookup can rescue a string that is not shaped like an identifier at all.

    Confidence is only ever lowered, never raised. A row an adapter could not vouch for
    (0.5, below ``PROMOTE_FLOOR``) must not be promoted to 0.8 by passing this screen.
    """
    verdicts = [(staged, isin_rules.classify(staged.transaction.isin)) for staged in result.rows]

    # One lookup for every checksum-failing identifier in the batch, not one per row.
    suspect = sorted(
        {
            verdict.normalized
            for _, verdict in verdicts
            if verdict.reason is isin_rules.Reason.CHECKSUM
        }
    )
    known = securities_master.lookup(conn, suspect) if suspect else {}

    rows: list[StagedRow] = []
    rejections = list(result.rejections)
    for staged, verdict in verdicts:
        if verdict.outcome is isin_rules.Outcome.VALID:
            # The normalized form, not the raw one: classify() strips the zero-width
            # characters and interior spaces that survive PDF extraction, and storing the
            # damaged spelling would key a phantom security on exactly the damage this
            # screen just forgave.
            rows.append(_restated(staged, verdict.normalized, staged.confidence))
        elif verdict.reason is isin_rules.Reason.CHECKSUM and verdict.normalized in known:
            rows.append(
                _restated(staged, verdict.normalized, min(staged.confidence, CONFIDENCE_DERIVED))
            )
        else:
            reason = _ISIN_REJECTION.get(verdict.reason, "unusable ISIN")
            rejections.append(
                Rejection(staged.row_number, f"{reason}: {verdict.raw!r}", staged.raw)
            )

    return ParseResult(
        rows=rows, rejections=rejections, notes=result.notes, fno_rows=result.fno_rows
    )


def _fno_lookup(conn: Any) -> fno_symbol.Lookup:
    """``fno_symbol.resolve``'s lookup, backed by the contract master (migration 063)."""

    def lookup(exchange: str, candidate: fno_symbol.SymbolCandidate, as_of: date) -> list:
        return fno_reference.find_contracts(
            conn,
            exchange,
            candidate.underlying,
            candidate.expiry_from,
            candidate.strike,
            None if candidate.option_type == fno_symbol.FUT else candidate.option_type,
            as_of,
            expiry_to=candidate.expiry_to,
        )

    return lookup


def _stage_fno(
    conn: Any, rows: list[FnoParsedRow], account_id: str
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Resolve each parsed F&O row to exactly one ``fno_contract`` (rule 21(a)).

    Returns ``(staged, rejected)`` as ``import_row`` value tuples: staged =
    (row_number, confidence, dedupe_key, payload); rejected = (row_number, payload,
    reason). A row that does not resolve is REJECTED with its candidates named -- staged
    as an exception, never dropped and never guessed. Where the contract master holds no
    bhavcopy at all for that exchange and day, the payload carries ``awaiting_master`` and
    the reason says so: that is a gap public data closes, so ``heal_fno_awaiting_master``
    (the post-import cycle) backfills the day and re-resolves the row with no operator
    step. Ambiguity is not a gap and is never flagged for healing.
    """
    lookup = _fno_lookup(conn)
    covered: dict[tuple[str, date], bool] = {}
    staged: list[tuple[Any, ...]] = []
    rejected: list[tuple[Any, ...]] = []
    for row in rows:
        parsed = {
            "symbol": row.symbol,
            "exchange": row.exchange,
            "side": str(row.side),
            "quantity": str(row.quantity),
            "price": str(row.price),
            "trade_date": row.trade_date.isoformat(),
            "trade_no": row.trade_no,
            "order_no": row.order_no,
            "contract_number": row.contract_number,
            "charges": str(row.charges),
            "stt": str(row.stt),
            "kind": str(row.kind),
        }
        resolution = fno_symbol.resolve(row.symbol, row.exchange, row.trade_date, lookup)
        candidates = [c.describe() for c in resolution.candidates]
        contract = resolution.contract
        if contract is None:
            reason = resolution.reason or "unresolved"
            key = (row.exchange, row.trade_date)
            awaiting = False
            if not resolution.matches and resolution.candidates:
                if key not in covered:
                    present = fno_reference.fno_price_days_present(
                        conn, row.trade_date, row.trade_date
                    )
                    covered[key] = key in present
                if not covered[key]:
                    awaiting = True
                    reason += (
                        f" -- the F&O contract master holds no {row.exchange} bhavcopy for "
                        f"{row.trade_date} yet; it is fetched and this row re-resolved "
                        "automatically"
                    )
            rejected.append(
                (
                    row.row_number,
                    json.dumps(
                        {
                            "raw": row.raw,
                            "fno": parsed,
                            "candidates": candidates,
                            "awaiting_master": awaiting,
                        }
                    ),
                    reason,
                )
            )
            continue
        dedupe_key = make_fno_dedupe_key(
            account_id=account_id,
            source=row.source,
            source_ref=row.trade_no,
            contract=contract,
            trade_date=row.trade_date,
            side=row.side,
            quantity=row.quantity,
            price=row.price,
        )
        payload = {
            "fno_trade": {
                "account_id": account_id,
                "contract_id": contract.id,
                "trade_date": row.trade_date.isoformat(),
                "side": str(row.side),
                "quantity": str(row.quantity),
                "price": str(row.price),
                "charges": str(row.charges),
                "stt": str(row.stt),
                "kind": str(row.kind),
                "source": str(row.source),
                "source_ref": row.trade_no,
            },
            "contract": {
                "exchange": contract.exchange,
                "instrument_type": contract.instrument_type,
                "underlying": contract.underlying,
                "expiry": contract.expiry.isoformat(),
                "strike": None if contract.strike is None else str(contract.strike),
                "option_type": contract.option_type,
            },
            "raw": {**row.raw, "fno": parsed, "candidates": candidates},
        }
        staged.append((row.row_number, row.confidence, dedupe_key, json.dumps(payload)))
    return staged, rejected


def _insert_rows(
    cur: Any,
    batch_id: str,
    result: ParseResult,
    fno_staged: list[tuple[Any, ...]],
    fno_rejected: list[tuple[Any, ...]],
) -> None:
    """Write one parse's equity rows, F&O rows and rejections into ``import_row``.

    ONE ROUND TRIP PER STATEMENT, NOT PER ROW. This was a `for` loop around `cur.execute`,
    which is 365 sequential round trips for one year's tradebook. Parsing that file takes
    ~6 ms; staging it that way cost 0.7-3.7 s against a remote Postgres -- a hundred times
    the parse, and the dominant term in the whole ingestion path. `executemany` sends the
    batch in one exchange. Measured and written up in docs/BACKFILL_PIPELINE.md section 4.0.
    """
    if result.rows:
        cur.executemany(
            """
            INSERT INTO import_row (batch_id, row_number, confidence, dedupe_key, payload)
            VALUES (%s,%s,%s,%s,%s)
            """,
            [
                (
                    batch_id,
                    staged.row_number,
                    staged.confidence,
                    make_dedupe_key(staged.transaction),
                    json.dumps(
                        {"transaction": _txn_to_payload(staged.transaction), "raw": staged.raw}
                    ),
                )
                for staged in result.rows
            ],
        )
    if result.rejections:
        cur.executemany(
            """
            INSERT INTO import_row (batch_id, row_number, status, payload, reason)
            VALUES (%s,%s,'REJECTED',%s,%s)
            """,
            [
                (
                    batch_id,
                    rejection.row_number,
                    json.dumps({"raw": rejection.raw}),
                    rejection.reason,
                )
                for rejection in result.rejections
            ],
        )
    if fno_staged:
        cur.executemany(
            """
            INSERT INTO import_row (batch_id, row_number, confidence, dedupe_key, payload,
                                    segment)
            VALUES (%s,%s,%s,%s,%s,'FNO')
            """,
            [(batch_id, *values) for values in fno_staged],
        )
    if fno_rejected:
        cur.executemany(
            """
            INSERT INTO import_row (batch_id, row_number, status, payload, reason, segment)
            VALUES (%s,%s,'REJECTED',%s,%s,'FNO')
            """,
            [(batch_id, *values) for values in fno_rejected],
        )


def _restated(staged: StagedRow, normalized: str, confidence: Decimal) -> StagedRow:
    """The same staged row, carrying the normalized ISIN. ``Transaction`` is frozen."""
    if staged.transaction.isin == normalized and staged.confidence == confidence:
        return staged
    return replace(
        staged,
        transaction=replace(staged.transaction, isin=normalized),
        confidence=confidence,
    )


def stage_batch(
    conn: Any,
    adapter: Any,
    content: bytes,
    investor_id: str,
    account_id: str,
    filename: str = "",
    store: Any = None,
    investor_code: str = "",
    actor_uid: str | None = None,
    doctype: str = "TRADEBOOK",
    content_type: str = "text/csv",
    delivery_id: str | None = None,
    email_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse a file into staged rows. Writes NOTHING to ``transaction``.

    ``doctype``/``content_type`` default to the values every caller used before these
    parameters existed (CSV/tradebook adapters) -- existing behaviour is unchanged.
    A caller staging a PDF (contract note, CAS) should pass ``doctype="CONTRACT_NOTE"``
    or ``"CAS"`` and ``content_type="application/pdf"``; passing neither previously
    meant every PDF this system has ever staged was recorded in ``document`` as a
    TRADEBOOK/text-csv, which is wrong metadata on an otherwise-correctly-stored object.

    If ``adapter`` defines ``parse_declared_holdings`` (currently only
    app.ingest.cas_pdf.CasPdfAdapter), its result is written to ``holding_snapshot`` via
    app.db.holdings.record_snapshot after staging succeeds -- centralized here so every
    caller (email and upload) gets it without duplicating the glue. This is separate
    from the staged-rows-and-review flow: holding_snapshot carries no promote gate (see
    app.db.holdings' docstring), so this write happens unconditionally, not gated on
    review.

    Raises:
        DuplicateUploadError: this exact file was staged before, for this account.
            Caught on the hash, before parsing, because re-parsing a known file to
            discover it is known is wasted work on the common path -- clients send
            overlapping exports routinely.

            Scoped to the destination account, matching dedupe_key's account_id term:
            matched globally instead, another tenant's identical upload blocked this one
            and named a batch the ownership guard would refuse to serve -- a cross-tenant
            dead end that leaked someone else's batch id. A file hash identifies bytes,
            not a tenant.
        app.db.identity.IdentityMismatchError: the document names an owner who is not
            this account's -- raised before anything is stored or inserted, so a
            refused document leaves no rows and no stored object behind. Only adapters
            defining ``identity`` can raise this; see app.ingest.base.IdentifyingAdapter.
    """
    digest = content_hash(content)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM import_batch WHERE sha256 = %s AND account_id = %s",
            (digest, account_id),
        )
        existing = cur.fetchone()
    if existing:
        raise DuplicateUploadError(digest, str(existing[0]))

    result: ParseResult = adapter.parse(content, account_id=account_id, filename=filename)

    # Every adapter's output converges here, so this is where a damaged identifier is
    # caught -- once, rather than in each parser with a different idea of what an ISIN is.
    # Must precede the import_batch insert below, which reads its counts off this result.
    result = _screen_isins(conn, result)

    # Identity check BEFORE store_document: a note belonging to somebody else must not be
    # written into this investor's document store on its way to being refused. Optional
    # protocol -- an adapter whose source carries no PAN (every tradebook CSV) simply does
    # not define ``identity`` and is skipped, exactly as parse_declared_holdings is below.
    identity_notes: list[str] = []
    if hasattr(adapter, "identity"):
        identity_notes = db_identity.verify(
            conn,
            adapter.identity(content),
            investor_id=investor_id,
            account_id=account_id,
            actor_uid=actor_uid,
        )

    document_id = None
    if store is not None:
        stored = documents.store_document(
            conn,
            store,
            investor_id=investor_id,
            investor_code=investor_code or "UNKNOWN",
            doctype=doctype,
            content=content,
            received=date.today(),
            filename=filename,
            content_type=content_type,
            account_id=account_id,
            actor_uid=actor_uid,
            email_meta=email_meta,
        )
        document_id = str(stored["id"])

    # F&O rows resolve to a contract here, in the DB layer -- the parser stopped at the
    # display symbol (CLAUDE.md rule 5). Counted into the batch like any other row.
    fno_staged, fno_rejected = _stage_fno(conn, result.fno_rows, account_id)
    parsed = result.parsed + len(fno_staged)
    rejected = result.rejected + len(fno_rejected)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO import_batch (investor_id, account_id, adapter, filename, sha256,
                                      document_id, row_count, rejected_count, notes,
                                      delivery_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                investor_id,
                account_id,
                adapter.name,
                filename or None,
                digest,
                document_id,
                parsed,
                rejected,
                json.dumps(result.notes + identity_notes),
                delivery_id,
            ),
        )
        batch_id = str(cur.fetchone()[0])
        _insert_rows(cur, batch_id, result, fno_staged, fno_rejected)

    audit.record(
        conn,
        ACTION_BATCH_STAGED,
        "import_batch",
        batch_id,
        actor_uid,
        {
            "adapter": adapter.name,
            "filename": filename,
            "parsed": parsed,
            "rejected": rejected,
        },
    )

    declared_holdings = 0
    parse_declared_holdings = getattr(adapter, "parse_declared_holdings", None)
    if parse_declared_holdings is not None:
        declared = parse_declared_holdings(content, account_id)
        declared_holdings = holdings.record_snapshot(conn, declared)
        if declared_holdings:
            _replay_for_statement(conn, account_id)

    return {
        "batch_id": batch_id,
        "parsed": parsed,
        "rejected": rejected,
        "fno_parsed": len(fno_staged),
        "fno_awaiting_master": sum(
            1 for _, p, _ in fno_rejected if json.loads(p).get("awaiting_master")
        ),
        "sha256": digest,
        "document_id": document_id,
        "notes": result.notes,
        "declared_holdings": declared_holdings,
    }


def _replay_for_statement(conn: Any, account_id: Any) -> None:
    """A CAS's declared quantities can confirm or contradict an auto-resolved row (rule
    20f, app.db.auto_reversal), but they are not transactions, so the insert trigger that
    queues a replay never fires for them. Queue it here."""
    from app.db import reference_sync

    key = str(account_id)
    reference_sync.enqueue(conn, "REPLAY", key, {"account_id": key}, refresh=True)


def reparse_batch(
    conn: Any, store: Any, batch_id: str, adapter: Any, actor_uid: str | None = None
) -> dict[str, Any]:
    """Re-run ``adapter.parse()`` against a STAGED batch's ORIGINALLY STORED bytes,
    replacing its staged/rejected rows with the fresh result.

    This is the recovery path docs/INGESTION.md's "Where documents land" section
    promises but nothing implemented until now: "storing before parsing is what
    makes a parser bug survivable... the fix is to reparse the originals." Without
    it, the only way to retry a document is re-uploading it -- and ``stage_batch``'s
    duplicate check is a permanent per-account sha256 lookup with no status filter,
    so re-uploading identical bytes returns the SAME batch, forever, no matter what
    changed since (a PAN added after the fact, a parser bug fixed after the fact).
    Real case this was built for: a CAS PDF staged before the investor's PAN was on
    file rejects with "could not unlock" -- adding the PAN afterward does nothing
    until something actually re-parses the stored original against it.

    Only a STAGED batch may be reparsed, matching promote_batch/discard_batch's own
    guard: a promoted batch's rows are the ledger's provenance record and must not
    change under it; a discarded batch is deliberately final.

    Raises:
        BatchNotFoundError: no such batch.
        BatchAlreadyResolvedError: already promoted or discarded.
        ValueError: the batch has no stored document (only possible if it was staged
            without a ``store``, which no real caller does).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, account_id, filename, document_id, adapter, sha256 "
            # FOR UPDATE: the same batch lock promote and the F&O heal take, so a reparse
            # never interleaves with either (see HEAL_LOCK_KEY).
            "FROM import_batch WHERE id = %s FOR UPDATE",
            (batch_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise BatchNotFoundError(batch_id)
        batch_status, account_id, filename, document_id, adapter_name, sha256 = row
        if batch_status != STATUS_STAGED:
            raise BatchAlreadyResolvedError(f"batch {batch_id} is {batch_status}")
        if document_id is None:
            raise ValueError(f"batch {batch_id} has no stored document to reparse")
        if adapter.name != adapter_name:
            raise ValueError(
                f"batch {batch_id} was staged with adapter {adapter_name!r}, not {adapter.name!r}"
            )

        cur.execute("SELECT storage_key FROM document WHERE id = %s", (document_id,))
        (storage_key,) = cur.fetchone()

    content = store.get(storage_key)
    result: ParseResult = adapter.parse(
        content, account_id=str(account_id), filename=filename or ""
    )

    # Screened here too, for the same reason stage_batch screens: this REPLACES the
    # batch's rows, so an unscreened reparse would be a way to land a damaged identifier
    # that the original staging had already refused.
    result = _screen_isins(conn, result)
    # Re-resolved too: an F&O row rejected because the contract master lacked that day
    # resolves once it has been backfilled -- the recovery path _stage_fno names.
    fno_staged, fno_rejected = _stage_fno(conn, result.fno_rows, str(account_id))
    parsed = result.parsed + len(fno_staged)
    rejected = result.rejected + len(fno_rejected)

    with conn.cursor() as cur:
        # REPLACED, not appended: a reparse answers "what would this document produce
        # now", not "union of every attempt". Safe to delete unconditionally -- the
        # STAGED-only guard above means nothing here has been promoted, so no row
        # being removed is the ledger's provenance record for anything live.
        cur.execute("DELETE FROM import_row WHERE batch_id = %s", (batch_id,))
        _insert_rows(cur, batch_id, result, fno_staged, fno_rejected)
        cur.execute(
            "UPDATE import_batch SET notes = %s WHERE id = %s",
            (json.dumps(result.notes), batch_id),
        )
    # Counts from the rows just written, never from arithmetic on the old ones.
    recompute_batch_counts(conn, batch_id)

    declared_holdings = 0
    parse_declared_holdings = getattr(adapter, "parse_declared_holdings", None)
    if parse_declared_holdings is not None:
        declared = parse_declared_holdings(content, str(account_id))
        declared_holdings = holdings.record_snapshot(conn, declared)
        if declared_holdings:
            _replay_for_statement(conn, account_id)

    audit.record(
        conn,
        ACTION_BATCH_REPARSED,
        "import_batch",
        batch_id,
        actor_uid,
        {"adapter": adapter.name, "parsed": parsed, "rejected": rejected},
    )
    return {
        "batch_id": batch_id,
        "parsed": parsed,
        "rejected": rejected,
        "fno_parsed": len(fno_staged),
        "fno_awaiting_master": sum(
            1 for _, p, _ in fno_rejected if json.loads(p).get("awaiting_master")
        ),
        "sha256": sha256,
        "document_id": str(document_id),
        "notes": result.notes,
        "declared_holdings": declared_holdings,
    }


def batch_summary(conn: Any, batch_id: str) -> dict[str, Any] | None:
    """Status and counts of one batch, or None. Lets a re-upload of a file whose batch is
    still STAGED resume it rather than report it as already imported."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status::text, row_count, rejected_count FROM import_batch WHERE id = %s",
            (batch_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"status": row[0], "row_count": row[1], "rejected_count": row[2]}


def list_batches(
    conn: Any, investor_id: str, batch_status: str | None = "STAGED"
) -> list[dict[str, Any]]:
    """The batches one investor can see, newest first -- the answer to "where is that
    batch id?" that does not require keeping the upload response around. ``None`` lists
    every status; anything else filters on ``import_batch_status`` (invalid values are a
    caller bug and surface as a Postgres error)."""
    query = (
        "SELECT id, filename, adapter, status::text, row_count, rejected_count, uploaded_at "
        "FROM import_batch WHERE investor_id = %s"
    )
    params: list[Any] = [investor_id]
    if batch_status is not None:
        query += " AND status = %s"
        params.append(batch_status)
    query += " ORDER BY uploaded_at DESC, id"

    with conn.cursor() as cur:
        cur.execute(query, params)
        return [
            {
                "batch_id": str(row[0]),
                "filename": row[1],
                "adapter": row[2],
                "status": row[3],
                "row_count": row[4],
                "rejected_count": row[5],
                "uploaded_at": row[6],
            }
            for row in cur.fetchall()
        ]


def review_batch(conn: Any, batch_id: str) -> dict[str, Any]:
    """What a human reads before deciding. Changes nothing.

    Predicts the duplicates promotion will skip, rather than leaving them to be discovered
    afterwards. "23 of your 115 rows are already in the ledger" is a fact worth knowing
    BEFORE clicking promote, not an explanation offered after.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, adapter, filename, row_count, rejected_count, notes, "
            "uploaded_at FROM import_batch WHERE id = %s",
            (batch_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise BatchNotFoundError(batch_id)
        batch = dict(zip([c.name for c in cur.description], row, strict=True))

        cur.execute(
            "SELECT row_number, reason FROM import_row "
            "WHERE batch_id = %s AND status = 'REJECTED' ORDER BY row_number",
            (batch_id,),
        )
        rejections = [{"row_number": r[0], "reason": r[1]} for r in cur.fetchall()]

        cur.execute(
            "SELECT row_number, confidence FROM import_row "
            "WHERE batch_id = %s AND status = 'STAGED' AND confidence <= %s "
            "ORDER BY confidence, row_number",
            (batch_id, REVIEW_THRESHOLD),
        )
        needs_review = [{"row_number": r[0], "confidence": r[1]} for r in cur.fetchall()]

        cur.execute(
            f"""
            SELECT count(*) FROM import_row r
            WHERE r.batch_id = %s AND r.status = 'STAGED' AND {_IN_LEDGER}
            """,
            (batch_id,),
        )
        already_present = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FROM import_row WHERE batch_id = %s AND status = 'STAGED'",
            (batch_id,),
        )
        staged = cur.fetchone()[0]

    return {
        **batch,
        "staged": staged,
        "will_insert": staged - already_present,
        "will_skip_as_duplicate": already_present,
        "rejections": rejections,
        "needs_review": needs_review,
    }


def promote_batch(
    conn: Any, batch_id: str, actor_uid: str, acknowledge_uncertain: bool = False
) -> ImportSummary:
    """The second explicit action. Writes staged rows to the ledger.

    Requires ``actor_uid``: an unattributed promotion of client financial data is a record
    that fails exactly when somebody asks who did it. The database enforces this too.

    Reuses ``db.repository.save_transactions``, so the UNIQUE INDEX on ``dedupe_key``
    stays the enforcement rather than this module reimplementing it.

    F&O rows (``segment = 'FNO'``) go to ``fno_trade`` through ``_promote_fno`` with the
    same unique-key enforcement, and never to ``transaction``. They skip source
    precedence (only contract notes carry F&O) and are counted into the returned
    ``inserted`` / ``skipped_duplicates``.

    ``acknowledge_uncertain`` defaults False, so every existing caller keeps its exact
    behaviour: rows at CONFIDENCE_DERIVED (0.8) and CONFIDENCE_CERTAIN (1.0) promote
    untouched. It gates only rows at or below ``PROMOTE_FLOOR`` -- today that means rows
    a parser could not verify against the document they came from (app/ingest/llm_verify).

    It is batch-level, not row-level: acknowledging accepts every uncertain row in the
    batch at once. A reviewer who wants to accept four of five has to discard and re-stage.
    That is a real limitation and a deliberate one -- the per-row APPROVED transition it
    would take instead is a schema change, an endpoint, and a UI, and none of that should
    be smuggled in as a side effect of adding a parser.

    Raises:
        BatchAlreadyResolvedError: already promoted or discarded. Re-promoting would be a
            no-op at the ledger thanks to the unique index, but it would misreport counts
            and imply a second review that never happened.
        UnreviewedRowsError: the batch holds rows at or below ``PROMOTE_FLOOR`` and
            ``acknowledge_uncertain`` is False. Counted and raised before any write, so
            the ledger and the batch are untouched.
    """
    if not actor_uid:
        raise ValueError("promotion requires an actor: who is putting this in the ledger?")

    with conn.cursor() as cur:
        # FOR UPDATE: a concurrent F&O heal (or a second promote) waits for this one, so
        # no row turns STAGED inside a batch that is being promoted (2026-10-08 race).
        cur.execute(
            "SELECT status FROM import_batch WHERE id = %s FOR UPDATE", (batch_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise BatchNotFoundError(batch_id)
        if row[0] != STATUS_STAGED:
            raise BatchAlreadyResolvedError(f"batch {batch_id} is {row[0]}")

        # Counted before anything is written, and before the rows are even read: a
        # refusal must leave no trace beyond the exception itself.
        cur.execute(
            "SELECT count(*) FROM import_row "
            "WHERE batch_id = %s AND status = 'STAGED' AND confidence <= %s",
            (batch_id, PROMOTE_FLOOR),
        )
        uncertain = cur.fetchone()[0]
        if uncertain and not acknowledge_uncertain:
            raise UnreviewedRowsError(batch_id, uncertain)

        cur.execute(
            "SELECT id, payload FROM import_row "
            "WHERE batch_id = %s AND status = 'STAGED' AND segment = 'EQ' ORDER BY row_number",
            (batch_id,),
        )
        staged = cur.fetchall()
        cur.execute(
            "SELECT dedupe_key, payload FROM import_row "
            "WHERE batch_id = %s AND status = 'STAGED' AND segment = 'FNO' ORDER BY row_number",
            (batch_id,),
        )
        fno_staged = cur.fetchall()

    candidates = [(row_id, _payload_to_txn(p["transaction"])) for row_id, p in staged]

    # SOURCE PRECEDENCE. A contract note and a tradebook describe the same trades, and
    # dedupe_key does not catch that (see db/precedence.py). Without this guard, importing
    # both double-counts - measured at 146 trades on this project's own files. Interim
    # measure; the real fix is a source-agnostic dedupe_key. See docs/DATA_MODEL.md.
    account_id = staged[0][1]["transaction"].get("account_id") if staged else None
    known = precedence.existing_identities(conn, account_id) if account_id else {}

    to_insert: list[Transaction] = []
    excluded: list[tuple[str, str]] = []
    superseded: list[tuple[str, str, str]] = []

    for row_id, txn in candidates:
        identity = precedence.trade_identity(txn)
        existing = known.get(identity) if identity else None
        decision = precedence.decide(
            txn, existing[0] if existing else None, existing[1] if existing else None
        )
        if decision.action == precedence.ACTION_EXCLUDE:
            excluded.append((str(row_id), decision.reason))
        elif decision.action == precedence.ACTION_SUPERSEDE:
            superseded.append((str(row_id), decision.reason, decision.superseded_id))
            to_insert.append(txn)
        else:
            to_insert.append(txn)

    # A superseded row cannot be deleted - the ledger is append-only - so it is REVERSED,
    # which is the correction pattern DATA_MODEL already specifies. The history of what was
    # believed, and when it changed, survives.
    reversals = _reversals_for(conn, [sid for _, _, sid in superseded])
    transactions = reversals + to_insert

    # transaction.isin references security, so the master has to know these ISINs before
    # any row can land. Requiring it to be pre-populated would mean no import could ever be
    # the first thing to mention a security, which is backwards: the import IS how a new
    # holding is discovered. The symbol comes from the source row and is display-only, so
    # an existing name is never overwritten by a later file's spelling of it.
    securities = {p["transaction"]["isin"]: (p.get("raw") or {}).get("symbol") for _, p in staged}
    with conn.cursor() as cur:
        for isin, symbol in sorted(securities.items()):
            cur.execute(
                """
                INSERT INTO security (isin, symbol) VALUES (%s, %s)
                ON CONFLICT (isin) DO UPDATE
                SET symbol = COALESCE(security.symbol, EXCLUDED.symbol)
                """,
                (isin, symbol),
            )

    summary = repository.save_transactions(conn, transactions, actor_uid=actor_uid)
    fno_inserted = _promote_fno(conn, fno_staged)
    summary = replace(
        summary,
        inserted=summary.inserted + fno_inserted,
        skipped_duplicates=summary.skipped_duplicates + len(fno_staged) - fno_inserted,
    )

    with conn.cursor() as cur:
        for row_id, reason in excluded:
            cur.execute(
                "UPDATE import_row SET status = 'EXCLUDED', reason = %s WHERE id = %s",
                (reason, row_id),
            )
        for row_id, reason, _ in superseded:
            cur.execute(
                "UPDATE import_row SET status = 'SUPERSEDED', reason = %s WHERE id = %s",
                (reason, row_id),
            )
        cur.execute(
            f"""
            UPDATE import_row r
            SET status = CASE
                WHEN {_IN_LEDGER} THEN 'PROMOTED'::import_row_status
                ELSE 'DUPLICATE'::import_row_status
            END
            WHERE r.batch_id = %s AND r.status = 'STAGED'
            """,
            (batch_id,),
        )
        cur.execute(
            "UPDATE import_batch SET status = 'PROMOTED', promoted_at = now(), "
            "promoted_by = %s WHERE id = %s",
            (actor_uid, batch_id),
        )

    if excluded or superseded:
        # Logged as its own audit event, not folded into the promotion summary. Somebody
        # asking "why is this trade not in the ledger?" must be able to find the answer.
        audit.record(
            conn,
            ACTION_ROWS_EXCLUDED,
            "import_batch",
            batch_id,
            actor_uid,
            {
                "excluded": [{"row": r, "reason": why} for r, why in excluded],
                "superseded": [{"row": r, "reason": why} for r, why, _ in superseded],
            },
        )

    audit.record(
        conn,
        ACTION_BATCH_PROMOTED,
        "import_batch",
        batch_id,
        actor_uid,
        {
            "inserted": summary.inserted,
            "skipped_duplicates": summary.skipped_duplicates,
            "excluded_by_precedence": len(excluded),
            "superseded": len(superseded),
            # A COUNT, never a value -- audit.py: "diff must never contain a PAN, a demat
            # number or ciphertext". This is also the record that answers, later, whether
            # a human was told these rows came from a parser that could not verify them:
            # the SEBI AI-usage disclosure obligation in docs/COMPLIANCE.md section 2.
            "acknowledged_uncertain": uncertain if acknowledge_uncertain else 0,
            # Already inside inserted/skipped_duplicates; named so the F&O share of a
            # promotion is answerable later (fno_trade, never transaction).
            "fno_inserted": fno_inserted,
        },
    )

    # Every fresh import is exactly the moment new ISINs (or new pre-action history for
    # an existing one) enter this account's ledger -- so it is also the moment to check
    # them against every VERIFIED corporate action already on file, automatically, no
    # separate step. Local import: app.db.corporate_actions imports FROM this module
    # (ACTION_BATCH_STAGED, _txn_to_payload) already, so a module-level import here
    # would be a cycle. Best-effort, deliberately: a corporate-action sweep failing must
    # never turn a successful trade promotion into a failed one -- the trades this
    # function exists for are the important write, and the sweep can simply run again
    # next time (it is idempotent, see apply_known_actions_for_account's docstring).
    if account_id:
        from app.db.corporate_actions import apply_known_actions_for_account

        try:
            apply_known_actions_for_account(conn, account_id, actor_uid=actor_uid)
        except Exception:
            logger.warning(
                "corporate-action auto-apply sweep failed for account %s after promoting "
                "batch %s -- trades are safe, promoted normally; the sweep will retry on "
                "the next import",
                account_id, batch_id, exc_info=True,
            )

    return summary


def _promote_fno(conn: Any, staged: list[tuple[str, dict[str, Any]]]) -> int:
    """Append staged F&O rows to ``fno_trade``; return how many were new.

    ``ON CONFLICT (dedupe_key) DO NOTHING``, as ``save_transactions`` does for equity: the
    unique index is the enforcement, so re-importing a note (or its PDF after its XLSX) is
    a no-op. Never touches ``transaction`` (rule 21(b)).
    """
    inserted = 0
    with conn.cursor() as cur:
        for dedupe_key, payload in staged:
            t = payload["fno_trade"]
            cur.execute(
                """
                INSERT INTO fno_trade (account_id, contract_id, trade_date, side, quantity,
                                       price, charges, stt, kind, source, source_ref,
                                       dedupe_key, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (dedupe_key) DO NOTHING
                """,
                (
                    t["account_id"],
                    t["contract_id"],
                    t["trade_date"],
                    t["side"],
                    Decimal(t["quantity"]),
                    Decimal(t["price"]),
                    Decimal(t["charges"]),
                    Decimal(t["stt"]),
                    t["kind"],
                    t["source"],
                    t["source_ref"],
                    dedupe_key,
                    json.dumps({"contract": payload["contract"], "raw": payload["raw"]}),
                ),
            )
            inserted += cur.rowcount
    return inserted


ACTION_FNO_ROWS_HEALED = "IMPORT_FNO_ROWS_HEALED"

_AWAITING = (
    "r.segment = 'FNO' AND r.status = 'REJECTED' "
    "AND (r.payload->>'awaiting_master')::boolean IS TRUE"
)


def fno_rows_awaiting_master(conn: Any, batch_id: str | None = None) -> int:
    """F&O rows rejected only because the contract master lacked their day -- in one
    batch, or (``None``) in every batch still open to healing."""
    query = (
        f"SELECT count(*) FROM import_row r JOIN import_batch b ON b.id = r.batch_id "
        f"WHERE {_AWAITING} AND b.status IN ('STAGED', 'PROMOTED')"
    )
    params: tuple[Any, ...] = ()
    if batch_id is not None:
        query += " AND r.batch_id = %s"
        params = (batch_id,)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()[0]


def _awaiting_row(row_number: int, payload: dict[str, Any]) -> FnoParsedRow:
    """The parsed F&O row a coverage rejection stored, rebuilt for re-resolution."""
    f = payload["fno"]
    return FnoParsedRow(
        row_number=row_number,
        symbol=f["symbol"],
        exchange=f["exchange"],
        side=FnoSide(f["side"]),
        quantity=Decimal(f["quantity"]),
        price=Decimal(f["price"]),
        trade_date=date.fromisoformat(f["trade_date"]),
        trade_no=f["trade_no"],
        order_no=f["order_no"],
        contract_number=f["contract_number"],
        charges=Decimal(f["charges"]),
        stt=Decimal(f["stt"]),
        kind=FnoTradeKind(f["kind"]),
        raw=payload.get("raw") or {},
    )


#: The retry cap for a contract-master day that will not arrive (owner-delegated numbers,
#: 2026-10-08). A row is re-tried with exponential backoff -- attempt n waits
#: min(2^(n-1), HEAL_BACKOFF_CAP_HOURS) hours after the previous failed attempt: 1, 2, 4,
#: 8, 16, 24 h -- and after HEAL_MAX_ATTEMPTS failed attempts it stops waiting and becomes
#: a visible REJECTED row with the reason, never a silent forever-retry.
HEAL_MAX_ATTEMPTS = 6
HEAL_BACKOFF_CAP_HOURS = 24
#: A trade this recent may simply not be published yet: it backs off but is never capped.
HEAL_RECENT_DAYS = 7
#: What the last fetch for a row's (exchange, day) said. NOT_PUBLISHED: the exchange
#: answered that no file exists (404 / closed day / a weekend). FETCH_FAILED: anything
#: transient -- network, a malformed file, a tier not built.
OUTCOME_NOT_PUBLISHED = "NOT_PUBLISHED"
OUTCOME_FETCH_FAILED = "FETCH_FAILED"
ACTION_FNO_ROWS_UNHEALABLE = "IMPORT_FNO_ROWS_UNHEALABLE"


def heal_backoff(attempts: int) -> timedelta:
    """The wait after the ``attempts``-th failed attempt: 1, 2, 4, 8, 16, then 24 h."""
    return timedelta(hours=min(2 ** (attempts - 1), HEAL_BACKOFF_CAP_HOURS))


def _fetch_outcome(conn: Any, exchange: str, day: date, summaries: list[dict]) -> str | None:
    """None when the day is now stored; otherwise why not (see OUTCOME_*)."""
    if not summaries:
        # Nothing was fetched: the day was already stored, or it is not a weekday.
        present = fno_reference.fno_price_days_present(conn, day, day)
        return None if (exchange, day) in present else OUTCOME_NOT_PUBLISHED
    summary = summaries[0]
    if exchange in summary.get("not_published", []):
        return OUTCOME_NOT_PUBLISHED
    if exchange in summary.get("failures", {}) or exchange in summary.get("not_covered", {}):
        return OUTCOME_FETCH_FAILED
    return None


#: pg advisory-lock key for healing: one heal at a time across every process. Several
#: post-import cycles run at once (each stage and each promote schedules one); concurrent
#: heals re-processed the same rows and deadlocked in the contract upsert (2026-10-08).
HEAL_LOCK_KEY = 7_401_064_001


def recompute_batch_counts(conn: Any, batch_id: str | None = None) -> int:
    """Set ``row_count`` / ``rejected_count`` from the batch's actual rows: every row
    that is not REJECTED, and every REJECTED row -- exactly what ``stage_batch`` counts
    when it writes them. One batch, or (``None``) every batch. Returns how many batches
    changed. Does not commit. Safe to re-run: it only ever converges on the rows."""
    where = "WHERE b.id = %s" if batch_id is not None else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH actual AS (
                SELECT b.id,
                       count(r.id) FILTER (WHERE r.status <> 'REJECTED') AS rows_ok,
                       count(r.id) FILTER (WHERE r.status = 'REJECTED') AS rows_rejected
                FROM import_batch b LEFT JOIN import_row r ON r.batch_id = b.id
                {where}
                GROUP BY b.id
            )
            UPDATE import_batch b SET row_count = a.rows_ok, rejected_count = a.rows_rejected
            FROM actual a
            WHERE b.id = a.id
              AND (b.row_count <> a.rows_ok OR b.rejected_count <> a.rows_rejected)
            """,
            (batch_id,) if batch_id is not None else (),
        )
        return cur.rowcount


def heal_fno_awaiting_master(
    conn: Any,
    sources_by_exchange: dict[str, Any] | None = None,
    notifier: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Close the one F&O staging gap public data can close (owner amendment, 8 Oct 2026):
    an F&O row rejected because the contract master held no bhavcopy for its day.

    1. Only rows that are DUE (``payload.heal_next_at`` absent or not in the future, by
       ``now``) are worked; a row not yet due is skipped without fetching.
    2. Backfill exactly the (exchange, day) pairs the due rows need
       (``fno_reference.backfill_fno_prices``, resumable: a stored day is not fetched).
    3. Re-resolve each due row with the same ``_stage_fno`` staging uses:
       - resolves -> STAGED in a STAGED batch (promoted by the human promote as usual), or,
         in a batch a human has ALREADY promoted, appended to ``fno_trade`` at once with
         the same unique-key enforcement and an audit event. Owner-confirmed 2026-10-08:
         the original approval covers the healed row; only the public reference data was
         late;
       - still unresolved on a covered day (zero or several contracts) -> stays REJECTED,
         reason rewritten with the candidates, no longer flagged: ambiguity is not a gap;
       - day still not covered -> a failed attempt: ``heal_attempts`` + 1,
         ``heal_next_at`` = now + ``heal_backoff``. After ``HEAL_MAX_ATTEMPTS`` for a trade
         older than ``HEAL_RECENT_DAYS``, the flag is cleared and the row stays a visible
         REJECTED naming why (not published / could not be fetched) and how to recover,
         with an audit event.

    Idempotent: a run with nothing flagged returns ``{"awaiting": 0}`` and fetches
    nothing. A fetch failure is logged and never raised. ``now`` is injectable (UTC).

    CONCURRENCY. One heal at a time (session advisory lock ``HEAL_LOCK_KEY``): a second
    caller returns ``{"skipped_locked": True}`` at once. Each batch is then processed
    under ``SELECT ... FOR UPDATE`` on its ``import_batch`` row (the lock
    ``promote_batch`` also takes first), each row is re-checked under its own lock, and
    the batch's counts are recomputed from its rows, never incremented. Committed per
    batch. A STAGED F&O row left inside an already-PROMOTED batch (the race this lock
    closes) is promoted into ``fno_trade`` here too.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (HEAL_LOCK_KEY,))
        got = cur.fetchone()[0]
    conn.commit()
    if not got:
        logger.info("fno_heal_skipped_locked")
        return {"skipped_locked": True}
    try:
        return _heal_locked(conn, sources_by_exchange, notifier, now or datetime.now(UTC))
    finally:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (HEAL_LOCK_KEY,))
            conn.commit()
        except Exception:  # noqa: BLE001 - a lost connection releases a session lock itself
            logger.warning("fno_heal_unlock_failed", exc_info=True)


def _heal_locked(
    conn: Any, sources_by_exchange: dict[str, Any] | None, notifier: Any, now: datetime
) -> dict[str, Any]:
    from app.prices import fno_sources

    stranded = _promote_stranded_fno(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.id, r.batch_id, b.account_id, r.row_number, r.payload
            FROM import_row r JOIN import_batch b ON b.id = r.batch_id
            WHERE {_AWAITING} AND b.status IN ('STAGED', 'PROMOTED')
            ORDER BY r.batch_id, r.row_number
            """
        )
        rows = cur.fetchall()
    conn.commit()
    if not rows:
        return {"awaiting": 0, **({"stranded_promoted": stranded} if stranded else {})}
    due = [
        r
        for r in rows
        if not r[4].get("heal_next_at")
        or datetime.fromisoformat(r[4]["heal_next_at"]) <= now
    ]

    if sources_by_exchange is None:
        sources_by_exchange = {
            "NSE": fno_sources.nse_fo_source(),
            "BSE": fno_sources.bse_fo_source(),
        }
    needed = sorted(
        {(p["fno"]["exchange"], date.fromisoformat(p["fno"]["trade_date"])) for *_, p in due}
    )
    outcomes: dict[tuple[str, date], str | None] = {}
    for exchange, day in needed:
        source = sources_by_exchange.get(exchange)
        if source is None:
            outcomes[(exchange, day)] = OUTCOME_FETCH_FAILED
            continue
        try:
            summaries = fno_reference.backfill_fno_prices(
                conn, day, day, sources=[source], notifier=notifier
            )
            outcomes[(exchange, day)] = _fetch_outcome(conn, exchange, day, summaries)
        except Exception:  # an exchange outage must not stop the rest; retried with backoff
            conn.rollback()
            outcomes[(exchange, day)] = OUTCOME_FETCH_FAILED
            logger.warning("fno_master_backfill_failed %s %s", exchange, day, exc_info=True)

    totals = {"healed": 0, "still_rejected": 0, "retrying": 0, "unhealable": 0}
    by_batch: dict[str, list[tuple]] = {}
    for row in due:
        by_batch.setdefault(str(row[1]), []).append(row)
    for batch_id, batch_rows in by_batch.items():
        try:
            counts = _heal_batch(conn, batch_id, batch_rows, outcomes, now)
            conn.commit()
        except Exception:  # one batch's failure must not stop the others; retried later
            conn.rollback()
            logger.warning("fno_heal_batch_failed %s", batch_id, exc_info=True)
            continue
        for key, value in counts.items():
            totals[key] += value
    logger.info("fno_heal_done %s", totals)
    return {
        "awaiting": len(rows),
        "due": len(due),
        "not_due": len(rows) - len(due),
        "days": len(needed),
        "fetch_failures": sum(1 for o in outcomes.values() if o == OUTCOME_FETCH_FAILED),
        **totals,
        **({"stranded_promoted": stranded} if stranded else {}),
    }


def _lock_batch(cur: Any, batch_id: str) -> str | None:
    cur.execute("SELECT status::text FROM import_batch WHERE id = %s FOR UPDATE", (batch_id,))
    found = cur.fetchone()
    return found[0] if found else None


def _promote_stranded_fno(conn: Any) -> int:
    """STAGED F&O rows inside a PROMOTED batch -- left by a heal that raced a promote
    before ``HEAL_LOCK_KEY`` and the batch lock existed. The human approved the batch, so
    they go to ``fno_trade`` like any healed row (owner-confirmed 2026-10-08)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT r.batch_id FROM import_row r JOIN import_batch b "
            "ON b.id = r.batch_id WHERE r.segment = 'FNO' AND r.status = 'STAGED' "
            "AND b.status = 'PROMOTED' ORDER BY r.batch_id"
        )
        batches = [str(r[0]) for r in cur.fetchall()]
    promoted = 0
    for batch_id in batches:
        with conn.cursor() as cur:
            if _lock_batch(cur, batch_id) != STATUS_PROMOTED:
                conn.rollback()
                continue
            cur.execute(
                "SELECT id, dedupe_key, payload FROM import_row WHERE batch_id = %s "
                "AND segment = 'FNO' AND status = 'STAGED' ORDER BY row_number FOR UPDATE",
                (batch_id,),
            )
            stuck = cur.fetchall()
            _promote_fno(conn, [(key, payload) for _, key, payload in stuck])
            cur.execute(
                f"""
                UPDATE import_row r SET status = CASE
                    WHEN {_IN_LEDGER} THEN 'PROMOTED'::import_row_status
                    ELSE 'DUPLICATE'::import_row_status END
                WHERE r.id = ANY(%s)
                """,
                ([row_id for row_id, _, _ in stuck],),
            )
            recompute_batch_counts(conn, batch_id)
            audit.record(
                conn, ACTION_FNO_ROWS_HEALED, "import_batch", batch_id, None,
                {"rows": len(stuck), "reason": "STAGED F&O rows in a promoted batch promoted"},
            )
        conn.commit()
        promoted += len(stuck)
    return promoted


def _heal_batch(
    conn: Any,
    batch_id: str,
    batch_rows: list[tuple],
    outcomes: dict[tuple[str, date], str | None],
    now: datetime,
) -> dict[str, int]:
    """Re-resolve one batch's due rows under the batch lock; recompute its counts."""
    counts = {"healed": 0, "still_rejected": 0, "retrying": 0, "unhealable": 0}
    with conn.cursor() as cur:
        batch_status = _lock_batch(cur, batch_id)
        if batch_status not in (STATUS_STAGED, STATUS_PROMOTED):
            return counts
        for row_id, _, account_id, row_number, _ in batch_rows:
            # Re-read under the row lock: another writer may have moved it since the scan.
            cur.execute(
                f"SELECT r.payload FROM import_row r WHERE r.id = %s AND {_AWAITING} "
                "FOR UPDATE",
                (row_id,),
            )
            current = cur.fetchone()
            if current is None:
                continue
            payload = current[0]
            staged, rejected = _stage_fno(
                conn, [_awaiting_row(row_number, payload)], str(account_id)
            )
            if staged:
                (_, confidence, dedupe_key, new_payload) = staged[0]
                cur.execute(
                    "UPDATE import_row SET status = 'STAGED', confidence = %s, dedupe_key = %s, "
                    "payload = %s, reason = NULL WHERE id = %s",
                    (confidence, dedupe_key, new_payload, row_id),
                )
                if batch_status == STATUS_PROMOTED:
                    _promote_fno(conn, [(dedupe_key, json.loads(new_payload))])
                    cur.execute(
                        f"""
                        UPDATE import_row r SET status = CASE
                            WHEN {_IN_LEDGER} THEN 'PROMOTED'::import_row_status
                            ELSE 'DUPLICATE'::import_row_status END
                        WHERE r.id = %s
                        """,
                        (row_id,),
                    )
                counts["healed"] += 1
                continue
            (_, new_payload, reason) = rejected[0]
            fresh = json.loads(new_payload)
            if not fresh.get("awaiting_master"):
                cur.execute(
                    "UPDATE import_row SET payload = %s, reason = %s WHERE id = %s",
                    (new_payload, reason, row_id),
                )
                counts["still_rejected"] += 1
                continue
            # A failed attempt: the day is still not in the master.
            exchange = payload["fno"]["exchange"]
            day = date.fromisoformat(payload["fno"]["trade_date"])
            outcome = outcomes.get((exchange, day)) or OUTCOME_FETCH_FAILED
            attempts = int(payload.get("heal_attempts") or 0) + 1
            fresh.update(heal_attempts=attempts, heal_last_outcome=outcome)
            recent = day >= now.date() - timedelta(days=HEAL_RECENT_DAYS)
            if attempts >= HEAL_MAX_ATTEMPTS and not recent:
                fresh.update(awaiting_master=False, heal_next_at=None)
                reason = (
                    f"No F&O bhavcopy published by {exchange} for {day} after "
                    f"{attempts} attempts; re-import or reparse once published"
                    if outcome == OUTCOME_NOT_PUBLISHED
                    else f"The {exchange} F&O bhavcopy for {day} could not be fetched after "
                    f"{attempts} attempts; re-import or reparse once published"
                )
                counts["unhealable"] += 1
            else:
                fresh["heal_next_at"] = (now + heal_backoff(attempts)).isoformat()
                counts["retrying"] += 1
            cur.execute(
                "UPDATE import_row SET payload = %s, reason = %s WHERE id = %s",
                (json.dumps(fresh), reason, row_id),
            )
    recompute_batch_counts(conn, batch_id)
    if counts["healed"]:
        audit.record(
            conn, ACTION_FNO_ROWS_HEALED, "import_batch", batch_id, None,
            {
                "rows": counts["healed"],
                "reason": "F&O contract master backfilled; rows re-resolved",
            },
        )
    if counts["unhealable"]:
        audit.record(
            conn, ACTION_FNO_ROWS_UNHEALABLE, "import_batch", batch_id, None,
            {
                "rows": counts["unhealable"],
                "reason": f"no contract-master day after {HEAL_MAX_ATTEMPTS} tries",
            },
        )
    return counts


def _reversals_for(conn: Any, transaction_ids: list[str]) -> list[Transaction]:
    """Reversing rows for transactions a better source has superseded.

    Append-only means the earlier row cannot be deleted. DATA_MODEL's correction pattern is
    a reversing entry, so that is what this builds: the same trade with the opposite
    txn_type, marked MANUAL with an explanation in raw_payload.
    """
    if not transaction_ids:
        return []

    opposite = {TxnType.BUY: TxnType.SELL, TxnType.SELL: TxnType.BUY}
    reversals: list[Transaction] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, isin, txn_date, txn_type, quantity, price, charges, stt, "
            "source, source_ref FROM transaction WHERE id = ANY(%s)",
            (transaction_ids,),
        )
        for row in cur.fetchall():
            original = TxnType(str(row[3]))
            if original not in opposite:
                continue
            reversals.append(
                Transaction(
                    isin=row[1],
                    txn_date=row[2],
                    txn_type=opposite[original],
                    quantity=row[4],
                    price=row[5],
                    charges=row[6],
                    # stt is a SUBSET of charges (never zeroed independently) --
                    # carrying it through the same way charges is carried through keeps
                    # the "stt <= charges" invariant true on this synthetic row too.
                    stt=row[7],
                    account_id=str(row[0]),
                    source=Source.MANUAL,
                    source_ref=f"REVERSAL-{row[9]}",
                    raw_payload={
                        "correction": "reversing entry",
                        "reason": (
                            f"superseded by a higher-precedence source; the original was "
                            f"{row[8]} and carried no charges"
                        ),
                    },
                )
            )
    return reversals


def discard_batch(conn: Any, batch_id: str, reason: str, actor_uid: str | None = None) -> None:
    """Reject a batch without promoting it. A bad parse ends here.

    The staged rows are kept, not deleted: what was rejected and why is the evidence that
    review happened, and is what a later investigation reads.
    """
    if not reason or not reason.strip():
        raise ValueError("discarding requires a reason")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM import_batch WHERE id = %s", (batch_id,))
        row = cur.fetchone()
        if row is None:
            raise BatchNotFoundError(batch_id)
        if row[0] != STATUS_STAGED:
            raise BatchAlreadyResolvedError(f"batch {batch_id} is {row[0]}")

        cur.execute(
            "UPDATE import_batch SET status = 'DISCARDED', discarded_at = now(), "
            "discard_reason = %s WHERE id = %s",
            (reason, batch_id),
        )
    audit.record(
        conn,
        ACTION_BATCH_DISCARDED,
        "import_batch",
        batch_id,
        actor_uid,
        {"reason": reason},
    )
