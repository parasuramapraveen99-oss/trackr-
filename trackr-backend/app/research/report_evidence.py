"""Report evidence: permitted issuer sources and historical coverage (RP 02), then exact
comparable history and calculations (RP 03). docs/BUTLER_REPORTS.md sections 5 and 7.

READS ONLY ISSUER EVIDENCE. ``research_source``, its extraction pin, ``research_fact`` and
``research_event`` for the company's VERIFIED alias family. Never a note, thesis, answer, chat
message or anything from the investor notebook (``make lint`` refuses the import); an
ISSUER_ONLY report is therefore identical for every reader of the same company, scope and as-of.

NO WHOLE-WEB CRAWL, NO ASSUMED PERMISSION. The job reads evidence already held under the
research pipeline's per-source verdicts. Live retrieval inside a report job needs an approved
adapter (BE 29); none exists (BE-Q04), which is recorded as the limitation
``LIVE_RETRIEVAL_NOT_APPROVED`` rather than hidden. Every source carries its per-use rights.

TEN YEARS IS A REQUESTED MAXIMUM. Coverage is checked per requested fiscal year x statement x
basis cell; a year with no supported evidence is a MISSING cell with its reason, never a zero,
never interpolated, never borrowed from another basis. Standalone and consolidated are separate
series. A short or shifted fiscal year is NOT_COMPARABLE. A later revision of the same origin
item is a RESTATEMENT that keeps the original; two independent sources that disagree are a
CONFLICT that keeps both. Same bytes from two places are one piece of evidence, not two.

Decimal strings end to end. Calculated values come only from the approved registry (BE 27).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.db import workspace as ws
from app.engine import research_metrics as rm
from app.engine import research_rules as rr
from app.research.source_policy import (
    USE_EXTRACT,
    USE_METADATA,
    USE_MODEL_DISCLOSE,
    USE_STORE,
)

DISCOVERY_VERSION = "report-discovery-1"
NORMALIZATION_VERSION = "report-normalization-1"

#: research_source.source_type -> RegisteredSource.origin / attribution. An issuer's own claim
#: is never labelled independent; results are UNAUDITED unless a person records otherwise.
_ORIGIN = {"ANNUAL_REPORT": "ANNUAL_REPORT", "RESULTS": "RESULTS",
           "EXCHANGE_FILING": "EXCHANGE_FILING", "NEWS": "NEWS",
           "USER_DOCUMENT": "USER_DOCUMENT"}
_ATTRIBUTION = {"ANNUAL_REPORT": "AUDITED", "RESULTS": "UNAUDITED", "NEWS": "NEWS_REPORT",
                "BOOK": "NEWS_REPORT"}

#: Registry statement -> report statement.
_STATEMENT = {"PROFIT_AND_LOSS": "INCOME_STATEMENT", "BALANCE_SHEET": "BALANCE_SHEET",
              "CASH_FLOW": "CASH_FLOW", "SEGMENTS": "SEGMENTS", "SHAREHOLDING": "SHAREHOLDING"}

#: Unit suffix -> report scale. Anything else is quarantined, never guessed.
_SCALES = {"": "UNITS", "THOUSANDS": "THOUSANDS", "LAKHS": "LAKHS", "MILLIONS": "MILLIONS",
           "CRORES": "CRORES", "BILLIONS": "BILLIONS"}
_PLAIN_UNITS = {"PERCENT", "RATIO", "SHARES", "COUNT", "DAYS"}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _verdict(policy: Any, source_type: str, use: str) -> str:
    return policy.verdict(source_type, use)


# =========================================================================================
# RP 02 -- discovery: the source register and candidate facts
# =========================================================================================


def discover(conn: Any, investor_id: str, isin: str, request: dict[str, Any], policy: Any,
             export_verdict: Any, adapters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every held source and fact for the company family, with rights per use, revision
    chains and same-bytes groups; point-in-time filtering applied. Pure read."""
    family = ws.isin_family(conn, isin)
    cutoff = date.fromisoformat(request["cutoff"]) if request.get("cutoff") else None
    rows = ws._rows(conn, """
        SELECT s.id, s.isin, s.source, s.source_type, s.source_url, s.publication_date,
               s.period, s.retrieved_at, s.document_reference, s.content_sha256,
               s.adapter_id, s.origin_item_id, s.revises_source_id,
               e.status AS extraction_status, e.text_sha256, e.text_document_id,
               e.extractor_id, e.extractor_version
          FROM research_source s
          LEFT JOIN research_source_extraction e
            ON e.investor_id = s.investor_id AND e.source_id = s.id
         WHERE s.investor_id = %s AND s.isin = ANY(%s::text[])
         ORDER BY s.publication_date, s.recorded_at, s.id
        """, (investor_id, family))
    excluded: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    by_bytes: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if cutoff is not None and r["publication_date"] > cutoff:
            # No hindsight: published after the as-of, so it did not exist then.
            excluded.append({"source_id": r["id"], "code": "PUBLISHED_AFTER_CUTOFF"})
            continue
        rights = {
            "FETCH": _verdict(policy, r["source_type"], USE_METADATA),
            "STORE": _verdict(policy, r["source_type"], USE_STORE),
            "EXTRACT": _verdict(policy, r["source_type"], USE_EXTRACT),
            "MODEL": _verdict(policy, r["source_type"], USE_MODEL_DISCLOSE),
            "EXPORT": export_verdict(r["source_type"]),
        }
        if rights["EXPORT"] not in ("ALLOWED", "DENIED", "UNDECIDED", "METADATA_ONLY"):
            rights["EXPORT"] = "UNDECIDED"
        fy = r["period"] if isinstance(r["period"], str) and len(r["period"]) == 6 \
            and r["period"].startswith("FY") and r["period"][2:].isdigit() else None
        entry = {
            "source_id": r["id"],
            "source_version": (r["content_sha256"] or "metadata")[:16],
            "title": r["source"][:300],
            "origin": _ORIGIN.get(r["source_type"], "ISSUER_FILING"),
            "attribution": _ATTRIBUTION.get(r["source_type"], "ISSUER_CLAIM"),
            "published_on": r["publication_date"].isoformat(),
            "retrieved_at": r["retrieved_at"].isoformat(),
            "content_sha256": r["content_sha256"] or "0" * 64,
            "fiscal_period": fy,
            "basis": None,
            "rights": rights,
            "link": r["source_url"] if r["source_url"].startswith("https://") else None,
            # Internal provenance (not part of RegisteredSource; kept for later stages).
            "_source_type": r["source_type"],
            "_locator": r["document_reference"],
            "_text_document_id": r["text_document_id"],
            "_text_sha256": r["text_sha256"],
            "_extractor": f"{r['extractor_id']}@{r['extractor_version']}"
            if r["extractor_id"] else None,
            "_revises": r["revises_source_id"],
            "_origin_item": f"{r['adapter_id']}:{r['origin_item_id']}"
            if r["adapter_id"] else None,
            "_metadata_only": r["content_sha256"] is None,
        }
        if r["content_sha256"]:
            by_bytes[r["content_sha256"]].append(r["id"])
        sources.append(entry)
    kept = {s["source_id"] for s in sources}
    # A revision published after the cutoff was excluded above; the original stays current.
    for s in sources:
        if s["_revises"] and s["_revises"] not in kept:
            s["_revises"] = None
    same_bytes = [ids for ids in by_bytes.values() if len(ids) > 1]

    facts = ws._rows(conn, """
        SELECT f.id, f.isin, f.source_id, f.metric, f.basis, f.unit, f.period_start,
               f.period_end, f.cadence, f.numeric_value, f.boolean_value, f.text_value,
               f.verification, f.supersedes_id, f.recorded_at, f.registry_version,
               s.publication_date, s.retrieved_at
          FROM research_fact f
          JOIN research_source s ON s.investor_id = f.investor_id AND s.id = f.source_id
         WHERE f.investor_id = %s AND f.isin = ANY(%s::text[]) AND f.source_id = ANY(%s::uuid[])
         ORDER BY f.seq
        """, (investor_id, family, sorted(kept) or ["00000000-0000-0000-0000-000000000000"]))
    fact_rows = [{**{k: (format(v, "f") if isinstance(v, Decimal) else
                         v.isoformat() if isinstance(v, date) else v)
                     for k, v in f.items()}} for f in facts]
    events: list[dict[str, Any]] = []
    if ws._one(conn, "SELECT to_regclass('research_event') IS NOT NULL AS ok", None)["ok"]:
        for e in ws._rows(conn, """
                SELECT e.id, e.source_id, e.event_type, e.status, e.event_date,
                       e.date_precision, e.description, e.locator, e.verification
                  FROM research_event e
                 WHERE e.investor_id = %s AND e.isin = ANY(%s::text[])
                   AND e.verification = 'VERIFIED' ORDER BY e.seq""",
                (investor_id, family)):
            if e["source_id"] in kept:
                events.append({**e, "event_date": e["event_date"].isoformat()
                               if e["event_date"] else None})
    limitations = []
    if not adapters:
        limitations.append({"code": "LIVE_RETRIEVAL_NOT_APPROVED", "applies_to": "COVER"})
    if not sources:
        limitations.append({"code": "NO_HELD_ISSUER_SOURCES", "applies_to": "COVER"})
    return {
        "version": DISCOVERY_VERSION,
        "isin": isin,
        "family": sorted(family),
        "cutoff": request.get("cutoff"),
        "mode": request["mode"],
        "sources": sources,
        "excluded_sources": excluded,
        "same_bytes_groups": same_bytes,
        "facts": fact_rows,
        "events": events,
        "limitations": limitations,
        # Retrieval checkpoint: what was considered, so a resumed stage re-reads the same
        # evidence under the same cutoff instead of whatever arrived since.
        "cursor": {"source_ids": sorted(kept), "fact_ids": sorted(f["id"] for f in fact_rows)},
    }


# =========================================================================================
# RP 03 -- normalization: comparable cells, exact values, restatements, calculations
# =========================================================================================


def split_unit(unit: str) -> tuple[str, str] | None:
    """('INR', 'CRORES') from 'INR_CRORES'; ('PERCENT','UNITS'); None when not understood."""
    if unit in _PLAIN_UNITS:
        return unit, "UNITS"
    head, _, tail = unit.partition("_")
    if len(head) == 3 and head.isalpha() and head.isupper() and tail in _SCALES:
        return head, _SCALES[tail]
    return None


def fiscal_label(start: date, end: date) -> tuple[str, int, bool]:
    """(label, months, comparable). Indian fiscal years run 1 April - 31 March; anything else
    is labelled by its end year and flagged not comparable (short/changed year)."""
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    standard = (end.month, end.day) == (3, 31) and (start.month, start.day) == (4, 1) \
        and start.year == end.year - 1
    return f"FY{end.year}", months if 1 <= months <= 24 else 24, standard


def _requested_years(request: dict[str, Any], latest_fy: int) -> list[str]:
    years = int(request["window_years"])
    return [f"FY{y}" for y in range(latest_fy - years + 1, latest_fy + 1)]


def normalise(discovery: dict[str, Any], request: dict[str, Any], latest_fy: int,
              registry: rr.MetricRegistry, formulas: rm.FormulaRegistry) -> dict[str, Any]:
    """Cells, rows, calculations and chart series. Pure: no database, no model."""
    sources = {s["source_id"]: s for s in discovery["sources"]}
    facts = discovery["facts"]
    successor = {f["supersedes_id"]: f["id"] for f in facts if f["supersedes_id"]}
    heads = [f for f in facts if f["id"] not in successor]
    by_id = {f["id"]: f for f in facts}
    basis = request["basis"]
    requested = _requested_years(request, latest_fy)
    statements = list(request["statements"])

    rows: dict[str, dict[str, Any]] = {}       # metric -> {fy -> entry}
    quarantined: list[dict[str, Any]] = []
    cell_facts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for f in heads:
        if f["cadence"] != "YEAR" or f["numeric_value"] is None:
            continue
        definition = registry.lookup(f["metric"], f["basis"], f["unit"])
        statement = _STATEMENT.get(getattr(definition, "statement", None) or "")
        if statement is None:
            quarantined.append({"fact_id": f["id"], "code": "METRIC_NOT_MAPPED_TO_STATEMENT"})
            continue
        unit = split_unit(f["unit"])
        if unit is None:
            quarantined.append({"fact_id": f["id"], "code": "UNIT_NOT_UNDERSTOOD"})
            continue
        start, end = date.fromisoformat(f["period_start"]), date.fromisoformat(f["period_end"])
        label, months, comparable = fiscal_label(start, end)
        if f["basis"] != basis:
            quarantined.append({"fact_id": f["id"], "code": "OTHER_BASIS_NOT_MIXED"})
            continue
        entry = {"fact_id": f["id"], "metric": f["metric"], "statement": statement,
                 "label": label, "months": months, "comparable": comparable,
                 "period_start": f["period_start"], "period_end": f["period_end"],
                 "value": f["numeric_value"], "unit": unit[0], "scale": unit[1],
                 "basis": f["basis"], "verification": f["verification"],
                 "source_id": f["source_id"], "locator": None}
        # Restatement: somewhere back along this head's supersession chain (past any
        # verification copies within its own source) is a fact from the source it revises.
        prior = by_id.get(f["supersedes_id"]) if f["supersedes_id"] else None
        hops = 0
        while prior is not None and prior["source_id"] == f["source_id"] and hops < 50:
            prior = by_id.get(prior["supersedes_id"]) if prior["supersedes_id"] else None
            hops += 1
        if prior is not None and prior["source_id"] != f["source_id"]:
            entry["original"] = {"fact_id": prior["id"], "source_id": prior["source_id"],
                                 "value": prior["numeric_value"]}
        cell_facts[(label, statement)].append(entry)

    cells: list[dict[str, Any]] = []
    for label in requested:
        for st in statements:
            entries = cell_facts.get((label, st), [])
            cell_id = f"{label}:{st}"
            cell = {"cell_id": cell_id, "label": label, "statement": st, "basis": basis,
                    "entries": entries}
            year = int(label[2:])
            period = {"label": label, "start": date(year - 1, 4, 1).isoformat(),
                      "end": date(year, 3, 31).isoformat(), "months": 12}
            if not entries:
                cell.update(status="MISSING", gap_code=_gap_for(discovery, facts, label, st,
                                                                 basis, registry))
            elif any(not e["comparable"] for e in entries):
                e0 = next(e for e in entries if not e["comparable"])
                period = {"label": label, "start": e0["period_start"], "end": e0["period_end"],
                          "months": e0["months"]}
                cell.update(status="NOT_COMPARABLE", gap_code="SHORT_OR_CHANGED_FISCAL_YEAR")
            elif any(e["verification"] != "VERIFIED" for e in entries) and not any(
                    e["verification"] == "VERIFIED" for e in entries):
                cell.update(status="MISSING", gap_code="UNVERIFIED_EXTRACTION_ONLY")
            else:
                verified = [e for e in entries if e["verification"] == "VERIFIED"]
                conflicting = _conflicts(verified)
                if conflicting:
                    cell.update(status="CONFLICT", gap_code="SOURCE_CONFLICT",
                                conflict=conflicting)
                elif any("original" in e for e in verified):
                    cell.update(status="RESTATED", gap_code=None)
                else:
                    cell.update(status="SUPPORTED", gap_code=None)
                cell["entries"] = verified
            if cell["status"] in ("SUPPORTED", "RESTATED", "CONFLICT"):
                rights_denied = [e for e in cell["entries"]
                                 if "DENIED" in (sources[e["source_id"]]["rights"]["EXTRACT"],
                                                 sources[e["source_id"]]["rights"]["FETCH"])]
                if rights_denied and len(rights_denied) == len(cell["entries"]):
                    cell.update(status="RIGHTS_DENIED", gap_code="SOURCE_RIGHTS_DENIED")
            cell["period"] = period
            cells.append(cell)
            for e in cell["entries"]:
                rows.setdefault(e["metric"], {})[label] = {**e, "cell_status": cell["status"]}

    calculations = _calculate(cells, facts, discovery, formulas)
    series = []
    for metric, by_fy in sorted(rows.items()):
        points, gaps = [], []
        for label in requested:
            e = by_fy.get(label)
            if e is None or e["cell_status"] not in ("SUPPORTED", "RESTATED"):
                gaps.append(label)
                points.append({"label": label, "value": None, "state": e["cell_status"]
                               if e else "MISSING"})
            else:
                points.append({"label": label, "value": e["value"], "unit": e["unit"],
                               "scale": e["scale"], "state": e["cell_status"],
                               "fact_id": e["fact_id"]})
        series.append({"metric": metric, "points": points, "discontinuities": gaps})
    return {
        "version": NORMALIZATION_VERSION,
        "requested_years": requested,
        "basis": basis,
        "cells": cells,
        "quarantined": quarantined,
        "calculations": calculations,
        "series": series,
        "supported_years": sorted({c["label"] for c in cells
                                   if c["status"] in ("SUPPORTED", "RESTATED")}),
    }


def _conflicts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_metric[e["metric"]].append(e)
    out = []
    for metric, es in by_metric.items():
        values = {Decimal(e["value"]) * scale_factor(e["scale"]) for e in es}
        if len(values) > 1:
            out.append({"metric": metric, "fact_ids": sorted(e["fact_id"] for e in es),
                        "source_ids": sorted({e["source_id"] for e in es})})
    return out


def scale_factor(scale: str) -> Decimal:
    return {"UNITS": Decimal(1), "THOUSANDS": Decimal(10) ** 3, "LAKHS": Decimal(10) ** 5,
            "MILLIONS": Decimal(10) ** 6, "CRORES": Decimal(10) ** 7,
            "BILLIONS": Decimal(10) ** 9}[scale]


def _gap_for(discovery: dict[str, Any], facts: list[dict[str, Any]], label: str,
             statement: str, basis: str, registry: rr.MetricRegistry) -> str:
    """Why a requested cell has nothing: the most specific honest reason."""
    if not discovery["sources"]:
        return "NO_PERMITTED_SOURCE_HELD"
    if statement == "AUDITOR_REPORT":
        return "AUDITOR_REPORT_NOT_EXTRACTED"
    year = int(label[2:])
    in_year = [f for f in facts if f["period_end"][:4] == str(year)]
    if not in_year:
        return "SOURCE_NOT_FOUND"
    if all(f["basis"] != basis for f in in_year):
        return "BASIS_NOT_AVAILABLE"
    if all(registry.lookup(f["metric"], f["basis"], f["unit"]) is None for f in in_year):
        return "METRIC_NOT_APPROVED"
    return "STATEMENT_NOT_FOUND"


def _calculate(cells: list[dict[str, Any]], facts: list[dict[str, Any]],
               discovery: dict[str, Any], formulas: rm.FormulaRegistry) -> list[dict[str, Any]]:
    """Every approved METRIC formula, per requested year, over the SUPPORTED/RESTATED cell
    facts only. Each result keeps its full engine trace; a formula that cannot compute is
    listed with its reason (never dropped, never approximated)."""
    if not formulas.formulas:
        return [{"code": "FORMULA_NOT_APPROVED", "gate": "BE-Q03"}]
    usable: dict[str, dict[str, Any]] = {}
    for c in cells:
        if c["status"] in ("SUPPORTED", "RESTATED"):
            for e in c["entries"]:
                usable[e["fact_id"]] = e
    pub = {s["source_id"]: s for s in discovery["sources"]}
    candidates = []
    for f in facts:
        if f["id"] not in usable:
            continue
        s = pub[f["source_id"]]
        candidates.append(rr.FactCandidate(
            id=f["id"], isin=f["isin"], metric=f["metric"], basis=f["basis"], unit=f["unit"],
            cadence=f["cadence"], period_start=date.fromisoformat(f["period_start"]),
            period_end=date.fromisoformat(f["period_end"]),
            numeric_value=Decimal(f["numeric_value"]), boolean_value=None, text_value=None,
            verification=f["verification"], source_id=f["source_id"],
            publication_date=date.fromisoformat(s["published_on"]),
            retrieved_at=_dt(s["retrieved_at"]), recorded_at=_dt(f["recorded_at"]),
            supersedes_id=f["supersedes_id"]))
    by_end: dict[str, list[rr.FactCandidate]] = defaultdict(list)
    for c in candidates:
        by_end[c.period_end.isoformat()].append(c)
    out = []
    for d in formulas.formulas:
        if d.kind != "METRIC":
            continue
        for end, current in sorted(by_end.items()):
            prior_end = (date.fromisoformat(end) - timedelta(days=365))
            # Prior-year facts are offered only to a definition that declares a PRIOR input;
            # otherwise they would (correctly) make the engine report a period mismatch.
            wants_prior = any(i.period_role == "PRIOR" for i in d.inputs)
            prior = [c for c in candidates if wants_prior
                     and abs((c.period_end - prior_end).days) <= 1]
            pool = [c for c in current + prior if c.metric in {i.metric for i in d.inputs}]
            if not pool:
                continue
            res = rm.calculate(formulas, d.formula_id, d.version, pool,
                               family=discovery["family"])
            out.append({"formula_id": d.formula_id, "version": d.version,
                        "period_end": end, "trace": res.as_dict()})
    return out


def _dt(value: Any) -> Any:
    from datetime import datetime

    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def digest(obj: Any) -> str:
    return _digest(obj)
