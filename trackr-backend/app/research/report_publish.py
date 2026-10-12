"""Claim checks, critique and honest publication state (RP 05). Pure: no SQL, no model.

docs/BUTLER_REPORTS.md section 7. The draft (RP 04) is checked by INDEPENDENT passes:

* the closed schema (unknown fields, missing references fail validation);
* ``report_contract.validate_document``: numeric reproduction (cells against pinned extraction
  values, calculated claims re-evaluated by the approved engine), exact excerpt and citation
  identity, temporal/basis consistency, point-in-time hindsight, authorised rights per use,
  section coverage, forbidden language and notebook/profile keys;
* the rights fence: each source's CURRENT verdicts replace the ones recorded at discovery, so a
  right lost since then withholds the claims resting on it;
* an optional separate model CRITIQUE whose flags can only withhold -- its agreement confers
  nothing, and a claim it does not flag is not thereby true;
* review attribution: no human review exists inside a job, so a claim labelled
  HUMAN_REVIEWED_INTERPRETATION is withheld (REVIEW_ATTRIBUTION_UNPROVEN) -- a reviewer is never
  invented.

WITHHOLD, DO NOT GUESS. A claim that fails or needs review is withheld from the published
document, with its reason kept in the attributed ledger; anything resting on it (an
interpretation's premise, a calculation's input) is withheld too. The published status is then
DERIVED by re-running the contract on what remains: READY_CHECKED_SCOPE only when every declared
check passed for the named scope, READY_WITH_LIMITATIONS when a declared source/year/section gap
remains. Neither means comprehensive knowledge or a flawless report. A document with no
supported evidence is BLOCKED and is not published at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.engine import research_metrics as rm
from app.research import report_contract as ct
from app.schemas.reports import ReportDocument

PUBLISH_VERSION = "report-publish-1"

#: Codes a critique may raise. Anything else in a critique is ignored.
CRITIQUE_CODES = frozenset({"UNSUPPORTED_INTERPRETATION", "CAUSAL_OVERREACH",
                            "MISSING_COUNTEREVIDENCE", "TEMPORAL_OR_UNIT_ERROR",
                            "DISGUISED_RECOMMENDATION", "MISLABELLED_ISSUER_CLAIM"})


@dataclass
class Published:
    status: str
    document: dict[str, Any] | None
    ledger: dict[str, Any]
    withheld: dict[str, list[str]] = field(default_factory=dict)
    findings: dict[str, Any] = field(default_factory=dict)


def canonical_text(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshots(draft: dict[str, Any], normal: dict[str, Any], texts: dict[str, str]
              ) -> dict[tuple[str, str], ct.SourceSnapshot]:
    """What each registered source actually says: its retained extracted text, and the
    structured values its facts pinned, keyed by the claim locator ``fact:<id>``."""
    versions = {s["source_id"]: s["source_version"] for s in draft["sources"]}
    values: dict[str, dict[str, dict[str, str]]] = {sid: {} for sid in versions}
    for cell in normal["cells"]:
        for e in cell["entries"]:
            values.setdefault(e["source_id"], {})[f"fact:{e['fact_id']}"] = {
                "value": e["value"], "unit": e["unit"], "scale": e["scale"],
                "period_label": e["label"], "basis": e["basis"]}
            o = e.get("original")
            if o:
                values.setdefault(o["source_id"], {})[f"fact:{o['fact_id']}"] = {
                    "value": o["value"], "unit": e["unit"], "scale": e["scale"],
                    "period_label": e["label"], "basis": e["basis"]}
    return {(sid, ver): ct.SourceSnapshot(texts.get(sid, ""), values.get(sid, {}))
            for sid, ver in versions.items()}


def _cascade(claims: list[dict[str, Any]], withheld: dict[str, list[str]]) -> None:
    changed = True
    while changed:
        changed = False
        for c in claims:
            if c["claim_id"] in withheld:
                continue
            deps = list(c.get("premise_claim_ids") or []) + list(c.get("input_claim_ids") or [])
            if any(d in withheld for d in deps):
                withheld[c["claim_id"]] = ["DEPENDS_ON_WITHHELD_CLAIM"]
                changed = True


def _without(source: dict[str, Any], withheld: dict[str, list[str]], lost: set[str]
             ) -> dict[str, Any]:
    """A copy of the draft with withheld claims removed, sections and cover limited, cells
    whose every source lost its rights downgraded, and status at the lowest label."""
    draft = json.loads(canonical_text(source))
    keep = {c["claim_id"] for c in draft["claims"] if c["claim_id"] not in withheld}
    draft["claims"] = [c for c in draft["claims"] if c["claim_id"] in keep]
    for sec in draft["sections"]:
        dropped = [x for x in sec["claim_ids"] if x not in keep]
        sec["claim_ids"] = [x for x in sec["claim_ids"] if x in keep]
        if dropped:
            sec["limitations"] = (sec["limitations"] + [{"code": "CLAIMS_WITHHELD",
                                                         "applies_to": sec["section_id"]}])[:40]
            if not sec["claim_ids"]:
                sec["status"] = "UNKNOWN"
            elif sec["status"] == "COVERED":
                sec["status"] = "PARTIAL"
    if withheld:
        draft["limitations"] = (draft["limitations"]
                                + [{"code": "CLAIMS_WITHHELD", "applies_to": "COVER"}])[:200]
    cited = {(r["source_id"], r["locator"]) for c in draft["claims"]
             if c["claim_class"] == "SOURCE_STATEMENT" for r in c.get("source_refs", [])}
    for cell in draft["coverage"]["cells"]:
        if cell["status"] not in ("SUPPORTED", "RESTATED"):
            continue
        if cell["source_refs"] and all(r["source_id"] in lost for r in cell["source_refs"]):
            cell["status"], cell["gap_code"] = "RIGHTS_DENIED", "RIGHTS_LOST"
        elif not any((r["source_id"], r["locator"]) in cited for r in cell["source_refs"]):
            # Every claim behind this cell was withheld: the published document does not
            # establish it, so it is not counted as supported coverage.
            cell["status"], cell["gap_code"] = "MISSING", "CLAIMS_WITHHELD"
            cell["original_source_refs"] = []
    draft["status"] = "BLOCKED"  # the lowest label cannot overclaim
    return draft


def publish(draft: dict[str, Any], snaps: dict[tuple[str, str], ct.SourceSnapshot],
            formulas: rm.FormulaRegistry, current_rights: dict[str, dict[str, str]],
            critique: dict[str, list[str]] | None = None,
            critique_provenance: dict[str, Any] | None = None) -> Published:
    """Check, withhold, derive. Never raises for content; a structural refusal is BLOCKED."""
    ledger: dict[str, Any] = {"version": PUBLISH_VERSION, "draft": draft,
                              "critique": critique_provenance or {"ran": False}}
    # Rights fence: current verdicts win over the ones recorded at discovery.
    draft = json.loads(canonical_text(draft))
    lost: set[str] = set()
    for s in draft["sources"]:
        now = current_rights.get(s["source_id"])
        if now is None:
            lost.add(s["source_id"])
            continue
        if now != s["rights"]:
            if "DENIED" in now.values() and "DENIED" not in s["rights"].values():
                lost.add(s["source_id"])
            s["rights"] = now
    try:
        doc = ReportDocument.model_validate(draft)
        findings = ct.validate_document(doc, snaps, formulas)
    except (ValidationError, ct.ContractViolation) as exc:
        code = exc.code if isinstance(exc, ct.ContractViolation) else "SCHEMA_INVALID"
        ledger["refusal"] = code
        return Published("BLOCKED", None, ledger, findings={"refusal": code})

    withheld: dict[str, list[str]] = {}
    for cid, codes in findings.rejected.items():
        withheld.setdefault(cid, []).extend(codes)
    for cid, codes in findings.needs_review.items():
        withheld.setdefault(cid, []).extend(codes)
    for c in draft["claims"]:
        if c["verification"] == "HUMAN_REVIEWED_INTERPRETATION":
            withheld.setdefault(c["claim_id"], []).append("REVIEW_ATTRIBUTION_UNPROVEN")
        if any(r["source_id"] in lost for r in c.get("source_refs", [])):
            withheld.setdefault(c["claim_id"], []).append("RIGHTS_LOST")
    for cid, codes in (critique or {}).items():
        kept = [x for x in codes if x in CRITIQUE_CODES]
        if kept:
            withheld.setdefault(cid, []).extend(f"CRITIQUE_{x}" for x in kept)
    original = draft
    for _pass in range(4):
        _cascade(original["claims"], withheld)
        candidate = _without(original, withheld, lost)
        try:
            derived = ct.validate_document(ReportDocument.model_validate(candidate), snaps,
                                           formulas)
        except (ValidationError, ct.ContractViolation):
            ledger["refusal"] = "PUBLISHED_DOCUMENT_INVALID"
            return Published("BLOCKED", None, ledger, withheld)
        more = {**derived.rejected, **derived.needs_review}
        if not more:
            break
        for cid, codes in more.items():
            withheld.setdefault(cid, []).extend(codes)
    status = derived.status
    ledger.update(withheld=withheld, findings=findings.as_dict(),
                  published_findings=derived.as_dict())
    if status not in ("READY_WITH_LIMITATIONS", "READY_CHECKED_SCOPE"):
        # Nothing publishable: no body, just the reasons (a truthful blocked explanation).
        return Published(status, None, ledger, withheld, derived.as_dict())
    candidate["status"] = status
    final = ct.validate_document(ReportDocument.model_validate(candidate), snaps, formulas)
    if final.status != status or final.document_findings:
        ledger["refusal"] = "STATUS_NOT_STABLE"
        return Published("NEEDS_REVIEW", None, ledger, withheld, final.as_dict())
    draft = candidate
    return Published(status, draft, ledger, withheld, final.as_dict())
