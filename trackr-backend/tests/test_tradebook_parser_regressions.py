"""Malformed tradebook rows must not hide trades or break the whole upload."""

from decimal import Decimal

import pytest

from app.ingest.zerodha_tradebook import ZerodhaTradebookAdapter

HEADER = "symbol,isin,trade_date,trade_type,quantity,price,trade_id\n"
ROW = "EXAMPLE,INE153T01027,2026-04-08,buy,10,423.1250,10001\n"


def parse(body, header=HEADER):
    return ZerodhaTradebookAdapter().parse((header + body).encode(), "TEST-ACCOUNT")


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["quantity", "price"])
def test_nonfinite_number_rejects_only_its_row(field, value):
    bad = ROW.replace(",10,", f",{value},") if field == "quantity" else ROW.replace(
        ",423.1250,", f",{value},"
    )
    result = parse(bad + ROW)
    assert result.parsed == 1
    assert result.rejected == 1
    assert result.rejections[0].row_number == 2
    assert "finite" in result.rejections[0].reason
    assert result.rows[0].transaction.price == Decimal("423.1250")


@pytest.mark.parametrize("bad", [ROW.rstrip() + ",unexpected\n", ROW.rsplit(",", 1)[0] + "\n"])
def test_wrong_column_count_cannot_silently_discard_data(bad):
    result = parse(bad + ROW)
    assert result.parsed == 1
    assert result.rejected == 1
    assert result.rejections[0].row_number == 2
    assert "columns" in result.rejections[0].reason


def test_blank_and_multiline_cells_keep_physical_line_numbers():
    multiline = ROW.replace("EXAMPLE", '"EXAMPLE\nLIMITED"')
    result = parse("\n" + multiline + "\n" + ROW.replace(",buy,", ",invalid,"))
    assert result.parsed == 1
    assert result.rows[0].row_number == 3
    assert result.rejections[0].row_number == 6


def test_empty_spreadsheet_rows_are_ignored():
    result = parse(",,,,,,\n" + ROW)
    assert result.parsed == 1
    assert result.rejected == 0
    assert result.rows[0].row_number == 3


def test_duplicate_normalized_header_cannot_overwrite_price():
    result = parse(ROW.rstrip() + ",999\n", HEADER.rstrip() + ",Price\n")
    assert result.parsed == 0
    assert result.rejected == 1
    assert "duplicate column" in result.rejections[0].reason


def test_invalid_csv_does_not_discard_prior_valid_rows():
    result = parse(ROW + '"unterminated\n')
    assert result.parsed == 1
    assert result.rejected == 1
    assert result.rejections[0].row_number == 3
    assert "invalid CSV" in result.rejections[0].reason


def test_preamble_title_headers_and_identical_partial_fills():
    preamble = '\n' * 6 + 'Client ID,TEST-CLIENT\n,\n,\n\nTradebook for Equity\n\n""\n\n'
    header = "Symbol,ISIN,Trade Date,Trade Type,Quantity,Price,Trade ID\n"
    content = preamble + header + ROW + ROW.replace("10001", "10002")
    result = ZerodhaTradebookAdapter().parse(content.encode(), "TEST-ACCOUNT")
    assert result.parsed == 2
    assert result.rejected == 0
    assert [row.row_number for row in result.rows] == [16, 17]
    assert [row.transaction.source_ref for row in result.rows] == ["10001", "10002"]
