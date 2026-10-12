"""CDSL/NSDL Consolidated Account Statement (CAS) PDF adapter.

Built on an explicit scope override: docs/INGESTION.md lists "CAS PDF" under
"Phase 2+ adapters -- design the seam, do not build", and CLAUDE.md's Phase 1 scope
excludes it. Because of that, this module stays conservative wherever the real data is
ambiguous rather than treating the override as license to guess -- see "VALIDATION-
FIRST" in app/ingest/base.py, which this adapter follows exactly.

WHY THIS ADAPTER NEVER EMITS BUY/SELL/TRANSFER. CDSL's "Transaction Details" section is
a depository ledger of QUANTITY movements (opening/credit/debit/closing balance) -- it
carries no price for equity, ever, verified against a real July-2026 CDSL statement.
That is exactly what docs/INGESTION.md means by "No equity cost basis". A market trade,
an off-market transfer, or anything else needing a real price or cost basis would have
to be recorded with price=0 to fit this source, which would silently understate cost
basis and corrupt XIRR -- the failure CLAUDE.md rule 4 forbids ("a missing price
raises, never defaults to zero"). So only the two FREE_SHARE_TYPES -- BONUS and
SPLIT_ADJ, where quantity moves and the cost pool deliberately does not -- are ever
staged as Transaction rows. Everything else is rejected with a reason.

WHY TEXT EXTRACTION ANCHORS ON ROW SHAPE, NOT SECTION HEADERS. CDSL prints the same
page letterhead and every column header in three interleaved languages (English, Tamil,
Hindi, character-by-character), which pdfplumber extracts as unreadable garbage. Actual
DATA rows -- ISINs, dates, quantities, prices -- carry no vernacular text and extract
cleanly. So, like app.ingest.zerodha_contract_note, this parser never trusts a header
or fixed column position: it scans every physical line for a shape that can only be a
transaction or holding row, and ignores everything else (which will simply never match).

WHY ONLY CDSL/NSDL, NOT CAMS/KFINTECH. CAMS/KFintech-issued CAS is mutual-fund-only and
uses a materially different layout that this adapter has never seen a real example of.
Handling it would be guessing at a format, which CLAUDE.md forbids ("never invent
parsing logic for a source the codebase does not yet handle"). Registered as a named
gap in app/ingest/webhook.py, not silently absent.

MULTI-ACCOUNT NOTE. A CAS PDF can span several demat accounts in one file -- the real
sample this was built from lists three (Zerodha, Groww, Axis), matching every other
adapter's one-document-one-account assumption only when exactly one of them actually
holds anything. When exactly one account has a nonzero declared balance -- the common
case, and what the real sample has -- every row is confidently attributed to it via
``broker_accounts``. When more than one does, this adapter cannot safely tell which
ISIN belongs to which depository participant (DP) from reliably-parseable text: the
per-DP section separators deeper in the real document render exactly as garbled as the
page letterhead, unlike the clean per-DP summary table near the top. Rather than guess,
the statement is REFUSED with ``CasAccountUnresolvedError`` naming the accounts -- as it
is when its one active DP matches none of the investor's accounts, or when no active DP
can be read at all. Attributing the holdings to a fallback account made "Check holdings"
report differences that did not exist -- see ``_resolve_account``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.engine.types import Source, Transaction, TxnType
from app.ingest.base import CONFIDENCE_DERIVED, ParseResult, Rejection, StagedRow

#: INE = equity, INF = mutual fund -- same narrowing as zerodha_contract_note.py, for
#: the same reason (a naive [A-Z0-9]{10} shape also matches non-security identifiers
#: printed elsewhere in the document).
ISIN_PATTERN = re.compile(r"^IN[EF][A-Z0-9]{9}$")

#: Clean, un-garbled in the real sample -- this is the one line of the page-1 title
#: block that renders without trilingual interleaving.
CAS_MARKER = "CONSOLIDATED ACCOUNT STATEMENT"

#: Transaction Details row, matched in two independent pieces rather than one
#: ISIN-then-date pattern. Verified against the real sample: an ISIN is SOMETIMES
#: followed immediately by the date ("INE731H01025 28-07-2026 ...") and sometimes by
#: extra description words first ("INE022Q01020 EQUITY SHARES FACE 27-07-2026 ...",
#: "INE955D01029 LIMITED EQTY SHARES - 28-07-2026 ...") -- a single anchored
#: ISIN-then-date regex silently fails to match the second shape at all, which is worse
#: than rejecting it: the row disappears with no row number, violating base.py's
#: VALIDATION-FIRST rule. So the data suffix (date, balances, charge) is matched as a
#: trailing pattern anywhere in the line, and a leading ISIN is checked separately.
#:
#: The balance run's token count varies too (3 or 4 observed for otherwise-identical
#: row shapes in the same real statement -- pdfplumber occasionally merges a "--" cell
#: into its neighbour), so this captures the whole run rather than fixed
#: opening/credit/debit/closing positions; only the FIRST (opening) and LAST (closing)
#: token are trusted.
DATA_SUFFIX = re.compile(
    r"(?P<date>\d{2}-\d{2}-\d{4})\s+"
    r"(?P<balances>(?:--|[\d,]+\.\d+)(?:\s+(?:--|[\d,]+\.\d+))+)\s+"
    r"(?P<charge>[\d,]+(?:\.\d+)?)\s*$"
)

#: ISIN is OPTIONAL at the start of a data line: CDSL groups multiple corporate-action
#: events for one security under a single ISIN header and omits it on the second event
#: -- verified against the real sample (EDELWEISS FINANCIAL SERVICES has two credit
#: events in one statement; only the first carries the ISIN).
LEADING_ISIN = re.compile(r"^(?P<isin>IN[EF][A-Z0-9]{9})\b")

#: Holdings ("Market Value") row: ISIN, free-text description, then Free / Pledge /
#: Lock-in / (other) / Total / Price / Value columns. Column count before the trailing
#: triplet varies with how many of those are "--", so only the trailing three decimal
#: tokens (total qty, market price, market value) are trusted -- not fixed positions.
HOLDING_ROW = re.compile(r"^(?P<isin>IN[EF][A-Z0-9]{9})\s+(?P<rest>.+)$")
DECIMAL_TOKEN = re.compile(r"-?[\d,]+\.\d+")

#: Corporate-action keywords, matched against a window of text AROUND the anchor row,
#: never the row itself -- CDSL prints the reason on the security's own description
#: line, one or two physical lines away from the balance figures, not on them.
#:
#: Matched as regexes, not plain substrings, because CDSL's two-column layout
#: interleaves the OTHER column's content into a single logical phrase: "AFTER SUB
#: DIVISION" turns up in the real sample as "AFTER SUB 1211232026138 DIVISION" (a
#: settlement reference number) or "AFTER SUB- 13906 SETT DIVISION" (a settlement
#: number plus the literal noise word "SETT") depending on where the line wraps.
#: ``_normalize_context`` strips known noise tokens and turns hyphens into spaces
#: before these run, so "SUB[-\\s]+DIVISION" in all its observed spacings collapses to
#: one pattern instead of needing a growing list of literal variants.
_NOISE_TOKENS = re.compile(r"\b(?:\d{4,}|SETT|CM|TM/CP|PAYOUT-CR|INTDEP-CR)\b")

BONUS_RE = re.compile(r"\bBONUS\b")
SPLIT_RE = re.compile(r"\bSPLIT\b|\bCONSOLIDATION\b|\bSUB\s+DIVISION\b")


def _normalize_context(raw: str) -> str:
    cleaned = _NOISE_TOKENS.sub(" ", raw.upper())
    cleaned = cleaned.replace("-", " ")
    return re.sub(r"\s+", " ", cleaned).strip()

#: Lines of context (before and after the anchor row) searched for a classification
#: keyword. Verified against the real sample: "AFTER SPLIT" sits 1-2 lines after the
#: ISIN row; generous on both sides to survive a wrapped security name or narration.
CONTEXT_WINDOW = 5

#: The clean per-account summary near the top of the statement (page 2 in the real
#: sample) -- "ZERODHA BROKING LIMITED" / "CDSL Demat Account 18 12,75,726.2" /
#: "DP Id: ... Client Id: ...", one triplet per account. Unlike the per-DP section
#: separators deeper in the document, this renders without trilingual interleaving.
DP_SUMMARY_ROW = re.compile(r"^(?P<broker>[A-Z][A-Z0-9 .&\-]+?(?:LIMITED|LTD))\s*$")
DP_VALUE_LINE = re.compile(r"^(?:CDSL|NSDL) Demat Account\s+\d+\s+(?P<value>[\d,]+\.\d+)$")

#: "Portfolio Value ` 12,75,726.20 as on 31-07-2026" -- clean in the real sample,
#: appears once per demat account right after its holdings table. Used as the
#: holding_snapshot as-of date.
AS_ON_LINE = re.compile(
    r"Portfolio Value `?\s*(?P<value>[\d,]+\.\d+)\s+as on\s+(?P<date>\d{2}-\d{2}-\d{4})",
    re.I,
)


class PdfTextExtractionError(RuntimeError):
    """The PDF could not be opened with any PAN case, or produced no text."""


def _decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", "").strip())


def _password_candidates(pan: str) -> list[str]:
    """PAN candidates to try opening the PDF with, in order.

    Unlike app.ingest.webhook.PDF_PASSWORD_CASE (right for Zerodha/Groww/Upstox, each
    with one documented case rule), no single case rule covers every CDSL/NSDL-issued
    CAS -- the task's own instruction is "typically lowercase or uppercase". So this
    tries, rather than asserts, the case: uppercase, lowercase, as-stored, then empty
    (a CAS regenerated without a password -- the real sample used for this build opens
    with an empty password, which is why the empty candidate is not skipped).
    """
    pan = (pan or "").strip()
    candidates: list[str] = []
    for candidate in (pan.upper(), pan.lower(), pan, ""):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def extract_text(content: bytes, pan: str = "") -> str:
    """Unlock and extract text, trying every PAN case candidate in turn.

    Separated from parse() so tests can feed text directly -- same reasoning as
    zerodha_contract_note.extract_text: pdfplumber is slow, and the parsing logic
    (where bugs live) is independent of how the bytes became text.
    """
    import io

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise PdfTextExtractionError(
            "pdfplumber is not installed. pip install -e '.[pdf]'"
        ) from exc

    last_error: Exception | None = None
    for password in _password_candidates(pan):
        try:
            with pdfplumber.open(io.BytesIO(content), password=password) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as exc:  # noqa: BLE001 - pdfplumber/pypdf raise assorted types
            last_error = exc
            continue
    raise PdfTextExtractionError(f"could not unlock CAS PDF with any PAN case: {last_error}")


@dataclass(frozen=True, slots=True)
class DeclaredHolding:
    """One ISIN's CAS-declared quantity -- feeds ``holding_snapshot``
    (app/db/migrations/001_core.sql: "CAS/broker-declared quantities, used to CHECK
    the engine"). NOT a Transaction: no cost basis, never promoted to the ledger, no
    review/promote gate -- the table's own comment says "NOT a source of truth for
    display".
    """

    isin: str
    account_id: str
    quantity: Decimal
    snap_date: date
    market_price: Decimal | None = None
    market_value: Decimal | None = None


def _match_broker(dp_name: str, broker_accounts: dict[str, str]) -> str | None:
    """The account whose registered broker matches this DP name.

    Substring, not equality: the DP name in a CAS carries legal-entity suffixes
    ('BROKING LIMITED', 'INVEST TECH PRIVATE LIMITED') that no account.broker value
    carries, e.g. account.broker='zerodha' must match DP name
    'ZERODHA BROKING LIMITED'.
    """
    name = dp_name.lower()
    for broker, account_id in broker_accounts.items():
        if broker and broker in name:
            return account_id
    return None


class CasAccountUnresolvedError(ValueError):
    """This CAS cannot be attributed to exactly one of the investor's accounts. The
    message is written for the investor: it names the depository accounts the statement
    shows and what to do. Nothing from the statement is staged or stored."""


class CasPdfAdapter:
    """CDSL/NSDL Consolidated Account Statement PDF. See module docstring for the
    scope decisions (no BUY/SELL/TRANSFER, CDSL/NSDL only, one account per statement)."""

    name = "cas_pdf"

    def __init__(self, pan: str = "", broker_accounts: dict[str, str] | None = None):
        self.pan = pan
        self.broker_accounts = {k.lower(): v for k, v in (broker_accounts or {}).items()}

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        try:
            text = (
                content.decode("utf-8")
                if content[:5] != b"%PDF-"
                else extract_text(content, self.pan)
            )
        except PdfTextExtractionError as exc:
            return ParseResult(rejections=[Rejection(0, str(exc))])
        except UnicodeDecodeError:
            return ParseResult(rejections=[Rejection(0, "not a readable CAS PDF")])

        return self.parse_text(text, account_id, filename)

    def parse_text(self, text: str, account_id: str, filename: str = "") -> ParseResult:
        if CAS_MARKER not in text.upper():
            return ParseResult(rejections=[Rejection(0, "not a CAS: header marker absent")])

        resolved_account = self._resolve_account(text)

        rows: list[StagedRow] = []
        rejections: list[Rejection] = []
        lines = text.splitlines()
        last_isin: str | None = None

        for i, raw_line in enumerate(lines):
            line = raw_line.strip()
            match = DATA_SUFFIX.search(line)
            if not match:
                continue

            isin_match = LEADING_ISIN.match(line)
            row_isin = isin_match.group("isin") if isin_match else None
            isin = row_isin.upper() if row_isin else last_isin
            if row_isin:
                last_isin = row_isin.upper()

            if not isin or not ISIN_PATTERN.match(isin):
                rejections.append(
                    Rejection(
                        i + 1,
                        "transaction row with no attributable ISIN",
                        {"line": line[:120]},
                    )
                )
                continue

            balances = [b for b in match.group("balances").split() if b]
            if len(balances) < 2:
                rejections.append(
                    Rejection(i + 1, "could not read opening/closing balance", {"line": line[:120]})
                )
                continue

            try:
                opening = Decimal("0") if balances[0] == "--" else _decimal(balances[0])
                closing = Decimal("0") if balances[-1] == "--" else _decimal(balances[-1])
            except InvalidOperation:
                rejections.append(
                    Rejection(i + 1, "unparseable balance figures", {"line": line[:120]})
                )
                continue

            net = closing - opening
            if net <= 0:
                rejections.append(
                    Rejection(
                        i + 1,
                        f"{isin}: not a credit (opening {opening} -> closing {closing}); "
                        "CAS carries no price, so a decrease (transfer-out, buyback, sale "
                        "settlement) cannot be safely recorded from this source",
                        {"line": line[:120]},
                    )
                )
                continue

            try:
                txn_date = datetime.strptime(match.group("date"), "%d-%m-%Y").date()
            except ValueError:
                rejections.append(Rejection(i + 1, f"unparseable date {match.group('date')!r}"))
                continue

            context = _normalize_context(
                " ".join(lines[max(0, i - CONTEXT_WINDOW) : i + CONTEXT_WINDOW + 1])
            )
            if BONUS_RE.search(context):
                txn_type = TxnType.BONUS
            elif SPLIT_RE.search(context):
                txn_type = TxnType.SPLIT_ADJ
            else:
                rejections.append(
                    Rejection(
                        i + 1,
                        f"{isin}: credit of {net} on {txn_date} but no BONUS/SPLIT/"
                        "SUB-DIVISION/CONSOLIDATION wording found nearby -- not guessed",
                        {"line": line[:120]},
                    )
                )
                continue

            rows.append(
                StagedRow(
                    row_number=i + 1,
                    transaction=Transaction(
                        isin=isin,
                        txn_date=txn_date,
                        txn_type=txn_type,
                        quantity=net,
                        price=Decimal("0"),
                        account_id=resolved_account,
                        source=Source.CAS,
                        source_ref=None,
                    ),
                    # Never CERTAIN: inferred from co-occurring balance-direction and
                    # narration-keyword signals, not read from an explicit field.
                    confidence=CONFIDENCE_DERIVED,
                    raw={"line": line[:200]},
                )
            )

        notes = self._holdings_notes(text)
        mf_note = self._mf_folio_note(text)
        if mf_note:
            notes.append(mf_note)

        return ParseResult(rows=rows, rejections=rejections, notes=notes)

    def parse_declared_holdings(
        self, content: bytes, account_id: str = ""
    ) -> list[DeclaredHolding]:
        """CAS-declared quantities per ISIN, for ``holding_snapshot`` -- see
        app.db.holdings.record_snapshot. Pure: reuses the same unlocked text as
        parse(), does no I/O of its own."""
        try:
            text = (
                content.decode("utf-8")
                if content[:5] != b"%PDF-"
                else extract_text(content, self.pan)
            )
        except (PdfTextExtractionError, UnicodeDecodeError):
            return []
        return self._extract_holdings(text, self._resolve_account(text))

    # -- internal ----------------------------------------------------------------

    def _resolve_account(self, text: str) -> str:
        """The one account every row in this CAS belongs to, matched from the statement's
        own DP summary against ``broker_accounts``. Raises ``CasAccountUnresolvedError``
        rather than falling back to any other account. See the module's "MULTI-ACCOUNT
        NOTE"."""
        active = self._active_dp_names(text)
        if len(active) == 1:
            matched = _match_broker(active[0], self.broker_accounts)
            if matched:
                return matched
            raise CasAccountUnresolvedError(
                f"This CAS shows holdings at {active[0]}, but none of your accounts in "
                f"Trackr+ is with that broker. Add that account in Settings, then upload "
                f"the CAS again. Nothing from this statement was imported."
            )
        if len(active) > 1:
            raise CasAccountUnresolvedError(
                f"This CAS shows holdings at {len(active)} demat accounts "
                f"({', '.join(active)}). Trackr+ cannot yet tell which holding belongs to "
                f"which account inside one statement, so nothing from it was imported."
            )
        raise CasAccountUnresolvedError(
            "Trackr+ could not find which demat account this CAS belongs to: its account "
            "summary lists no demat account with a balance. Nothing from this statement "
            "was imported."
        )

    @staticmethod
    def _active_dp_names(text: str) -> list[str]:
        """DP names with a nonzero declared value, from the clean per-account summary
        near the top of the statement."""
        lines = [ln.strip() for ln in text.splitlines()]
        active: list[str] = []
        for i, line in enumerate(lines):
            name_match = DP_SUMMARY_ROW.match(line)
            if not name_match:
                continue
            for lookahead in lines[i + 1 : i + 3]:
                value_match = DP_VALUE_LINE.match(lookahead)
                if not value_match:
                    continue
                try:
                    if _decimal(value_match.group("value")) > 0:
                        active.append(name_match.group("broker"))
                except InvalidOperation:
                    pass
                break
        return active

    def _extract_holdings(self, text: str, account_id: str) -> list[DeclaredHolding]:
        holdings: list[DeclaredHolding] = []
        as_of = self._as_of_date(text)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            # A Transaction Details row also starts with an ISIN and ends with
            # decimal-looking tokens -- HOLDING_ROW would otherwise match it too and
            # double-count the same security. The two tables are mutually exclusive by
            # construction (only Transaction Details rows carry a date), so anything
            # DATA_SUFFIX claims belongs there, not here.
            if DATA_SUFFIX.search(line):
                continue
            match = HOLDING_ROW.match(line)
            if not match:
                continue
            isin = match.group("isin").upper()
            if not ISIN_PATTERN.match(isin):
                continue
            nums = DECIMAL_TOKEN.findall(match.group("rest"))
            if len(nums) < 3:
                continue
            try:
                total_qty, price, value = (_decimal(n) for n in nums[-3:])
            except InvalidOperation:
                continue
            holdings.append(
                DeclaredHolding(
                    isin=isin,
                    account_id=account_id,
                    quantity=total_qty,
                    snap_date=as_of,
                    market_price=price,
                    market_value=value,
                )
            )
        return holdings

    def _holdings_notes(self, text: str) -> list[str]:
        holdings = self._extract_holdings(text, "")
        if not holdings:
            return ["CAS holdings table: no equity holdings found or table unparsed"]
        total = sum((h.market_value or Decimal("0")) for h in holdings)
        return [
            f"CAS declares {len(holdings)} equity holding(s) totalling {total} as "
            "declared quantities -- see holding_snapshot for reconciliation"
        ]

    @staticmethod
    def _as_of_date(text: str) -> date:
        match = AS_ON_LINE.search(text)
        if match:
            try:
                return datetime.strptime(match.group("date"), "%d-%m-%Y").date()
            except ValueError:
                pass
        return date.today()

    @staticmethod
    def _mf_folio_note(text: str) -> str | None:
        count = text.count("Folio No :")
        if count == 0:
            return None
        return (
            f"{count} mutual fund folio(s) present in this CAS were not parsed -- "
            "Phase 1 is equities-only (CLAUDE.md scope); see docs/INGESTION.md"
        )
