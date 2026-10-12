"""The research-report contract checks (RP 00, docs/BUTLER_REPORTS.md sections 4-8).

PURE: typed documents and source snapshots in, findings out. No SQL, no model, no network, no
float. This is the gate every later report stage (RP 02-06) must pass through; it is written
first, against a synthetic adversarial corpus (``fixtures/reports/corpus.json``), so that no
report can be marked ready by prose alone.

WHAT PASSING MEANS -- AND DOES NOT. Passing means the DECLARED checks held for the NAMED scope:
entity/scope/cutoff valid, every cited source registered and rights-permitted, every excerpt
present verbatim in its source, every numeric cell reproduced exactly (Decimal, scale-normalised,
same unit), no mixed fiscal basis in a series, no hindsight source in a point-in-time report, no
recommendation or self-completeness language, no notebook/profile field and no echoed injected
instruction. It does NOT mean every material fact was found, that interpretations are true, or
that the company is safe. A citation existing does not prove entailment; ``READY_CHECKED_SCOPE``
is the strongest label and it is still scope-limited.

WRITTEN FIXTURES ARE NOT A VERIFIED SOURCE. The corpus proves these checks behave; it certifies
no provider, no source, no formula and no deployed service.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine import research_metrics as rm
from app.schemas.reports import SECTION_IDS, ArtifactManifest, Claim, ReportDocument

#: Keys that would carry the investor notebook into a company report. Refused anywhere in a
#: packet or document, at any depth. Deliberately broad: a false refusal is a bug report; a
#: false pass is a privacy breach.
NOTEBOOK_KEYS = frozenset({
    "notebook", "pattern", "patterns", "episode", "episodes", "self_description",
    "self_descriptions", "coaching", "coaching_use", "hypothesis", "hypotheses",
    "consent_version", "catalogue_id", "polarity", "investor_pattern", "reasoning_habits",
    "profile", "psychology", "personality", "suitability",
})
NOTEBOOK_KEY_PREFIXES = ("butler_", "notebook_")

#: Scale factors, exact.
SCALES = {"UNITS": Decimal(1), "THOUSANDS": Decimal(10) ** 3, "LAKHS": Decimal(10) ** 5,
          "MILLIONS": Decimal(10) ** 6, "CRORES": Decimal(10) ** 7, "BILLIONS": Decimal(10) ** 9}

#: Calculated cells are reproduced by the ONE approved calculator registry (BE 27,
#: ``app/engine/research_metrics.py``) -- never by a second arithmetic implementation here. A
#: CALCULATED_VALUE whose formula id/version is not in the live registry (BE-Q03, decided
#: 2026-09-29) is held at NEEDS_REVIEW with FORMULA_NOT_APPROVED. The synthetic corpus
#: supplies its own test registry.
LIVE_FORMULAS = rm.LIVE_FORMULAS

_RECOMMENDATION = [
    ("RECOMMENDATION_LANGUAGE", re.compile(
        r"\b(we|i)\s+(recommend|advise|suggest)\b|\brecommend(s|ed)?\s+(buying|selling|"
        r"holding|accumulating)\b|\b(strong\s+)?(buy|sell|hold|accumulate|outperform|"
        r"underperform)\s+(rating|recommendation|call)\b|\bis\s+a\s+(strong\s+)?(buy|sell)\b|"
        r"\b(rate|rated|rating)\s+(it|this|the stock)?\s*(a\s+)?(strong\s+)?(buy|sell|hold)\b",
        re.I)),
    ("TARGET_OR_FAIR_VALUE", re.compile(
        r"\btarget\s+price\b|\bprice\s+target\b|\bfair\s+value\s+(of|is|at)\b|"
        r"\bintrinsic\s+value\s+(of|is|at)\b|\bupside\s+of\b", re.I)),
    ("CONVICTION_OR_RETURN_PROMISE", re.compile(
        r"\bconviction\b|\bguarantee(d|s)?\b|\bassured\s+return|\bwill\s+(double|multiply)\b|"
        r"\brisk[- ]free\b|\bzero[- ]risk\b|\bno\s+risk\b", re.I)),
    ("SELF_COMPLETENESS", re.compile(
        r"\bflawless\b|\bperfect\s+report\b|\bcomplete\s+(coverage|picture|analysis)\b|"
        r"\ball\s+material\s+facts\b|\bnothing\s+(was\s+)?missed\b|\bfully\s+verified\b", re.I)),
    ("SUITABILITY_VERDICT", re.compile(
        r"\bsuitable\s+for\s+you\b|\bright\s+for\s+you\b|\byour\s+(personality|temperament|"
        r"risk\s+profile)\b", re.I)),
]

#: Text that only appears when injected instructions were echoed rather than treated as data.
_INJECTION_ECHO = re.compile(
    r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b|\bsystem\s+prompt\b|"
    r"\bas\s+an\s+ai\b|\byou\s+are\s+now\b|\bdisregard\s+(the\s+)?(rules|instructions)\b",
    re.I)
_URL = re.compile(r"https?://[^\s)\]]+", re.I)
_CONDITIONAL = re.compile(r"\b(if|could|may|might|would|should\s+.+\s+occur|in\s+the\s+event)\b",
                          re.I)


class ContractViolation(ValueError):
    def __init__(self, code: str, where: str) -> None:
        super().__init__(f"{code} at {where}")
        self.code = code
        self.where = where


def forbid_notebook_keys(obj: Any, where: str = "$") -> None:
    """Raise if any key, at any depth, is notebook-shaped. Used on every report packet before
    it is sealed and on every document before it is stored or rendered."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            if key in NOTEBOOK_KEYS or key.startswith(NOTEBOOK_KEY_PREFIXES):
                raise ContractViolation("NOTEBOOK_FIELD_IN_REPORT", f"{where}.{k}")
            forbid_notebook_keys(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            forbid_notebook_keys(v, f"{where}[{i}]")


def screen_text(text: str) -> list[str]:
    """Reviewed codes for prohibited report language in one piece of text."""
    codes = [code for code, rx in _RECOMMENDATION if rx.search(text)]
    if _INJECTION_ECHO.search(text):
        codes.append("INJECTION_ECHO")
    return codes


def normalised(value: str, scale: str) -> Decimal:
    try:
        return Decimal(value) * SCALES[scale]
    except (InvalidOperation, KeyError) as exc:
        raise ContractViolation("VALUE_NOT_DECIMAL", value) from exc


def document_digest(doc: ReportDocument) -> str:
    payload = json.dumps(doc.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _squash(text: str) -> str:
    return " ".join(text.split())


# =========================================================================================
# planning -- which cells a request needs
# =========================================================================================


def planned_periods(window_years: int, latest_fiscal_year: int) -> list[str]:
    """FY labels the request asks for, newest last. A requested window, not a promise."""
    return [f"FY{y}" for y in range(latest_fiscal_year - window_years + 1,
                                    latest_fiscal_year + 1)]


# =========================================================================================
# validation
# =========================================================================================


@dataclass
class SourceSnapshot:
    """What a registered source actually says, for checking. ``text`` is the extracted text;
    ``values`` maps a locator to {"value", "unit", "scale", "period_label", "basis"} for
    structured cells. Built by RP 02/03 from real extraction; hand-written in the corpus."""

    text: str
    values: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Findings:
    status: str
    rejected: dict[str, list[str]] = field(default_factory=dict)
    needs_review: dict[str, list[str]] = field(default_factory=dict)
    document_findings: list[str] = field(default_factory=list)
    missing_periods: list[str] = field(default_factory=list)
    gap_cells: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "rejected": self.rejected,
                "needs_review": self.needs_review, "document_findings": self.document_findings,
                "missing_periods": self.missing_periods, "gap_cells": self.gap_cells}


def _reject(f: Findings, claim_id: str, code: str) -> None:
    f.rejected.setdefault(claim_id, [])
    if code not in f.rejected[claim_id]:
        f.rejected[claim_id].append(code)


def _review(f: Findings, claim_id: str, code: str) -> None:
    f.needs_review.setdefault(claim_id, [])
    if code not in f.needs_review[claim_id]:
        f.needs_review[claim_id].append(code)


def _unit_mode(v: Any, spec_unit: str) -> str | None:
    """BASE when the definition's unit is the claim's base unit (values are normalised by
    scale); FILED when it names the claim's scaled unit exactly; None otherwise."""
    if v.unit == spec_unit:
        return "BASE"
    if v.scale != "UNITS" and f"{v.unit}_{v.scale}" == spec_unit:
        return "FILED"
    return None


def _operand(v: Any, mode: str) -> Decimal:
    return normalised(v.value, v.scale) if mode == "BASE" else Decimal(v.value)


def _check_claim(c: Claim, doc: ReportDocument, sources: dict[tuple[str, str], Any],
                 snapshots: dict[tuple[str, str], SourceSnapshot],
                 claims: dict[str, Claim], formulas: rm.FormulaRegistry, f: Findings) -> None:
    for code in screen_text(c.text):
        _reject(f, c.claim_id, code)
    links = {s.link for s in doc.sources if s.link}
    for url in _URL.findall(c.text):
        if url.rstrip(".,;") not in links:
            _reject(f, c.claim_id, "UNREGISTERED_URL")

    for ref in c.source_refs:
        reg = sources.get((ref.source_id, ref.source_version))
        if reg is None:
            _reject(f, c.claim_id, "FAKE_CITATION")
            continue
        if reg.rights.get("MODEL") in ("DENIED",) or reg.rights.get("EXTRACT") == "DENIED":
            _reject(f, c.claim_id, "RIGHTS_DENIED")
        elif "UNDECIDED" in (reg.rights.get("MODEL"), reg.rights.get("EXTRACT")):
            _review(f, c.claim_id, "RIGHTS_UNDECIDED")
        if doc.mode == "POINT_IN_TIME":
            if reg.published_on is None:
                _reject(f, c.claim_id, "AVAILABILITY_UNKNOWN")
            elif doc.cutoff is not None and reg.published_on > doc.cutoff:
                _reject(f, c.claim_id, "HINDSIGHT_LEAK")

    if c.claim_class == "SOURCE_STATEMENT":
        if not c.source_refs or c.excerpt is None:
            _reject(f, c.claim_id, "UNSUPPORTED_STATEMENT")
        else:
            snap = snapshots.get((c.source_refs[0].source_id, c.source_refs[0].source_version))
            if snap is None or _squash(c.excerpt) not in _squash(snap.text):
                _reject(f, c.claim_id, "FAKE_CITATION")
            elif c.value is not None:
                cell = snap.values.get(c.source_refs[0].locator)
                if cell is None:
                    _reject(f, c.claim_id, "FAKE_CITATION")
                elif (cell["unit"] != c.value.unit
                      or normalised(cell["value"], cell["scale"])
                      != normalised(c.value.value, c.value.scale)):
                    _reject(f, c.claim_id, "NUMERIC_MISMATCH")
                elif cell.get("basis") and cell["basis"] != c.value.basis:
                    _reject(f, c.claim_id, "MIXED_BASIS")
        if c.verification not in ("CHECKED_BY_CODE", "EXTRACTIVE", "NEEDS_REVIEW"):
            _reject(f, c.claim_id, "LABEL_MISMATCH")

    elif c.claim_class == "CALCULATED_VALUE":
        inputs = [claims.get(i) for i in c.input_claim_ids]
        definition = (formulas.get(c.calculation_ref, c.calculation_version)
                      if c.calculation_ref and c.calculation_version else None)
        if c.value is None or not inputs or any(i is None or i.value is None for i in inputs):
            _reject(f, c.claim_id, "INCOMPLETE_LINEAGE")
        elif definition is None or definition.kind != "METRIC":
            _review(f, c.claim_id, "FORMULA_NOT_APPROVED")
        elif len(inputs) != len(definition.inputs):
            _reject(f, c.claim_id, "INCOMPLETE_LINEAGE")
        else:
            pairs = list(zip(inputs, definition.inputs, strict=True))
            bases = {i.value.basis for i in inputs} | {c.value.basis}
            modes = [_unit_mode(i.value, spec.unit) for i, spec in pairs]
            out_mode = _unit_mode(c.value, definition.output_unit)
            if len(bases) > 1 or any(i.value.basis != spec.basis for i, spec in pairs):
                _reject(f, c.claim_id, "MIXED_BASIS")
            elif None in modes or out_mode is None:
                _reject(f, c.claim_id, "UNIT_MISMATCH")
            else:
                # A definition in base units ('INR') is evaluated on scale-normalised values; one
                # in scaled units ('INR_CRORES') on the values exactly as filed. Never mixed.
                operands = {f"input:{spec.name}": _operand(i.value, mode)
                            for (i, spec), mode in zip(pairs, modes, strict=True)}
                status, got, _code = rm.evaluate_operands(definition, operands)
                if status != rm.OK or got is None:
                    _reject(f, c.claim_id, "CALCULATION_UNDEFINED")
                elif Decimal(got) != _operand(c.value, out_mode):
                    _reject(f, c.claim_id, "NUMERIC_MISMATCH")
        if c.verification == "CHECKED_BY_CODE" and c.claim_id in f.needs_review:
            _reject(f, c.claim_id, "LABEL_OVERCLAIM")

    elif c.claim_class in ("INTERPRETATION", "HYPOTHETICAL_RISK"):
        if c.verification in ("CHECKED_BY_CODE", "EXTRACTIVE"):
            _reject(f, c.claim_id, "LABEL_OVERCLAIM")
        if not c.premise_claim_ids or any(p not in claims for p in c.premise_claim_ids):
            _reject(f, c.claim_id, "MISSING_PREMISES")
        if c.claim_class == "HYPOTHETICAL_RISK" and not _CONDITIONAL.search(c.text):
            _reject(f, c.claim_id, "UNCONDITIONAL_RISK")
        if c.value is not None:
            _reject(f, c.claim_id, "VALUE_IN_INTERPRETATION")
        if c.verification == "NEEDS_REVIEW":
            _review(f, c.claim_id, "INTERPRETATION_UNREVIEWED")

    elif c.claim_class == "UNKNOWN":
        if c.unknown_reason is None:
            _reject(f, c.claim_id, "UNKNOWN_WITHOUT_REASON")
        if c.value is not None:
            _reject(f, c.claim_id, "UNKNOWN_FILLED")

    if c.verification == "NEEDS_REVIEW" and c.claim_id not in f.needs_review:
        _review(f, c.claim_id, "MARKED_NEEDS_REVIEW")


def validate_document(doc: ReportDocument, snapshots: dict[tuple[str, str], SourceSnapshot],
                      formulas: rm.FormulaRegistry = LIVE_FORMULAS) -> Findings:
    """Every declared check, then the honest status. Never raises for a content problem --
    problems become findings -- except a notebook field, which is a hard refusal."""
    forbid_notebook_keys(doc.model_dump(mode="json"))
    f = Findings(status="READY_CHECKED_SCOPE")

    order = [s.section_id for s in doc.sections]
    if tuple(order) != SECTION_IDS:
        f.document_findings.append("SECTION_SET_INVALID")
    if doc.coverage.isin != doc.isin:
        f.document_findings.append("ENTITY_MISMATCH")
    if doc.mode == "POINT_IN_TIME" and doc.cutoff is None:
        f.document_findings.append("CUTOFF_REQUIRED")
    if doc.mode == "CURRENT_WITH_HISTORY" and doc.cutoff is not None:
        f.document_findings.append("CUTOFF_UNEXPECTED")

    claims = {c.claim_id: c for c in doc.claims}
    if len(claims) != len(doc.claims):
        f.document_findings.append("DUPLICATE_CLAIM_ID")
    placed: dict[str, str] = {}
    for s in doc.sections:
        for cid in s.claim_ids:
            if cid not in claims:
                f.document_findings.append(f"UNKNOWN_CLAIM:{cid}")
            elif cid in placed:
                f.document_findings.append(f"CLAIM_IN_TWO_SECTIONS:{cid}")
            placed[cid] = s.section_id
    for cid, c in claims.items():
        if placed.get(cid) != c.section_id:
            f.document_findings.append(f"CLAIM_SECTION_MISMATCH:{cid}")

    sources = {(s.source_id, s.source_version): s for s in doc.sources}
    for c in doc.claims:
        _check_claim(c, doc, sources, snapshots, claims, formulas, f)

    # Coverage matrix: completeness, comparability, conflicts.
    supported = 0
    requested_periods: set[str] = set()
    for cell in doc.coverage.cells:
        requested_periods.add(cell.period.label)
        if cell.basis != doc.coverage.basis:
            f.gap_cells[cell.cell_id] = "MIXED_BASIS_CELL"
            f.document_findings.append(f"MIXED_BASIS_CELL:{cell.cell_id}")
        if cell.status in ("SUPPORTED", "RESTATED"):
            if cell.period.months != 12:
                f.document_findings.append(f"SHORT_PERIOD_NOT_FLAGGED:{cell.cell_id}")
            if not cell.source_refs:
                f.document_findings.append(f"UNSOURCED_CELL:{cell.cell_id}")
            if cell.status == "RESTATED" and not cell.original_source_refs:
                f.document_findings.append(f"RESTATEMENT_ORIGINAL_DROPPED:{cell.cell_id}")
            supported += 1
        else:
            if cell.gap_code is None:
                f.document_findings.append(f"GAP_WITHOUT_CODE:{cell.cell_id}")
            f.gap_cells[cell.cell_id] = cell.status
    by_period: dict[str, list[str]] = {}
    for cell in doc.coverage.cells:
        by_period.setdefault(cell.period.label, []).append(cell.status)
    f.missing_periods = sorted(p for p, st in by_period.items()
                               if all(s in ("MISSING", "RIGHTS_DENIED") for s in st))
    if len(requested_periods) != doc.coverage.requested_years:
        f.document_findings.append("REQUESTED_WINDOW_NOT_REPRESENTED")

    # A value claim on a CONFLICT/NOT_COMPARABLE period must not be presented as settled.
    unsettled = {(cell.period.label, cell.basis) for cell in doc.coverage.cells
                 if cell.status in ("CONFLICT", "NOT_COMPARABLE")}
    for c in doc.claims:
        if c.value is not None and (c.value.period_label, c.value.basis) in unsettled \
                and c.claim_class == "SOURCE_STATEMENT" and c.verification != "NEEDS_REVIEW" \
                and c.claim_class != "UNKNOWN":
            _review(f, c.claim_id, "PERIOD_UNSETTLED")

    # Historical series: one basis only, whatever the request said.
    hist_bases = {c.value.basis for c in doc.claims
                  if c.value is not None and c.section_id == "HISTORICAL_FINANCIALS"}
    if len(hist_bases) > 1:
        f.document_findings.append("MIXED_BASIS_SERIES")

    # Status -- derived, never taken from the document.
    derived = "READY_CHECKED_SCOPE"
    partial = any(s.status != "COVERED" for s in doc.sections) or bool(f.gap_cells) \
        or bool(doc.limitations)
    if partial:
        derived = "READY_WITH_LIMITATIONS"
    if f.rejected or f.needs_review:
        derived = "NEEDS_REVIEW"
    if supported == 0 or any(x.startswith(("SECTION_SET_INVALID", "ENTITY_MISMATCH",
                                           "CUTOFF_REQUIRED", "MIXED_BASIS_SERIES"))
                             for x in f.document_findings):
        derived = "BLOCKED"
    elif f.document_findings and derived != "BLOCKED":
        derived = "NEEDS_REVIEW"
    rank = {"BLOCKED": 0, "NEEDS_REVIEW": 1, "READY_WITH_LIMITATIONS": 2,
            "READY_CHECKED_SCOPE": 3}
    if rank[doc.status] > rank[derived]:
        # A document that labels itself more ready than its checks allow is itself a defect:
        # confident labelling cannot turn a gap green, so it goes to review (or stays blocked).
        f.document_findings.append("STATUS_OVERCLAIM")
        if derived != "BLOCKED":
            derived = "NEEDS_REVIEW"
    f.status = derived
    return f


def check_artifact(manifest: ArtifactManifest, doc: ReportDocument,
                   findings: Findings) -> list[str]:
    """Why this artifact may NOT be published, or [] when it may."""
    problems: list[str] = []
    if findings.status in ("BLOCKED", "NEEDS_REVIEW"):
        problems.append("REPORT_NOT_PUBLISHABLE")
    if manifest.document_sha256 != document_digest(doc):
        problems.append("DOCUMENT_DIGEST_MISMATCH")
    if manifest.report_id != doc.report_id or manifest.report_version != doc.report_version:
        problems.append("VERSION_MISMATCH")
    if sorted(manifest.claim_ids) != sorted(c.claim_id for c in doc.claims
                                            if c.claim_id not in findings.rejected):
        problems.append("CLAIM_SET_MISMATCH")
    export = {s.rights.get("EXPORT") for s in doc.sources}
    if manifest.contains_raw_source_rows and export - {"ALLOWED"}:
        problems.append("EXPORT_DENIED")
    if "DENIED" in export or "UNDECIDED" in export:
        # Derived values and attributed descriptions only; the renderer must omit raw rows.
        if manifest.contains_raw_source_rows:
            problems.append("RAW_ROWS_WITHOUT_EXPORT_RIGHT")
    return sorted(set(problems))


def issuer_only_packet_keys() -> frozenset[str]:
    """The ONLY top-level keys an ISSUER_ONLY report packet may carry. Private notes, theses,
    answers, chat and the notebook are absent by construction; ISSUER_PLUS_PRIVATE_RESEARCH adds
    ``private_research`` only after the ritual gate and an explicit scope choice."""
    return frozenset({"request", "company", "sources", "facts", "coverage", "contract_version"})


def check_packet(packet: dict[str, Any], scope: str) -> None:
    """Refuse a report packet that carries more than its scope allows."""
    forbid_notebook_keys(packet)
    allowed = set(issuer_only_packet_keys())
    if scope == "ISSUER_PLUS_PRIVATE_RESEARCH":
        allowed.add("private_research")
    extra = set(packet) - allowed
    if extra:
        raise ContractViolation("PACKET_SCOPE_EXCEEDED", ",".join(sorted(extra)))


def cutoff_ok(published_on: date | None, cutoff: date | None) -> bool:
    return cutoff is None or (published_on is not None and published_on <= cutoff)
