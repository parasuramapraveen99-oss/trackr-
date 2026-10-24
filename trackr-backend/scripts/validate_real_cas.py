"""Compare real CDSL PDFs with an independent Poppler extraction.

Run from the deployment backend with --documents PATH --split train|test.
Use PORTFOLIO_LOCAL_PAN or --password-file; no password is printed or stored.
Detailed reports belong in ignored local storage, not version control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

# The chosen backend must win over an editable install of a different checkout.
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from cas_reference import extract  # noqa: E402

from app.ingest.cas_pdf import CasPdfAdapter  # noqa: E402
from app.ingest.file_validation import UNLOCK_OPENED, unlock_pdf  # noqa: E402

TRAIN = {"MAR2025", "MAR2026", "APR2025", "MAY2025", "JUN2025", "JUL2025", "AUG2025"}
TEST = {"APR2026", "MAY2026", "JUN2026", "JUL2026", "JULY CAS"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--split", choices=["train", "test"], required=True)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    password = (
        args.password_file.read_text().strip()
        if args.password_file
        else os.environ.get("PORTFOLIO_LOCAL_PAN", "")
    )
    selected = TRAIN if args.split == "train" else TEST
    source = BACKEND / "app" / "ingest" / "cas_pdf.py"
    report = {
        "split": args.split,
        "parser_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "files": {},
    }
    seen = set()
    for path in sorted(args.documents.glob("*.pdf")):
        if path.stem.split("_")[0].upper() not in selected:
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        try:
            unlocked = unlock_pdf(content, [("pan", password)])
            if unlocked.outcome != UNLOCK_OPENED:
                raise ValueError("PDF could not be opened with the available password")
            text = subprocess.run(
                ["pdftotext", "-upw", unlocked.password, "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            expected, expected_movements = extract(text)
            adapter = CasPdfAdapter(
                password=unlocked.password, broker_accounts={"zerodha": "audit-account"}
            )
            result = adapter.parse(content, "audit-account", path.name)
            holdings = adapter.parse_declared_holdings(content, "audit-account")
            movements = adapter.extract_movements(content)
            actual = {h.isin: (h.quantity, h.market_price, h.market_value) for h in holdings}
            actual_movements = [
                (m.isin, m.settle_date.strftime("%d-%m-%Y"), m.credit, m.debit, m.closing)
                for m in movements
            ]
            passed = (
                expected == actual
                and expected_movements == actual_movements
                and not result.rows
                and not result.rejections
            )
            outcome = {
                "passed": passed,
                "as_of": str(holdings[0].snap_date),
                "holdings": len(holdings),
                "movements": len(movements),
                "total": str(sum((h.market_value for h in holdings), Decimal(0))),
            }
        except Exception as exc:
            outcome = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        report["files"][path.name] = outcome
        print(path.name, "PASS" if outcome["passed"] else "FAIL", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    return 0 if report["files"] and all(r["passed"] for r in report["files"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
