"""Proposal guardrails: PASS or BLOCKED with reasons, and deterministic rendering (BE 21, 25).

A model proposal is UNTRUSTED DATA. The transport shape is checked first by the closed DTO
(``app/schemas/tasks.py::Proposal``): unknown keys, unknown headings, wrong types, a value
reference with both or neither of ``context_id``/``query_ref``, secret-shaped text and oversize
output are 422/413 and are NOT STORED. What reaches this module is well-formed, and is judged
against the pinned method contract and the task's SEALED context:

* a heading the method does not permit, or a deterministic heading (ORIGINAL_COMMITMENT) the
  model tried to write;
* a citation to anything outside this task's sealed context;
* a FACTS paragraph without a filed source, verified fact or stored evaluation; an ASSUMPTIONS
  paragraph without the person's own dated record; a CONFLICT with fewer than two citations;
* a quote that is not byte-for-byte present in the pinned text it names;
* numbers, number words, month names and trigger labels written in prose instead of being
  value references resolved by code;
* recommendation, price-target, scoring, misconduct, suggested-revision, URL and active-content
  language;
* purpose-specific output: INLINE needs a note proposal (OBSERVATION only with a pinned
  source), ANSWER_REVIEW needs ``answer_review`` whose ``gap`` entries are stated absences, not
  instructions.

A violating proposal is RETAINED as BLOCKED with its reasons, visible to the reviewer, and can
only be rejected (migration 044 refuses an ACCEPT of a non-PASS result).

THE STATED LIMIT, which this docstring and the handoff both keep saying: these are STRUCTURAL
and LEXICAL checks. They detect digits, a fixed list of number words and month names, and fixed
phrase patterns. They cannot prove that arbitrary prose is true, that a citation supports the
sentence it is attached to, or that a paraphrase carries no hidden quantity ("most", "nearly
all", "a small fraction" pass). A PASS means "nothing structurally forbidden was found", never
"verified". Human review (BE 22) is the enforceable boundary, and the UI says so.

VALUES ARE NEVER TAKEN FROM MODEL TEXT. ``{{vN}}`` placeholders are filled by ``render`` from the
pinned row content named by the Nth value reference. A position reference renders as a live
reference that the UI must read freshly; a calculation reference renders the value the approved
calculator (BE 27-28) computes from the task's pinned facts at its cutoff, or UNKNOWN with the
calculator's own reason code -- never a number the model wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.research.skills import CompiledContract, quote_matches

GUARDRAIL_VERSION = "guardrails-1"

PASS = "PASS"
BLOCKED = "BLOCKED"

_PLACEHOLDER = re.compile(r"\{\{v(\d{1,2})\}\}")
_DIGIT = re.compile(r"[0-9٠-٩۰-۹०-९０-９]")
# "one" and "a" are deliberately absent: they are pronouns far more often than quantities, and
# a guard that blocks every proposal teaches reviewers to ignore it. This is the heuristic
# limit named in the module docstring.
_NUMBER_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundreds?|thousands?|lakhs?|crores?|millions?|billions?|trillions?|"
    r"dozens?|half|halves|twice|double|doubled|triple|tripled|quadrupled|percent|percentage|"
    r"per\s+cent|bps|basis\s+points?)\b",
    re.IGNORECASE,
)
_MONTHS = re.compile(
    r"\b(january|february|march|april|june|july|august|september|october|november|december|"
    r"jan|feb|apr|jun|jul|aug|sept?|oct|nov|dec)\b",
    re.IGNORECASE,
)
_TRIGGER_UPPER = re.compile(r"\b(TRIGGERED|NOT_TRIGGERED|NOT_MET|MET|NOT MET)\b")
_TRIGGER_WORDS = re.compile(r"\b(triggered|has\s+fired|was\s+breached|been\s+crossed)\b",
                            re.IGNORECASE)
_RECOMMENDATION = (
    re.compile(r"\b(buy|sell|hold|accumulate|exit|trim|add\s+to)\s+(the\s+|this\s+|your\s+)?"
               r"(stock|shares?|position|holding)\b", re.IGNORECASE),
    re.compile(r"\b(strong\s+)?(buy|sell|hold)\s+(rating|call|recommendation)\b", re.IGNORECASE),
    re.compile(r"\b(recommend\w*|advis\w*)\s+(you\s+)?(to\s+)?(buy|sell|hold|exit|trim|"
               r"accumulate)\b", re.IGNORECASE),
    re.compile(r"\b(price\s+target|target\s+price|fair\s+value|intrinsic\s+value|"
               r"expected\s+return)\b", re.IGNORECASE),
)
_SCORING = re.compile(
    r"(%|\b(probability|likelihood|confidence\s+(level|score)|conviction\s+(score|level|"
    r"rating)|score[sd]?|scoring|rating|ranked|out\s+of\s+ten)\b)",
    re.IGNORECASE,
)
_MISCONDUCT = re.compile(
    r"\b(fraud\w*|misconduct|embezzl\w*|siphon\w*|money\s+laundering|manipulat\w*|"
    r"cooked\s+the\s+books|window\s+dressing)\b",
    re.IGNORECASE,
)
_REVISION = (
    re.compile(r"\b(suggested|proposed|recommended|revised|new)\s+(revision|threshold|exit|"
               r"exit\s+rule|thesis|pre-?mortem|holding\s+period)\b", re.IGNORECASE),
    re.compile(r"\b(revise|rewrite|replace|lower|raise|loosen|tighten)\s+(your\s+|the\s+)?"
               r"(thesis|threshold|exit|exit\s+rule|pre-?mortem|holding\s+period)\b",
               re.IGNORECASE),
)
_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|org|net|io)\b)", re.IGNORECASE)
_ACTIVE = re.compile(r"(<\s*/?\s*(script|iframe|img|a|object|embed|style|svg)\b|javascript:|"
                     r"\bon[a-z]+\s*=)", re.IGNORECASE)
_INSTRUCTION = re.compile(
    r"^\s*(you\s+should|you\s+must|you\s+need\s+to|rewrite|change\s+your|replace|delete|"
    r"add\s+that|say\s+that|your\s+answer\s+should)\b",
    re.IGNORECASE,
)

#: Fields a value reference may name, per context kind. Anything else is refused.
VALUE_FIELDS: dict[str, frozenset[str]] = {
    "fact": frozenset({"numeric_value", "boolean_value", "text_value", "unit", "metric",
                       "basis", "period_start", "period_end", "cadence", "verification"}),
    "source": frozenset({"publication_date", "period", "source", "source_type",
                         "retrieved_at"}),
    "note": frozenset({"written_on", "title", "kind"}),
    "thesis": frozenset({"version", "version_on", "formed_on", "status",
                         "intended_holding_period"}),
    "answer": frozenset({"answered_on", "question_written_on"}),
    "evaluation": frozenset({"state", "evidence_cutoff", "recorded_at", "engine_version"}),
}
POSITION_FIELDS = frozenset({"quantity", "avg_cost", "invested", "value", "unrealised",
                             "price", "as_of"})
#: Fields a calculation reference may name (a pinned calculation spec, BE 27-28).
CALCULATION_FIELDS = frozenset({"value", "unit", "period_start", "period_end", "formula_id"})
_FILED_KINDS = frozenset({"source", "fact", "evaluation"})


@dataclass(frozen=True)
class ContextEntry:
    """One sealed context row as the guardrails see it."""

    context_id: str
    kind: str
    content: dict[str, Any]
    quotable: tuple[str, ...]


@dataclass
class Outcome:
    status: str
    reasons: list[str] = field(default_factory=list)
    citations: list[tuple[str, str]] = field(default_factory=list)


def _text_reasons(text: str, where: str, *, instruction_check: bool = False) -> list[str]:
    """Lexical house-rule checks on one template, placeholders removed first."""
    bare = _PLACEHOLDER.sub(" ", text)
    reasons = []
    checks: list[tuple[str, bool]] = [
        ("UNREFERENCED_NUMBER", bool(_DIGIT.search(bare))),
        ("UNREFERENCED_NUMBER_WORD", bool(_NUMBER_WORDS.search(bare))),
        ("UNREFERENCED_DATE", bool(_MONTHS.search(bare))),
        ("TRIGGER_LABEL_IN_PROSE", bool(_TRIGGER_UPPER.search(bare) or
                                        _TRIGGER_WORDS.search(bare))),
        ("RECOMMENDATION", any(p.search(bare) for p in _RECOMMENDATION)),
        ("SCORING", bool(_SCORING.search(bare))),
        ("MISCONDUCT_ASSERTION", bool(_MISCONDUCT.search(bare))),
        ("SUGGESTED_REVISION", any(p.search(bare) for p in _REVISION)),
        ("MODEL_URL", bool(_URL.search(bare))),
        ("ACTIVE_CONTENT", bool(_ACTIVE.search(text))),
    ]
    if instruction_check:
        checks.append(("INSTRUCTION_SHAPED", bool(_INSTRUCTION.search(bare))))
    for code, hit in checks:
        if hit:
            reasons.append(f"{code}@{where}")
    return reasons


def _paragraph(
    p: dict[str, Any], where: str, context: dict[str, ContextEntry], contract: CompiledContract,
    *, heading: str | None, calc_refs: set[str], has_position: bool,
    require_citation: bool, instruction_check: bool = False,
) -> tuple[list[str], list[tuple[str, str]]]:
    reasons: list[str] = []
    citations: list[tuple[str, str]] = []
    limits = contract.limits
    text = p["text_template"]
    if len(text) > limits.get("max_paragraph_chars", 2000):
        reasons.append(f"LIMIT_EXCEEDED@{where}")
    cls = p["classification"]
    if heading is not None:
        allowed = contract.heading_classes.get(heading)
        if allowed is not None and cls not in allowed:
            reasons.append(f"CLASSIFICATION_NOT_PERMITTED@{where}")
    ids = list(p["context_ids"])
    if len(ids) > limits.get("max_context_ids_per_paragraph", 8):
        reasons.append(f"LIMIT_EXCEEDED@{where}")
    kinds: set[str] = set()
    for cid in ids:
        entry = context.get(cid)
        if entry is None:
            reasons.append(f"CITATION_OUTSIDE_CONTEXT@{where}")
            continue
        kinds.add(entry.kind)
        citations.append((cid, where))
    if require_citation and not ids:
        reasons.append(f"UNCITED@{where}")
    if heading is not None and heading in contract.heading_kinds and ids:
        if not kinds & contract.heading_kinds[heading]:
            reasons.append(f"WRONG_EVIDENCE_KIND@{where}")
    if cls == "FILED" and not kinds & _FILED_KINDS:
        reasons.append(f"FILED_WITHOUT_FILING@{where}")
    if cls == "FILED":
        facts = [context[c] for c in ids if c in context and context[c].kind == "fact"]
        if facts and not any(context[c].kind in ("source", "evaluation") for c in ids
                             if c in context) and \
                any(f.content.get("verification") != "VERIFIED" for f in facts):
            reasons.append(f"UNVERIFIED_FACT@{where}")
    if cls == "CONFLICT" and len(set(ids)) < 2:
        reasons.append(f"CONFLICT_NEEDS_TWO@{where}")

    refs = list(p["value_refs"])
    if len(refs) > limits.get("max_value_refs_per_paragraph", 8):
        reasons.append(f"LIMIT_EXCEEDED@{where}")
    for i, ref in enumerate(refs):
        field_name = ref["field"]
        if ref.get("context_id") is not None:
            entry = context.get(ref["context_id"])
            if entry is None:
                reasons.append(f"VALUE_REF_OUTSIDE_CONTEXT@{where}.v{i}")
            elif field_name not in VALUE_FIELDS.get(entry.kind, frozenset()):
                reasons.append(f"VALUE_FIELD_NOT_PERMITTED@{where}.v{i}")
            else:
                citations.append((ref["context_id"], f"{where}.v{i}"))
        else:
            qref = ref["query_ref"]
            if qref == "position":
                if not has_position or field_name not in POSITION_FIELDS:
                    reasons.append(f"VALUE_REF_INVALID@{where}.v{i}")
            elif qref not in calc_refs or field_name not in CALCULATION_FIELDS:
                reasons.append(f"VALUE_REF_INVALID@{where}.v{i}")
    for match in _PLACEHOLDER.finditer(text):
        if int(match.group(1)) >= len(refs):
            reasons.append(f"PLACEHOLDER_INVALID@{where}")

    quotes = list(p["exact_quotes"])
    if len(quotes) > limits.get("max_quotes_per_paragraph", 4):
        reasons.append(f"LIMIT_EXCEEDED@{where}")
    for i, quote in enumerate(quotes):
        entry = context.get(quote["context_id"])
        if entry is None:
            reasons.append(f"QUOTE_OUTSIDE_CONTEXT@{where}.q{i}")
        elif len(quote["text"]) > limits.get("max_quote_chars", 600):
            reasons.append(f"LIMIT_EXCEEDED@{where}.q{i}")
        elif not quote_matches(quote["text"], list(entry.quotable)):
            reasons.append(f"QUOTE_MISMATCH@{where}.q{i}")
        else:
            citations.append((quote["context_id"], f"{where}.q{i}"))
    reasons += _text_reasons(text, where, instruction_check=instruction_check)
    return reasons, citations


def validate(
    proposal: dict[str, Any],
    *,
    purpose: str,
    contract: CompiledContract,
    context: dict[str, ContextEntry],
    calc_refs: set[str],
    has_position: bool,
) -> Outcome:
    """PASS or BLOCKED with reasons. ``proposal`` has already passed the closed DTO."""
    reasons: list[str] = []
    citations: list[tuple[str, str]] = []
    limits = contract.limits

    summary = proposal["summary_template"]
    if len(summary) > limits.get("max_summary_chars", 600):
        reasons.append("LIMIT_EXCEEDED@summary")
    if _PLACEHOLDER.search(summary):
        reasons.append("PLACEHOLDER_INVALID@summary")
    reasons += _text_reasons(summary, "summary")

    sections = proposal["sections"]
    if len(sections) > limits.get("max_sections", 10):
        reasons.append("LIMIT_EXCEEDED@sections")
    seen: set[str] = set()
    for si, section in enumerate(sections):
        heading = section["heading"]
        where = f"s{si}"
        if heading in seen:
            reasons.append(f"DUPLICATE_HEADING@{where}")
        seen.add(heading)
        if heading in contract.deterministic_headings:
            reasons.append(f"DETERMINISTIC_SECTION@{where}")
            continue
        if heading not in contract.headings:
            reasons.append(f"HEADING_NOT_PERMITTED@{where}")
            continue
        if len(section["paragraphs"]) > limits.get("max_paragraphs_per_section", 12):
            reasons.append(f"LIMIT_EXCEEDED@{where}")
        for pi, paragraph in enumerate(section["paragraphs"]):
            r, c = _paragraph(paragraph, f"{where}.p{pi}", context, contract, heading=heading,
                              calc_refs=calc_refs, has_position=has_position,
                              require_citation=heading in contract.cited_headings)
            reasons += r
            citations += c
    if purpose in ("RESEARCH", "CHAT", "INLINE"):
        for heading in sorted(contract.required_headings - seen):
            reasons.append(f"MISSING_HEADING:{heading}")

    note = proposal.get("note_proposal")
    if purpose == "INLINE":
        if note is None:
            reasons.append("MISSING_NOTE_PROPOSAL")
        else:
            ids = list(note["source_context_ids"])
            for cid in ids:
                if cid not in context:
                    reasons.append("CITATION_OUTSIDE_CONTEXT@note")
                else:
                    citations.append((cid, "note"))
            if note["kind"] == "OBSERVATION" and not any(
                    context.get(c) is not None and context[c].kind == "source" for c in ids):
                reasons.append("OBSERVATION_WITHOUT_SOURCE@note")
            if len(note["body_template"]) > 64 * 1024:
                reasons.append("LIMIT_EXCEEDED@note")
            reasons += _text_reasons(note["title"], "note.title")
            reasons += _text_reasons(note["body_template"], "note.body")
            if _PLACEHOLDER.search(note["body_template"] + note["title"]):
                reasons.append("PLACEHOLDER_INVALID@note")
    elif note is not None:
        reasons.append("NOTE_PROPOSAL_NOT_EXPECTED")

    review = proposal.get("answer_review")
    if purpose == "ANSWER_REVIEW":
        if review is None:
            reasons.append("MISSING_ANSWER_REVIEW")
        else:
            for key in ("ok", "gap"):
                for i, paragraph in enumerate(review[key]):
                    r, c = _paragraph(paragraph, f"{key}{i}", context, contract, heading=None,
                                      calc_refs=calc_refs, has_position=has_position,
                                      require_citation=True,
                                      instruction_check=(key == "gap"))
                    reasons += r
                    citations += c
    elif review is not None:
        reasons.append("ANSWER_REVIEW_NOT_EXPECTED")

    # Deduplicate while keeping order; one citation row per (context, claim).
    reasons = list(dict.fromkeys(reasons))
    citations = list(dict.fromkeys(citations))
    return Outcome(status=BLOCKED if reasons else PASS, reasons=reasons, citations=citations)


# =========================================================================================
# deterministic rendering
# =========================================================================================


def _value(ref: dict[str, Any], context: dict[str, ContextEntry],
           calculations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if ref.get("context_id") is not None:
        entry = context.get(ref["context_id"])
        if entry is None or ref["field"] not in VALUE_FIELDS.get(entry.kind, frozenset()):
            return {"status": "UNAVAILABLE", "text": "[unavailable]"}
        raw = entry.content.get(ref["field"])
        if raw is None:
            return {"status": "UNAVAILABLE", "text": "[not recorded]"}
        return {"status": "RESOLVED", "text": str(raw), "context_id": ref["context_id"],
                "field": ref["field"]}
    if ref.get("query_ref") == "position":
        # A live slot: never a stored or model-supplied number. The UI reads it fresh.
        return {"status": "LIVE_REFERENCE", "text": "[current position -- read live]",
                "query_ref": "position", "field": ref["field"]}
    calc = (calculations or {}).get(ref.get("query_ref") or "")
    if calc is not None and calc.get("status") == "OK" and ref["field"] in CALCULATION_FIELDS:
        period = calc.get("period") or {}
        raw = {"value": calc.get("value"), "unit": calc.get("unit"),
               "formula_id": f"{calc.get('formula_id')} v{calc.get('version')}",
               "period_start": period.get("period_start"),
               "period_end": period.get("period_end")}.get(ref["field"])
        if raw is not None:
            return {"status": "CALCULATED", "text": str(raw), "query_ref": ref["query_ref"],
                    "field": ref["field"]}
    code = (calc or {}).get("code") or "FORMULA_NOT_APPROVED"
    return {"status": "UNKNOWN", "text": f"[unavailable: {code.lower().replace('_', ' ')}]",
            "query_ref": ref.get("query_ref"), "field": ref["field"], "code": code}


def _fill(paragraph: dict[str, Any], context: dict[str, ContextEntry],
          calculations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    values = [_value(r, context, calculations) for r in paragraph["value_refs"]]

    def sub(match: re.Match[str]) -> str:
        i = int(match.group(1))
        return values[i]["text"] if i < len(values) else "[invalid reference]"

    return {
        "text": _PLACEHOLDER.sub(sub, paragraph["text_template"]),
        "classification": paragraph["classification"],
        "citations": list(paragraph["context_ids"]),
        "quotes": [dict(q) for q in paragraph["exact_quotes"]],
        "values": values,
    }


def original_commitment(context: dict[str, ContextEntry]) -> list[dict[str, Any]]:
    """ORIGINAL_COMMITMENT, filled by code from the pinned thesis version, verbatim."""
    theses = [e for e in context.values() if e.kind == "thesis"]
    if not theses:
        return []
    t = theses[0].content
    paragraphs = []
    for label, key in (("Argument", "argument"), ("Pre-mortem", "pre_mortem"),
                       ("Intended holding period", "intended_holding_period")):
        if t.get(key):
            paragraphs.append({"label": label, "text": t[key], "citations":
                               [theses[0].context_id], "verbatim": True})
    for rule in t.get("exits") or []:
        paragraphs.append({"label": "Exit", "text": rule.get("label") or rule.get("id", ""),
                           "rule": rule, "citations": [theses[0].context_id],
                           "verbatim": True})
    return paragraphs


def render(proposal: dict[str, Any], context: dict[str, ContextEntry],
           contract: CompiledContract | None,
           calculations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The proposal as a person reads it. Values come from pinned rows; nothing is invented.
    Rendered on every read, so it always reflects the pinned context, never a stored copy."""
    sections = [
        {"heading": s["heading"],
         "paragraphs": [_fill(p, context, calculations) for p in s["paragraphs"]]}
        for s in proposal.get("sections", [])
        if contract is None or s["heading"] not in contract.deterministic_headings
    ]
    if contract is not None and "ORIGINAL_COMMITMENT" in contract.deterministic_headings:
        sections.append({"heading": "ORIGINAL_COMMITMENT", "deterministic": True,
                         "paragraphs": original_commitment(context)})
    note = proposal.get("note_proposal")
    review = proposal.get("answer_review")
    return {
        "summary": proposal.get("summary_template", ""),
        "sections": sections,
        "note_proposal": ({"kind": note["kind"], "title": note["title"],
                           "body": note["body_template"],
                           "source_context_ids": list(note["source_context_ids"])}
                          if note else None),
        "answer_review": ({"ok": [_fill(p, context, calculations) for p in review["ok"]],
                           "gap": [_fill(p, context, calculations) for p in review["gap"]]}
                          if review else None),
    }
