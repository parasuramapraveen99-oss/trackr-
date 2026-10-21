"""Compare local Zerodha CSVs with an independent positional reader.

Run from trackr-backend with python -m scripts.verify_tradebook_corpus --root ... --out ...
An optional --reference JSON freezes a manually checked file's summary. Source rows and
reports belong in an ignored local/ folder; this script uses no database or network.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.engine.dedupe import dedupe, make_dedupe_key
from app.ingest.file_validation import classify
from app.ingest.zerodha_tradebook import ZerodhaTradebookAdapter

ACCOUNT = "TRADEBOOK-AUDIT"
HEADERS = (
    "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,"
    "trade_id,order_id,order_execution_time",
    "Symbol,ISIN,Trade Date,Exchange,Segment,Series,Trade Type,Auction,Quantity,Price,"
    "Trade ID,Order ID,Order Execution Time",
)


def independent_rows(content: bytes) -> list[dict]:
    """Read the known 13-column broker layout without application parser helpers.

    Positional decoding deliberately differs from the production header mapping. Keep
    every occurrence in order: a dictionary keyed by trade ID could conceal lost fills.
    """
    reader = csv.reader(io.StringIO(content.decode("utf-8-sig")), strict=True)
    for cells in reader:
        if ",".join(cells) in HEADERS:
            break
    else:
        raise ValueError("Independent reader: no known Zerodha header")
    rows = []
    while True:
        line = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) != 13 or cells[4] != "EQ" or cells[6] not in ("buy", "sell"):
            raise ValueError(f"Independent reader: unexpected source row at line {line}")
        quantity, price = Decimal(cells[8]), Decimal(cells[9])
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price < 0:
            raise ValueError(f"Independent reader: invalid numeric field at line {line}")
        rows.append({
            "row_number": line, "isin": cells[1], "txn_date": date.fromisoformat(cells[2]),
            "txn_type": cells[6].upper(), "quantity": quantity, "price": price,
            "source_ref": cells[10], "source": "TRADEBOOK", "charges": Decimal(0),
            "account_id": ACCOUNT,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    totals = defaultdict(lambda: {"trades": 0, "quantity": Decimal(0), "notional": Decimal(0)})
    for row in rows:
        group = totals[f"{row['isin']}:{row['txn_type']}"]
        group["trades"] += 1
        group["quantity"] += row["quantity"]
        group["notional"] += row["quantity"] * row["price"]
    return {
        "trades": len(rows),
        "securities": len({row["isin"] for row in rows}),
        "date_from": min(row["txn_date"] for row in rows).isoformat() if rows else None,
        "date_to": max(row["txn_date"] for row in rows).isoformat() if rows else None,
        "sides": dict(Counter(row["txn_type"] for row in rows)),
        "groups": dict(sorted(totals.items())),
    }


def actual_rows(parsed) -> list[dict]:
    return [
        {"row_number": row.row_number, **{
            field: getattr(row.transaction, field) for field in (
                "isin", "txn_date", "txn_type", "quantity", "price", "source_ref",
                "source", "charges", "account_id",
            )
        }}
        for row in parsed.rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    paths = sorted(args.root.glob("*.csv"))
    if not paths:
        parser.error("No CSV files found")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    reference = (
        json.loads(args.reference.read_text(), parse_float=Decimal) if args.reference else None
    )
    results, transactions = [], []
    for path in paths:
        content = path.read_bytes()
        expected = independent_rows(content)
        parsed = ZerodhaTradebookAdapter().parse(content, ACCOUNT, path.name)
        actual = actual_rows(parsed)
        # Compare by occurrence, including physical line numbers and exact Decimals.
        differences = [
            {"index": index, "expected": left, "actual": right}
            for index in range(max(len(expected), len(actual)))
            if (left := expected[index] if index < len(expected) else None)
            != (right := actual[index] if index < len(actual) else None)
        ]
        verdict = classify(content, path.name)
        expected_summary, actual_summary = summarize(expected), summarize(actual)
        manual_match = None
        if reference and reference["file"] == path.name:
            manual_match = (
                reference["sha256"] == hashlib.sha256(content).hexdigest()
                and reference["summary"] == expected_summary == actual_summary
            )
        result = {
            "file": path.name, "sha256": hashlib.sha256(content).hexdigest(),
            "adapter": verdict.adapter, "reference_rows": len(expected),
            "parsed_rows": parsed.parsed, "rejected_rows": parsed.rejected,
            "field_discrepancies": differences, "manual_reference_matches": manual_match,
            "expected_summary": expected_summary, "actual_summary": actual_summary,
            "passed": not differences and not parsed.rejected
            and verdict.adapter == ZerodhaTradebookAdapter.name and manual_match is not False,
        }
        transactions.extend(row.transaction for row in parsed.rows)
        results.append(result)
        (args.out / (path.stem + ".rows.json")).write_text(json.dumps(
            {"independent": expected, "trackerplus": actual}, default=str, indent=2,
        ))
    kept, skipped = dedupe(transactions)
    replay_kept, replay_skipped = dedupe(
        transactions, {make_dedupe_key(txn) for txn in kept},
    )
    manual_passed = any(r["manual_reference_matches"] is True for r in results)
    summary = {
        "files_compared": len(results), "rows_compared": len(transactions),
        "field_discrepancies": sum(len(r["field_discrepancies"]) for r in results),
        "rejected_rows": sum(r["rejected_rows"] for r in results),
        "unique_trades": len(kept), "overlap_duplicates": skipped,
        "reimport_new_trades": len(replay_kept), "reimport_duplicates": replay_skipped,
        "manual_reference_matches": manual_passed if reference else None,
        "passed": all(r["passed"] for r in results) and not replay_kept
        and replay_skipped == len(transactions) and (not reference or manual_passed),
    }
    (args.out / "comparison.json").write_text(json.dumps(
        {**summary, "results": results}, default=str, indent=2,
    ))
    print(json.dumps(summary, indent=2))
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
