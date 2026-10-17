"""CDSL Consolidated Account Statement (CAS) PDF adapter -- holdings and discovery ONLY.

CLAUDE.md is explicit: a CAS "may write ``holding_snapshot`` and may identify which
brokers an investor actually holds. It may **never** produce a ``Transaction``." This
adapter therefore returns no staged rows, ever. What it produces is a list of
``DeclaredHolding`` (every ISIN and quantity the depository declares at the statement
date) plus notes describing the depository movements it read and did not import.

WHY NOTHING FROM "TRANSACTION DETAILS" IS IMPORTED -- found against 11 real monthly CDSL
statements (Mar-2025 .. Jul-2026, one investor). An earlier version staged BONUS and
SPLIT_ADJ rows from this section by looking for "BONUS"/"SPLIT"/"SUB-DIVISION" within a
few lines of a credit. Every row it staged across those statements was wrong:

  - CDSL prints "AFTER SUB-DIVISION" / "AFTER SPLIT" in the SECURITY NAME of any stock
    that once split ("BLS INTERNATIONAL ... FACE VALUE RE1/- AFTER SUB-DIVISION"), so
    ordinary purchase settlements (PAYOUT-CR, INTDEP-CR, ON-CR) became free shares at
    price 0 -- BLS +100 on 28-07-2026 was a purchase, not a split.
  - The keyword window bled into the NEIGHBOURING security: CAPLIN POINT's ON-CR
    purchase was tagged BONUS because GUJARAT THEMIS's "CA-Bonus" row sat below it.
  - A continuation row (CDSL omits the ISIN and the opening balance on a security's
    second movement: "28-07-2026 315.000 -- 846.000 0") was read with the credit as the
    opening balance, so +315 became +531.
  - A bonus is TWO depository events: "CA-Bonus" credits a temporary ISIN
    (IN8942C01028, "listing/trading approval awaited"), then "CA-Listed Bonus" debits it
    and credits the real ISIN. Read row by row, one 162-share bonus became two -- one of
    them on a different company, because the temporary ISIN failed the INE/INF pattern
    and the row inherited the previous ISIN.
  - "CA-Listed Rights" (South Indian Bank) is a PAID rights allotment; it was staged as
    a free split.

And even read perfectly, the section carries no price for any equity movement, so the
only honest ledger row it could ever produce is one with an invented price. Corporate
actions are closed by rule 20 (docs/ARCHITECTURE_INVARIANTS.md), and a CAS CONFIRMS or
REVERSES those inferred rows through ``holding_snapshot`` (app.db.auto_reversal) --
which is what this adapter feeds.

WHY THE HOLDINGS TABLE IS VALIDATED, NOT TRUSTED. Every holdings row the parser keeps
must satisfy ``quantity x price == value`` to the paisa, and the whole table must
reproduce the statement's own totals: the ISIN count and value printed for the demat
account in the page-1 summary, and the "Portfolio Value ... as on" line. A statement
that does not add up raises ``CasStatementMismatchError`` naming what disagreed; nothing
is stored. All 11 real statements reproduce both totals exactly.

Facts about the holdings table established from those statements:

  - Columns are Current / Frozen / Pledge / Pledge Setup / Free balance, then market
    price and value. Quantity is CURRENT (total owned). An earlier version read the
    fifth column, which would undercount any pledged holding.
  - A position closed during the period is still listed, as "-- -- -- -- -- <price>
    0.00". It is recorded as quantity 0: an explicit depository statement that nothing
    is held is exactly what proves a sale reached the ledger, and it supersedes the
    older nonzero snapshot that ``app.db.holdings.latest_snapshot`` would otherwise keep
    reporting.
  - Temporary ISINs (IN8.../IN9...) appear, followed by CDSL's legend marks ("*" =
    ISIN frozen for debit). They are part of the declared count, so they are kept.

WHY TEXT EXTRACTION ANCHORS ON ROW SHAPE, NOT SECTION HEADERS. CDSL prints the
letterhead and every column header in three interleaved languages (English, Tamil,
Hindi), which pdfplumber extracts as unreadable garbage. DATA rows -- ISINs, dates,
quantities, prices -- carry no vernacular text and extract cleanly. Totals checks are
what make shape-anchoring safe: a row the shape misses breaks the count.

WHY ONLY CDSL. The only real statements this has been run against are CDSL-issued. An
NSDL-issued CAS or a CAMS/KFintech (mutual fund) CAS has a different layout; neither is
guessed at. One would fail the totals check loudly rather than import partially.

MULTI-ACCOUNT NOTE. A CAS spans every demat account on the PAN (the real statements
list Zerodha, Groww and Axis). When exactly one account lists any security, every
holding is attributed to it via ``broker_accounts``. When more than one does, the
per-DP section separators deeper in the document render as garbled as the letterhead,
so which ISIN belongs to which DP cannot be read reliably -- the statement is REFUSED
with ``CasAccountUnresolvedError`` naming the accounts, never attributed to a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.ingest.base import DocumentIdentity, ParseResult, Rejection
from app.ingest.cas_folios import DeclaredFundFolio, FolioParseError, parse_folios
from app.isin import has_valid_check_digit

#: Clean, un-garbled in the real sample -- this is the one line of the page-1 title
#: block that renders without trilingual interleaving.
CAS_MARKER = "CONSOLIDATED ACCOUNT STATEMENT"

#: Any ISIN the depository can print, temporary ones included (IN8942C01028), followed by
#: CDSL's optional legend marks. The check digit is verified separately.
_ISIN = r"IN[A-Z0-9]{9}\d"
_LEGEND_MARKS = r"[*~#<>@$^]*"
_QTY = r"(?:--|[\d,]+\.\d+)"
_AMOUNT = r"[\d,]+\.\d+"
_DATE = re.compile(r"\d{2}-\d{2}-\d{4}")

#: Holdings row: ISIN, optional description words, then exactly five balance columns
#: (Current, Frozen, Pledge, Pledge Setup, Free), market price and value. All 152
#: holdings rows in the 11 real statements have exactly this seven-column tail.
HOLDING_ROW = re.compile(
    rf"^(?P<isin>{_ISIN}){_LEGEND_MARKS}\s+(?:.*?\s)?"
    rf"(?P<current>{_QTY})\s+{_QTY}\s+{_QTY}\s+{_QTY}\s+{_QTY}\s+"
    rf"(?P<price>{_AMOUNT})\s+(?P<value>{_AMOUNT})$"
)
LEADING_ISIN = re.compile(rf"^(?P<isin>{_ISIN}){_LEGEND_MARKS}(?:\s|$)")

#: A Transaction Details row: a date followed by a run of balance figures and the stamp
#: duty column. Only counted and described, never imported -- see the module docstring.
MOVEMENT_ROW = re.compile(
    rf"(?P<date>\d{{2}}-\d{{2}}-\d{{4}})\s+(?P<balances>{_QTY}(?:\s+{_QTY})+)\s+"
    r"[\d,]+(?:\.\d+)?\s*$"
)
#: A corporate-action movement carries "Cr Current" / "Db Current" on its data line.
CORPORATE_ACTION_ROW = re.compile(r"\b(?P<direction>Cr|Db) Current\b")

#: The clean per-account summary on page 1 -- "ZERODHA BROKING LIMITED" /
#: "CDSL Demat Account 18 12,75,726.2" / "DP Id: ... Client Id: ...", one triplet per
#: account. The number after "Demat Account" is the count of ISINs in that account.
DP_SUMMARY_ROW = re.compile(r"^(?P<broker>[A-Z][A-Z0-9 .&\-]+?(?:LIMITED|LTD))\s*$")
DP_VALUE_LINE = re.compile(
    r"^(?:CDSL|NSDL) Demat Account\s+(?P<count>\d+)\s+(?P<value>[\d,]+\.\d+)$"
)

#: "Portfolio Value ` 12,75,726.20 as on 31-07-2026" -- printed right after a demat
#: account's holdings table. Its date is the snapshot date; its value is a total the
#: parsed rows must reproduce.
AS_ON_LINE = re.compile(
    r"Portfolio Value `?\s*(?P<value>[\d,]+\.\d+)\s+as on\s+(?P<date>\d{2}-\d{2}-\d{4})",
    re.I,
)

#: ``value`` is printed to the paisa; ``quantity x price`` may differ by its rounding.
_PAISA = Decimal("0.01")


class PdfTextExtractionError(RuntimeError):
    """The PDF could not be opened with the password given, or produced no text."""


def _decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", "").strip())


def _quantity(raw: str) -> Decimal:
    return Decimal("0") if raw == "--" else _decimal(raw)


def extract_text(content: bytes, password: str = "") -> str:
    """Unlock with ``password`` EXACTLY as given ("" for a file with none) and extract text.

    No case variants: the caller passes the password app.ingest.file_validation.unlock_pdf
    found opens this file, so a second guess here could only open it with something the
    investor never gave us.

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

    try:
        with pdfplumber.open(io.BytesIO(content), password=password) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:  # noqa: BLE001 - pdfplumber/pypdf raise assorted types
        raise PdfTextExtractionError(f"could not open CAS PDF: {exc}") from exc


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


@dataclass(frozen=True, slots=True)
class _DematAccount:
    """One row of the page-1 account summary."""

    dp_name: str
    isin_count: int
    value: Decimal


def _match_broker(dp_name: str, broker_accounts: dict[str, str]) -> str | None:
    """The account whose registered broker matches this DP name.

    Substring, not equality: the DP name in a CAS carries legal-entity suffixes
    ('BROKING LIMITED', 'INVEST TECH PRIVATE LIMITED') that no account.broker value
    carries, e.g. account.broker='zerodha' must match DP name
    'ZERODHA BROKING LIMITED'.
    """
    name = dp_name.lower()
    matches = {
        account_id for broker, account_id in broker_accounts.items() if broker and broker in name
    }
    return next(iter(matches)) if len(matches) == 1 else None


class CasStatementError(ValueError):
    """This CAS cannot be imported. The message is written for the investor: it says
    what was wrong and what to do. Nothing from the statement is staged or stored."""


class CasAccountUnresolvedError(CasStatementError):
    """This CAS cannot be attributed to exactly one of the investor's accounts. Names
    the depository accounts the statement shows."""


class CasStatementMismatchError(CasStatementError):
    """The holdings read from this CAS do not reproduce the statement's own totals (or
    the PDF could not be read at all), so the layout is not one this parser reads
    correctly. Raised rather than storing a partial or misread holdings list."""


class CasPdfAdapter:
    """CDSL Consolidated Account Statement PDF. See module docstring for the scope
    decisions (no Transactions, CDSL only, one account per statement, totals-checked)."""

    name = "cas_pdf"

    def __init__(self, password: str = "", broker_accounts: dict[str, str] | None = None):
        self.password = password
        self._text_content: bytes | None = None
        self._text = ""
        self._movement_content: bytes | None = None
        self._movements: list[CasMovement] = []
        self.broker_accounts = {k.lower(): v for k, v in (broker_accounts or {}).items()}

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        try:
            text = self._read_text(content)
        except PdfTextExtractionError as exc:
            return ParseResult(rejections=[Rejection(0, str(exc))])
        except UnicodeDecodeError:
            return ParseResult(rejections=[Rejection(0, "not a readable CAS PDF")])

        # Text-only callers retain their diagnostic summary. For actual PDFs the
        # geometry reader below supplies it: flat-text row counts are not a second
        # authority for the same movements.
        result = self.parse_text(
            text, account_id, filename, include_movement_notes=content[:5] != b"%PDF-"
        )
        if content[:5] == b"%PDF-" and not result.rejections:
            movements = self.extract_movements(content)
            declared = {h.isin: h.quantity for h in self.parse_declared_holdings(content)}
            closing = {m.isin: m.closing for m in movements}
            for isin, quantity in closing.items():
                if isin not in declared or declared[isin] != quantity:
                    raise CasStatementMismatchError(
                        f"Movement closing balance for {isin} does not match the holdings table."
                    )
            result.notes.append(_movement_summary(movements))
        return result

    def _read_text(self, content: bytes) -> str:
        if content != self._text_content:
            text = (
                extract_text(content, self.password)
                if content[:5] == b"%PDF-"
                else content.decode("utf-8")
            )
            self._text_content, self._text = content, text
        return self._text

    def extract_movements(self, content: bytes) -> list[CasMovement]:
        if content != self._movement_content:
            movements = extract_movements(content, self.password)
            self._movement_content, self._movements = content, movements
        return list(self._movements)

    def identity(self, content: bytes) -> DocumentIdentity:
        """Read the investor PAN printed in the CAS, never derive it from its password.

        A CAS has no single broker UCC: its direct-MF and demat accounts can have
        different identifiers. Only the parenthesised holder PAN is used here;
        unrelated PAN-shaped text and folio UCCs cannot select an investor/account.
        Multiple distinct holder PANs are unsupported by this single-investor adapter.
        The existing import identity gate checks the result before storing evidence.
        """
        try:
            text = self._read_text(content)
        except (PdfTextExtractionError, UnicodeDecodeError):
            return DocumentIdentity()
        pans = {
            match.upper()
            for match in re.findall(r"\(\s*PAN\s*:\s*([A-Z]{5}\d{4}[A-Z])\s*\)", text, re.I)
        }
        if len(pans) > 1:
            raise CasAccountUnresolvedError(
                "This CAS names more than one investor PAN. Trackr+ cannot yet attribute "
                "its sections safely, so nothing from it was imported."
            )
        return DocumentIdentity(pan=next(iter(pans)) if pans else "")

    def parse_text(
        self, text: str, account_id: str, filename: str = "", *, include_movement_notes: bool = True
    ) -> ParseResult:
        """Never any staged rows. Validates the holdings table (raising
        ``CasStatementError`` if it cannot be attributed or does not add up) so a bad
        statement is refused here, before a batch exists, and describes what was read."""
        if CAS_MARKER not in text.upper():
            return ParseResult(rejections=[Rejection(0, "not a CAS: header marker absent")])

        holdings = self._extract_holdings(text, self._resolve_account(text))
        total = sum((h.market_value or Decimal("0")) for h in holdings)
        held = sum(1 for h in holdings if h.quantity > 0)
        notes = [
            f"CAS declares {len(holdings)} ISIN(s) as of {holdings[0].snap_date.isoformat()} "
            f"({held} held, {len(holdings) - held} at zero), totalling {total}; matches the "
            "statement's own totals -- recorded in holding_snapshot for reconciliation"
        ]

        mf_note = self._mf_folio_note(text)
        if mf_note:
            notes.append(mf_note)
        if include_movement_notes:
            notes.extend(self._movement_notes(text))
        return ParseResult(notes=notes)

    def parse_declared_holdings(
        self, content: bytes, account_id: str = ""
    ) -> list[DeclaredHolding]:
        """CAS-declared quantities per ISIN, for ``holding_snapshot`` -- see
        app.db.holdings.record_snapshot. Pure: reuses the same unlocked text as
        parse(), does no I/O of its own. Raises ``CasStatementError`` rather than
        returning an empty list for a statement it cannot read."""
        try:
            text = self._read_text(content)
        except PdfTextExtractionError as exc:
            raise CasStatementMismatchError(
                "Trackr+ could not open this CAS with the password it was given. Upload it "
                "again and enter the statement's password when asked. Nothing was imported."
            ) from exc
        except UnicodeDecodeError as exc:
            raise CasStatementMismatchError(
                "This file is not a readable CAS PDF. Nothing was imported."
            ) from exc
        return self._extract_holdings(text, self._resolve_account(text))

    def parse_declared_folios(self, content: bytes) -> list[DeclaredFundFolio]:
        """Direct MF balances with their printed date, quantities, NAV and value."""
        text = self._read_text(content)
        on, _ = self._portfolio_value(text)
        end = list(AS_ON_LINE.finditer(text))[-1].end()
        try:
            return parse_folios(text, on, end)
        except FolioParseError as exc:
            raise CasStatementMismatchError(str(exc)) from exc

    # -- internal ----------------------------------------------------------------

    def _resolve_account(self, text: str) -> str:
        """The one account every holding in this CAS belongs to, matched from the
        statement's own DP summary against ``broker_accounts``. Raises
        ``CasAccountUnresolvedError`` rather than falling back to any other account. See
        the module's "MULTI-ACCOUNT NOTE"."""
        listed = [a.dp_name for a in self._demat_accounts(text) if a.isin_count or a.value]
        if len(listed) == 1:
            matched = _match_broker(listed[0], self.broker_accounts)
            if matched:
                return matched
            raise CasAccountUnresolvedError(
                f"This CAS shows holdings at {listed[0]}, but none of your accounts in "
                f"Trackr+ is with that broker. Add that account in Settings, then upload "
                f"the CAS again. Nothing from this statement was imported."
            )
        if len(listed) > 1:
            raise CasAccountUnresolvedError(
                f"This CAS shows holdings at {len(listed)} demat accounts "
                f"({', '.join(listed)}). Trackr+ cannot yet tell which holding belongs to "
                f"which account inside one statement, so nothing from it was imported."
            )
        raise CasAccountUnresolvedError(
            "Trackr+ could not find which demat account this CAS belongs to: its account "
            "summary lists no demat account with a balance. Nothing from this statement "
            "was imported."
        )

    @staticmethod
    def _demat_accounts(text: str) -> list[_DematAccount]:
        """Every demat account in the page-1 summary, with its ISIN count and value."""
        lines = [ln.strip() for ln in text.splitlines()]
        accounts: list[_DematAccount] = []
        for i, line in enumerate(lines):
            name_match = DP_SUMMARY_ROW.match(line)
            if not name_match:
                continue
            for lookahead in lines[i + 1 : i + 3]:
                value_match = DP_VALUE_LINE.match(lookahead)
                if not value_match:
                    continue
                accounts.append(
                    _DematAccount(
                        dp_name=name_match.group("broker"),
                        isin_count=int(value_match.group("count")),
                        value=_decimal(value_match.group("value")),
                    )
                )
                break
        return accounts

    def _extract_holdings(self, text: str, account_id: str) -> list[DeclaredHolding]:
        """Every holdings row, checked row by row and against the statement's totals.

        ``account_id`` comes from ``_resolve_account``, which has already established
        that exactly one demat account lists any security -- so that account's summary
        line is the one the table must reproduce.
        """
        as_of, stated_total = self._portfolio_value(text)
        (account,) = [a for a in self._demat_accounts(text) if a.isin_count or a.value]

        holdings: list[DeclaredHolding] = []
        seen: set[str] = set()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            lead = LEADING_ISIN.match(line)
            # A Transaction Details row also starts with an ISIN, but always carries a
            # date; a holdings row never does.
            if not lead or _DATE.search(line):
                continue
            isin = lead.group("isin")
            row = HOLDING_ROW.match(line)
            if not row or not has_valid_check_digit(isin):
                raise CasStatementMismatchError(
                    f"Trackr+ could not read the holdings row for {isin} in this CAS, so "
                    "it cannot be checked against your ledger. Nothing was imported."
                )
            if isin in seen:
                raise CasStatementMismatchError(
                    f"This CAS repeats the holdings row for {isin} within one account. "
                    "Trackr+ cannot check its completeness, so nothing was imported."
                )
            seen.add(isin)
            quantity = _quantity(row.group("current"))
            price = _decimal(row.group("price"))
            value = _decimal(row.group("value"))
            if abs(quantity * price - value) > _PAISA:
                raise CasStatementMismatchError(
                    f"The CAS row for {isin} does not add up ({quantity} x {price} is not "
                    f"{value}), so it was not read correctly. Nothing was imported."
                )
            holdings.append(
                DeclaredHolding(
                    isin=isin,
                    account_id=account_id,
                    quantity=quantity,
                    snap_date=as_of,
                    market_price=price,
                    market_value=value,
                )
            )

        read_total = sum((h.market_value or Decimal("0")) for h in holdings)
        if len(holdings) != account.isin_count or read_total != stated_total:
            raise CasStatementMismatchError(
                f"This CAS lists {account.isin_count} securities worth {stated_total} at "
                f"{account.dp_name}, but Trackr+ read {len(holdings)} worth {read_total}. "
                "The statement's layout is not one Trackr+ reads correctly yet, so nothing "
                "was imported."
            )
        if account.value != stated_total:
            raise CasStatementMismatchError(
                f"This CAS's account summary says {account.value} at {account.dp_name} "
                f"but its holdings table totals {stated_total}. Nothing was imported."
            )
        return holdings

    @staticmethod
    def _portfolio_value(text: str) -> tuple[date, Decimal]:
        """The statement date and demat portfolio total from the "Portfolio Value ...
        as on" lines. Raises when absent or when they disagree on the date -- the
        snapshot date is never guessed."""
        matches = AS_ON_LINE.findall(text)
        dates = {d for _, d in matches}
        if len(dates) != 1:
            raise CasStatementMismatchError(
                "Trackr+ could not find this CAS's statement date (the 'Portfolio Value "
                "... as on' line). Nothing was imported."
            )
        try:
            as_of = datetime.strptime(dates.pop(), "%d-%m-%Y").date()
        except ValueError as exc:
            raise CasStatementMismatchError("This CAS has an invalid statement date.") from exc
        return as_of, sum((_decimal(v) for v, _ in matches), Decimal("0"))

    @staticmethod
    def _movement_notes(text: str) -> list[str]:
        """What the Transaction Details section contained, stated -- not imported."""
        movements = 0
        corporate: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            row = MOVEMENT_ROW.search(line)
            if not row:
                continue
            movements += 1
            action = CORPORATE_ACTION_ROW.search(line)
            if not action:
                continue
            lead = LEADING_ISIN.match(line)
            isin = lead.group("isin") if lead else "ISIN not printed on the row"
            direction = "credit" if action.group("direction") == "Cr" else "debit"
            closing = row.group("balances").split()[-1]
            corporate.append(f"{row.group('date')} {isin} {direction}, balance after {closing}")
        if not movements:
            return []
        notes = [
            f"{movements} depository movement(s) read and not imported: a CAS carries no "
            "price, so it never creates ledger rows (corporate actions are resolved by "
            "rule 20 and confirmed against this statement's holdings)"
        ]
        if corporate:
            notes.append("Corporate-action movements in this CAS: " + "; ".join(corporate))
        return notes

    @staticmethod
    def _mf_folio_note(text: str) -> str | None:
        count = text.count("Folio No :")
        if count == 0:
            return None
        named = re.findall(
            r"Scheme Name\s*:\s*(.*?)\s+Scheme Code\s*:\s*[^\n]+\n"
            r"(?:(?!Scheme Name).)*?ISIN\s*:\s*(INF[A-Z0-9]{8}\d)",
            text,
            re.S,
        )
        detail = "; ".join(f"{name.strip()} ({isin})" for name, isin in named)
        return (
            f"{count} direct mutual fund folio(s) listed separately from demat holdings"
            + (f": {detail}" if detail else "")
            + ". Validated balances are retained as dated statement evidence; "
            "fund transaction history is needed for cost basis and returns."
        )


# -- movements, read by position -----------------------------------------------------

#: Column bands on a CDSL Transaction Details page, as x ranges in PDF points (measured on
#: 7 real statements, Apr 2024 - Mar 2026: right edges 406 / 448 / 490 / 536). A
#: character belongs to the band its centre falls in.
_ISIN_BAND = (15.0, 96.0)
_DESC_BAND = (96.0, 218.0)
_PARTICULARS_BAND = (218.0, 312.0)
_DATE_BAND = (310.0, 362.0)
_TXN_BANDS = (
    ("opening", 360.0, 408.0),
    ("credit", 408.0, 450.0),
    ("debit", 450.0, 492.0),
    ("closing", 492.0, 538.0),
)
_DATE = re.compile(r"\d{2}-\d{2}-\d{4}")
_ACCOUNT_HEADER = re.compile(r"\b(?:BO ID|DP ?ID)\b")
#: The narration code CDSL prints in the particulars column of a movement.
_CODE = re.compile(r"^(?:CA-[A-Za-z ]+|[A-Z]+-(?:CR|DR)|BSEDR|NSEDR|BSECR|NSECR)\b")

#: Narration code -> what the movement is. Read from the movement's OWN particulars,
#: never from its neighbours (the defect the keyword window had).
_PURCHASE_CODES = {"PAYOUT-CR", "ON-CR", "BSECH-CR", "NSECH-CR", "INTDEP-CR", "BSECR", "NSECR"}
_SALE_CODES = {"EP-DR", "PAYIN-DR", "BSEDR", "NSEDR", "ON-DR"}


@dataclass(frozen=True, slots=True)
class CasMovement:
    """One Transaction Details row. ``settle_date`` is the depository date -- the day
    shares moved, which is the trade's settlement date, not its trade date."""

    isin: str
    settle_date: date
    opening: Decimal | None
    credit: Decimal
    debit: Decimal
    closing: Decimal
    code: str
    kind: str
    page: int

    @property
    def net(self) -> Decimal:
        return self.credit - self.debit


def _kind(code: str, particulars: str, isin: str, credit: Decimal) -> str:
    upper = f"{code} {particulars}".upper()
    if code.upper().startswith("CA-"):
        if "BONUS" in upper:
            return "BONUS"
        if "SPLIT" in upper or "CONSOLIDATION" in upper:
            return "SPLIT"
        if "RIGHTS" in upper:
            return "RIGHTS"
        return "CORPORATE_ACTION"
    # Settlement narration describes a movement, not a verified instrument category.
    # INF alone cannot distinguish an ETF from an unlisted mutual fund.
    fund = isin.startswith("INF")
    if code in _PURCHASE_CODES:
        if code == "INTDEP-CR" and " CM " not in f" {upper} ":
            return "TRANSFER_IN"
        return (
            "FUND_PURCHASE"
            if fund and code in {"BSECH-CR", "NSECH-CR", "BSECR", "NSECR"}
            else "PURCHASE"
        )
    if code in _SALE_CODES:
        return "FUND_REDEMPTION" if fund and code in {"BSEDR", "NSEDR"} else "SALE"
    if code in {"INTDEP-DR", "OF-DR"}:
        return "TRANSFER_OUT"
    if code == "OF-CR":
        return "TRANSFER_IN"
    return "UNKNOWN"


def _qty(raw: str) -> Decimal | None:
    raw = raw.replace(",", "").strip()
    if raw == "":
        return None
    if raw == "--":
        return Decimal("0")
    return Decimal(raw)


def _band_text(chars: list[dict], lo: float, hi: float) -> str:
    out, prev = "", None
    for ch in chars:
        mid = (ch["x0"] + ch["x1"]) / 2
        if not lo <= mid < hi:
            continue
        if prev is not None and ch["x0"] - prev > 1.2:
            out += " "
        out += ch["text"]
        prev = ch["x1"]
    return out.strip()


def _visual_lines(page) -> list[tuple[float, list[dict]]]:
    rows: dict[float, list[dict]] = {}
    for ch in page.chars:
        top = ch["top"]
        key = next((k for k in rows if abs(k - top) <= 1.5), top)
        rows.setdefault(key, []).append(ch)
    return [(top, sorted(rows[top], key=lambda c: c["x0"])) for top in sorted(rows)]


def extract_movements(content: bytes, password: str = "") -> list[CasMovement]:
    """All Transaction Details rows of a CDSL CAS, in statement order.

    Each row's ISIN is its own, or -- when CDSL omits it on a security's later rows -- the
    one above it within the same demat account. Its narration code comes from the
    particulars lines nearer to it than to any other row. Every row is checked:
    opening (or, when blank, the previous closing of that ISIN) + credit - debit must equal
    closing; a row that fails raises ``CasStatementError`` instead of being passed on.
    """
    import io

    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(content), password=password)
    except Exception as exc:  # noqa: BLE001 - pdfplumber raises assorted types
        raise PdfTextExtractionError(f"could not open CAS PDF: {exc}") from exc
    movements: list[CasMovement] = []
    with pdf:
        last_isin: str | None = None
        balances: dict[str, Decimal] = {}
        for page_no, page in enumerate(pdf.pages, 1):
            lines = _visual_lines(page)
            anchors = [top for top, chars in lines if _DATE.search(_band_text(chars, *_DATE_BAND))]
            for top, chars in lines:
                full = _band_text(chars, 0, 10_000)
                if _ACCOUNT_HEADER.search(full):
                    last_isin = None
                    balances.clear()
                    continue
                date_match = _DATE.search(_band_text(chars, *_DATE_BAND))
                if not date_match:
                    continue
                isin_text = _band_text(chars, *_ISIN_BAND).split(" ")[0]
                isin_match = LEADING_ISIN.match(isin_text + " ")
                if isin_match:
                    last_isin = isin_match.group("isin").upper()
                if last_isin is None or not has_valid_check_digit(last_isin):
                    raise CasStatementError(
                        f"A movement on page {page_no} ({date_match.group(0)}) has no ISIN "
                        "Trackr+ can attribute it to. Nothing from this statement was imported."
                    )
                try:
                    cols = {name: _qty(_band_text(chars, lo, hi)) for name, lo, hi in _TXN_BANDS}
                    settle_date = datetime.strptime(date_match.group(0), "%d-%m-%Y").date()
                except (InvalidOperation, ValueError) as exc:
                    raise CasStatementMismatchError(
                        f"Unreadable movement for {last_isin} on page {page_no}."
                    ) from exc
                if cols["credit"] is None or cols["debit"] is None:
                    raise CasStatementMismatchError(
                        f"Missing credit/debit column for {last_isin} on page {page_no}."
                    )
                credit, debit, closing = cols["credit"], cols["debit"], cols["closing"]
                opening = cols["opening"]
                start = opening if opening is not None else balances.get(last_isin)
                if (
                    closing is None
                    or start is None
                    or min(start, credit, debit, closing) < 0
                    or start + credit - debit != closing
                ):
                    raise CasStatementError(
                        f"Trackr+ misread a movement for {last_isin} on {date_match.group(0)} "
                        f"(page {page_no}): its balances do not add up. Nothing from this "
                        "statement was imported."
                    )
                if last_isin in balances and opening is not None and opening != balances[last_isin]:
                    raise CasStatementError(
                        f"Discontinuous balances for {last_isin} on page {page_no}."
                    )
                balances[last_isin] = closing
                k = anchors.index(top)
                lo_b = (anchors[k - 1] + top) / 2 if k else top - 30
                hi_b = (anchors[k + 1] + top) / 2 if k + 1 < len(anchors) else top + 30
                own = sorted(
                    (
                        (abs(t - top), _band_text(c, *_PARTICULARS_BAND))
                        for t, c in lines
                        if lo_b < t < hi_b
                    ),
                    key=lambda item: item[0],
                )
                particulars = " ".join(text for _, text in own if text)
                code = next((m.group(0).strip() for _, text in own if (m := _CODE.match(text))), "")
                movements.append(
                    CasMovement(
                        isin=last_isin,
                        settle_date=settle_date,
                        opening=opening,
                        credit=credit,
                        debit=debit,
                        closing=closing,
                        code=code,
                        kind=_kind(code, particulars, last_isin, credit),
                        page=page_no,
                    )
                )
    return movements


_KIND_WORDS = {
    "PURCHASE": "purchase",
    "SALE": "sale",
    "FUND_PURCHASE": "fund purchase",
    "FUND_REDEMPTION": "fund redemption",
    "BONUS": "bonus",
    "SPLIT": "split",
    "RIGHTS": "rights",
    "CORPORATE_ACTION": "corporate action",
    "TRANSFER_IN": "transfer in",
    "TRANSFER_OUT": "transfer out",
    "UNKNOWN": "unclassified movement",
}


def _movement_summary(movements: list[CasMovement]) -> str:
    if not movements:
        return "No movements in this statement's period."
    counts: dict[str, int] = {}
    for m in movements:
        counts[m.kind] = counts.get(m.kind, 0) + 1
    parts = ", ".join(f"{n} {_KIND_WORDS.get(k, k.lower())} movement(s)" for k, n in counts.items())
    return (
        f"{len(movements)} movement(s) read: {parts}. A CAS has no prices, so none of them "
        "becomes a ledger row; contract-note evidence is needed to check these movements."
    )
