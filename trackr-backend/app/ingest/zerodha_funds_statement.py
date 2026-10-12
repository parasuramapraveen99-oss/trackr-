"""Zerodha Console funds statement -> DIVIDEND ledger rows.

THE GAP THIS CLOSES, STATED EXACTLY. Every other dividend source in this system stops at
DECLARATION. ``corporate_action_feed`` reads NSE's notices; ``corporate_action_yfinance``
reads Yahoo's dividend history. Both answer "what did the company say it would pay per
share", and neither knows an account holder exists, let alone whether one was paid --
``Ticker.dividends`` is a two-column series (ex-date, rupees per share) and
``Ticker.actions`` carries only ``['Dividends', 'Stock Splits']``, verified live
2026-09-22. Multiplying either by a held quantity produces a PROJECTION, which is what
``app.engine.dividends.compute_expected_dividends`` already does and labels EXPECTED.

A funds statement is different in kind, not degree: it is the broker's own books saying
a specific rupee amount was credited to a specific account on a specific date. That is
the evidence ``docs/ARCHITECTURE_INVARIANTS.md`` invariant 18 requires before anything
may be called RECEIVED, and ``docs/INGESTION.md`` names this document family as the
automatable receipt path. Authorised 2026-09-22.

WHAT IS PARSED, AND WHAT IS REFUSED. The sheet is the Console P&L export's **"Other
Debits and Credits"** tab, whose real shape is confirmed against a genuine export:
a preamble carrying ``Client ID``, then a header row ``Particulars | Posting Date |
Debit | Credit``, then one row per non-trade entry. Most rows are charges -- AMC, DP,
payment-gateway -- and are skipped, not rejected: they are not transactions in this model
and their absence is not an error.

**A dividend row is recognised by its ISIN, never by its wording.** The narration's exact
phrasing is a broker formatting detail that has changed before and will change again, and
CLAUDE.md forbids inventing financial logic to cover a case nobody has verified. So the
test is structural and self-validating: the narration must say "dividend", and it must
contain a literal ISIN. An ISIN identifies the security unambiguously and carries its own
check digit; a ticker does not, and resolving one would violate CLAUDE.md rule 2 -- the
same hazard that made ``TATAMOTORS`` resolve to a different company's symbol. Zerodha
genuinely does write some narrations with a bare symbol (``"DP Charges for Sale of KDL"``
in the confirmed sample), so **a dividend narration carrying no ISIN is REJECTED with its
row number, never guessed at**. A human resolves it; the other rows still import.

THE AMOUNT IS THE CREDIT COLUMN, AND IT IS NET. Not parsed out of the narration, which may
or may not state a per-share rate, and never recomputed as rate x quantity -- that would
be re-deriving a projection and calling it a receipt, which is the one move this whole
module exists to avoid. The credit is what reached the account.

TDS IS LEFT UNKNOWN, DELIBERATELY. This sheet states a net credit and no tax line, so
``tds_amount`` stays ``None``. Per migration 030, NULL means unknown and 0 asserts "no tax
was withheld" -- a different, checkable claim that would be false for most dividends above
the section 194 threshold and would silently discard a real tax credit at ITR time.

WHY ROWS ARE NOT LINKED TO A ``dividend_event``. A linked row would suppress the matching
EXPECTED projection, which is desirable -- but linking requires matching a statement line
to a declaration by date and amount, and ``app.engine.dividends.realised_event_ids``
already rules that out in writing: guessing the link "would hide a real second dividend
that happened to resemble one already received". These rows therefore carry a free-form
``source_ref``, exactly as a manually entered dividend does, and are counted as real
income without being tied to a declaration.
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.types import Source, Transaction, TxnType
from app.ingest.base import (
    CONFIDENCE_CERTAIN,
    DocumentIdentity,
    ParseResult,
    Rejection,
    StagedRow,
)

#: The tab Console writes non-trade entries to. Matched case-insensitively on a
#: normalised name because the export has used both "&" and "and" across versions.
_SHEET_CANDIDATES = ("other debits and credits", "other debit and credit")

#: The header cells that mark the start of real data, normalised to lowercase. Console
#: puts a title block above them, so the header is found by content, never by row index.
_REQUIRED_HEADERS = ("particulars", "posting date", "credit")

#: A literal Indian ISIN. `IN` + issuer-type letter + 9 alphanumerics. Anchored on a word
#: boundary so a substring of a longer token is not mistaken for one.
_ISIN_RE = re.compile(r"\b(IN[EFD0-9][0-9A-Z]{9})\b")

#: What makes a narration a dividend at all.
_DIVIDEND_RE = re.compile(r"\bdividend\b", re.IGNORECASE)

#: Narrations that mention a dividend but are NOT a dividend receipt into this account.
#: Mirrors the intent of app.ingest.corporate_action_feed's _DISQUALIFYING_TERMS: a
#: reversal or a charge must never be read as income. Anything matching is rejected with
#: its row number rather than skipped, because a human should see it.
_DISQUALIFYING_RE = re.compile(
    r"\b(reversal|reversed|recovery|recovered|charges?|penalty|interest|"
    r"preference|debenture|mutual\s+fund)\b",
    re.IGNORECASE,
)

#: Prefix on every source_ref this adapter mints -- "Zerodha Funds Statement".
SOURCE_REF_PREFIX = "ZFS"


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _parse_date(value: Any) -> date | None:
    """Console writes this column as a real date in some exports and a string in others."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _cell_str(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: Any) -> Decimal | None:
    """Money as Decimal, via str -- never float. A blank cell is zero, not an error;
    Console writes 0.0 in the unused side of every debit/credit pair."""
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def build_source_ref(isin: str, posting_date: date, amount: Decimal, narration: str) -> str:
    """A stable identity for one statement line, derived from its CONTENT.

    NOT from the file, and the distinction is load-bearing for CLAUDE.md rule 3.
    ``app.engine.dedupe.make_dedupe_key`` excludes ``dividend_amount`` and a DIVIDEND row
    has zero quantity and zero price, so the key collapses to account, source,
    ``source_ref``, ISIN and date -- leaving ``source_ref`` as the only field that can
    tell two dividends apart. Hashing the file would defeat this: re-exporting the same
    period with a wider date range yields different bytes, and every dividend already
    imported would import a second time.

    Hashing the line's own content instead makes a re-download of any overlapping window
    produce identical keys, so re-importing is the no-op rule 3 requires. The narration is
    included so a genuine interim and special of equal value on one date stay distinct.
    """
    digest = hashlib.sha256(
        "|".join([isin, posting_date.isoformat(), str(amount), " ".join(narration.split())])
        .encode("utf-8")
    ).hexdigest()[:12]
    return f"{SOURCE_REF_PREFIX}:{isin}:{posting_date.isoformat()}:{digest}"


def _find_sheet(workbook: Any) -> Any | None:
    for name in workbook.sheetnames:
        if " ".join(name.lower().split()) in _SHEET_CANDIDATES:
            return workbook[name]
    return None


def _find_header(rows: list[tuple[Any, ...]]) -> tuple[int, dict[str, int]] | None:
    """Locate the header by content and map column name -> index.

    Console's preamble length varies by export (client block, date range, blank rows), so
    an index would be a guess that breaks silently on the next version.
    """
    for i, row in enumerate(rows):
        cells = [" ".join(_cell_str(c).lower().split()) for c in row]
        if all(any(h == c for c in cells) for h in _REQUIRED_HEADERS):
            return i, {c: j for j, c in enumerate(cells) if c}
    return None


class ZerodhaFundsStatementAdapter:
    """Layer 1. Produces candidate DIVIDEND rows; never touches the ledger."""

    name = "zerodha_funds_statement"

    def _workbook(self, content: bytes) -> Any:
        import openpyxl

        # NOT read_only, matching ZerodhaContractNoteXlsxAdapter: in read-only mode
        # openpyxl trusts the sheet dimension recorded in the file, which Console writes
        # wrongly often enough to truncate real rows.
        return openpyxl.load_workbook(io.BytesIO(content), data_only=True)

    def identity(self, content: bytes) -> DocumentIdentity:
        """The ``Client ID`` Console prints in the preamble -- Zerodha's UCC.

        Satisfies ``IdentifyingAdapter`` so ``app.db.imports.stage_batch`` can refuse a
        statement being staged into somebody else's account. No PAN: this export does not
        carry one, and reporting a field the document does not state would be an
        invention (see ``DocumentIdentity``'s own docstring).
        """
        try:
            workbook = self._workbook(content)
        except Exception:  # noqa: BLE001 - openpyxl raises assorted types
            return DocumentIdentity()

        for sheet_name in workbook.sheetnames:
            rows = list(workbook[sheet_name].iter_rows(values_only=True))
            for row in rows:
                cells = [_cell_str(c) for c in row]
                for j, cell in enumerate(cells):
                    if " ".join(cell.lower().split()) == "client id":
                        rest = [c for c in cells[j + 1 :] if c]
                        if rest:
                            return DocumentIdentity(client_code=rest[0])
        return DocumentIdentity()

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        try:
            workbook = self._workbook(content)
        except ImportError as exc:
            return ParseResult(rejections=[Rejection(0, f"openpyxl is not installed: {exc}")])
        except Exception as exc:  # noqa: BLE001 - openpyxl raises assorted types
            return ParseResult(rejections=[Rejection(0, f"not a readable .xlsx file: {exc}")])

        sheet = _find_sheet(workbook)
        if sheet is None:
            return ParseResult(
                rejections=[
                    Rejection(
                        0,
                        "no 'Other Debits and Credits' sheet -- this is the Console P&L "
                        f"export's dividend tab; found {workbook.sheetnames}",
                    )
                ]
            )

        rows = list(sheet.iter_rows(values_only=True))
        header = _find_header(rows)
        if header is None:
            return ParseResult(
                rejections=[
                    Rejection(
                        0,
                        "could not find a 'Particulars / Posting Date / Debit / Credit' "
                        "header row in the 'Other Debits and Credits' sheet",
                    )
                ]
            )
        header_index, columns = header

        staged: list[StagedRow] = []
        rejections: list[Rejection] = []
        skipped_non_dividend = 0

        for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
            narration = _cell_str(row[columns["particulars"]]) if "particulars" in columns else ""
            if not narration:
                continue

            if not _DIVIDEND_RE.search(narration):
                skipped_non_dividend += 1
                continue

            raw = {"particulars": narration, "row": offset}

            if _DISQUALIFYING_RE.search(narration):
                rejections.append(
                    Rejection(
                        offset,
                        "mentions a dividend but is a reversal, charge or non-equity "
                        f"entry -- not income into this account: {narration!r}",
                        raw,
                    )
                )
                continue

            isins = set(_ISIN_RE.findall(narration.upper()))
            if not isins:
                rejections.append(
                    Rejection(
                        offset,
                        "dividend narration carries no ISIN, and a ticker is not a safe "
                        "substitute (CLAUDE.md rule 2 -- tickers get renamed and reused). "
                        f"Resolve it by hand: {narration!r}",
                        raw,
                    )
                )
                continue
            if len(isins) > 1:
                rejections.append(
                    Rejection(
                        offset,
                        f"narration names {len(isins)} ISINs, so which security was paid "
                        f"is ambiguous: {sorted(isins)}",
                        raw,
                    )
                )
                continue
            isin = isins.pop()

            posting_date = _parse_date(row[columns["posting date"]])
            if posting_date is None:
                rejections.append(Rejection(offset, "unreadable posting date", raw))
                continue

            credit = _parse_amount(row[columns["credit"]]) if "credit" in columns else None
            debit = _parse_amount(row[columns["debit"]]) if "debit" in columns else Decimal("0")
            if credit is None or debit is None:
                rejections.append(Rejection(offset, "unreadable debit/credit amount", raw))
                continue
            if credit <= 0:
                # A dividend that credited nothing is not a receipt. Surfaced rather than
                # skipped: a zero or debit-side dividend line means something happened
                # that this adapter does not model, and silence would hide it.
                rejections.append(
                    Rejection(
                        offset,
                        f"dividend row credits {credit} (debit {debit}) -- no money "
                        "arrived, so this is not a receipt",
                        raw,
                    )
                )
                continue

            transaction = Transaction(
                isin=isin,
                txn_date=posting_date,
                txn_type=TxnType.DIVIDEND,
                # DIVIDEND's ledger convention -- the amount lives in dividend_amount.
                quantity=Decimal("0"),
                price=Decimal("0"),
                dividend_amount=credit,
                # Unknown, NOT zero. This sheet states no tax line. See migration 030.
                tds_amount=None,
                account_id=account_id,
                source=Source.FUNDS_STATEMENT,
                source_ref=build_source_ref(isin, posting_date, credit, narration),
                raw_payload={"particulars": narration, "row": offset, "filename": filename},
                # dedupe_key is deliberately NOT set here. app.db.imports.stage_batch and
                # app.db.repository.save_transactions both derive it with
                # make_dedupe_key, and an adapter that pre-computed one would be asserting
                # an identity the staging layer owns. Rule 3's idempotency comes from
                # source_ref above being content-derived and stable.
            )
            staged.append(
                StagedRow(
                    row_number=offset,
                    transaction=transaction,
                    # Nothing was inferred: the ISIN was read literally, the amount is the
                    # broker's own credit column, the date is its own posting date.
                    confidence=CONFIDENCE_CERTAIN,
                    raw=raw,
                )
            )

        notes = []
        if skipped_non_dividend:
            notes.append(
                f"{skipped_non_dividend} non-dividend entries (AMC, DP and similar "
                "charges) were skipped -- they are not ledger transactions in this model"
            )
        if not staged and not rejections:
            notes.append(
                "no dividend entries in this statement. That is a normal result for a "
                "period with no payouts, not a parse failure"
            )
        return ParseResult(rows=staged, rejections=rejections, notes=notes)
