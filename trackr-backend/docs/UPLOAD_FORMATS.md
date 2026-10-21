# Supported upload formats

The import screen accepts mixed uploads and chooses the parser from the file contents.
Renaming a CSV to `.xlsx` does not convert it and is rejected.

| Document | CSV | XLSX | PDF |
| --- | --- | --- | --- |
| Zerodha Tradebook | Yes | Yes | No deterministic Tradebook reader |
| Zerodha contract note | Complete single-day note | Bulk daily sheets or the existing three-sheet layout | Existing text PDF reader |
| CAS | Structured demat holdings table | Structured demat holdings table | Existing CAS reader, including supported folios and movement diagnostics |

These are layout-specific readers, not a promise to parse every broker's document.
The broker registry advertises the installed adapters. Unrecognised layouts are refused
with an explanation. Optional model extraction is separate from these capabilities.

## Tradebooks

CSV and XLSX use the same required columns: `ISIN`, `Trade Date`, `Trade Type`,
`Quantity`, `Price`, and `Trade ID`. Spaces/underscores and header case are normalized.
Title blocks and blank rows above the header are accepted. XLSX date cells are accepted.
Only equity rows are supported. Prices and quantities stay Decimal, and Trade ID keeps
otherwise identical partial fills distinct across formats and repeated imports.

Tradebooks contain no charges. Contract notes supply brokerage and taxes.

## Contract-note CSVs

A CSV represents one complete trading-day sheet from the supported Zerodha XLSX layout,
with its client identity, trade date, trades, charge breakdown, pay-in/pay-out obligation,
and net amount. The CSV reader shares the XLSX reader's row arithmetic, note-total
checks and charge apportionment. A Trades-only sheet from a three-sheet note is incomplete;
upload that original XLSX instead. A CSV combining notes or conflicting client/date
metadata is refused.

## CAS holdings CSV/XLSX

Use the template available under **CAS spreadsheet format** in the upload screen. A CSV
and a workbook use the same cells. The workbook contains one holdings table. This is a
structured export/template format, not an arbitrary PDF-to-Excel conversion.

The first cell is `CAS Holdings` (or `CONSOLIDATED ACCOUNT STATEMENT`). Before the table,
include each metadata label in column A and its value in column B:

| Label | Value |
| --- | --- |
| Statement Date | Required, YYYY-MM-DD or an XLSX date cell |
| Broker | Required; must identify one of this investor's registered broker accounts |
| Total Securities | Required; total ISIN rows, including zero balances |
| Portfolio Value | Required; total market value, stated to the paisa |
| PAN | Optional holder PAN; checked against the investor when supplied |

Follow this metadata with the header `ISIN,Quantity,Market Price,Market Value` and one row
per ISIN. Zero quantities are retained. Numeric cells must be finite and nonnegative.
Each row's quantity times price must reproduce its value within one paisa. The security
count and portfolio value must exactly reproduce the statement totals. Duplicate ISINs,
invalid check digits, ambiguous brokers and incomplete tables refuse the entire snapshot.

CAS tables feed the existing dated holding snapshots and account reconciliation flow.
They do not create transactions, cost basis, returns, settlement matches or corporate
actions. For mutual fund folios and depository movement details, use the original CAS PDF.
New spreadsheet formats are registered for interactive upload/reparse; they do not
enable unattended email promotion.

## Verification

`tests/test_document_formats.py` covers cross-format trade identity, fees, XLSX dates,
incorrect worksheet dimensions, and CAS completeness/account checks.
`tests/test_db_document_formats.py` exercises mixed uploads, document metadata, CAS
snapshots, duplicate uploads and isolation of a refused file through HTTP and PostgreSQL.
Both frontend applications share the updated upload guidance and history display.
