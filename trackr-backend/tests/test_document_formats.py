"""Format parity, trustworthy CAS snapshots, and the real broker workbook quirk."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pytest

from app.engine.dedupe import dedupe
from app.ingest.cas_pdf import CasStatementError
from app.ingest.cas_table import CasCsvAdapter, CasXlsxAdapter
from app.ingest.file_validation import classify
from app.ingest.zerodha_contract_note_csv import ZerodhaContractNoteCsvAdapter
from app.ingest.zerodha_contract_note_xlsx import ZerodhaContractNoteXlsxAdapter
from app.ingest.zerodha_tradebook import ZerodhaTradebookAdapter
from app.ingest.zerodha_tradebook_xlsx import ZerodhaTradebookXlsxAdapter
from app.routers.ingest import _DOCTYPES, _build_adapter

TRADE_ROWS = [
    ["Symbol", "ISIN", "Trade Date", "Trade Type", "Quantity", "Price", "Trade ID"],
    ["EXAMPLE", "INE153T01027", "2026-04-08", "buy", 10, "100.1250", "20001"],
    ["EXAMPLE", "INE153T01027", "2026-04-08", "buy", 10, "100.1250", "20002"],
]
NOTE_ROWS = [
    ["CONTRACT NOTE CUM TAX INVOICE"],
    ["PAN of Client:", "ABCDE1234F"], ["UCC of Client:", "TEST123"],
    ["CONTRACT NOTE NO:", "TEST-NOTE"], ["Trade Date:", "08-04-2026"],
    ["Order No.", "Trade No.", "Security/Contract Description", "Buy(B) / Sell(S)",
     "Quantity", "Exchange", "Gross Rate", "Net Total (Before Levies)"],
    ["10001", "20001", "EXAMPLE - EQ / INE153T01027", "B", 10, "NSE", 100, -1000],
    [None, None, None, None, None, None, None, "NET TOTAL"],
    ["Pay in/Pay out obligation", None, None, None, None, None, None, -1000],
    ["Brokerage", None, None, None, None, None, None, -2],
    ["Securities transaction tax", None, None, None, None, None, None, -1],
    ["Integrated GST", None, None, None, None, None, None, "-0.36"],
    ["Net amount payable", None, None, None, None, None, None, "-1003.36"],
]
CAS_ROWS = [
    ["CAS Holdings"], ["Statement Date", "2026-08-31"], ["Broker", "Zerodha"],
    ["Total Securities", 2], ["Portfolio Value", "1000.00"], ["PAN", "ABCDE1234F"],
    ["ISIN", "Quantity", "Market Price", "Market Value"],
    ["INE153T01027", 10, 100, 1000], ["INE866K01023", 0, 423, 0],
]


def as_csv(rows):
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode()


def as_xlsx(rows, *, name="Equity", second=None, bad_dimensions=False):
    workbook = openpyxl.Workbook()
    workbook.active.title = name
    for row in rows:
        workbook.active.append(row)
    if second is not None:
        other = workbook.create_sheet("Other")
        for row in second:
            other.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    if not bad_dimensions:
        return buffer.getvalue()
    fixed = io.BytesIO()
    with zipfile.ZipFile(buffer) as original, zipfile.ZipFile(fixed, "w") as changed:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                content = re.sub(br'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', content)
            changed.writestr(item, content)
    return fixed.getvalue()


@pytest.mark.parametrize("bad_dimensions", [False, True])
def test_tradebook_csv_xlsx_keep_exactly_the_same_partial_fills(bad_dimensions):
    csv_result = ZerodhaTradebookAdapter().parse(as_csv(TRADE_ROWS), "ACCOUNT")
    xlsx = as_xlsx(TRADE_ROWS, name="08-04-2026", bad_dimensions=bad_dimensions)
    verdict = classify(xlsx, "tradebook.xlsx")
    assert verdict.ok and verdict.adapter == ZerodhaTradebookXlsxAdapter.name
    result = _build_adapter(verdict.adapter).parse(xlsx, "ACCOUNT")
    assert result.rejected == 0
    assert [r.transaction for r in result.rows] == [r.transaction for r in csv_result.rows]
    assert result.rows[0].transaction.price == Decimal("100.1250")
    kept, skipped = dedupe([r.transaction for r in csv_result.rows + result.rows])
    assert len(kept) == skipped == 2
    assert _DOCTYPES[verdict.adapter][0] == "TRADEBOOK"


def test_excel_dates_padding_and_multiple_trade_sheets_keep_unique_row_numbers():
    rows = [row.copy() for row in TRADE_ROWS]
    rows[1][2] = datetime(2026, 4, 8)
    rows[2][2] = date(2026, 4, 8)
    rows = [[None, *row] for row in rows]
    result = ZerodhaTradebookXlsxAdapter().parse(as_xlsx(rows, second=rows), "ACCOUNT")
    assert result.rejected == 0 and result.parsed == 4
    assert [r.row_number for r in result.rows] == [2, 3, 5, 6]
    assert {r.transaction.txn_date for r in result.rows} == {date(2026, 4, 8)}


def test_contract_note_csv_keeps_xlsx_charges_identity_and_trade_identity():
    csv_content, xlsx_content = as_csv(NOTE_ROWS), as_xlsx(NOTE_ROWS)
    csv_adapter, xlsx_adapter = ZerodhaContractNoteCsvAdapter(), ZerodhaContractNoteXlsxAdapter()
    csv_result = csv_adapter.parse(csv_content, "ACCOUNT")
    xlsx_result = xlsx_adapter.parse(xlsx_content, "ACCOUNT")
    assert csv_result.rejected == xlsx_result.rejected == 0
    assert csv_result.parsed == xlsx_result.parsed == 1
    assert csv_result.rows[0].transaction == xlsx_result.rows[0].transaction
    assert csv_result.rows[0].transaction.charges == Decimal("3.36")
    assert csv_adapter.identity(csv_content) == xlsx_adapter.identity(xlsx_content)
    assert csv_adapter.identity(csv_content).client_code == "TEST123"
    verdict = classify(csv_content, "note.csv")
    assert verdict.ok and verdict.adapter == csv_adapter.name
    assert _DOCTYPES[verdict.adapter] == ("CONTRACT_NOTE", "text/csv")


def test_contract_note_csv_without_charge_totals_cannot_be_imported_as_free_trades():
    result = ZerodhaContractNoteCsvAdapter().parse(as_csv(NOTE_ROWS[:7]), "ACCOUNT")
    assert result.parsed == 0 and result.rejected > 0
    assert any("obligation or net amount" in r.reason for r in result.rejections)


def test_csv_cannot_combine_notes_or_conflicting_client_identities():
    adapter = ZerodhaContractNoteCsvAdapter()
    for rows in (
        NOTE_ROWS + NOTE_ROWS,
        [*NOTE_ROWS, ["PAN of Client:", "FGHIJ5678K"]],
        [*NOTE_ROWS, ["Trade Date:", "09-04-2026"]],
    ):
        result = adapter.parse(as_csv(rows), "ACCOUNT")
        assert result.parsed == 0 and result.rejected == 1
        assert "one complete contract note" in result.rejections[0].reason


def test_contract_note_broker_pan_in_footer_is_not_a_second_client():
    adapter = ZerodhaContractNoteCsvAdapter()
    content = as_csv([*NOTE_ROWS, ["PAN:", "FGHIJ5678K"]])
    result = adapter.parse(content, "ACCOUNT")
    assert result.parsed == 1 and result.rejected == 0
    assert adapter.identity(content).pan == "ABCDE1234F"


@pytest.mark.parametrize("adapter,encode,extension", [
    (CasCsvAdapter, as_csv, "csv"), (CasXlsxAdapter, as_xlsx, "xlsx"),
])
def test_cas_formats_record_the_same_dated_balances_and_never_create_trades(
    adapter, encode, extension,
):
    content = encode(CAS_ROWS)
    verdict = classify(content, f"statement.{extension}")
    assert verdict.ok and verdict.adapter == adapter.name
    instance = _build_adapter(verdict.adapter, broker_accounts={"zerodha": "ACCOUNT"})
    result = instance.parse(content, "ACCOUNT")
    assert result.rows == result.fno_rows == result.rejections == []
    holdings = instance.parse_declared_holdings(content)
    assert len(holdings) == 2
    assert holdings[0].quantity == 10 and holdings[1].quantity == 0
    assert sum(h.market_value for h in holdings) == Decimal(1000)
    assert {h.snap_date for h in holdings} == {date(2026, 8, 31)}
    assert {h.account_id for h in holdings} == {"ACCOUNT"}
    assert instance.identity(content).pan == "ABCDE1234F"
    assert _DOCTYPES[verdict.adapter][0] == "CAS"


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[3].__setitem__(1, 3),  # missing security
    lambda rows: rows[4].__setitem__(1, "1001.00"),  # mismatched statement total
    lambda rows: rows[7].__setitem__(1, 11),  # wrong row arithmetic
    lambda rows: rows[8].__setitem__(0, "INE153T01027"),  # duplicate ISIN
    lambda rows: rows[7].__setitem__(1, "NaN"),
    lambda rows: rows[7].__setitem__(2, "Infinity"),
    lambda rows: rows[1].__setitem__(1, "bad-date"),
    lambda rows: rows[2].__setitem__(1, "Groww"),  # not this investor's broker
    lambda rows: rows[7].__setitem__(0, "INE153T01028"),  # wrong ISIN check digit
    lambda rows: rows[4].__setitem__(1, "1000.001"),  # cannot round away evidence
])
@pytest.mark.parametrize("adapter,encode", [(CasCsvAdapter, as_csv), (CasXlsxAdapter, as_xlsx)])
def test_bad_cas_refuses_the_whole_snapshot(adapter, encode, mutation):
    rows = [row.copy() for row in CAS_ROWS]
    mutation(rows)
    instance = adapter(broker_accounts={"zerodha": "ACCOUNT"})
    with pytest.raises(CasStatementError):
        instance.parse(encode(rows), "ACCOUNT")


def test_cas_table_requires_its_statement_metadata_and_one_table_per_file():
    adapter = CasCsvAdapter(broker_accounts={"zerodha": "ACCOUNT"})
    with pytest.raises(CasStatementError, match="missing"):
        adapter.parse(as_csv([CAS_ROWS[0], *CAS_ROWS[6:]]), "ACCOUNT")
    adapter = CasXlsxAdapter(broker_accounts={"zerodha": "ACCOUNT"})
    with pytest.raises(CasStatementError, match="one CAS holdings table"):
        adapter.parse(as_xlsx(CAS_ROWS, second=CAS_ROWS), "ACCOUNT")


def test_renaming_csv_to_xlsx_still_fails_container_validation():
    assert not classify(as_csv(TRADE_ROWS), "tradebook.xlsx").ok
    assert not classify(as_xlsx(TRADE_ROWS), "tradebook.csv").ok
