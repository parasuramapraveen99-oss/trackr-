"""The LLM step of rule-20 research: read fetched exchange documents, return JSON terms.

The model never searches and never decides. It is handed (1) the system's research
request -- what Trackr+ could not compute and what kind of event would explain it -- and
(2) the exchange documents ``app.ingest.exa_search`` fetched, and asked to fill a fixed
JSON shape with what those documents literally say, including a verbatim quote.
Everything it returns is then checked deterministically by
``app.engine.gap_resolution.check_event_against_document`` (and the price and ledger checks
after it); an unverifiable answer is simply discarded. Public data only: company name,
ISIN, symbol, dates and exchange text -- no investor data is sent.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.gap_resolution import EVENT_TYPES, ResearchedEvent

SYSTEM_PROMPT = """You extract Indian stock corporate-action terms from exchange documents.
You are given a RESEARCH REQUEST (JSON) and numbered DOCUMENTS from nseindia.com or
bseindia.com. Report ONLY events a document explicitly states for the requested company.
Never infer, compute or guess a ratio, face value, ISIN or date. If no document states it,
return an empty list.

Return exactly this JSON object:
{"events": [{
  "type": "BONUS" | "SPLIT" | "ISIN_CHANGE",
  "doc": <document number>,
  "quote": "<the exact sentence(s) from that document stating the terms, copied verbatim>",
  "ex_date": "YYYY-MM-DD" | null,     // ex-date, else record date; null if neither stated
  "bonus_new": <int>, "bonus_for": <int>,          // BONUS only: bonus_new for every bonus_for held
  "fv_before": "<number>", "fv_after": "<number>", // SPLIT only: face value before and after
  "old_isin": "<ISIN>", "new_isin": "<ISIN>"       // ISIN_CHANGE (and SPLIT if it changed the ISIN)
}]}
Omit fields that do not apply. The quote must contain every number you report."""


def build_user_content(request: dict[str, Any], documents: list[Any]) -> str:
    docs = "\n\n".join(
        f"[DOCUMENT {i}] {d.url}\n{d.text}" for i, d in enumerate(documents, start=1)
    )
    return f"RESEARCH REQUEST:\n{json.dumps(request, default=str)}\n\nDOCUMENTS:\n{docs}"


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_events(payload: dict[str, Any], documents: list[Any]) -> list[ResearchedEvent]:
    """The model's JSON -> events. Malformed entries are dropped, never repaired."""
    out: list[ResearchedEvent] = []
    for raw in payload.get("events") or []:
        if not isinstance(raw, dict) or raw.get("type") not in EVENT_TYPES:
            continue
        doc = _int(raw.get("doc"))
        if doc is None or not 1 <= doc <= len(documents):
            continue
        try:
            ex_date: date | None = date.fromisoformat(str(raw.get("ex_date")))
        except ValueError:
            # No date in the document. A bonus/split may still be dated by the exchange's
            # own price data (``jump_for``); an ISIN change may not.
            if raw["type"] == "ISIN_CHANGE":
                continue
            ex_date = None
        out.append(
            ResearchedEvent(
                type=raw["type"],
                ex_date=ex_date,
                doc_url=documents[doc - 1].url,
                quote=str(raw.get("quote") or ""),
                bonus_new=_int(raw.get("bonus_new")),
                bonus_for=_int(raw.get("bonus_for")),
                fv_before=_dec(raw.get("fv_before")),
                fv_after=_dec(raw.get("fv_after")),
                old_isin=(str(raw["old_isin"]).upper() if raw.get("old_isin") else None),
                new_isin=(str(raw["new_isin"]).upper() if raw.get("new_isin") else None),
            )
        )
    return out


#: Words that mark a passage as possibly stating an event's terms. A document with none of
#: them is not sent to the model at all.
EVENT_WORDS = ("bonus", "sub-division", "subdivision", "sub division", "split", "isin")
#: Characters kept either side of each hit, and the cap per document.
EXCERPT_RADIUS = 700
EXCERPT_CAP = 6000
#: Total excerpt characters in the ONE extraction call a gap gets. OpenRouter refused ten
#: FULL filings in one request (HTTP 413, seen live 2026-10-08); excerpts are a fraction of
#: that, and this cap keeps the batch well under it. A document that would cross the cap
#: is left out rather than truncated, so every quote still comes from a whole excerpt.
BATCH_CAP = 30000


def excerpt(text: str) -> str:
    """The passages around event words, in document order, joined by a marker. Each passage
    is a verbatim slice of ``text``, so a quote copied from it is still found by the
    verifier, which always checks against the FULL fetched document."""
    lower = text.lower()
    spans: list[list[int]] = []
    for word in EVENT_WORDS:
        start = 0
        while (i := lower.find(word, start)) != -1:
            spans.append(
                [max(0, i - EXCERPT_RADIUS), min(len(text), i + len(word) + EXCERPT_RADIUS)]
            )
            start = i + len(word)
    spans.sort()
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return "\n[...]\n".join(text[lo:hi] for lo, hi in merged)[:EXCERPT_CAP]


def extract_events(
    llm: Any, request: dict[str, Any], documents: list[Any]
) -> list[ResearchedEvent]:
    """One LLM call for all of a gap's documents -- not one per document, which spent a
    free model's whole daily quota on a single account (52 calls for 9 gaps, 2026-10-10)."""
    shown, used = [], 0
    for doc in documents:
        passages = excerpt(doc.text)
        if not passages or used + len(passages) > BATCH_CAP:
            continue
        shown.append(replace(doc, text=passages))
        used += len(passages)
    if not shown:
        return []
    payload = llm.complete_json(SYSTEM_PROMPT, build_user_content(request, shown))
    return parse_events(payload, shown)


REVIEW_PROMPT = """You audit one claimed Indian corporate action against ONE exchange document.
Answer only from the document. Confirm ONLY if the document explicitly states, for the
named company, every term in the CLAIM: the event type (a bonus and a share split are
different events), the ratio or face values, the ISINs if given, and a date within a week
of the claimed date (unless the CLAIM says the date is from exchange price data -- then
the document need not state a date, but must show the event was carried out, not merely
proposed). If anything is missing, different, about another company, merely
proposed and not approved, or later withdrawn, do not confirm.
Return exactly this JSON object: {"confirmed": true|false, "reason": "<one sentence>"}"""


def _claim(event: ResearchedEvent) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "type": event.type,
        "date": f"{event.ex_date} (from exchange price data; not stated in the document)"
        if event.dated_by_price
        else event.ex_date.isoformat(),
    }
    if event.type == "BONUS":
        claim["ratio"] = f"{event.bonus_new} new for every {event.bonus_for} held"
    elif event.type == "SPLIT":
        claim["face_value"] = f"{event.fv_before} -> {event.fv_after}"
    if event.old_isin or event.new_isin:
        claim["old_isin"], claim["new_isin"] = event.old_isin, event.new_isin
    return claim


def review_event(llm: Any, event: ResearchedEvent, document: Any, company: str) -> str | None:
    """A second, independent reading -- a VETO only. None when the reviewer explicitly
    confirms; otherwise the reason it did not. It can block a write the deterministic
    checks allowed; it can never allow one they refused, because it is only ever asked
    about events that already passed them."""
    content = (
        f"COMPANY: {company}\nCLAIM: {json.dumps(_claim(event))}\n\n"
        f"DOCUMENT {document.url}\n{excerpt(document.text)}"
    )
    payload = llm.complete_json(REVIEW_PROMPT, content)
    if isinstance(payload, dict) and payload.get("confirmed") is True:
        return None
    reason = payload.get("reason") if isinstance(payload, dict) else None
    return f"second reading did not confirm: {reason or 'no reason given'}"
