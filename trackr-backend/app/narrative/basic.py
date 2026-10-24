"""The Basic-view one-liners: the prompt, the request, and the checks on what comes back.

docs/ANALYTICS_BASIC_NARRATIVE.md is the specification. In one paragraph: the model is
given the WHOLE portfolio (every fact in the pack, the dated ledger) plus the findings
``app.analytics.basic_slots`` already measured, and chooses what to say about one tab in
at most four lines. It never types a number -- every figure is a ``fact`` segment filled
from the engine -- and every line is checked here before it can be shown:

  V1-V3, V5-V7   the existing rules (``verify.py``): ids resolve, no digits, no spelled
                 quantities, evidence declared, thin coverage shown, no advice.
  V4             only for the model's own observations (``OBS``), which must declare a
                 relation from ``claims.RELATIONS``; it is recomputed over the pack.
  V8  SHAPE      lead <= 7 words, body <= 20, <= 140 rendered characters, the slot's
                 must-cite facts present, no more lines than allowed.
  V9  POLARITY   a comparative or size word must be one code allowed for that slot.
  V10 FORECAST   no prediction language.
  V11 JARGON     no term a class-10 reader needs a gloss for.
  V12 IMPLICATION  an implication only where the slot permits one.
  V13 TONE       wording may not contradict the tone code set (calling a red finding
                 "healthy").

Pure: no network (that is ``client.py``), no database (that is the router).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.narrative.claims import RELATIONS, Claim, Segment
from app.narrative.factpack import FactPack
from app.narrative.lexicon import LEXICON
from app.narrative.render import render
from app.narrative.select import select
from app.narrative.verify import _ADVICE, _DIGIT, _QUANTITY_WORDS, verify

#: Part of the artifact cache key. Bump on ANY change to the prompt text or the contract.
BASIC_TEMPLATE_VERSION = "2026.10.basic.3"

TAB_SURFACE = {
    "overview": "analytics.overview",
    "risk": "analytics.risk",
    "diversification": "analytics.diversification",
    "fundamentals": "analytics.fundamentals",
    "performance": "analytics.performance",
    # Advanced Overview's Explanation column: one line for EVERY fired finding, any tab.
    "insights": "analytics.insights",
}

#: Advanced explanations are generated in bounded groups, one line per fired finding.
INSIGHT_BATCH_SIZE = 4

MAX_LINES = 4
LEAD_WORDS = 7
BODY_WORDS = 20
MAX_CHARS = 140
#: A ledger longer than this is sent as per-holding summaries rather than row by row, so
#: a large book still fits the model's context without dropping a holding.
MAX_LEDGER_ROWS = 1500

OBS = "OBS"
INSUFFICIENT = "INSUFFICIENT"

IMPLICATIONS = {
    "MOVES_WHOLE_BOOK": "results in this holding or sector can move the whole portfolio",
    "SWINGS_BOTH_WAYS": "bigger swings, in both directions",
    "MOVES_TOGETHER": "these holdings have tended to rise and fall together",
    "MOVES_INDEPENDENTLY": "these holdings have tended to move separately",
    "SLOW_TO_SELL": "selling quickly could move the price",
    "NOT_PRICED_IN_PE": "loss-making companies have no P/E and are left out of it",
    "NONE": "no implication",
}

BASIC_SYSTEM_PROMPT = f"""You write the one-line insights on the Basic view of one tab of a \
portfolio analytics page for an Indian investor in listed stocks.

YOU ARE GIVEN: the whole portfolio (every computed figure as a named fact with its value, \
unit and date, plus the dated transaction ledger); the candidate findings code has already \
measured against published thresholds, each with a final tone, direction and size word; \
the findings that could not be measured yet; and the fact ids each line may cite.

YOUR JOB: read the portfolio, decide the most useful things to tell this investor about \
this tab, and say each in one plain line. At most max_lines lines. Prefer candidates with \
tone needs_attention, then watch, then positive, then neutral. A candidate's tone, \
direction and size word are final: never contradict them. You may instead add your own \
observation that connects facts (slot "OBS") only if you can state it as one of the \
listed relations; it is recomputed and shown as neutral. If "insufficient" is not empty, \
add one final line (slot "INSUFFICIENT") saying plainly what is not measured yet and why.

FORMAT OF A LINE
- "lead": at most {LEAD_WORDS} words, a short headline. No figures in the lead.
- "body": at most {BODY_WORDS} words, containing every fact listed in the candidate's \
must_cite. Lead and body read as one sentence, or two very short ones.
- Lead and body together must fit in {MAX_CHARS} displayed characters. Aim below 110; \
omit an optional implication before making a line too long.
- Both lead and body are arrays of segment objects, NEVER plain strings. Keep this \
exact schema when correcting a rejected answer. Include spaces in text segments \
where they meet fact or phrase segments.
- Plain words a class-10 reader knows. Never use: beta, VaR, volatility, correlation, P/E, \
P/B, drawdown, HHI, TWRR, XIRR, CAGR, Sharpe, Sortino. Describe what it means instead.

NUMBERS
- You can read every value. Never write one, in digits or in words ("two", "half", "a \
week", "double", "most"). Use the value to decide; use the id to show it.
- To show a figure: {{"t":"fact","id":"<fact id>"}}. It is filled from the engine exactly.
- A name containing digits (Nifty 50) is {{"t":"lex","id":"<LEX id>"}} from "phrases".
- A candidate line may only cite that candidate's facts (and coverage facts).
- Use fact segments for holding names when the candidate supplies name facts.
- In the INSUFFICIENT line, use the supplied gap facts when present (for example the \
remaining trading sessions). Do not invent a measurement or a reason.

DIRECTION AND SIZE
- Use only comparative words from the candidate's allowed_words, never a word from \
forbidden_words. If it has a magnitude_word, you may use exactly that phrase and no other \
size or quantity word. With no magnitude_word, use no size word at all.
- For an OBS line, the comparative word is {{"t":"rel"}}; it is chosen from how the \
relation evaluated.

IMPLICATION
- Only if the candidate's implication is not NONE, you may add its meaning in a few words.
- Describe the portfolio, never the person. Never advise: no buy, sell, hold, trim, add, \
rebalance, should, consider, avoid.
- Never predict: no will, expect, likely, going to, forecast, guarantee, safe.
- Never claim a cause, a change over time, or anything not in the facts.

COVERAGE: if a candidate has a coverage_fact, the body must include it.

Return JSON only:
{{"lines":[{{"slot":"<candidate slot, OBS or INSUFFICIENT>",
  "relation":"<RELATION, only for OBS>","args":{{}},
  "lead":[segments],"body":[segments],"cites":["<fact id>",...]}}]}}
Segments: {{"t":"text","v":"..."}} | {{"t":"fact","id":"..."}} | {{"t":"lex","id":"..."}} \
| {{"t":"rel"}} (OBS only)."""


# --- the request -----------------------------------------------------------------------


def ledger_for_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Dated ledger rows -> the ledger part of the request.

    ``rows`` are already plain strings (date, isin, symbol, type, quantity, price,
    charges, dividend_amount) built by the router from ``load_transactions``. A book over
    ``MAX_LEDGER_ROWS`` is summarised per holding instead of truncated: truncating would
    show the model a ledger that silently stops.
    """
    if len(rows) <= MAX_LEDGER_ROWS:
        return {"basis": "rows", "rows": rows}
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = summary.setdefault(
            row["isin"],
            {
                "isin": row["isin"],
                "symbol": row.get("symbol"),
                "first_date": row["date"],
                "last_date": row["date"],
                "rows": 0,
                "types": {},
            },
        )
        entry["rows"] += 1
        entry["first_date"] = min(entry["first_date"], row["date"])
        entry["last_date"] = max(entry["last_date"], row["date"])
        entry["types"][row["type"]] = entry["types"].get(row["type"], 0) + 1
    return {
        "basis": "per_holding_summary",
        "row_count": len(rows),
        "holdings": sorted(summary.values(), key=lambda e: e["isin"]),
    }


def _read(value: Any) -> Any:
    return float(f"{value:.6g}") if isinstance(value, float) else value


def build_request(
    pack: FactPack,
    basic: dict[str, Any],
    tab: str,
    ledger: dict[str, Any],
    max_lines: int = MAX_LINES,
    revamp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the model reads for one tab. The model may READ every fact in ``pack``;
    it may CITE only the facts the tab's surface selects (``citable``)."""
    surface = TAB_SURFACE[tab]
    selected = select(pack, surface)
    if tab == "insights":
        every = [c for t in (basic.get("tabs") or {}).values() for c in t["candidates"]]
        findings = {"candidates": every, "insufficient": []}
        max_lines = len(findings["candidates"])
    else:
        findings = (basic.get("tabs") or {}).get(tab) or {"candidates": [], "insufficient": []}
    return {
        "template_version": BASIC_TEMPLATE_VERSION,
        "rule_version": basic.get("rule_version"),
        "tab": tab,
        "as_of": pack.as_of,
        "benchmark": pack.benchmark,
        "max_lines": max_lines,
        "portfolio": {
            # One as_of for the whole pack: every fact is built from the same replay.
            "facts_as_of": pack.as_of,
            # Floats to 6 significant digits: enough to judge by, and the screen always
            # renders the engine's own value, never this one. Money is already a string.
            "facts": {fid: [_read(f.value), f.unit] for fid, f in sorted(pack.facts.items())},
            "fact_format": "fact id -> [value, unit]; money is a decimal string in rupees",
            "ledger": ledger,
            # Read-only context: per-holding betas, risk shares, liquidity, 1Y correlation,
            # the Valuation Map. Citable only through basic.* facts.
            "revamp": revamp,
        },
        "candidates": [
            {
                k: c[k]
                for k in (
                    "slot",
                    "tone",
                    "direction",
                    "magnitude_word",
                    "facts",
                    "must_cite",
                    "subjects",
                    "allowed_words",
                    "forbidden_words",
                    "coverage_fact",
                )
            }
            | {
                "must_cite": [c["facts"][k] for k in c["must_cite"]],
                "implication": {
                    "kind": c["implication"],
                    "meaning": IMPLICATIONS[c["implication"]],
                },
            }
            for c in findings["candidates"]
        ],
        "insufficient": findings["insufficient"],
        "citable": sorted(selected.facts),
        "phrases": dict(sorted(LEXICON.items())),
        "relations": list(RELATIONS),
    }


def user_content(request: dict[str, Any]) -> str:
    """The user turn. ``portfolio`` first: it is identical across a book's tab calls, so
    a provider prompt cache can reuse it."""
    ordered = {"portfolio": request["portfolio"]} | {
        k: v for k, v in request.items() if k != "portfolio"
    }
    return json.dumps(ordered, separators=(",", ":"), default=str)


# --- parsing -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    slot: str
    lead: tuple[Segment, ...]
    body: tuple[Segment, ...]
    cites: tuple[str, ...]
    relation: str | None = None
    args: dict[str, Any] = field(default_factory=dict)


def parse_lines(payload: Any) -> tuple[list[Line], list[str]]:
    """The model's JSON -> lines, plus reasons for anything unreadable."""
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        raise ValueError("the model returned no 'lines' list")
    lines: list[Line] = []
    dropped: list[str] = []
    for index, raw in enumerate(payload["lines"][: MAX_LINES + 2]):
        try:
            lines.append(_line(raw))
        except ValueError as exc:
            dropped.append(f"line {index}: {exc}")
    if len(payload["lines"]) > MAX_LINES + 2:
        dropped.append("lines beyond the cap were not read")
    return lines, dropped


def _segments(raw: Any, part: str) -> tuple[Segment, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"no {part} segments")
    out = []
    for seg in raw:
        if not isinstance(seg, dict):
            raise ValueError(f"a {part} segment is not an object")
        kind = seg.get("t")
        if kind == "text" and isinstance(seg.get("v"), str):
            out.append(Segment("text", v=seg["v"]))
        elif kind in ("fact", "lex") and isinstance(seg.get("id"), str) and seg["id"]:
            out.append(Segment(kind, id=seg["id"]))
        elif kind == "rel":
            out.append(Segment("rel"))
        else:
            raise ValueError(f"unreadable {part} segment {seg!r}")
    return tuple(out)


def _line(raw: Any) -> Line:
    if not isinstance(raw, dict) or not isinstance(raw.get("slot"), str):
        raise ValueError("no slot")
    cites = raw.get("cites") or []
    if not isinstance(cites, list) or not all(isinstance(c, str) for c in cites):
        raise ValueError("cites is not a list of fact ids")
    relation = raw.get("relation")
    if relation is not None and not isinstance(relation, str):
        raise ValueError("relation is not a string")
    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("args is not an object")
    return Line(
        raw["slot"],
        _segments(raw.get("lead"), "lead"),
        _segments(raw.get("body"), "body"),
        tuple(dict.fromkeys(cites)),
        relation,
        args,
    )


# --- verification -------------------------------------------------------------------------

#: Calendar time made up in prose ("about a week more"). "a year" is not here: it is the
#: per-annum unit of an annualised return, not a quantity.
_TIME_QUANTITY = re.compile(
    r"\b(?:a|an|one|few|a few|couple of|several)\s+(?:day|week|month|quarter|fortnight)s?\b",
    re.IGNORECASE,
)
_COMPARATIVES = re.compile(
    r"\b(?:above|below|higher|lower|more|less|fewer|cheaper|dearer|pricier|ahead|behind|"
    r"bumpier|calmer|smoother|steadier|better|worse|bigger|smaller|larger|greater|"
    r"outperform\w*|underperform\w*|beat|beats|trailed|trails|lagged|lags|short)\b",
    re.IGNORECASE,
)
_SIZE_WORDS = re.compile(
    r"\b(?:slightly|clearly|far|much|a little|hugely|significantly|substantially|"
    r"marginally|roughly|about|nearly|almost|in line|way|barely|sizeable|large part|"
    r"small part|nearly all|most)\b",
    re.IGNORECASE,
)
_FORECAST = re.compile(
    r"\b(?:will|won't|expect\w*|likely|unlikely|going to|forecast\w*|predict\w*|"
    r"guarantee\w*|safe|safer|protected|risk-free|worst case|worst-case|bound to|certain)\b",
    re.IGNORECASE,
)
_JARGON = re.compile(
    r"\b(?:beta|VaR|volatility|volatile|correlation|correlated|P/E|PE ratio|P/B|"
    r"drawdown|HHI|TWRR|XIRR|CAGR|Sharpe|Sortino|standard deviation)\b",
    re.IGNORECASE,
)
_IMPLICATION = re.compile(r"\b(?:could|can|means|meaning|so a|so any|so that)\b", re.IGNORECASE)
_REASSURING = re.compile(
    r"\b(?:healthy|good|great|strong|fine|solid|well spread|balanced)\b", re.IGNORECASE
)
_ALARM = re.compile(r"\b(?:risky|worrying|concern\w*|danger\w*|alarming|bad|weak)\b", re.IGNORECASE)

_RENDER_WIDTH = {"money": 10, "percent": 6, "fraction": 6, "ratio": 5}


@dataclass(frozen=True, slots=True)
class LineRejection:
    rule: str
    slot: str
    reason: str


@dataclass(frozen=True, slots=True)
class CheckedLine:
    line: Line
    tone: str


def _text(segments: tuple[Segment, ...]) -> str:
    return " ".join(s.v or "" for s in segments if s.t == "text")


def _words(segments: tuple[Segment, ...]) -> int:
    return sum(len((s.v or "").split()) if s.t == "text" else 1 for s in segments)


def _rendered_chars(line: Line, pack: FactPack) -> int:
    total = 0
    for seg in line.lead + line.body:
        if seg.t == "text":
            total += len(seg.v or "")
        elif seg.t == "lex":
            total += len(LEXICON.get(seg.id or "", ""))
        elif seg.t == "rel":
            total += 8
        else:
            fact = pack.get(seg.id or "")
            total += _RENDER_WIDTH.get(fact.unit, len(str(fact.value))) if fact else 0
    return total + 1


def _strip(text: str, phrase: str | None) -> str:
    return re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE) if phrase else text


def verify_lines(
    lines: list[Line], request: dict[str, Any], pack: FactPack
) -> tuple[list[CheckedLine], list[LineRejection]]:
    """Each line -> kept with the tone code set, or rejected naming the rule."""
    selected = select(pack, TAB_SURFACE[request["tab"]])
    candidates = {c["slot"]: c for c in request["candidates"]}
    insufficient_facts = {
        fid for item in request["insufficient"] for fid in (item.get("facts") or {}).values()
    }
    kept: list[CheckedLine] = []
    rejected: list[LineRejection] = []
    seen: set[str] = set()

    for line in lines:
        failure = _check(line, request, candidates, insufficient_facts, selected, seen)
        if failure is not None:
            rejected.append(LineRejection(failure[0], line.slot, failure[1]))
            continue
        seen.add(line.slot)
        tone = (
            candidates[line.slot]["tone"]
            if line.slot in candidates
            else "insufficient_data"
            if line.slot == INSUFFICIENT
            else "neutral"
        )
        kept.append(CheckedLine(line, tone))

    findings = [c for c in kept if c.line.slot != INSUFFICIENT]
    if len(findings) > request["max_lines"]:
        for extra in findings[request["max_lines"] :]:
            kept.remove(extra)
            rejected.append(LineRejection("V8", extra.line.slot, "more lines than max_lines"))
    return kept, rejected


def _check(line, request, candidates, insufficient_facts, selected, seen):
    slot = line.slot
    if request["tab"] == "insights" and slot not in candidates:
        return "V8", "the explanation table only accepts supplied findings"
    if slot in seen:
        return "V8", "the same slot appears twice"
    if slot not in candidates and slot not in (OBS, INSUFFICIENT):
        return "V8", f"unknown slot {slot!r}"
    if slot == INSUFFICIENT and not request["insufficient"]:
        return "V8", "an INSUFFICIENT line with nothing insufficient on this tab"
    candidate = candidates.get(slot)
    segments = line.lead + line.body

    # V1: everything named exists, in the tab's selection.
    for seg in segments:
        if seg.t == "fact" and (seg.id not in selected):
            return "V1", f"fact {seg.id!r} is not citable on this tab"
        if seg.t == "lex" and seg.id not in LEXICON:
            return "V1", f"unknown phrase {seg.id!r}"
        if seg.t == "rel" and slot != OBS:
            return "V1", "a relation word in a line that declares no relation"
    if any(s.t == "fact" for s in line.lead):
        return "V8", "a figure in the lead"

    # V8: shape.
    if _words(line.lead) > LEAD_WORDS:
        return "V8", f"lead longer than {LEAD_WORDS} words"
    if _words(line.body) > BODY_WORDS:
        return "V8", f"body longer than {BODY_WORDS} words"
    if _rendered_chars(line, selected) > MAX_CHARS:
        return "V8", f"line longer than {MAX_CHARS} characters"
    body_facts = {s.id for s in line.body if s.t == "fact"}
    fact_ids = {s.id for s in segments if s.t == "fact"}
    if candidate is not None:
        missing = [f for f in candidate["must_cite"] if f not in body_facts]
        if missing:
            return "V8", f"must-cite facts missing from the body: {missing}"
        allowed = set(candidate["facts"].values())
        stray = [f for f in fact_ids if f not in allowed and not f.startswith("coverage.")]
        if stray:
            return "V5", f"cites facts outside this finding: {sorted(stray)}"
        if candidate.get("coverage_fact") and candidate["coverage_fact"] not in body_facts:
            return "V6", "thin coverage not shown"
    if slot == INSUFFICIENT:
        stray = [f for f in fact_ids if f not in insufficient_facts]
        if stray:
            return "V5", f"an INSUFFICIENT line cites measured facts: {sorted(stray)}"
    if slot == OBS:
        if line.relation not in RELATIONS:
            return "V8", "an OBS line must declare a relation"

    # V5: declared evidence.
    undeclared = sorted(f for f in fact_ids if f not in line.cites)
    if undeclared:
        return "V5", f"renders facts it does not cite: {undeclared}"

    # Text rules. The candidate's own magnitude phrase is the one quantity word allowed.
    magnitude = candidate.get("magnitude_word") if candidate else None
    raw = _text(segments)
    # "as much as" / "as many as" compare to a figure that is shown; they are not sizes.
    text = re.sub(r"\bas (?:much|many) as\b", " ", _strip(raw, magnitude), flags=re.IGNORECASE)
    if _DIGIT.search(raw):
        return "V2", "a digit in model-written text"
    if found := _QUANTITY_WORDS.search(text) or _TIME_QUANTITY.search(text):
        return "V3", f"a quantity in words: {found.group(0)!r}"
    if found := _ADVICE.search(raw):
        return "V7", f"recommendation language: {found.group(0)!r}"
    if found := _FORECAST.search(raw):
        return "V10", f"prediction language: {found.group(0)!r}"
    if found := _JARGON.search(raw):
        return "V11", f"jargon in Basic: {found.group(0)!r}"

    # V9: direction and size words belong to code.
    allowed_words = (
        " ".join(candidate.get("allowed_words") or [])
        if candidate
        # "{n} more trading days" -- the gap is a rendered fact.
        else "more"
        if slot == INSUFFICIENT
        else ""
    )
    for found in _COMPARATIVES.finditer(text):
        if not re.search(rf"\b{re.escape(found.group(0))}\b", allowed_words, re.IGNORECASE):
            return "V9", f"comparative {found.group(0)!r} not allowed for this finding"
    for word in (candidate.get("forbidden_words") or []) if candidate else []:
        if re.search(rf"\b{re.escape(word)}\b", raw, re.IGNORECASE):
            return "V9", f"forbidden word {word!r} for this finding"
    if found := _SIZE_WORDS.search(text):
        return "V9", f"size word {found.group(0)!r} not the one code chose"

    # V12: an implication only where permitted.
    kind = (candidate or {}).get("implication", {}).get("kind", "NONE") if candidate else "NONE"
    if kind == "NONE" and (found := _IMPLICATION.search(raw)):
        return "V12", f"implication {found.group(0)!r} where none is allowed"

    # V13: wording may not contradict the tone.
    tone = candidate["tone"] if candidate else None
    if tone in ("needs_attention", "watch") and (found := _REASSURING.search(raw)):
        return "V13", f"reassuring word {found.group(0)!r} on a {tone} finding"
    if tone == "positive" and (found := _ALARM.search(raw)):
        return "V13", f"alarm word {found.group(0)!r} on a positive finding"

    # V4 (and V1-V7 again, by the existing verifier) for the model's own observations.
    if slot == OBS:
        claim = Claim(relation=line.relation, args=line.args, segments=segments, cites=line.cites)
        result = verify([claim], selected)
        if result.rejections:
            r = result.rejections[0]
            return r.rule, r.reason
    return None


# --- rendering ------------------------------------------------------------------------------


def _render_part(claim: Claim, segments: tuple[Segment, ...], pack: FactPack) -> list:
    tokens = render(
        Claim(claim.relation, claim.args, segments, claim.cites, outcome=claim.outcome), pack
    )
    return space_tokens(tokens)


def space_tokens(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Models often omit boundary spaces in otherwise valid segment arrays. Keep every
    # word and fact intact while preventing "IGILhas25.3%of" in React's token renderer.
    spaced = []
    previous = ""
    for token in tokens:
        text = str(token.get("v", "") if token["t"] == "text" else token.get("value", ""))
        if (
            previous
            and text
            and not previous[-1].isspace()
            and not text[0].isspace()
            and previous[-1] not in "([{"
            and text[0] not in ".,;:!?%)]}’'×"
        ):
            spaced.append({"t": "text", "v": " "})
        spaced.append(token)
        if text:
            previous = text
    return spaced


def render_lines(checked: list[CheckedLine], pack: FactPack) -> list[dict[str, Any]]:
    """Kept lines -> token lists the client renders (formatting and privacy masking stay
    in the browser, as in ``render.py``). Findings first in request order, the
    INSUFFICIENT line last."""
    out = []
    ordered = [c for c in checked if c.line.slot != INSUFFICIENT] + [
        c for c in checked if c.line.slot == INSUFFICIENT
    ]
    for item in ordered:
        line = item.line
        claim = Claim(line.relation or "DEFINITION", line.args, line.lead + line.body, line.cites)
        if line.slot == OBS:
            # The relation word comes from how the relation evaluated, over the live pack.
            result = verify([claim], pack)
            if result.claims:
                claim = result.claims[0]
        out.append(
            {
                "slot": line.slot,
                "tone": item.tone,
                "lead": _render_part(claim, line.lead, pack),
                "body": _render_part(claim, line.body, pack),
                "cites": list(line.cites),
            }
        )
    return out


def lines_to_json(lines: list[Line]) -> list[dict[str, Any]]:
    def seg(s: Segment) -> dict[str, Any]:
        return {k: v for k, v in (("t", s.t), ("v", s.v), ("id", s.id)) if v is not None}

    return [
        {
            "slot": ln.slot,
            "relation": ln.relation,
            "args": ln.args,
            "lead": [seg(s) for s in ln.lead],
            "body": [seg(s) for s in ln.body],
            "cites": list(ln.cites),
        }
        for ln in lines
    ]


def lines_from_json(rows: list[dict[str, Any]]) -> list[Line]:
    return [_line(row) for row in rows]
