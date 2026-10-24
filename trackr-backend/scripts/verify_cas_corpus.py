"""Offline CAS comparison against Poppler extraction and a frozen manual reference.

Run with TRACKR_CAS_PASSWORDS containing private (source, password) pairs in JSON.
All document text and comparison artifacts must be written to an ignored private folder.
No model, database or network client is used by this script.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path

from app.ingest.cas_pdf import CasPdfAdapter
from app.ingest.file_validation import UNLOCK_OPENED, classify, unlock_pdf


def independent_holdings(text: str) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """Read numeric tails from Poppler layout lines, without the application reader."""
    rows = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"IN[A-Z0-9]{9}\d[*~#<>@$^]*", parts[0]):
            continue
        tail = parts[-7:]
        if len(tail) != 7 or not all(re.fullmatch(r"--|[\d,]+\.\d+", t) for t in tail):
            continue
        isin = parts[0][:12]
        if isin in rows:
            raise ValueError("Repeated ISIN in independent single-account reference")
        values = [Decimal(t.replace(",", "")) if t != "--" else Decimal(0) for t in tail]
        rows[isin] = (values[0], values[5], values[6])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidates = json.loads(os.environ["TRACKR_CAS_PASSWORDS"])
    results = []
    seen = set()
    for path in sorted(args.root.glob("*.pdf")):
        # Explicit owner exclusion from the earlier CAS investigation.
        if path.name == "CAS_01042023-11082026_CP220552081_11082026080000778.pdf":
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        unlocked = unlock_pdf(content, candidates)
        if unlocked.outcome != UNLOCK_OPENED:
            raise RuntimeError(f"Cannot open corpus file: {path.name} ({unlocked.outcome})")
        verdict = classify(content, path.name, unlocked.password)
        if verdict.adapter != CasPdfAdapter.name:
            results.append({"file": path.name, "status": "non_cas_control",
                            "detected_document": verdict.document})
            continue
        extracted = subprocess.run(
            ["pdftotext", "-upw", unlocked.password, "-layout", str(path), "-"],
            capture_output=True, check=True,
        ).stdout.decode()
        (args.out / (path.name + ".poppler.txt")).write_text(extracted)
        expected = independent_holdings(extracted)
        adapter = CasPdfAdapter(password=unlocked.password, broker_accounts={"zerodha": "AUDIT"})
        parsed = adapter.parse(content, "AUDIT", path.name)
        holdings = adapter.parse_declared_holdings(content)
        movements = adapter.extract_movements(content)
        actual = {h.isin: (h.quantity, h.market_price, h.market_value) for h in holdings}
        differences = [i for i in sorted(set(expected) | set(actual))
                       if expected.get(i) != actual.get(i)]
        results.append({
            "file": path.name, "status": "parsed", "sha256": digest,
            "expected_holdings": len(expected), "actual_holdings": len(holdings),
            "field_discrepancies": differences, "priced_trades": parsed.parsed,
            "holdings": [dataclasses.asdict(h) for h in holdings],
            "movements": [dataclasses.asdict(m) for m in movements], "notes": parsed.notes,
        })
        if differences or parsed.parsed or parsed.rejected:
            raise AssertionError(f"CAS comparison failed: {path.name}")
    reference = json.loads((args.out / "manual-reference.json").read_text())
    chosen = next(r for r in results if r["file"] == reference["file"])
    expected_manual = {i: tuple(Decimal(x) for x in (q, p, v))
                       for i, q, p, v in reference["holdings"]}
    actual_manual = {h["isin"]: (h["quantity"], h["market_price"], h["market_value"])
                     for h in chosen["holdings"]}
    assert expected_manual == actual_manual
    actual_movements = [[m["isin"], str(m["settle_date"])] + [m[k] for k in
                        ("opening", "credit", "debit", "closing")] for m in chosen["movements"]]
    expected_movements = [[i, day] + [Decimal(v) for v in values]
                         for i, day, *values in reference["movements"]]
    assert actual_movements == expected_movements
    parsed_files = [r for r in results if r["status"] == "parsed"]
    summary = {"parsed_pdf_count": len(parsed_files),
               "holding_rows_compared": sum(r["actual_holdings"] for r in parsed_files),
               "field_discrepancies": 0, "manual_reference_matches": True,
               "manual_movement_count": len(actual_movements), "model_calls": 0,
               "direct_fund_structured": False, "results": results}
    (args.out / "comparison.json").write_text(json.dumps(summary, default=str, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
