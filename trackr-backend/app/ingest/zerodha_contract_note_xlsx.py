"""Zerodha contract-note XLSX adapter -- the same document as
app.ingest.zerodha_contract_note (trades WITH charges), from a spreadsheet export
instead of a PDF.

WHY THIS EXISTS. Historical XIRR needs price AND charges going back to account
inception. Tradebook CSV upload already covers "one file per FY, merge and dedupe"
(app.ingest.zerodha_tradebook); contract notes -- the only source of charges -- had
no upload path at all, only email forwarding. Console's bulk contract-note export for
a chosen date range closes that gap: this is what "custom date range" means here --
the range is picked when the file is downloaded from Console, not inside this app.

TWO REAL SHAPES, BOTH VERIFIED AGAINST ACTUAL EXPORTS, NOT INVENTED:

**Bulk** (one workbook, one sheet per trading day, sheet name "DD-MM-YYYY"): a
cell-for-cell mirror of the same PDF layout app.ingest.zerodha_contract_note.TRADE_ROW
already parses -- same header labels, same "SYMBOL - SERIES / ISIN" security
description, same charge-line wording family. Verified against 67 real trading-day
sheets across five real per-FY exports (2022-23 through 2026-27, one investor).

**Clean** (a workbook with exactly the sheets "Trades" / "Charges & Net" /
"Note Details"): one note, three purpose-built sheets -- trades in Trades (ISIN and
Symbol already split into their own columns, unlike the bulk shape's combined
description cell), a labelled charge breakdown in "Charges & Net", note metadata in
"Note Details". Verified against one real single-day export.

**Neither shape's charge labels match app.ingest.zerodha_contract_note.CHARGE_PATTERNS.**
That module's regexes require PDF-exact phrasing ("Taxable value of Supply
(Brokerage)", bare "IGST"/"CGST"/"SGST") the bulk shape spells differently ("Brokerage",
"Integrated GST", "Central GST") and the clean shape differently again ("Brokerage
(taxable value)", "IGST @18%"). Reusing those regexes on reconstructed text would
silently under-count charges -- confirmed by checking each one against the real label
text before writing classify_charge_label, not assumed. A lenient, cell-native
keyword classifier is the correct fix here, not a third regex family.

Turnover apportionment itself IS reused, not reinvented:
app.ingest.zerodha_contract_note.apportion_charges/pro_rata_share -- refactored out of
that module into plain functions for exactly this reuse. One implementation of "spread
a note's charges pro rata by turnover, last row absorbs the rounding remainder" for
every contract-note shape this codebase reads, not one per format.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.ingest import fno_symbol
from app.ingest.base import DocumentIdentity, FnoParsedRow, ParseResult, Rejection
from app.ingest.zerodha_contract_note import (
    ISIN_PATTERN,
    SIDES,
    _decimal,
    apportion_charges,
    fno_rows,
    parse_identity,
)

#: "SYMBOL - SERIES / ISIN", the bulk shape's combined description cell -- same split
#: app.ingest.zerodha_contract_note.TRADE_ROW performs on PDF text, applied here to an
#: already-isolated cell instead of a whole line.
SYMBOL_ISIN = re.compile(r"^(?P<symbol>[A-Z0-9&.\-]+?)\s*-\s*\w+\s*/\s*(?P<isin>IN[A-Z0-9]{10})$")

#: How far into a sheet the client identity block sits. Read from a real per-FY export,
#: where "PAN of Client:" and "UCC of Client:" are inside the first dozen rows of every
#: sheet -- generous enough for layout drift, bounded so identity never scans a workbook
#: of trades.
IDENTITY_MAX_ROW = 25

#: The clean single-note shape has exactly these three sheets, and only these -- an
#: extra or missing sheet means it is not this shape, not a partial match to guess at.
CLEAN_SHEET_NAMES = frozenset({"Trades", "Charges & Net", "Note Details"})

#: An exchange-traded option or future as Zerodha prints it: underlying, expiry, then
#: strike + CE/PE or FUT -- "NIFTY2690823850PE", "ADANIENT24AUG3050PE". Fallback for a
#: sheet whose "Derivatives" section label is missing; the label is the primary signal.
DERIVATIVE_CONTRACT = re.compile(r"^[A-Z0-9&\-]+\d{2}[A-Z0-9]*(CE|PE|FUT)$")

DATE_IN_TEXT = re.compile(r"\d{2}[/-]\d{2}[/-]\d{4}")

#: Contract notes print rupees to the paisa; Excel stores some as binary floats
#: (-15791.599999999999), so every printed amount is rounded to this before comparing.
PAISA = Decimal("0.01")


def _paise(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


#: (bucket, keywords) in priority order -- checked top to bottom, first match wins.
#: Order matters: a real label like "Integrated GST (@18% of Brokerage, SEBI and
#: Transaction Charges)" contains the substrings "brokerage" and "sebi" purely as rate-
#: basis explanation, not because it is a second charge -- checking the GST/STT/
#: exchange/SEBI/stamp keywords before the bare "brokerage" keyword is what keeps that
#: row classified as igst, not brokerage. Verified against every real label text in
#: both shapes before this list was written, not assumed to be exhaustive.
_CHARGE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("igst", ("integrated gst", "igst")),
    ("cgst", ("central gst", "cgst")),
    ("sgst", ("state gst", "sgst")),
    ("stt", ("securities transaction",)),
    ("exchange", ("exchange transaction",)),
    ("sebi", ("sebi turnover",)),
    ("stamp", ("stamp duty",)),
    ("brokerage", ("brokerage",)),
]


def classify_charge_label(label: Any) -> str | None:
    """Which charge bucket a label cell names, or None if it names something this
    adapter does not recognise -- e.g. "Clearing Charges" (always 0 for equity in
    every real sample) or a totals/boilerplate row ("PAY IN / PAY OUT OBLIGATION",
    "Net amount Receivable..."). An unrecognised label contributes nothing, the same
    behaviour as the PDF adapter's CHARGE_PATTERNS simply not matching a line."""
    if not isinstance(label, str):
        return None
    lowered = label.strip().lower()
    for bucket, keywords in _CHARGE_RULES:
        if any(k in lowered for k in keywords):
            return bucket
    return None


def _cell_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_date(raw: str) -> date | None:
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


class ZerodhaContractNoteXlsxAdapter:
    """Zerodha contract notes exported as XLSX -- bulk (one sheet per trading day, a
    chosen date range) or clean (one note, three labelled sheets). See module
    docstring for how each shape was verified and why charge labels need their own
    classifier rather than reusing the PDF adapter's regexes."""

    name = "zerodha_contract_note_xlsx"

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult:
        import io

        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - dependency guard
            return ParseResult(rejections=[Rejection(0, f"openpyxl is not installed: {exc}")])

        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        except Exception as exc:  # noqa: BLE001 - openpyxl raises assorted types
            return ParseResult(rejections=[Rejection(0, f"could not read XLSX: {exc}")])

        if frozenset(workbook.sheetnames) == CLEAN_SHEET_NAMES:
            return self._parse_clean(workbook, account_id)
        return self._parse_bulk(workbook, account_id)

    def identity(self, content: bytes) -> DocumentIdentity:
        """The client this workbook names -- see ``app.ingest.base.IdentifyingAdapter``.

        The XLSX export states the same two identifiers as the PDF, worded "PAN of
        Client:" / "UCC of Client:" and split across cells rather than printed on a line.
        Flattening a sheet's cells back into text lets ``parse_identity`` -- already
        proven against 40 real notes -- do the reading, instead of a second pair of
        regexes that could drift away from it.

        Only as many sheets as it takes: a bulk workbook is one client's trading days, so
        the first sheet stating an identity settles the whole file. Reading all fifteen to
        learn the same answer fifteen times is work for nothing.

        Returns an empty identity when the workbook will not open. That is ``parse``'s
        error to report, on the same bytes, moments later.
        """
        import io

        try:
            import openpyxl

            # NOT read_only. In read-only mode openpyxl trusts the sheet dimension the
            # workbook declares, and one of the five real per-FY exports declares a single
            # row -- iter_rows then yields one row and the identity block is invisible.
            # ``parse`` above loads the same way for the same reason; a check that reads
            # the file differently from the parser is a check that can silently disagree
            # with it.
            workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        except Exception:  # noqa: BLE001 - openpyxl raises assorted types; parse reports it
            return DocumentIdentity()

        for sheet_name in workbook.sheetnames:
            # The identity block sits in the header of each sheet; the rest is trades.
            rows = workbook[sheet_name].iter_rows(values_only=True, max_row=IDENTITY_MAX_ROW)
            text = "\n".join(
                " ".join(_cell_str(cell) for cell in row if cell is not None) for row in rows
            )
            identity = parse_identity(text)
            if identity:
                return identity
        return DocumentIdentity()

    # -- bulk: one sheet per trading day ------------------------------------------

    def _parse_bulk(self, workbook: Any, account_id: str) -> ParseResult:
        all_rows = []
        all_rejections = []
        all_fno: list[FnoParsedRow] = []
        fno_days: list[date] = []
        parsed_dates: list[date] = []
        row_counter = 0
        recognised_days = 0

        for sheet_name in workbook.sheetnames:
            rows_2d = list(workbook[sheet_name].iter_rows(values_only=True))
            header = self._find_bulk_header(rows_2d)
            if header is None:
                row_counter += 1
                all_rejections.append(
                    Rejection(
                        row_counter,
                        f"sheet {sheet_name!r}: does not look like a contract-note "
                        "trading day (no recognisable header row)",
                    )
                )
                continue

            header_idx, col = header
            txn_date = self._bulk_trade_date(rows_2d, header_idx, sheet_name)
            if txn_date is None:
                row_counter += 1
                all_rejections.append(
                    Rejection(row_counter, f"sheet {sheet_name!r}: no readable trade date")
                )
                continue

            contract_number = self._bulk_contract_number(rows_2d, header_idx)
            day = self._bulk_rows(rows_2d, header_idx, col, txn_date, row_counter, sheet_name)
            candidates, charges_by_bucket, rejections, totals_problem = day[:4]
            fno_candidates, fno_charges = day[4], day[5]
            row_counter += len(rows_2d) - header_idx
            all_rejections.extend(rejections)

            if (candidates or fno_candidates) and totals_problem:
                # The note's own arithmetic does not close over what was read, so what was
                # read is not proven -- a misread or missed row is possible. Refuse the
                # whole day rather than stage figures that may be wrong (a failed
                # verification is an exception, never a guess).
                all_rejections.append(
                    Rejection(
                        row_counter,
                        f"sheet {sheet_name!r}: "
                        f"{len(candidates) + len(fno_candidates)} trade(s) not imported -- "
                        f"{totals_problem}",
                    )
                )
                continue

            if fno_candidates:
                fno_days.append(txn_date)
                if fno_charges is None:
                    # Rule 21(f): no F&O charge column and equity trades beside the F&O
                    # ones -- the note's charges cannot be split by segment. Refused, not
                    # guessed.
                    all_rejections.extend(
                        Rejection(
                            c["row_number"],
                            f"sheet {sheet_name!r}: {c['symbol']} -- this note has no F&O "
                            "charge column, so its F&O charges cannot be separated from "
                            "the equity ones",
                        )
                        for c in fno_candidates
                    )
                else:
                    all_fno.extend(
                        fno_rows(
                            fno_candidates,
                            sum(fno_charges.values(), Decimal("0")),
                            fno_charges.get("stt", Decimal("0")),
                            contract_number,
                        )
                    )

            if not candidates:
                continue

            recognised_days += 1
            parsed_dates.append(txn_date)
            # stt is a SUBSET of total_charges, not additive -- same convention as
            # zerodha_contract_note.py, see that module's Session 10 note.
            total_charges = sum(charges_by_bucket.values(), Decimal("0"))
            total_stt = charges_by_bucket.get("stt", Decimal("0"))
            all_rows.extend(
                apportion_charges(candidates, total_charges, total_stt, account_id, contract_number)
            )

        notes = [self._range_note(recognised_days, parsed_dates)]
        if fno_days:
            notes.append(
                f"{len(all_fno)} F&O trade(s) on {len(set(fno_days))} day(s), "
                f"{min(fno_days)} to {max(fno_days)} -- F&O charges kept with them, "
                "out of equity cost"
            )
        return ParseResult(
            rows=all_rows, rejections=all_rejections, notes=notes, fno_rows=all_fno
        )

    @staticmethod
    def _find_bulk_header(rows_2d: list[tuple]) -> tuple[int, dict[str, int]] | None:
        for idx, row in enumerate(rows_2d[:40]):
            cells = [_cell_str(c).lower() for c in row]
            if not any("order no" in c for c in cells):
                continue
            if not any("trade no" in c for c in cells):
                continue
            col: dict[str, int] = {}
            for i, c in enumerate(cells):
                if "order no" in c:
                    col["order"] = i
                elif "trade no" in c:
                    col["trade"] = i
                elif "trade time" in c:
                    col["trade_time"] = i
                elif "security" in c and "description" in c:
                    col["description"] = i
                elif "buy" in c and "sell" in c:
                    col["side"] = i
                elif c == "quantity":
                    col["quantity"] = i
                elif c == "exchange":
                    col["exchange"] = i
                elif "gross rate" in c or "trade price per unit" in c:
                    col["price"] = i
                elif "net total" in c and "levies" in c:
                    col["net_total"] = i
                elif "brokerage per unit" in c:
                    col["brokerage_unit"] = i
            required = {
                "order",
                "trade",
                "description",
                "side",
                "quantity",
                "exchange",
                "price",
                "net_total",
            }
            if required <= set(col):
                return idx, col
        return None

    @staticmethod
    def _bulk_trade_date(rows_2d: list[tuple], header_idx: int, sheet_name: str) -> date | None:
        for row in rows_2d[: header_idx + 1]:
            for i, cell in enumerate(row):
                if "trade date" not in _cell_str(cell).lower():
                    continue
                for later in row[i:]:
                    text = _cell_str(later)
                    match = DATE_IN_TEXT.search(text)
                    if match:
                        found = _parse_date(match.group(0))
                        if found:
                            return found
        # Fall back to the sheet name itself -- confirmed identical to the in-sheet
        # "Trade Date:" value on every real sample, and the only signal left when a
        # label cell wraps unusually.
        return _parse_date(sheet_name.strip())

    @staticmethod
    def _bulk_contract_number(rows_2d: list[tuple], header_idx: int) -> str | None:
        for row in rows_2d[: header_idx + 1]:
            for i, cell in enumerate(row):
                if "contract note no" not in _cell_str(cell).lower():
                    continue
                for later in row[i:]:
                    text = _cell_str(later)
                    if text and "contract note no" not in text.lower():
                        return text
        return None

    def _bulk_rows(
        self,
        rows_2d: list[tuple],
        header_idx: int,
        col: dict[str, int],
        txn_date: date,
        row_offset: int,
        sheet_name: str,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Decimal],
        list[Rejection],
        str | None,
        list[dict[str, Any]],
        dict[str, Decimal] | None,
    ]:
        """Trades, equity charges, rejections, why the note's own totals do NOT close over
        what was read (None when they do) -- then the F&O trades and the F&O charges
        (None when the note cannot separate them from equity charges).

        THE NOTE CHECKS ITSELF. Every Zerodha note prints, per trade, a "Net Total (Before
        Levies)"; per note, a pay-in/pay-out obligation and a net amount payable. Three
        identities must hold, to the paisa, before a day's trades are trusted:

          1. per trade: quantity x (gross rate +/- brokerage per unit) = its net total
             -- a misread quantity or price fails here (that row is rejected);
          2. per note: the trades' net totals sum to the obligation (NET TOTAL column,
             every segment) -- a trade the loop never recognised fails here;
          3. per note: obligation + every charge line = net amount -- a charge line in
             the wrong column, or one not read, fails here.

        Verified against all 75 real trading-day sheets of the five per-FY exports
        (2026-10-08): every one closes on all three. Pure arithmetic, no model.
        """
        candidates: list[dict[str, Any]] = []
        charges: dict[str, Decimal] = {}
        rejections: list[Rejection] = []
        fno_candidates: list[dict[str, Any]] = []
        fno_charges: dict[str, Decimal] = {}
        net_charges: dict[str, Decimal] = {}
        # The charges sub-table below the trades has its OWN "NET TOTAL" column,
        # confirmed at a DIFFERENT index than the trades table's "Net Total (Before
        # Levies)" column on the real sample (11 vs 12) -- the two blocks are not
        # column-aligned. Found by locating the literal "NET TOTAL" sub-header cell
        # rather than assumed to match col["net_total"].
        charge_net_total_idx: int | None = None
        # NET TOTAL is every segment on the note, equity AND derivatives. On a day with an
        # option trade beside an equity one (real 02-09-2026 sheet: "NCL-Cash" / "NCL-F&O"
        # / "NET TOTAL"; older notes say "Futures & Options") the F&O column's charges must
        # come back out, or an option's brokerage lands in an equity cost basis. Subtracting
        # rather than reading the equity column directly is deliberate: one real layout
        # (07-12-2022) fills only NET TOTAL and leaves the per-segment cells blank.
        derivative_idxs: list[int] = []
        # The trades table is sectioned by a lone label row ("Equity" / "Derivatives").
        in_derivatives = False
        # Totals the note prints about itself -- see the docstring.
        printed_trades = Decimal("0")
        unreadable_trade_total = False
        obligation: Decimal | None = None
        charge_lines = Decimal("0")
        net_amount: Decimal | None = None

        def get(row: tuple, key: str) -> Any:
            idx = col[key]
            return row[idx] if idx < len(row) else None

        for offset, row in enumerate(rows_2d[header_idx + 1 :], start=1):
            row_number = row_offset + offset
            side_raw = _cell_str(get(row, "side")).upper()
            description = _cell_str(get(row, "description"))
            section = _cell_str(row[0]).lower() if row else ""
            if section in ("equity", "derivatives") and not any(_cell_str(c) for c in row[1:]):
                in_derivatives = section == "derivatives"
                continue

            if side_raw in SIDES and description:
                try:
                    printed = _paise(_decimal(str(get(row, "net_total"))))
                    printed_trades += printed
                except InvalidOperation:
                    printed = None
                    unreadable_trade_total = True
                match = SYMBOL_ISIN.match(description)
                if not match and (in_derivatives or DERIVATIVE_CONTRACT.match(description)):
                    # An option or future is not an equity holding: it becomes an
                    # unresolved F&O row (rule 21), never a phantom security.
                    fno = self._fno_candidate(
                        row, get, col, description, side_raw, printed, row_number, txn_date
                    )
                    if isinstance(fno, str):
                        rejections.append(Rejection(row_number, f"sheet {sheet_name!r}: {fno}"))
                    else:
                        fno_candidates.append(fno)
                    continue
                if not match:
                    rejections.append(
                        Rejection(
                            row_number,
                            f"sheet {sheet_name!r}: unrecognised security description "
                            f"{description!r}",
                        )
                    )
                    continue
                isin = match.group("isin").upper()
                if not ISIN_PATTERN.match(isin):
                    rejections.append(
                        Rejection(
                            row_number, f"sheet {sheet_name!r}: not a security ISIN: {isin!r}"
                        )
                    )
                    continue
                try:
                    quantity = _decimal(str(get(row, "quantity")))
                    price = _decimal(str(get(row, "price")))
                except InvalidOperation:
                    rejections.append(
                        Rejection(
                            row_number, f"sheet {sheet_name!r}: unparseable quantity or price"
                        )
                    )
                    continue
                if quantity <= 0 or price < 0:
                    rejections.append(
                        Rejection(
                            row_number,
                            f"sheet {sheet_name!r}: implausible quantity/price {quantity}/{price}",
                        )
                    )
                    continue
                per_unit = Decimal("0")
                if "brokerage_unit" in col and _cell_str(get(row, "brokerage_unit")):
                    try:
                        per_unit = _decimal(str(get(row, "brokerage_unit")))
                    except InvalidOperation:
                        per_unit = Decimal("0")
                rate = price + per_unit if side_raw == "B" else price - per_unit
                expected = _paise(quantity * rate)
                if printed is None or expected != abs(printed):
                    rejections.append(
                        Rejection(
                            row_number,
                            f"sheet {sheet_name!r}: {description} does not reconcile -- "
                            f"{quantity} x {rate} = {expected}, the note prints {printed}",
                        )
                    )
                    continue
                trade_no = _cell_str(get(row, "trade"))
                if not trade_no:
                    rejections.append(
                        Rejection(row_number, f"sheet {sheet_name!r}: trade row has no Trade No.")
                    )
                    continue
                candidates.append(
                    {
                        "row_number": row_number,
                        "isin": isin,
                        "symbol": match.group("symbol"),
                        "txn_type": SIDES[side_raw],
                        "quantity": quantity,
                        "price": price,
                        "trade_id": trade_no,
                        "order_id": _cell_str(get(row, "order")),
                        "exchange": _cell_str(get(row, "exchange")),
                        "txn_date": txn_date,
                    }
                )
                continue

            if charge_net_total_idx is None:
                for i, cell in enumerate(row):
                    if _cell_str(cell).strip().upper() == "NET TOTAL":
                        charge_net_total_idx = i
                        break
                if charge_net_total_idx is not None:
                    derivative_idxs = [
                        i
                        for i, cell in enumerate(row)
                        if "F&O" in _cell_str(cell).upper() or "FUTURES" in _cell_str(cell).upper()
                    ]
                    continue  # this row is the sub-header itself, not a charge line

            label = row[0] if row else None
            if (
                charge_net_total_idx is not None
                and net_amount is None
                and isinstance(label, str)
                and charge_net_total_idx < len(row)
                and _cell_str(row[charge_net_total_idx])
            ):
                lowered = label.lower()
                try:
                    value = _paise(_decimal(str(row[charge_net_total_idx])))
                except InvalidOperation:
                    value = None
                if value is not None:
                    if "obligation" in lowered:
                        obligation = value
                        continue
                    if "net amount" in lowered:
                        net_amount = value
                        continue
                    if obligation is not None:
                        charge_lines += value
            bucket = classify_charge_label(label)
            if bucket is None or charge_net_total_idx is None:
                continue
            if charge_net_total_idx >= len(row):
                continue
            try:
                amount = _decimal(str(row[charge_net_total_idx]))
                net_charges[bucket] = net_charges.get(bucket, Decimal("0")) + abs(amount)
                fno_amount = Decimal("0")
                for i in derivative_idxs:
                    if i < len(row) and _cell_str(row[i]):
                        amount -= _decimal(str(row[i]))
                        fno_amount += _decimal(str(row[i]))
                amount = abs(amount)
            except InvalidOperation:
                continue
            charges[bucket] = charges.get(bucket, Decimal("0")) + amount
            fno_charges[bucket] = fno_charges.get(bucket, Decimal("0")) + abs(fno_amount)

        # The F&O charges (rule 21(f)): the F&O column when the note has one; the whole
        # NET TOTAL when it has none and carries no equity trade (every charge is then an
        # F&O charge); otherwise None -- not separable, the caller refuses the F&O rows.
        fno_out: dict[str, Decimal] | None = (
            fno_charges if derivative_idxs else (None if candidates else net_charges)
        )

        problem: str | None = None
        if unreadable_trade_total:
            problem = "a trade's printed net total could not be read"
        elif obligation is None or net_amount is None:
            problem = "the note's pay-in/pay-out obligation or net amount was not found"
        elif printed_trades != obligation:
            problem = (
                f"trades total {printed_trades} but the note's obligation is {obligation} "
                "(a trade may have been missed)"
            )
        elif obligation + charge_lines != net_amount:
            problem = f"obligation {obligation} + charges {charge_lines} != net amount {net_amount}"
        return candidates, charges, rejections, problem, fno_candidates, fno_out

    @staticmethod
    def _fno_candidate(
        row: tuple,
        get: Any,
        col: dict[str, int],
        description: str,
        side_raw: str,
        printed: Decimal | None,
        row_number: int,
        txn_date: date,
    ) -> dict[str, Any] | str:
        """An F&O trade row as a candidate dict, or the reason it is refused. Identity 1
        (quantity x (gross rate +/- brokerage per unit) = printed net total) and the Trade
        No. are required exactly as for an equity row."""
        try:
            quantity = _decimal(str(get(row, "quantity")))
            price = _decimal(str(get(row, "price")))
            per_unit = Decimal("0")
            if "brokerage_unit" in col and _cell_str(get(row, "brokerage_unit")):
                per_unit = _decimal(str(get(row, "brokerage_unit")))
        except InvalidOperation:
            return f"{description}: unparseable quantity or price"
        if quantity <= 0 or price < 0:
            return f"{description}: implausible quantity/price {quantity}/{price}"
        rate = price + per_unit if side_raw == "B" else price - per_unit
        expected = _paise(quantity * rate)
        if printed is None or expected != abs(printed):
            return (
                f"{description} does not reconcile -- {quantity} x {rate} = {expected}, "
                f"the note prints {printed}"
            )
        trade_no = _cell_str(get(row, "trade"))
        if not trade_no:
            return f"{description}: trade row has no Trade No."
        return {
            "row_number": row_number,
            "symbol": description,
            "side": side_raw,
            "quantity": quantity,
            "price": price,
            "trade_id": trade_no,
            "order_id": _cell_str(get(row, "order")),
            "exchange": _cell_str(get(row, "exchange")),
            "txn_date": txn_date,
            "raw": {"trade_time": _cell_str(get(row, "trade_time"))} if "trade_time" in col else {},
        }

    @staticmethod
    def _range_note(days: int, dates: list[date]) -> str:
        if not dates:
            return "no recognisable trading days found in this workbook"
        return (
            f"{days} trading day(s) parsed, {min(dates)} to {max(dates)} -- "
            "confirm this matches the date range you downloaded from Console"
        )

    # -- clean: one note, three sheets ---------------------------------------------

    def _parse_clean(self, workbook: Any, account_id: str) -> ParseResult:
        trades_ws = workbook["Trades"]
        charges_ws = workbook["Charges & Net"]
        details_ws = workbook["Note Details"]

        details = {
            _cell_str(row[0]).lower(): _cell_str(row[1])
            for row in details_ws.iter_rows(values_only=True)
            if row and row[0] and row[1] is not None
        }
        contract_number = details.get("contract note no.")
        raw_date = details.get("trade date", "")
        match = DATE_IN_TEXT.search(raw_date)
        txn_date = _parse_date(match.group(0)) if match else None
        if txn_date is None:
            return ParseResult(
                rejections=[Rejection(0, "Note Details sheet: no readable trade date")]
            )

        header_idx = None
        col: dict[str, int] = {}
        trade_rows = list(trades_ws.iter_rows(values_only=True))
        for idx, row in enumerate(trade_rows):
            cells = [_cell_str(c).lower() for c in row]
            if "isin" in cells and "side" in cells and "quantity" in cells:
                header_idx = idx
                for i, c in enumerate(cells):
                    if c == "isin":
                        col["isin"] = i
                    elif c == "symbol":
                        col["symbol"] = i
                    elif c == "side":
                        col["side"] = i
                    elif c == "exchange":
                        col["exchange"] = i
                    elif c == "quantity":
                        col["quantity"] = i
                    elif "price" in c or "wap" in c:
                        col.setdefault("price", i)
                break
        if header_idx is None or "isin" not in col:
            return ParseResult(
                rejections=[Rejection(0, "Trades sheet: no recognisable header row")]
            )

        candidates: list[dict[str, Any]] = []
        rejections: list[Rejection] = []
        for offset, row in enumerate(trade_rows[header_idx + 1 :], start=1):
            row_number = header_idx + 1 + offset
            isin = _cell_str(row[col["isin"]] if col["isin"] < len(row) else None).upper()
            if not isin:
                # A derivative has no ISIN. Its row must not vanish with the blank and
                # total rows: this shape has no F&O charge column and no verified F&O
                # layout, so it is named and refused (rule 16), never silently dropped.
                contract = next(
                    (
                        text
                        for text in (_cell_str(c).upper() for c in row)
                        if text and fno_symbol.decode(text)
                    ),
                    None,
                )
                if contract:
                    rejections.append(
                        Rejection(
                            row_number,
                            f"{contract} is a derivatives contract with no ISIN -- F&O rows "
                            "are not read from this single-note export; import the bulk "
                            "Console contract-note export or the PDF instead",
                        )
                    )
                continue
            if not ISIN_PATTERN.match(isin):
                rejections.append(Rejection(row_number, f"not a security ISIN: {isin!r}"))
                continue
            # The clean shape spells the side out ("BUY"/"SELL"), unlike the bulk
            # shape's single letter ("B"/"S") -- confirmed against the real sample,
            # not assumed to match SIDES' PDF-derived keys.
            side_raw = _cell_str(row[col["side"]] if col["side"] < len(row) else None).upper()
            side_key = {"BUY": "B", "SELL": "S"}.get(side_raw, side_raw)
            if side_key not in SIDES:
                rejections.append(Rejection(row_number, f"unknown side {side_raw!r}"))
                continue
            try:
                quantity = _decimal(str(row[col["quantity"]]))
                price = _decimal(str(row[col["price"]]))
            except InvalidOperation:
                rejections.append(Rejection(row_number, "unparseable quantity or price"))
                continue
            if quantity <= 0 or price < 0:
                rejections.append(
                    Rejection(row_number, f"implausible quantity/price {quantity}/{price}")
                )
                continue
            candidates.append(
                {
                    "row_number": row_number,
                    "isin": isin,
                    "symbol": _cell_str(row[col["symbol"]]) if "symbol" in col else "",
                    "txn_type": SIDES[side_key],
                    "quantity": quantity,
                    "price": price,
                    # This sheet carries no per-trade trade number -- source_ref is
                    # left unset, same as any adapter with no stable per-trade id
                    # (app.db.precedence.trade_identity returns None without one,
                    # which is the honest outcome, not a guess).
                    "trade_id": None,
                    "order_id": None,
                    "exchange": _cell_str(row[col["exchange"]]) if "exchange" in col else "",
                    "txn_date": txn_date,
                }
            )

        charges: dict[str, Decimal] = {}
        for row in charges_ws.iter_rows(values_only=True):
            if not row or row[0] is None:
                continue
            bucket = classify_charge_label(row[0])
            if bucket is None:
                continue
            try:
                amount = abs(_decimal(str(row[1])))
            except (InvalidOperation, IndexError, TypeError):
                continue
            charges[bucket] = charges.get(bucket, Decimal("0")) + amount

        total_charges = sum(charges.values(), Decimal("0"))
        total_stt = charges.get("stt", Decimal("0"))
        rows = apportion_charges(candidates, total_charges, total_stt, account_id, contract_number)
        notes = [
            f"contract note {contract_number or '(number not found)'} dated {txn_date}",
            f"charges {total_charges} apportioned pro rata by turnover across "
            f"{len(candidates)} trade(s): {', '.join(sorted(charges)) or 'none found'}",
        ]
        return ParseResult(rows=rows, rejections=rejections, notes=notes)
