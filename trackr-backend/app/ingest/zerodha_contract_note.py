"""Zerodha contract-note PDF adapter.

The contract note is the only document that carries CHARGES. A tradebook has quantities
and prices but no brokerage, STT, GST, SEBI turnover fee or stamp duty, so a cost basis
built from tradebooks alone is understated by exactly those. This adapter is how they get
in.

Two facts about the format that shape everything here:

**Charges are stated once per NOTE, not per trade.** A single note can cover eight trades
across five securities and reports one brokerage figure, one STT figure, and so on. Getting
them onto individual transactions therefore requires apportionment, which is a derivation
rather than a reading -- and that is why every row from this adapter scores
CONFIDENCE_DERIVED rather than CONFIDENCE_CERTAIN.

**The broker's own SEBI registration looks like an ISIN.** `INZ000031633` matches a naive
`^IN[A-Z0-9]{10}$`, and it appears in the header of every note. Real Indian securities are
`INE` (equity) or `INF` (fund), so the prefix is narrowed here. Without that, every note
would yield a phantom security named after the broker's licence.

**F&O lines become ``ParseResult.fno_rows``**, unresolved (docs/INGESTION.md "F&O
adapter"): the contract symbol is resolved at staging, and an F&O-only note's charges --
each charge line's NET TOTAL -- are apportioned across its F&O trades only. A note with
both segments cannot be split from PDF text, so its F&O rows are refused by name.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.fno_types import FnoSide, FnoTradeKind
from app.engine.types import Source, Transaction, TxnType
from app.ingest.base import (
    CONFIDENCE_DERIVED,
    DocumentIdentity,
    FnoParsedRow,
    ParseResult,
    Rejection,
    StagedRow,
)

#: INE = equity, INF = mutual fund. Deliberately NOT [A-Z0-9]: that admits INZ000031633,
#: Zerodha's SEBI registration, which is printed on every note.
ISIN_PATTERN = re.compile(r"^IN[EF][A-Z0-9]{9}$")

#: order-no time trade-no time SYMBOL-SERIES/ISIN B|S EXCHANGE qty brokerage rate ...
TRADE_ROW = re.compile(
    r"^(?P<order>\d{10,})\s+\S+\s+(?P<trade>\d+)\s+\S+\s+"
    r"(?P<symbol>[A-Z0-9&.\-]+?)\s*-\s*\w+\s*/\s*(?P<isin>IN[A-Z0-9]{10})\s+"
    r"(?P<side>[BS])\s+(?P<exchange>\w+)\s+(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<brokerage>[\d,]+(?:\.\d+)?)\s+(?P<rate>[\d,]+(?:\.\d+)?)"
)

#: An F&O trade line: no ISIN, a contract symbol, then quantity and the gross rate
#: (real notes 34 and 35: "... NIFTY2481424200PE B NSE 75 100.15 100.15 (7511.25)").
FNO_TRADE_ROW = re.compile(
    r"^(?P<order>\d{5,})\s+(?P<order_time>\d{1,2}:\d{2}:\d{2})\s+(?P<trade>\d+)\s+"
    r"(?P<trade_time>\d{1,2}:\d{2}:\d{2})\s+(?P<symbol>[A-Z0-9&.\-]+(?:CE|PE|FUT))\s+"
    r"(?P<side>[BS])\s+(?P<exchange>\w+)\s+(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<rate>[\d,]+\.\d+)\s"
)

#: Zerodha changed the layout between 2023 and 2026: older notes print "Tradedate:" and
#: "ContractNoteno.:" with no spaces, newer ones "Trade Date:" and "Contract Note No:".
#: A client's history spans the change, so both have to work -- and three of the forty real
#: notes in this project are the older kind.
TRADE_DATE = re.compile(r"Trade\s*date\s*:\s*(\d{2}/\d{2}/\d{4})", re.I)
CONTRACT_NUMBER = re.compile(r"Contract\s*Note\s*no\.?\s*:\s*(\S+)", re.I)

#: The client's own identifiers, printed in the header block of every note.
#:
#: THE LABEL IS LOAD-BEARING, exactly as the ISIN prefix is above. A contract note is a
#: note *cum tax invoice*, so it also carries the broker's GSTIN -- and a GSTIN embeds a
#: PAN (`29AABCZ1234M1ZX`). All 40 real notes in this project contain exactly two
#: PAN-shaped tokens: the client's and Zerodha's own. A bare `[A-Z]{5}\d{4}[A-Z]` match
#: therefore has a coin-flip chance of verifying a document against the BROKER's identity,
#: which would pass for every investor alike and verify nothing at all. Anchoring on the
#: label yields exactly one match on all 40.
#:
#: The value pattern stays strict (5 letters, 4 digits, 1 letter) so a loose label match
#: still cannot admit a non-PAN, and the label side stays loose ("PAN:", "PAN No:",
#: "PAN of Client") because that wording is the part that varies between brokers.
CLIENT_PAN = re.compile(
    r"\bPAN\b\s*(?:of\s*Client)?\s*(?:No\.?)?\s*[:\-]?\s*([A-Z]{5}\d{4}[A-Z])", re.I
)

#: UCC -- Zerodha's Unique Client Code, the broker's own handle for the account. Checked
#: against ``account.client_id``. Alphanumeric and short; bounded so a runaway match
#: cannot swallow the rest of the line.
CLIENT_UCC = re.compile(r"\bUCC\b\s*(?:of\s*Client)?\s*[:\-]?\s*([A-Z0-9]{3,12})\b", re.I)

#: Every charge line states its value as a bracketed negative. What sits between the label
#: and that value varies: a currency marker in its own brackets ("(Rs)", "(₹)"), a footnote
#: digit, or nothing. So the gap is matched lazily and the CAPTURE is required to be
#: numeric -- otherwise "(₹)" is read as the amount and the charge silently becomes zero.
_AMOUNT = r".{0,60}?\((\d[\d,]*(?:\.\d+)?)\)"

CHARGE_PATTERNS = {
    "brokerage": re.compile(r"Taxable value of Supply \(Brokerage\)" + _AMOUNT),
    "stt": re.compile(r"Securities [Tt]ransaction [Tt]ax" + _AMOUNT),
    "exchange": re.compile(r"Exchange transaction charges" + _AMOUNT),
    "igst": re.compile(r"IGST" + _AMOUNT),
    "cgst": re.compile(r"CGST" + _AMOUNT),
    "sgst": re.compile(r"SGST" + _AMOUNT),
    "sebi": re.compile(r"SEBI turnover fees" + _AMOUNT),
    "stamp": re.compile(r"Stamp duty" + _AMOUNT),
}

SIDES = {"B": TxnType.BUY, "S": TxnType.SELL}

# --- the note checks itself (2026-10-08) -----------------------------------------------
#
# Every Zerodha note prints, per trade, a "Net Total (Before Levies)" at the end of the
# trade line; per note, a pay-in/pay-out obligation, the charge lines and a net amount.
# Three identities must hold before a note's trades are trusted (same as the XLSX
# adapter's ``_bulk_rows``):
#
#   1. per trade: quantity x net rate = the line's printed net total (to the paisa);
#   2. per note: the printed net totals of EVERY trade line -- equity and F&O -- sum to
#      the obligation's NET TOTAL, exactly; a trade line the parser never read fails it;
#   3. per note: obligation + charge lines = net amount, within half a paisa per charge
#      line -- Zerodha rounds each printed charge separately, and its PDF and XLSX for the
#      same day differ by exactly that (12-08-2024: -346659.48 vs -346659.49).
#
# Verified against all 40 real PDF notes on this machine (2026-10-08): every one closes.

#: Any trade line, equity or derivative: order no, time, trade no, time, description,
#: side, exchange. Order numbers can be short ("111111" on an F&O expiry line).
ANY_TRADE_LINE = re.compile(
    r"^\d{5,}\s+\d{1,2}:\d{2}:\d{2}\s+\d+\s+\d{1,2}:\d{2}:\d{2}\s+\S.*?\s[BS]\s+\w+\s+"
)
#: A printed rupee amount: digits WITH paise, optionally bracketed (= negative). The
#: decimal point is required so footnote markers ("(Brokerage)2") are never read as money.
_MONEY = re.compile(r"\(?-?[\d,]+\.\d+\)?")
PAISA = Decimal("0.01")


def _last_amount(line: str) -> Decimal | None:
    tokens = _MONEY.findall(line)
    if not tokens:
        return None
    tok = tokens[-1]
    value = Decimal(tok.strip("()").replace(",", "").lstrip("-"))
    return -value if tok.startswith(("(", "-")) else value


def note_totals_problem(text: str) -> str | None:
    """Why the note's own arithmetic does not close (identities 2 and 3), or None."""
    trades = Decimal("0")
    obligation: Decimal | None = None
    net_amount: Decimal | None = None
    charge_lines = Decimal("0")
    charge_count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if ANY_TRADE_LINE.match(line):
            amount = _last_amount(line)
            if amount is None:
                return "a trade line's printed net total could not be read"
            trades += amount
            continue
        squashed = line.lower().replace(" ", "")
        if obligation is None and "payin/payoutobligation" in squashed:
            obligation = _last_amount(line)
        elif obligation is not None and net_amount is None:
            if squashed.startswith("netamount"):
                net_amount = _last_amount(line)
            else:
                amount = _last_amount(line)
                if amount is not None:
                    charge_lines += amount
                    charge_count += 1
    if obligation is None or net_amount is None:
        return "the note's pay-in/pay-out obligation or net amount was not found"
    if trades != obligation:
        return (
            f"trades total {trades} but the note's obligation is {obligation} "
            "(a trade may have been missed)"
        )
    if abs(obligation + charge_lines - net_amount) > Decimal("0.005") * max(charge_count, 1):
        return f"obligation {obligation} + charges {charge_lines} != net amount {net_amount}"
    return None


def note_charges_net_total(text: str) -> tuple[Decimal, Decimal]:
    """(every charge, STT) from each charge line's LAST amount -- its NET TOTAL column.

    Only meaningful on a note with no equity trade: then every charge is an F&O charge.
    PDF text drops blank cells, so on a note carrying both segments a two-amount line is
    either "equity, net" or "F&O, net" and the text cannot say which; the caller refuses
    that case rather than reading it. Same line walk as ``note_totals_problem``.
    """
    total = Decimal("0")
    stt = Decimal("0")
    in_charges = False
    for raw in text.splitlines():
        line = raw.strip()
        squashed = line.lower().replace(" ", "")
        if not in_charges:
            in_charges = "payin/payoutobligation" in squashed
            continue
        if squashed.startswith("netamount"):
            break
        amount = _last_amount(line)
        if amount is None:
            continue
        total += abs(amount)
        if squashed.startswith("securitiestransactiontax"):
            stt += abs(amount)
    return total, stt


CONTRACT_NOTE_MARKER = "CONTRACT NOTE"


class PdfTextExtractionError(RuntimeError):
    """The PDF could not be opened or produced no text."""


def _decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", "").strip())


def extract_text(content: bytes, password: str = "") -> str:
    """Pull text out of a contract-note PDF.

    Separated so tests can feed text directly and so the PDF library stays one import
    behind a seam -- pdfplumber is slow and heavy, and nothing else here needs it.
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
    except Exception as exc:
        raise PdfTextExtractionError(f"could not read PDF: {exc}") from exc


def parse_identity(text: str) -> DocumentIdentity:
    """Read the client identifiers off a note's header block.

    Pure text in, plain strings out -- no hashing, no database. The comparison against
    the destination account lives in ``app.db.identity``; keeping it out of here is what
    lets this module stay framework-free per docs/ARCHITECTURE.md.

    An absent identifier comes back empty rather than raising. A note that states nothing
    is a note this cannot vouch for, which is a different outcome from one that states
    somebody else -- and only the caller can decide what to do about either.
    """
    pan = CLIENT_PAN.search(text)
    ucc = CLIENT_UCC.search(text)
    return DocumentIdentity(
        pan=pan.group(1).upper() if pan else "",
        client_code=ucc.group(1).upper() if ucc else "",
    )


def parse_charges(text: str) -> dict[str, Decimal]:
    """Note-level charge components. Absent lines are simply absent, never assumed zero."""
    flat = " ".join(text.split())
    charges: dict[str, Decimal] = {}
    for name, pattern in CHARGE_PATTERNS.items():
        match = pattern.search(flat)
        if match:
            try:
                charges[name] = _decimal(match.group(1))
            except InvalidOperation:
                continue
    return charges


class ZerodhaContractNoteAdapter:
    """Contract notes: trades WITH charges.

    Charges are apportioned across the note's trades **pro rata by turnover**
    (quantity x price). That is an approximation, and a deliberate one: STT applies at
    different rates to buys and sells, brokerage is capped per order, and the note does not
    break any of it down per trade. Turnover is the honest single basis available, and the
    resulting rows are marked CONFIDENCE_DERIVED so a reviewer knows the charge figure was
    computed rather than read.

    The alternative -- charging everything to the first trade, or dropping charges
    entirely -- would be exact for one row and wrong for the rest, or understate every cost
    basis. Spreading it is the least wrong option, and saying so is the point of the
    confidence score.
    """

    name = "zerodha_contract_note"

    def __init__(self, password: str = ""):
        self.password = password

    def _text(self, content: bytes) -> str:
        """Bytes -> text, however the bytes arrived. Shared by ``parse`` and
        ``identity`` so the two can never disagree about what the document says.

        Raises:
            PdfTextExtractionError: the PDF would not open (a wrong password is the
                usual cause) or yielded nothing.
            UnicodeDecodeError: not a PDF and not decodable text either.
        """
        if content[:5] != b"%PDF-":
            return content.decode("utf-8")
        return extract_text(content, self.password)

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        try:
            text = self._text(content)
        except PdfTextExtractionError as exc:
            return ParseResult(rejections=[Rejection(0, str(exc))])
        except UnicodeDecodeError:
            return ParseResult(rejections=[Rejection(0, "not a readable contract note")])

        return self.parse_text(text, account_id, filename)

    def identity(self, content: bytes) -> DocumentIdentity:
        """The client this note names -- see ``app.ingest.base.IdentifyingAdapter``.

        Returns an empty identity when the document cannot be read at all. That is not
        this method's error to report: ``parse`` is called on the same bytes moments
        later and rejects them with the real reason. Raising here instead would replace
        "could not read PDF" with an identity complaint about a file that was never
        readable in the first place.
        """
        try:
            return parse_identity(self._text(content))
        except (PdfTextExtractionError, UnicodeDecodeError):
            return DocumentIdentity()

    def parse_text(self, text: str, account_id: str, filename: str = "") -> ParseResult:
        if CONTRACT_NOTE_MARKER not in text.upper():
            return ParseResult(
                rejections=[Rejection(0, "not a contract note: header marker absent")]
            )

        date_match = TRADE_DATE.search(text)
        if not date_match:
            return ParseResult(rejections=[Rejection(0, "no trade date on the note")])
        try:
            txn_date = datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
        except ValueError:
            return ParseResult(
                rejections=[Rejection(0, f"unparseable trade date {date_match.group(1)!r}")]
            )

        contract_number = (
            CONTRACT_NUMBER.search(text).group(1) if CONTRACT_NUMBER.search(text) else None
        )
        charges = parse_charges(text)
        total_charges = sum(charges.values(), Decimal("0"))
        # STT is a SUBSET of total_charges, not additive -- apportioned separately below
        # (same pro-rata-by-turnover treatment) only so Transaction.stt can carry it
        # through for app.engine.realised.tax_cost/tax_proceeds. See docs/TAX_ENGINE.md's
        # Session 10 status note.
        total_stt = charges.get("stt", Decimal("0"))

        candidates, fno_candidates, rejections = self._extract_rows(text, txn_date)

        problem = note_totals_problem(text)
        if (candidates or fno_candidates) and problem:
            # The note's own totals do not close over what was read: refuse the note whole
            # rather than stage figures that may be wrong or incomplete.
            rejections.append(
                Rejection(
                    0,
                    f"contract note {contract_number or '(number not found)'}: "
                    f"{len(candidates) + len(fno_candidates)} trade(s) not imported -- "
                    f"{problem}",
                )
            )
            return ParseResult(rows=[], rejections=rejections)

        fno: list[FnoParsedRow] = []
        if fno_candidates and candidates:
            # Rule 21(f): F&O charges stay with F&O trades. On a PDF carrying both
            # segments the charge columns cannot be told apart in the extracted text
            # (see note_charges_net_total), so these trades are refused, not guessed.
            rejections.extend(
                Rejection(
                    c["row_number"],
                    f"{c['symbol']}: this PDF note carries equity and F&O trades, and its "
                    "F&O charges cannot be separated from the equity ones in the PDF text "
                    "-- import the XLSX export for this day instead",
                    {"line": c["raw"]["line"]},
                )
                for c in fno_candidates
            )
        elif fno_candidates:
            fno_total, fno_stt = note_charges_net_total(text)
            fno = fno_rows(fno_candidates, fno_total, fno_stt, contract_number)

        notes = [
            f"contract note {contract_number or '(number not found)'} dated {txn_date}",
            (
                f"charges {total_charges} apportioned pro rata by turnover across "
                f"{len(candidates)} trade(s): {', '.join(sorted(charges)) or 'none found'}"
            ),
        ]
        if not charges:
            notes.append(
                "NO CHARGE LINES FOUND. Rows carry charges of 0, which is absence of data "
                "rather than evidence the trades were free."
            )

        if fno:
            notes.append(
                f"{len(fno)} F&O trade(s): F&O charges "
                f"{sum((r.charges for r in fno), Decimal('0'))} apportioned by turnover "
                "across them, kept out of equity cost"
            )

        rows = apportion_charges(candidates, total_charges, total_stt, account_id, contract_number)
        return ParseResult(rows=rows, rejections=rejections, notes=notes, fno_rows=fno)

    def _extract_rows(
        self, text: str, txn_date: date
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Rejection]]:
        candidates: list[dict[str, Any]] = []
        fno_candidates: list[dict[str, Any]] = []
        rejections: list[Rejection] = []

        for line_number, line in enumerate(text.splitlines(), start=1):
            match = TRADE_ROW.match(line.strip())
            if not match:
                if ANY_TRADE_LINE.match(line.strip()):
                    fno = _fno_candidate(line.strip(), line_number, txn_date)
                    if isinstance(fno, Rejection):
                        rejections.append(fno)
                    else:
                        fno_candidates.append(fno)
                continue

            isin = match.group("isin").upper()
            if not ISIN_PATTERN.match(isin):
                # INZ000031633 is the broker's SEBI registration, printed on every note.
                # It matches a looser ISIN shape and would become a phantom security.
                rejections.append(
                    Rejection(line_number, f"not a security ISIN: {isin!r}", {"line": line[:120]})
                )
                continue

            try:
                quantity = _decimal(match.group("qty"))
                price = _decimal(match.group("rate"))
            except InvalidOperation:
                rejections.append(
                    Rejection(line_number, "unparseable quantity or rate", {"line": line[:120]})
                )
                continue

            if quantity <= 0 or price < 0:
                rejections.append(
                    Rejection(line_number, f"implausible quantity/price {quantity}/{price}")
                )
                continue

            printed = _last_amount(line.strip())
            expected = (quantity * price).quantize(PAISA)
            if printed is None or expected != abs(printed):
                # Identity 1: a misread quantity or rate shows up here, on its own line.
                rejections.append(
                    Rejection(
                        line_number,
                        f"{match.group('symbol')} does not reconcile -- {quantity} x {price} = "
                        f"{expected}, the note prints {printed}",
                        {"line": line[:120]},
                    )
                )
                continue

            candidates.append(
                {
                    "row_number": line_number,
                    "isin": isin,
                    "symbol": match.group("symbol"),
                    "txn_type": SIDES[match.group("side")],
                    "quantity": quantity,
                    "price": price,
                    "trade_id": match.group("trade"),
                    "order_id": match.group("order"),
                    "exchange": match.group("exchange"),
                    "txn_date": txn_date,
                }
            )
        return candidates, fno_candidates, rejections


def _fno_candidate(line: str, line_number: int, txn_date: date) -> dict[str, Any] | Rejection:
    """A trade line with no ISIN: an F&O trade, or a rejection naming the line. Identity 1
    (quantity x rate = the printed net total) is checked here as for equity."""
    match = FNO_TRADE_ROW.match(line)
    if not match:
        return Rejection(line_number, "unrecognised trade line", {"line": line[:120]})
    try:
        quantity = _decimal(match.group("qty"))
        price = _decimal(match.group("rate"))
    except InvalidOperation:
        return Rejection(line_number, "unparseable quantity or rate", {"line": line[:120]})
    printed = _last_amount(line)
    expected = (quantity * price).quantize(PAISA)
    if quantity <= 0 or printed is None or expected != abs(printed):
        return Rejection(
            line_number,
            f"{match.group('symbol')} does not reconcile -- {quantity} x {price} = "
            f"{expected}, the note prints {printed}",
            {"line": line[:120]},
        )
    return {
        "row_number": line_number,
        "symbol": match.group("symbol"),
        "side": match.group("side"),
        "quantity": quantity,
        "price": price,
        "trade_id": match.group("trade"),
        "order_id": match.group("order"),
        "exchange": match.group("exchange"),
        "txn_date": txn_date,
        "raw": {"line": line[:120], "trade_time": match.group("trade_time")},
    }


#: Module-level, not a method: app.ingest.zerodha_contract_note_xlsx parses the same
#: document (a contract note), just from XLSX cells instead of PDF text, and needs the
#: identical turnover apportionment -- one implementation, not two that could drift.
#: `candidates` is a plain list of dicts (row_number/isin/symbol/txn_type/quantity/
#: price/trade_id/order_id/exchange/txn_date), the same shape both parsers build.
def apportion_charges(
    candidates: list[dict[str, Any]],
    total_charges: Decimal,
    total_stt: Decimal,
    account_id: str,
    contract_number: str | None,
    confidence: Decimal = CONFIDENCE_DERIVED,
) -> list[StagedRow]:
    """Spread note-level charges across a note's trades, pro rata by turnover.

    ``confidence`` defaults to CONFIDENCE_DERIVED, which is what both deterministic
    callers want and what every row this function has ever produced has scored. It is a
    parameter so the LLM path (app/ingest/llm_verify.py) can reuse this apportionment at
    CONFIDENCE_UNCERTAIN instead of growing a second copy that could drift from it. The
    arithmetic is identical either way; only the claim being made about it differs.

    A candidate may carry an ``evidence`` key. When present it is merged into the row's
    ``raw`` payload, which is where a staged row records where each of its numbers came
    from and what proved it -- see app/ingest/llm_verify.py.
    """
    rows: list[StagedRow] = []
    shares = charge_shares(candidates, total_charges, total_stt)
    for candidate, (charges_share, stt_share) in zip(candidates, shares, strict=True):
        rows.append(
            StagedRow(
                row_number=candidate["row_number"],
                transaction=Transaction(
                    isin=candidate["isin"],
                    txn_date=candidate["txn_date"],
                    txn_type=candidate["txn_type"],
                    quantity=candidate["quantity"],
                    price=candidate["price"],
                    charges=charges_share,
                    stt=stt_share,
                    account_id=account_id,
                    source=Source.CONTRACT_NOTE,
                    # The TRADE number, matching the tradebook's trade_id, so the same
                    # trade from either source has the same identity.
                    source_ref=candidate["trade_id"],
                ),
                # Never CERTAIN: the charge figure is apportioned, not read.
                confidence=confidence,
                raw={
                    "symbol": candidate["symbol"],
                    "exchange": candidate["exchange"],
                    "order_id": candidate["order_id"],
                    "contract_number": contract_number,
                    **(candidate.get("evidence") or {}),
                },
            )
        )
    return rows


def charge_shares(
    candidates: list[dict[str, Any]], total_charges: Decimal, total_stt: Decimal
) -> list[tuple[Decimal, Decimal]]:
    """Each candidate's (charges, stt) share of a note's totals, pro rata by turnover
    (quantity x price). The one apportionment both the equity rows (``apportion_charges``)
    and the F&O rows (``fno_rows``) use."""
    turnovers = [c["quantity"] * c["price"] for c in candidates]
    total_turnover = sum(turnovers, Decimal("0"))
    shares: list[tuple[Decimal, Decimal]] = []
    allocated_charges = Decimal("0")
    allocated_stt = Decimal("0")
    for index in range(len(candidates)):
        is_last = index == len(candidates) - 1
        charges_share, allocated_charges = pro_rata_share(
            total_charges, turnovers[index], total_turnover, allocated_charges, is_last
        )
        # stt is a SUBSET of charges, so it gets the identical treatment -- pro rata
        # by turnover, last row absorbs its own rounding remainder -- but ties to its
        # own note total independently of charges' allocation.
        stt_share, allocated_stt = pro_rata_share(
            total_stt, turnovers[index], total_turnover, allocated_stt, is_last
        )
        shares.append((charges_share, stt_share))
    return shares


#: The order number Zerodha prints on an exchange-generated expiry line (real note 34,
#: 14-08-2024: "111111 00:00:00 111111 00:00:00 NIFTY2481424200PE S NSE 275 56.25").
EXCHANGE_SETTLEMENT_ORDER_NO = "111111"

FNO_SIDES = {"B": FnoSide.BUY, "S": FnoSide.SELL}


def fno_rows(
    candidates: list[dict[str, Any]],
    total_charges: Decimal,
    total_stt: Decimal,
    contract_number: str | None,
) -> list[FnoParsedRow]:
    """One note's F&O trades, carrying that note's F&O charges apportioned by turnover
    across them (docs/FNO_PLAN.md OD-F6). ``candidates`` are dicts with row_number,
    symbol, side (B/S), quantity, price, trade_id, order_id, exchange, txn_date and an
    optional ``raw``. Unresolved: the symbol is resolved at staging, not here."""
    shares = charge_shares(candidates, total_charges, total_stt)
    return [
        FnoParsedRow(
            row_number=c["row_number"],
            symbol=c["symbol"],
            exchange=c["exchange"],
            side=FNO_SIDES[c["side"]],
            quantity=c["quantity"],
            price=c["price"],
            trade_date=c["txn_date"],
            trade_no=c["trade_id"],
            order_no=c["order_id"],
            contract_number=contract_number,
            charges=charges,
            # STT is a subset of charges (fno_trade CHECK stt <= charges); the two shares
            # are rounded independently, so cap it rather than fail the whole promote.
            stt=min(stt, charges),
            kind=(
                FnoTradeKind.EXCHANGE_SETTLEMENT
                if c["order_id"] == EXCHANGE_SETTLEMENT_ORDER_NO
                else FnoTradeKind.TRADE
            ),
            raw={
                "symbol": c["symbol"],
                "exchange": c["exchange"],
                "order_id": c["order_id"],
                "contract_number": contract_number,
                **(c.get("raw") or {}),
            },
        )
        for c, (charges, stt) in zip(candidates, shares, strict=True)
    ]


def pro_rata_share(
    total: Decimal,
    turnover: Decimal,
    total_turnover: Decimal,
    allocated_so_far: Decimal,
    is_last: bool,
) -> tuple[Decimal, Decimal]:
    """One row's share of ``total``, apportioned by turnover -- the same rule for
    ``charges`` and ``stt`` alike, since ``stt`` is just a labelled subset of
    ``charges`` and gets an identical, independent apportionment.

    The last row absorbs the rounding remainder so the apportioned shares sum to
    exactly what the note states, rather than being out by a few paise. Returns
    ``(this row's share, the updated running allocation)``.
    """
    if total_turnover <= 0 or total <= 0:
        return Decimal("0"), allocated_so_far
    if is_last:
        return total - allocated_so_far, total
    share = (total * turnover / total_turnover).quantize(Decimal("0.01"))
    return share, allocated_so_far + share
