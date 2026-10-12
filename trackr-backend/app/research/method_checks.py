"""Method-specific preconditions, code-computed blocks and proposal checks (BE 30-35). Pure.

Each of the six methods declared in sessions 30-35 carries rules the shared guardrails cannot
express: which statements a period needs, what makes two periods comparable, which ISINs are
the person's peer set, what an event's identity is, and how a due-diligence requirement is
resolved. They live here, framework-free and database-free, so they can be read as a spec:

* ``precondition(method_key, entries, ...)`` -- READY or UNKNOWN with every named gap, checked
  against the task's PINNED context before a model may run. Missing required inputs are
  UNKNOWN, never a shorter confident answer (BE 35's degradation rule).
* ``blocks(method_key, entries, proposal, ...)`` -- deterministic tables computed by CODE from
  pinned rows (statement coverage, per-cell peer comparability, event timeline, requirement
  states). A model never writes these; the reader sees exactly what is established.
* ``proposal_reasons(method_key, proposal, entries)`` -- BLOCKED reasons specific to the method.

THE STATED LIMIT (same as guardrails.py): the lexical checks match fixed patterns, including
softened phrasings we could name. They cannot prove prose is true or that a paraphrase carries
no hidden conclusion. PASS means nothing structurally forbidden was found; human review is the
boundary.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

# Statement types an approved registry definition may name (BE 30).
STATEMENTS_REQUIRED_FOR_ANALYSIS = ("BALANCE_SHEET", "PROFIT_AND_LOSS", "CASH_FLOW")

#: Source types whose content is a claim by an interested party, never verification.
COMMENTARY_SOURCE_TYPES = frozenset({"MANAGEMENT_COMMENTARY", "TRANSCRIPT", "PRESENTATION",
                                     "NEWS"})

_P = re.IGNORECASE

_ALLEGATION = re.compile(
    r"\b(misstat\w*|irregularit\w*|aggressive\s+accounting|red\s+flags?|suspicious\w*|"
    r"dubious|questionable\s+(accounting|practices)|inflat(ed|ing|es)\s+(its\s+)?(profits?|"
    r"earnings|revenues?|assets)|hid(den|ing|es)?\s+(losses|debt|liabilities)|"
    r"diver(ted|sion)\s+of\s+funds|round[- ]?tripp\w*|creative\s+accounting|"
    r"management\s+(intends?|intended|wants?|is\s+trying)\s+to|deliberately|deceiv\w*|"
    r"mislead\w*|cover[- ]?up)\b", _P)
_PLUG = re.compile(
    r"\b(plug|balancing\s+(figure|item|number)|residual\s+(item|difference|balance)|"
    r"other\s+adjustments?\s+explain|rounding\s+(explains|difference|accounts\s+for)|"
    r"assume[sd]?\s+(an?\s+)?adjustment|reconciling\s+item\s+(of|is)\s+assumed)\b", _P)
_FORECAST = re.compile(
    r"\b(will\s+(likely\s+)?(grow|rise|fall|decline|increase|decrease|improve|deteriorate|"
    r"reach|recover|continue|accelerate|slow)|is\s+(likely|expected|poised|set|on\s+track)\s+to|"
    r"expected\s+to\s+(grow|rise|fall|report|deliver)|run[- ]?rate|next\s+(quarter|year|period)"
    r"\s+(should|will|could|may)|going\s+forward|trajector\w*|outlook\s+(is|remains)|"
    r"(beat|beats|missed|misses|miss)\s+(estimates|expectations|consensus|the\s+street)|"
    r"consensus|street\s+estimates?|analysts?\s+expect\w*|forecast\w*|project(ed|ion)s?\s+"
    r"(for|of|at))\b", _P)
_COMMITMENT = re.compile(
    r"\b((your|the|a|my)\s+(investment\s+)?thesis\s+(is|should|could|would|might)|"
    r"(an?\s+)?anti[- ]?thesis\s+(is|would)|pre[- ]?mortem\s+(is|would|should)|"
    r"exit\s+(when|if|once|at|below|above)|stop[- ]?loss|holding\s+period\s+(of|should)|"
    r"hold\s+for\s+(the\s+)?(long|short)\s+term|conviction|"
    r"(good|great|bad|poor|attractive|compelling|solid|strong|safe)\s+(investment|buy|bet|"
    r"opportunity|stock|pick)|undervalued|overvalued|cheap(ly)?\s+valued|worth\s+buying|"
    r"worth\s+owning|suitable\s+for\s+(you|investors))\b", _P)
_SELF_COMPLETE = re.compile(
    r"\b(comprehensive(ly)?|complete\s+(picture|coverage|view|analysis)|all\s+(the\s+)?"
    r"material\s+(facts|information|risks)|nothing\s+(significant|material|important)\s+"
    r"(is\s+|remains\s+)?(outstanding|missing|left)|fully\s+(covered|diligenced|verified|"
    r"researched)|no\s+(further|remaining)\s+(gaps|questions)|thorough(ly)?\s+covered)\b", _P)
_PEER_VALUATION = re.compile(
    r"\b(median\s+(multiple|valuation|p/?e|ev)|peer\s+(average|median|mean)|"
    r"(discount|premium)\s+to\s+(its\s+)?peers|trades?\s+(at|below|above)|cheaper\s+than|"
    r"(more\s+)?expensive\s+than|relative\s+(value|valuation|rating)|implied\s+(value|price|"
    r"multiple)|re-?rating|de-?rating|best\s+in\s+class|ranks?\s+(first|last|top|bottom)|"
    r"(top|bottom)\s+(quartile|decile)|composite|scorecard)\b", _P)
_PRICE_TIMING = re.compile(
    r"\b(price\s+(reaction|move|impact)|stock\s+(could|may|might|will|should)\s+(rise|fall|"
    r"react|rally|jump|drop|move)|shares?\s+(could|may|might|will|should)\s+(rise|fall|react|"
    r"rally|jump|drop)|buying\s+opportunity|selling\s+opportunity|entry\s+point|"
    r"ahead\s+of\s+the\s+(results|announcement|event)|before\s+the\s+(results|announcement)\s+"
    r"(to|is)|time\s+(your|the|an)\s+(entry|exit|trade)|catalyst\s+for\s+(a\s+)?"
    r"(re-?rating|upside|rally))\b", _P)
_PREDICTED_DATE = re.compile(
    r"\b(expected\s+(on|by|around|in)|likely\s+(on|by|around|in)|typically\s+(files|reports|"
    r"announces|holds)|usually\s+(files|reports|announces|holds)|should\s+be\s+announced|"
    r"is\s+due\s+(on|by)|deadline\s+(implies|suggests)|historically\s+(reports|announces)|"
    r"rumou?r\w*\s+(date|to\s+be))\b", _P)
_SCORE_VERDICT = re.compile(
    r"\b(pass(es|ed)?\s+(the\s+)?(checklist|diligence)|fail(s|ed)?\s+(the\s+)?(checklist|"
    r"diligence)|checklist\s+(is\s+)?(complete|satisfied|cleared)|all\s+requirements\s+(are\s+)?"
    r"(met|satisfied)|most\s+requirements|(none|some|all|most)\s+of\s+the\s+requirements|"
    r"ready\s+to\s+invest|investable|green\s+light|red\s+light|amber|traffic[- ]light|"
    r"readiness|clean\s+bill)\b", _P)

#: Per method: which extra lexical families apply.
_METHOD_LEXICON: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "financial_statement_analysis": (("MISCONDUCT_ASSERTION", _ALLEGATION),
                                     ("BALANCING_FIGURE", _PLUG),
                                     ("SELF_COMPLETENESS", _SELF_COMPLETE)),
    "earnings_analysis": (("FORECAST", _FORECAST), ("MISCONDUCT_ASSERTION", _ALLEGATION),
                          ("SELF_COMPLETENESS", _SELF_COMPLETE)),
    "initiating_coverage": (("COMMITMENT_AUTHORED", _COMMITMENT),
                            ("SELF_COMPLETENESS", _SELF_COMPLETE),
                            ("FORECAST", _FORECAST), ("MISCONDUCT_ASSERTION", _ALLEGATION)),
    "comparable_companies": (("PEER_VALUATION", _PEER_VALUATION),
                             ("COMMITMENT_AUTHORED", _COMMITMENT),
                             ("SELF_COMPLETENESS", _SELF_COMPLETE)),
    "catalyst_calendar": (("PRICE_OR_TIMING", _PRICE_TIMING),
                          ("PREDICTED_DATE", _PREDICTED_DATE), ("FORECAST", _FORECAST),
                          ("COMMITMENT_AUTHORED", _COMMITMENT)),
    "due_diligence": (("SCORE_OR_VERDICT", _SCORE_VERDICT),
                      ("COMMITMENT_AUTHORED", _COMMITMENT),
                      ("SELF_COMPLETENESS", _SELF_COMPLETE)),
    "forensic_accounting": (("MISCONDUCT_ASSERTION", _ALLEGATION),
                            ("BALANCING_FIGURE", _PLUG)),
}

METHOD_KEYS = tuple(k for k in _METHOD_LEXICON if k != "forensic_accounting")


def lexical_reasons(method_key: str, text: str, where: str) -> list[str]:
    return [f"{code}@{where}" for code, rx in _METHOD_LEXICON.get(method_key, ())
            if rx.search(text)]


# =========================================================================================
# helpers over pinned entries (duck-typed ContextEntry: context_id, kind, content)
# =========================================================================================


def _d(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _facts(entries: Mapping[str, Any]) -> list[Any]:
    return [e for e in entries.values() if e.kind == "fact"]


def _gap(id_: str, code: str, **detail: Any) -> dict[str, Any]:
    return {"id": id_, "kind": "INPUT", "code": code, "gate": detail.pop("gate", None),
            **({"detail": detail} if detail else {})}


def _series(c: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (c["metric"], c["basis"], c["unit"], c["cadence"])


def _period(c: Mapping[str, Any]) -> tuple[date, date]:
    return (_d(c["period_start"]), _d(c["period_end"]))  # type: ignore[return-value]


def _adjacent(a: tuple[date, date], b: tuple[date, date]) -> bool:
    return a[1] + timedelta(days=1) == b[0]


def _current(entries: Mapping[str, Any]) -> list[Any]:
    """Pinned facts that no other pinned fact supersedes."""
    facts = _facts(entries)
    superseded = {f.content.get("supersedes_id") for f in facts if f.content.get("supersedes_id")}
    return [f for f in facts if f.content["id"] not in superseded]


# =========================================================================================
# preconditions
# =========================================================================================


def _statement_precondition(entries: Mapping[str, Any], registry: Any) -> dict[str, Any]:
    missing: list[dict[str, Any]] = []
    by_period: dict[tuple[date, date], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    unmapped = []
    for f in _current(entries):
        c = f.content
        d = registry.lookup(c["metric"], c["basis"], c["unit"]) if registry else None
        if d is None or not getattr(d, "statement", None):
            unmapped.append(c["id"])
            continue
        by_period[_period(c)][d.statement].add(c["basis"])
    if not by_period:
        missing.append(_gap("AUDITED_STATEMENTS_BY_PERIOD", "NO_STATEMENT_FACTS_PINNED",
                            gate="BE-Q03" if unmapped else None))
    for (ps, pe), statements in sorted(by_period.items()):
        label = f"{ps.isoformat()}..{pe.isoformat()}"
        for st in STATEMENTS_REQUIRED_FOR_ANALYSIS:
            if st not in statements:
                missing.append(_gap(f"{st}:{label}", "STATEMENT_MISSING", statement=st,
                                    period=label))
        bases = set().union(*statements.values())
        if len(bases) > 1:
            missing.append(_gap(f"BASIS:{label}", "BASIS_MISMATCH", period=label,
                                bases=sorted(bases)))
    for fid in unmapped:
        missing.append(_gap(f"FACT:{fid}", "STATEMENT_UNMAPPED", gate="BE-Q03"))
    return {"state": "UNKNOWN" if missing else "READY", "missing": missing}


def _earnings_precondition(entries: Mapping[str, Any]) -> dict[str, Any]:
    """Every pinned series must cover ADJACENT periods; a gap is named, never skipped."""
    missing: list[dict[str, Any]] = []
    series: dict[tuple, list[tuple[date, date]]] = defaultdict(list)
    for f in _current(entries):
        series[_series(f.content)].append(_period(f.content))
    if not series:
        missing.append(_gap("REPORTED_EARNINGS_BY_PERIOD", "NO_EARNINGS_FACTS_PINNED"))
    for key, periods in sorted(series.items()):
        ps = sorted(set(periods))
        if len(ps) < 2:
            missing.append(_gap(f"{key[0]}:{key[1]}", "SINGLE_PERIOD_NOT_COMPARABLE",
                                metric=key[0], basis=key[1]))
        for a, b in zip(ps, ps[1:], strict=False):
            if not _adjacent(a, b):
                gap_start = a[1] + timedelta(days=1)
                missing.append(_gap(f"{key[0]}:{gap_start.isoformat()}", "MISSING_PERIOD",
                                    metric=key[0], basis=key[1],
                                    after=a[1].isoformat(), before=b[0].isoformat()))
    return {"state": "UNKNOWN" if missing else "READY", "missing": missing}


def _coverage_precondition(entries: Mapping[str, Any]) -> dict[str, Any]:
    if not any(e.kind in ("source", "fact") for e in entries.values()):
        return {"state": "UNKNOWN",
                "missing": [_gap("FILED_SOURCES", "NO_ISSUER_EVIDENCE_PINNED")]}
    return {"state": "READY", "missing": []}


def _peers(entries: Mapping[str, Any], subject_family: frozenset[str],
           families: Mapping[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """The person's peer set: every non-subject company whose facts or sources were pinned,
    grouped by VERIFIED alias family (never by ticker or name)."""
    peers: dict[str, frozenset[str]] = {}
    for e in entries.values():
        if e.kind not in ("fact", "source"):
            continue
        isin = e.content["isin"]
        if isin in subject_family:
            continue
        fam = families.get(isin, frozenset({isin}))
        peers[min(fam)] = fam
    return peers


def _comps_precondition(entries: Mapping[str, Any], subject_family: frozenset[str],
                        families: Mapping[str, frozenset[str]]) -> dict[str, Any]:
    missing: list[dict[str, Any]] = []
    peers = _peers(entries, subject_family, families)
    if not peers:
        missing.append(_gap("HUMAN_SELECTED_PEERS", "NO_PEERS_PINNED"))
    if not any(e.kind == "fact" and e.content["isin"] in subject_family
               for e in entries.values()):
        missing.append(_gap("SUBJECT_FACTS", "NO_SUBJECT_FACTS_PINNED"))
    return {"state": "UNKNOWN" if missing else "READY", "missing": missing}


def _dd_precondition(entries: Mapping[str, Any]) -> dict[str, Any]:
    reqs = [e for e in entries.values() if e.kind == "note" and e.content.get("kind") == "QUESTION"]
    if not reqs:
        return {"state": "UNKNOWN",
                "missing": [_gap("DECLARED_REQUIREMENTS", "NO_REQUIREMENTS_PINNED")]}
    return {"state": "READY", "missing": []}


def precondition(method_key: str, entries: Mapping[str, Any], *, registry: Any = None,
                 subject_family: frozenset[str] = frozenset(),
                 families: Mapping[str, frozenset[str]] | None = None) -> dict[str, Any]:
    if method_key == "financial_statement_analysis":
        return _statement_precondition(entries, registry)
    if method_key == "earnings_analysis":
        return _earnings_precondition(entries)
    if method_key == "initiating_coverage":
        return _coverage_precondition(entries)
    if method_key == "comparable_companies":
        return _comps_precondition(entries, subject_family, families or {})
    if method_key == "due_diligence":
        return _dd_precondition(entries)
    return {"state": "READY", "missing": []}


# =========================================================================================
# deterministic blocks (computed by code, rendered beside the proposal)
# =========================================================================================


def _cell(fs: list[Any]) -> dict[str, Any]:
    verified = [f for f in fs if f.content.get("verification") == "VERIFIED"]
    if not verified:
        return {"state": "UNKNOWN", "reason": "NO_VERIFIED_VALUE" if fs else "NO_VALUE",
                "fact_ids": [f.content["id"] for f in fs]}
    values = {f.content.get("numeric_value") for f in verified}
    if len(values) > 1:
        return {"state": "CONFLICT", "fact_ids": sorted(f.content["id"] for f in verified),
                "source_ids": sorted({f.content["source_id"] for f in verified})}
    f = verified[0]
    return {"state": "OK", "fact_id": f.content["id"], "value": f.content.get("numeric_value"),
            "unit": f.content["unit"], "basis": f.content["basis"],
            "period_start": str(f.content["period_start"]),
            "period_end": str(f.content["period_end"]), "source_id": f.content["source_id"]}


def peer_table(entries: Mapping[str, Any], subject_family: frozenset[str],
               families: Mapping[str, frozenset[str]]) -> dict[str, Any]:
    """Per-cell comparability: company x series, each cell OK / UNKNOWN / CONFLICT with its own
    period, unit, basis and source. A peer with no usable cell is EXCLUDED with its reason --
    never silently dropped, never filled with a peer-group average or an older period."""
    peers = _peers(entries, subject_family, families)
    companies = {"SUBJECT": subject_family, **peers}
    cur = _current(entries)
    subject_series = {_series(f.content) for f in cur if f.content["isin"] in subject_family}
    rows = []
    excluded = []
    for label, fam in companies.items():
        cells = {}
        for key in sorted(subject_series):
            fs = [f for f in cur if f.content["isin"] in fam and _series(f.content) == key]
            subj = [f for f in cur if f.content["isin"] in subject_family
                    and _series(f.content) == key]
            cell = _cell(fs)
            if cell["state"] == "OK" and label != "SUBJECT":
                s_cell = _cell(subj)
                if s_cell["state"] == "OK" and (s_cell["period_start"], s_cell["period_end"]) \
                        != (cell["period_start"], cell["period_end"]):
                    cell = {**cell, "state": "UNKNOWN", "reason": "PERIOD_MISMATCH"}
            cells[":".join(key)] = cell
        # A peer with a same-metric fact on another basis/unit is shown, never adjusted.
        other = [f for f in cur if f.content["isin"] in fam
                 and _series(f.content) not in subject_series]
        for f in other:
            cells.setdefault(":".join(_series(f.content)), {
                "state": "UNKNOWN", "reason": "SERIES_NOT_COMPARED", "fact_ids": [f.content["id"]]})
        row = {"company": label, "isins": sorted(fam), "cells": cells}
        rows.append(row)
        if label != "SUBJECT" and not any(c["state"] == "OK" for c in cells.values()):
            reasons = sorted({c.get("reason", c["state"]) for c in cells.values()}) or ["NO_DATA"]
            excluded.append({"company": label, "isins": sorted(fam), "reasons": reasons})
    return {"block": "PEER_TABLE", "rows": rows, "excluded_peers": excluded,
            "compared_series": [":".join(k) for k in sorted(subject_series)]}


def statement_table(entries: Mapping[str, Any], registry: Any) -> dict[str, Any]:
    cells = defaultdict(dict)
    for f in _current(entries):
        c = f.content
        d = registry.lookup(c["metric"], c["basis"], c["unit"]) if registry else None
        st = getattr(d, "statement", None) or "UNMAPPED"
        label = f"{c['period_start']}..{c['period_end']}"
        cells[label].setdefault(st, []).append(c["id"])
    return {"block": "STATEMENT_COVERAGE", "periods": {
        p: {st: v for st, v in sorted(s.items())} for p, s in sorted(cells.items())},
        "required": list(STATEMENTS_REQUIRED_FOR_ANALYSIS)}


def requirement_states(entries: Mapping[str, Any], proposal: Mapping[str, Any] | None
                       ) -> dict[str, Any]:
    """Due diligence: each pinned requirement resolves to exactly one of SUPPORTED (a FILED
    paragraph citing it and non-commentary evidence), CONTRADICTED (a CONFLICT paragraph
    citing it and an opposing record) or OPEN (anything else, with the reason). Derived by
    code from the proposal's structure; there is no fourth state and no count."""
    reqs = {cid: e for cid, e in entries.items()
            if e.kind == "note" and e.content.get("kind") == "QUESTION"}
    by_req: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for s in (proposal or {}).get("sections", []):
        for p in s["paragraphs"]:
            for cid in p["context_ids"]:
                if cid in reqs:
                    by_req[cid].append((s["heading"], p))
    out = []
    for cid, e in reqs.items():
        paras = by_req.get(cid, [])
        state, reason = "OPEN", "NOT_ADDRESSED"
        if len(paras) > 1:
            state, reason = "OPEN", "AMBIGUOUS_RESOLUTION"
        elif paras:
            heading, p = paras[0]
            evidence = [entries[x] for x in p["context_ids"] if x in entries and x != cid]
            filed = [x for x in evidence if x.kind in ("source", "fact")
                     and not (x.kind == "source" and x.content.get("source_type")
                              in COMMENTARY_SOURCE_TYPES)
                     and not (x.kind == "fact" and x.content.get("verification") != "VERIFIED")]
            if p["classification"] == "FILED" and filed:
                state, reason = "SUPPORTED", None
            elif p["classification"] == "FILED":
                state, reason = "OPEN", "SUPPORT_NOT_FILED_OR_UNVERIFIED"
            elif p["classification"] == "CONFLICT" and evidence:
                state, reason = "CONTRADICTED", None
            else:
                state, reason = "OPEN", "EVIDENCE_MISSING"
        out.append({"requirement_context_id": cid, "title": e.content.get("title"),
                    "written_on": str(e.content.get("written_on")), "state": state,
                    **({"reason": reason} if reason else {})})
    return {"block": "REQUIREMENTS", "requirements": out}


def event_timeline(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Catalyst calendar: held events for the pinned sources, scheduled and observed kept apart,
    reschedules linked, claims dateless, unverified labelled. Code-built, never model-built."""
    return {"block": "EVENT_TIMELINE",
            "scheduled": [e for e in events if e["status"] == "SCHEDULED"],
            "observed": [e for e in events if e["status"] == "OBSERVED"],
            "claimed": [e for e in events if e["status"] == "CLAIMED"],
            "cancelled": [e for e in events if e["status"] == "CANCELLED"],
            "unknown_dates": [e["id"] for e in events if e["date_precision"] != "EXACT"
                              and e["status"] != "CANCELLED"]}


# =========================================================================================
# proposal checks
# =========================================================================================


def _paragraphs(proposal: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    out = []
    for si, s in enumerate(proposal.get("sections", [])):
        for pi, p in enumerate(s["paragraphs"]):
            out.append((f"s{si}.p{pi}", s["heading"], p))
    return out


def proposal_reasons(method_key: str, proposal: Mapping[str, Any],
                     entries: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if method_key not in _METHOD_LEXICON:
        return reasons
    reasons += lexical_reasons(method_key, proposal.get("summary_template", ""), "summary")
    for where, heading, p in _paragraphs(proposal):
        reasons += lexical_reasons(method_key, p["text_template"], where)
        cited = [entries[c] for c in p["context_ids"] if c in entries]
        facts = [e for e in cited if e.kind == "fact"]
        # Like-for-like per paragraph: cited facts of one metric must share basis and cadence.
        by_metric: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for f in facts:
            by_metric[f.content["metric"]].add((f.content["basis"], f.content["cadence"]))
        if method_key in ("financial_statement_analysis", "earnings_analysis",
                          "comparable_companies") and any(len(v) > 1 for v in by_metric.values()):
            reasons.append(f"MIXED_BASIS_COMPARISON@{where}")
        # An issuer's explanation is a cited claim, never a filed fact.
        if p["classification"] == "FILED" and cited and all(
                e.kind == "source" and e.content.get("source_type") in COMMENTARY_SOURCE_TYPES
                for e in cited) and not p["exact_quotes"]:
            reasons.append(f"COMMENTARY_ADOPTED_AS_FACT@{where}")
        # An interpretation must rest on evidence, not on another interpretation.
        if p["classification"] == "INTERPRETATION" and heading in ("INTERPRETATION",
                                                                   "FACTS") and cited and all(
                e.kind == "note" and e.content.get("kind") == "INTERPRETATION" for e in cited):
            reasons.append(f"INTERPRETATION_ON_INTERPRETATION@{where}")
        if method_key == "catalyst_calendar" and heading == "WHY_IT_MATTERS" \
                and not any(e.kind == "thesis" for e in cited):
            reasons.append(f"NOT_RELATED_TO_COMMITMENT@{where}")
        # Comparable companies: a model cannot add a peer, because a citation outside the
        # pinned context is already CITATION_OUTSIDE_CONTEXT in guardrails.py, and the peer set
        # IS the pinned non-subject companies (``_peers``).
    if method_key == "due_diligence":
        states = requirement_states(entries, proposal)["requirements"]
        for r in states:
            if r.get("reason") == "AMBIGUOUS_RESOLUTION":
                reasons.append(f"REQUIREMENT_AMBIGUOUS:{r['requirement_context_id']}")
    return list(dict.fromkeys(reasons))
