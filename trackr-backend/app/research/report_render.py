"""Private HTML and PDF artifacts rendered from ONE validated report document (RP 06).

docs/BUTLER_REPORTS.md section 8 and architecture invariant 17.

ONE MODEL, TWO FORMATS. Both artifacts are built from the same immutable published document
(``report_document.document_json``) through the same ``_export_view``; the manifest pins the
document digest and the exact claim ids, and ``report_contract.check_artifact`` must return no
problem before an artifact is stored. A draft is never relabelled: a new status is a new
document and new artifacts.

A PRIVATE DOWNLOAD IS STILL AN EXPORT. A claim resting on a source whose EXPORT verdict is not
ALLOWED is rendered as a withheld line naming the source, date and locator -- the attributed
description stays, the filed value and the quoted excerpt do not. No raw source rows, notices or
price series are ever included (``contains_raw_source_rows`` is always false). While BE-Q04 is
open every export verdict is UNDECIDED, so every source-derived value is withheld from artifacts.

SAFE BY CONSTRUCTION. HTML is built from escaped strings with an inline stylesheet and a
restrictive CSP meta tag: no script, no external URL fetch, no tracking resource. Source links
are plain text (never auto-loaded) and only permitted https links. The PDF comes from typed
blocks (``report_pdf``), never from HTML. The company PDF never contains the investor notebook
or any personal material -- the document type cannot carry it.
"""

from __future__ import annotations

import hashlib
import html
from datetime import UTC, datetime
from typing import Any

from app.research import report_contract as ct
from app.research import report_pdf as pdf
from app.research.report_claims import indian_grouping
from app.schemas.reports import ArtifactManifest, ReportDocument

RENDERER_HTML = "trackr-report-html-1"
RENDERER_PDF = "trackr-report-pdf-1"

_SECTION_TITLES = {
    "COVER_AND_SCOPE": "1. Cover and scope",
    "BUSINESS": "2. Business",
    "HISTORICAL_FINANCIALS": "3. Historical financials",
    "CASH_FUNDING_OBLIGATIONS": "4. Cash, funding and obligations",
    "GOVERNANCE_ACCOUNTING": "5. Governance and accounting",
    "WHAT_CHANGED": "6. What changed",
    "EVIDENCE_FOR_AND_AGAINST": "7. Evidence for and against key business explanations",
    "RISKS_AND_OPEN_QUESTIONS": "8. Risks, uncertainties and open questions",
    "METHODS_AND_SOURCES": "9. Methods and source register",
}
_LABEL = {
    "SOURCE_STATEMENT": "What the source reports",
    "CALCULATED_VALUE": "Calculated by approved formula",
    "INTERPRETATION": "AI interpretation (not a verified fact)",
    "HYPOTHETICAL_RISK": "Conditional risk scenario (not an event)",
    "UNKNOWN": "Not established",
}
_STATUS_TEXT = {
    "READY_CHECKED_SCOPE": "Declared checks passed for the stated scope. This is not a claim "
                           "that every material fact was found.",
    "READY_WITH_LIMITATIONS": "Published with stated limitations: some requested years, "
                              "sections or sources are missing or withheld.",
}


def _export_view(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Claims as they may appear in an EXPORT: value/excerpt withheld where the source's export
    verdict is not ALLOWED. Returns (claims, withheld claim ids)."""
    export = {s["source_id"]: s["rights"].get("EXPORT") for s in doc["sources"]}
    out, masked = [], []
    for c in doc["claims"]:
        refs = c.get("source_refs") or []
        if refs and any(export.get(r["source_id"]) != "ALLOWED" for r in refs):
            masked.append(c["claim_id"])
            src = next((s for s in doc["sources"] if s["source_id"] == refs[0]["source_id"]), None)
            out.append({**c, "text": (f"Withheld from this download: the export of "
                                      f"{src['title'] if src else 'this source'} "
                                      f"(published {src['published_on'] if src else 'unknown'}, "
                                      f"{refs[0]['locator']}) is not approved (BE-Q04)."),
                        "excerpt": None, "value": None, "_masked": True})
        elif c["claim_class"] == "CALCULATED_VALUE" and any(
                i in masked for i in c.get("input_claim_ids", [])):
            masked.append(c["claim_id"])
            out.append({**c, "text": "Withheld from this download: its inputs are withheld.",
                        "value": None, "_masked": True})
        else:
            out.append(c)
    return out, masked


def _value_text(v: dict[str, Any] | None) -> str:
    if not v:
        return ""
    scale = "" if v["scale"] == "UNITS" else f" {v['scale'].lower()}"
    return f"{indian_grouping(v['value'])} {v['unit']}{scale} ({v['period_label']}, " \
           f"{v['basis'].lower()})"


def _cover_lines(doc: dict[str, Any]) -> list[str]:
    cov = doc["coverage"]
    supported = sorted({c["period"]["label"] for c in cov["cells"]
                        if c["status"] in ("SUPPORTED", "RESTATED")})
    missing = sorted({c["period"]["label"] for c in cov["cells"]} - set(supported))
    return [
        f"Company ISIN: {doc['isin']}",
        f"Question: {doc['question']}",
        f"Report {doc['report_id']} version {doc['report_version']} · mode "
        f"{doc['mode'].replace('_', ' ').lower()}"
        + (f" · cutoff {doc['cutoff']}" if doc.get("cutoff") else "")
        + f" · as of {doc['as_of'][:19]} UTC",
        f"Basis: {cov['basis'].lower()} · requested years: {cov['requested_years']} · "
        f"years with supported evidence: {', '.join(supported) or 'none'}",
        f"Years missing or not comparable: {', '.join(missing) or 'none'}",
        f"Status: {doc['status']} — {_STATUS_TEXT.get(doc['status'], '')}",
        "Limitations: " + (", ".join(sorted({x['code'] for x in doc['limitations']})) or "none"),
    ]


def _coverage_rows(doc: dict[str, Any]) -> list[list[str]]:
    return [[c["period"]["label"], c["statement"].replace("_", " ").lower(), c["basis"].lower(),
             c["status"], c["gap_code"] or "",
             ", ".join(dict.fromkeys(r["source_id"][:8] for r in c["source_refs"]))]
            for c in doc["coverage"]["cells"]]


def _source_rows(doc: dict[str, Any]) -> list[list[str]]:
    return [[s["source_id"][:8], s["title"], s["attribution"].lower(),
             s["published_on"] or "unknown", s["retrieved_at"][:10],
             " ".join(f"{k.lower()}:{v.lower()}" for k, v in sorted(s["rights"].items())),
             s["link"] if s.get("link") and s["rights"].get("EXPORT") == "ALLOWED" else ""]
            for s in doc["sources"]]


# =========================================================================================
# HTML
# =========================================================================================

_CSS = """
body{font:15px/1.5 Georgia,serif;color:#1d1d1b;background:#fff;max-width:880px;margin:2rem auto;
padding:0 1rem}h1{font-size:1.6rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid
#ccc}.claim{margin:.6rem 0}.lab{font:600 11px/1.4 system-ui,sans-serif;text-transform:uppercase;
letter-spacing:.04em;color:#555}.masked{color:#6b6b6b;font-style:italic}.val{font-family:ui-
monospace,monospace}table{border-collapse:collapse;width:100%;font:12px/1.4 system-ui,sans-serif}
th,td{border-bottom:1px solid #e3e3e3;padding:4px 6px;text-align:left;vertical-align:top}
thead th{position:sticky;top:0;background:#f6f6f4}blockquote{margin:.3rem 0 .3rem 1rem;color:#333}
.notice{border:1px solid #c9b37e;background:#fbf7ec;padding:.6rem .8rem}
"""


def render_html(doc: dict[str, Any]) -> tuple[bytes, list[str]]:
    claims, masked = _export_view(doc)
    by_id = {c["claim_id"]: c for c in claims}
    e = html.escape
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
             "style-src 'unsafe-inline'\">",
             f"<title>{e(doc['isin'])} research report v{doc['report_version']}</title>",
             f"<style>{_CSS}</style></head><body>",
             f"<h1>Company research report · {e(doc['isin'])}</h1>",
             f"<p class=\"notice\">{e(doc['decision_notice'])}</p><ul>"]
    parts += [f"<li>{e(line)}</li>" for line in _cover_lines(doc)]
    parts.append("</ul>")
    for sec in doc["sections"]:
        parts.append(f"<h2 id=\"{e(sec['section_id'])}\">{e(_SECTION_TITLES[sec['section_id']])}"
                     f" <span class=\"lab\">{e(sec['status'].lower())}</span></h2>")
        for lim in sec["limitations"]:
            parts.append(f"<p class=\"lab\">Limitation: {e(lim['code'])}</p>")
        for cid in sec["claim_ids"]:
            c = by_id[cid]
            cls = "claim masked" if c.get("_masked") else "claim"
            parts.append(f"<div class=\"{cls}\" id=\"{e(cid)}\"><div class=\"lab\">"
                         f"{e(_LABEL[c['claim_class']])} · {e(cid)}</div><div>{e(c['text'])}")
            if c.get("value") and not c.get("_masked"):
                parts.append(f" <span class=\"val\">[{e(_value_text(c['value']))}]</span>")
            parts.append("</div>")
            if c.get("excerpt") and not c.get("_masked"):
                parts.append(f"<blockquote>“{e(c['excerpt'])}”</blockquote>")
            refs = c.get("source_refs") or []
            prem = c.get("premise_claim_ids") or c.get("input_claim_ids") or []
            if refs or prem:
                parts.append("<div class=\"lab\">"
                             + e(" · ".join([f"source {r['source_id'][:8]} {r['locator']}"
                                             for r in refs] + [f"rests on {p}" for p in prem]))
                             + "</div>")
            parts.append("</div>")
        if sec["section_id"] == "METHODS_AND_SOURCES":
            parts.append(_html_table(["Year", "Statement", "Basis", "Status", "Gap", "Sources"],
                                     _coverage_rows(doc)))
            parts.append(_html_table(["Source", "Title", "Attribution", "Published", "Retrieved",
                                      "Rights", "Link (text only)"], _source_rows(doc)))
    parts.append(f"<p class=\"lab\">Withheld from this download: {len(masked)} claim(s). "
                 "A file already downloaded cannot be recalled.</p></body></html>")
    return "".join(parts).encode("utf-8"), masked


def _html_table(columns: list[str], rows: list[list[str]]) -> str:
    e = html.escape
    head = "".join(f"<th>{e(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{e(v)}</td>" for v in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# =========================================================================================
# PDF
# =========================================================================================


def render_pdf(doc: dict[str, Any]) -> tuple[bytes, int, int, list[str]]:
    claims, masked = _export_view(doc)
    by_id = {c["claim_id"]: c for c in claims}
    B = pdf.Block
    blocks = [B("TITLE", f"Company research report — {doc['isin']}"),
              B("PARA", doc["decision_notice"])]
    blocks += [B("SMALL", line) for line in _cover_lines(doc)]
    blocks.append(B("RULE"))
    for sec in doc["sections"]:
        blocks.append(B("HEADING", f"{_SECTION_TITLES[sec['section_id']]}  "
                                   f"[{sec['status'].lower()}]"))
        for lim in sec["limitations"]:
            blocks.append(B("SMALL", f"Limitation: {lim['code']}"))
        for cid in sec["claim_ids"]:
            c = by_id[cid]
            blocks.append(B("SMALL", f"{_LABEL[c['claim_class']].upper()} · {cid}"))
            text = c["text"]
            if c.get("value") and not c.get("_masked"):
                text += f" [{_value_text(c['value'])}]"
            blocks.append(B("PARA", text))
            if c.get("excerpt") and not c.get("_masked"):
                blocks.append(B("MONO", f"“{c['excerpt']}”"))
            refs = c.get("source_refs") or []
            prem = c.get("premise_claim_ids") or c.get("input_claim_ids") or []
            if refs or prem:
                blocks.append(B("SMALL", " · ".join(
                    [f"source {r['source_id'][:8]} {r['locator']}" for r in refs]
                    + [f"rests on {p}" for p in prem])))
        if sec["section_id"] == "METHODS_AND_SOURCES":
            blocks.append(B("SMALL", "Coverage: fiscal year x statement x basis. Source page "
                                     "locators refer to the source, not to this PDF."))
            blocks.append(B("TABLE", columns=["Year", "Statement", "Basis", "Status", "Gap",
                                              "Sources"],
                            rows=_coverage_rows(doc), widths=[.1, .2, .14, .16, .24, .16]))
            blocks.append(B("TABLE", columns=["Source", "Title", "Attribution", "Published",
                                              "Retrieved", "Rights", "Link"],
                            rows=_source_rows(doc),
                            widths=[.09, .22, .11, .11, .11, .2, .16]))
    blocks.append(B("SMALL", f"Withheld from this download: {len(masked)} claim(s). A file "
                             "already downloaded cannot be recalled."))
    header = (f"{doc['isin']} · report v{doc['report_version']} · {doc['status']} · "
              f"as of {doc['as_of'][:10]}")
    data, pages, replaced = pdf.render_pdf(blocks, header,
                                           "Research information only. The investor decides.")
    return data, pages, replaced, masked


# =========================================================================================
# manifests
# =========================================================================================


def manifest(doc: dict[str, Any], fmt: str, data: bytes, pages: int | None,
             artifact_id: str, findings: ct.Findings) -> tuple[dict[str, Any], list[str]]:
    """The artifact manifest and the contract's publication problems (must be empty)."""
    model = ReportDocument.model_validate(doc)
    m = ArtifactManifest.model_validate({
        "schema_version": "report.artifact.v1",
        "artifact_id": artifact_id,
        "report_id": doc["report_id"],
        "report_version": doc["report_version"],
        "format": fmt,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
        "page_count": pages,
        "document_sha256": ct.document_digest(model),
        "claim_ids": [c["claim_id"] for c in doc["claims"]],
        "check_receipts": list(doc["check_receipts"]) or ["unchecked"],
        "contains_raw_source_rows": False,
        "renderer": RENDERER_PDF if fmt == "PDF" else RENDERER_HTML,
        "created_at": datetime.now(UTC).isoformat(),
    })
    return m.model_dump(mode="json"), ct.check_artifact(m, model, findings)
