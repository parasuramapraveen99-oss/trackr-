"""Expand ``fixtures/reports/corpus.json`` into typed documents (RP 00 test support).

The corpus is SYNTHETIC. Every company, figure and source text in it is invented for testing
the contract checks; none is a real issuer disclosure, and nothing here certifies a source,
provider, formula or report. The expected outcomes in the corpus were written by hand from the
rules in docs/BUTLER_REPORTS.md, independently of ``report_contract.py``'s code, and pinned
numeric values are stated as exact decimals so a scale or sign slip cannot pass.

A case is compact on purpose: fiscal cells are generated from year ranges and per-cell
overrides, so each case states only what is adversarial about it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.engine import research_metrics as rm
from app.research.report_contract import SourceSnapshot, document_digest
from app.schemas.reports import SECTION_IDS, ArtifactManifest, ReportDocument

CORPUS = Path(__file__).resolve().parents[3] / "fixtures" / "reports" / "corpus.json"


#: SYNTHETIC test formula for the corpus's calculated-value cases: later minus earlier, in
#: absolute INR units (claims are scale-normalised before evaluation). Not a BE-Q03 approval.
_CORPUS_FORMULAS = (
    rm.FormulaDefinition(
        formula_id="ABSOLUTE_CHANGE", version="1", title="Synthetic absolute change (test only)",
        expression=("sub", ("input", "later"), ("input", "earlier")),
        inputs=(rm.InputSpec("earlier", "SYN_ANY", "CONSOLIDATED", "INR", "YEAR", "PRIOR"),
                rm.InputSpec("later", "SYN_ANY", "CONSOLIDATED", "INR", "YEAR", "CURRENT")),
        output_unit="INR", precision=rm.Precision("0.000001"),
        approval=rm.Approval("SYNTHETIC_FIXTURE", "2026-09-28", "fixtures/reports/corpus.json")),
)


def formulas_for(case: dict[str, Any]) -> rm.FormulaRegistry:
    """The registry a corpus case runs under: only the synthetic formulas it names."""
    wanted = set(case.get("approved_formulas", []))
    return rm.FormulaRegistry(version="corpus-synthetic-1",
                              formulas=tuple(d for d in _CORPUS_FORMULAS
                                             if d.formula_id in wanted),
                              synthetic=True)


def load(path: Path = CORPUS) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _period(label: str, months: int = 12) -> dict[str, Any]:
    year = int(label[2:6])
    end = date(year, 3, 31)
    start_month = 4 + (12 - months)
    start_year = year - 1 + (start_month - 1) // 12
    start_month = (start_month - 1) % 12 + 1
    return {"label": label, "start": date(start_year, start_month, 1).isoformat(),
            "end": end.isoformat(), "months": months}


def build(case: dict[str, Any], defaults: dict[str, Any]) -> tuple[
        ReportDocument, dict[tuple[str, str], SourceSnapshot], ArtifactManifest | None]:
    base = {**defaults, **case}
    latest = base["latest_fy"]
    window = base["window_years"]
    statements = base["statements"]
    basis = base.get("basis", "CONSOLIDATED")
    overrides = {(o["fy"], o["statement"]): o for o in base.get("cell_overrides", [])}
    supported_from = base.get("supported_from", latest - window + 1)
    default_ref = base.get("default_ref")
    cells = []
    for year in range(latest - window + 1, latest + 1):
        for st in statements:
            o = overrides.get((year, st), {})
            label = o.get("label", f"FY{year}")
            status = o.get("status", "SUPPORTED" if year >= supported_from else "MISSING")
            cell = {
                "cell_id": f"{label}:{st}",
                "period": _period(label, o.get("months", 12)),
                "statement": st,
                "basis": o.get("basis", basis),
                "status": status,
                "gap_code": o.get("gap_code", None if status in ("SUPPORTED", "RESTATED")
                                  else "SOURCE_NOT_FOUND"),
                "source_refs": o.get("source_refs", [default_ref] if status in (
                    "SUPPORTED", "RESTATED", "CONFLICT") and default_ref else []),
                "original_source_refs": o.get("original_source_refs", []),
            }
            cells.append(cell)
    claims = list(base.get("claims", [])) + list(case.get("extra_claims", []))
    sources = [dict(src) for src in base["sources"]] + list(case.get("extra_sources", []))
    for src in sources:
        src.update(case.get("source_overrides", {}).get(src["source_id"], {}))
    snapshots = {**base.get("snapshots", {}), **case.get("extra_snapshots", {})}
    section_status = base.get("section_status", {})
    sections = [{"section_id": s, "status": section_status.get(s, "COVERED"),
                 "claim_ids": [c["claim_id"] for c in claims if c["section_id"] == s],
                 "limitations": []} for s in SECTION_IDS]
    doc = ReportDocument.model_validate({
        "schema_version": "report.document.v1",
        "report_id": base["id"],
        "report_version": 1,
        "isin": base["isin"],
        "question": base["question"],
        "mode": base.get("mode", "CURRENT_WITH_HISTORY"),
        "scope": base.get("scope", "ISSUER_ONLY"),
        "as_of": base["as_of"],
        "cutoff": base.get("cutoff"),
        "status": base["document_status"],
        "decision_notice": "Research information only. The investor decides.",
        "coverage": {"isin": base.get("coverage_isin", base["isin"]),
                     "requested_years": window, "basis": basis, "cells": cells},
        "sections": sections,
        "claims": claims,
        "sources": sources,
        "limitations": base.get("limitations", []),
        "check_receipts": ["corpus-check-1"],
    })
    snaps = {tuple(k.split("@", 1)): SourceSnapshot(v["text"], v.get("values", {}))
             for k, v in snapshots.items()}
    manifest = None
    if "artifact" in base:
        a = base["artifact"]
        manifest = ArtifactManifest.model_validate({
            "schema_version": "report.artifact.v1",
            "artifact_id": f"{base['id']}-pdf",
            "report_id": base["id"],
            "report_version": 1,
            "format": "PDF",
            "sha256": "0" * 64,
            "byte_size": 1024,
            "page_count": 3,
            "document_sha256": document_digest(doc),
            "claim_ids": [c["claim_id"] for c in claims
                          if c["claim_id"] not in a.get("omit_claims", [])],
            "check_receipts": ["corpus-check-1"],
            "contains_raw_source_rows": a.get("contains_raw_source_rows", False),
            "renderer": "corpus-renderer",
            "created_at": datetime(2026, 9, 28).isoformat() + "Z",
        })
    return doc, snaps, manifest
