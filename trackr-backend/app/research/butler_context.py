"""Authorized retrieval and bounded, sealed Butler evidence packets (BT 03).

docs/BUTLER_ARCHITECTURE.md sections 3-5; schema/butler_context.schema.json.

ORDER OF OPERATIONS IS THE SECURITY PROPERTY. By the time ``gather`` runs, the router has
authenticated the caller, checked Workspace capability and investor ownership, and opened a
connection whose RLS scope is that one investor. ``gather`` then applies, BEFORE reading any
content: the company's ritual gate (``require_unlocked``), the forgotten-memory tombstones,
source retractions and the known-at cutoff. Nothing filtered out is read into memory at all,
so nothing filtered out can leak into a packet by a later bug.

WHAT A PACKET CONTAINS. Structured JSON plus SELECTED ORIGINAL TEXT: exact substrings of saved
notes, thesis fields, answers and chat turns, each with its locator and hash. Numbers are
decimal strings from the stored records (counts from SQL, facts as recorded, evaluation states
from the deterministic engine) -- a model is never asked to calculate. Source documents appear
as metadata observations; their retained TEXT is not disclosed because no source type has a
recorded model-disclosure verdict (BE-Q04), and the packet says so instead of pretending.

COUNTEREVIDENCE IS NOT OPTIONAL. The anti-thesis and every note the investor linked as
CONTRADICTS are CONTRARY excerpts and are packed with the same priority as supporting ones.
When the budget cannot hold them, the packet is marked incomplete
(``COUNTEREVIDENCE_DID_NOT_FIT``) and a caller must narrow or abstain -- a supportive-only
packet is never produced silently.

THE MODEL-FACING PACKET NEVER CARRIES A DATABASE ID. Local refs (b1, x1, o1, r1) resolve
through ``authority``, which stays on the server. Sealing to a task happens at BE 20
integration; before that, ``digest`` binds a packet to exactly the records it was built from,
and rebuilding with the same cutoff reproduces it only while every record is unchanged.

No embeddings, no vector store, no network, no model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.db import butler as bd
from app.db.workspace import _rows, identity, isin_family, require_unlocked
from app.research import butler_policy as bp
from app.research import canonical

INTENTS = ("review_evidence", "new_material", "checkpoint", "decision_review")

# Priorities: lower packs first. CONTRARY shares its tier with SUPPORTING and they are
# interleaved, so neither side can crowd the other out.
_TIER_QUESTION = 0
_TIER_THESIS = 1
_TIER_LINKED = 2
_TIER_MEMORY = 3


@dataclass(frozen=True)
class AuthorityRef:
    """Server-side resolution of one packet-local ref. Never serialised to a model."""

    kind: str  # NOTE | THESIS | ANSWER | CHAT_MESSAGE | SOURCE | FACT | EVALUATION | ACTIVITY
    record_id: str
    isin: str
    field: str | None = None
    #: sha256 of the WHOLE field text the excerpt was cut from, for re-verification.
    field_hash: str | None = None


@dataclass
class SealedPacket:
    packet: dict[str, Any]
    authority: dict[str, AuthorityRef]
    investor_id: str
    digest: str
    sealed_at: datetime
    expires_at: datetime
    complete: bool
    versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """One excerpt candidate before budgeting."""

    kind: str
    record_id: str
    isin: str
    field: str
    full_text: str
    trust: str
    stance: str
    tier: int
    recorded_at: datetime
    published_on: Any = None
    belief: str | None = None  # author_kind when this is a belief_ref
    record_kind: str | None = None
    remembered: bool = False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cut(text: str) -> tuple[str, int, int, bool]:
    """An exact prefix substring within the excerpt limit. Truncation is flagged, never hidden."""
    if len(text) <= bp.MAX_EXCERPT_CHARS:
        return text, 0, len(text), False
    return text[: bp.MAX_EXCERPT_CHARS], 0, bp.MAX_EXCERPT_CHARS, True


def _token(value: str, pattern: str, limit: int) -> str:
    """Reduce a stored label to the packet's closed character set. The authority map keeps the
    record id, so the renderer always re-reads the original label; this is only model-facing."""
    cleaned = re.sub(pattern, "_", value)[:limit].strip() or "unspecified"
    return cleaned


def _decimal_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite value cannot enter a packet")
        return format(value.normalize(), "f")
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"refusing to put {type(value).__name__} into a packet as a number")


# =========================================================================================
# retrieval -- filtered BEFORE content is read
# =========================================================================================


def gather(
    conn: Any, investor_id: str, isin: str, as_of: datetime
) -> dict[str, Any]:
    """Every record a packet for this company may draw on, already filtered.

    Raises 404/409 before reading content if the company is not linked or is ritual-locked.
    """
    isin = require_unlocked(conn, investor_id, isin)
    family = isin_family(conn, isin)
    forgotten = bd.forgotten_refs(conn, investor_id)
    params = {"inv": investor_id, "fam": family, "cut": as_of}

    thesis = _rows(
        conn,
        """
        SELECT id, isin, version, status, argument, anti_thesis, intended_holding_period,
               version_on, recorded_at
          FROM thesis WHERE investor_id = %(inv)s AND isin = ANY(%(fam)s::text[])
           AND recorded_at <= %(cut)s
         ORDER BY version DESC LIMIT 1
        """,
        params,
    )
    questions = _rows(
        conn,
        """
        SELECT q.id, n.id AS note_id, n.isin, n.title, n.recorded_at, q.blocking
          FROM question q JOIN note n ON n.investor_id = q.investor_id AND n.id = q.note_id
         WHERE q.investor_id = %(inv)s AND q.isin = ANY(%(fam)s::text[])
           AND q.recorded_at <= %(cut)s
           AND NOT EXISTS (SELECT 1 FROM question_answer a WHERE a.investor_id = q.investor_id
                            AND a.question_id = q.id AND a.recorded_at <= %(cut)s)
         ORDER BY q.blocking DESC, n.seq LIMIT 8
        """,
        params,
    )
    # Notes the investor explicitly linked to the thesis or to a remembered note. The relation
    # is theirs; a model never decides what supports or contradicts what.
    linked = _rows(
        conn,
        """
        SELECT DISTINCT ON (n.id) n.id, n.isin, n.title, n.body, n.authorship, n.kind,
               n.recorded_at, l.relation
          FROM note_link l
          JOIN note n ON n.investor_id = l.investor_id AND n.id = l.from_note_id
         WHERE l.investor_id = %(inv)s AND n.isin = ANY(%(fam)s::text[])
           AND l.relation IN ('SUPPORTS','CONTRADICTS')
           AND l.recorded_at <= %(cut)s AND n.recorded_at <= %(cut)s
         ORDER BY n.id, l.recorded_at DESC
        """,
        params,
    )
    memories = _rows(
        conn,
        """
        SELECT m.ref_kind, m.note_id, m.answer_id, m.thesis_id, m.chat_message_id
          FROM butler_memory m
         WHERE m.investor_id = %(inv)s AND m.isin = ANY(%(fam)s::text[])
           AND m.recorded_at <= %(cut)s
           AND NOT EXISTS (SELECT 1 FROM butler_memory_event e
                            WHERE e.investor_id = m.investor_id AND e.memory_id = m.id
                              AND e.recorded_at <= %(cut)s)
        """,
        params,
    )
    refs = [(m["ref_kind"], bd._ref_id(m)) for m in memories]
    remembered = {k: v for k, v in bd._referenced_records(conn, investor_id, refs).items()
                  if v["recorded_at"] <= as_of and k not in forgotten}
    sources = _rows(
        conn,
        """
        SELECT s.id, s.isin, s.source, s.source_type, s.publication_date, s.retrieved_at,
               s.recorded_at, e.status AS extraction_status
          FROM research_source s
          LEFT JOIN research_source_extraction e
            ON e.investor_id = s.investor_id AND e.source_id = s.id
         WHERE s.investor_id = %(inv)s AND s.isin = ANY(%(fam)s::text[])
           AND s.recorded_at <= %(cut)s AND s.retrieved_at <= %(cut)s
           AND NOT EXISTS (SELECT 1 FROM butler_source_retraction r
                            WHERE r.investor_id = s.investor_id AND r.source_id = s.id
                              AND r.recorded_at <= %(cut)s)
         ORDER BY s.publication_date DESC, s.recorded_at DESC LIMIT 8
        """,
        params,
    )
    retracted_count = _rows(
        conn,
        "SELECT count(*) AS n FROM butler_source_retraction WHERE investor_id = %(inv)s "
        "AND isin = ANY(%(fam)s::text[]) AND recorded_at <= %(cut)s",
        params,
    )[0]["n"]
    facts = _rows(
        conn,
        """
        SELECT f.id, f.isin, f.source_id, f.metric, f.basis, f.unit, f.period_start,
               f.period_end, f.cadence, f.numeric_value, f.boolean_value, f.verification,
               f.recorded_at
          FROM research_fact f
         WHERE f.investor_id = %(inv)s AND f.isin = ANY(%(fam)s::text[])
           AND f.recorded_at <= %(cut)s AND f.text_value IS NULL
           AND NOT EXISTS (SELECT 1 FROM research_fact s WHERE s.investor_id = f.investor_id
                            AND s.supersedes_id = f.id AND s.recorded_at <= %(cut)s)
           AND NOT EXISTS (SELECT 1 FROM butler_source_retraction r
                            WHERE r.investor_id = f.investor_id AND r.source_id = f.source_id
                              AND r.recorded_at <= %(cut)s)
         ORDER BY f.period_end DESC, f.seq DESC LIMIT 12
        """,
        params,
    )
    evaluation = []
    if thesis:
        evaluation = _rows(
            conn,
            "SELECT id, state, evidence_cutoff, evaluated_at FROM trigger_evaluation "
            "WHERE investor_id = %(inv)s AND thesis_id = %(t)s AND evaluated_at <= %(cut)s "
            "ORDER BY evaluated_at DESC LIMIT 1",
            {**params, "t": thesis[0]["id"]},
        )
    window_start = as_of - timedelta(days=30)
    activity = bd.activity_counts(conn, investor_id, family, window_start, as_of)
    return {
        "isin": isin,
        "identity": identity(conn, isin),
        "thesis": thesis[0] if thesis and ("THESIS", thesis[0]["id"]) not in forgotten else None,
        "thesis_forgotten": bool(thesis) and ("THESIS", thesis[0]["id"]) in forgotten,
        "questions": [q for q in questions if ("NOTE", q["note_id"]) not in forgotten],
        "linked": [n for n in linked if ("NOTE", n["id"]) not in forgotten],
        "remembered": remembered,
        "sources": sources,
        "facts": facts,
        "evaluation": evaluation[0] if evaluation else None,
        "activity": activity,
        "activity_window": (window_start, as_of),
        "forgotten_count": len(forgotten),
        "retracted_count": int(retracted_count),
    }


# =========================================================================================
# assembly -- pure
# =========================================================================================


def _candidates(g: dict[str, Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for q in g["questions"]:
        out.append(Candidate("NOTE", q["note_id"], q["isin"], "title", q["title"],
                             "USER_STATEMENT", "UNLINKED", _TIER_QUESTION, q["recorded_at"],
                             record_kind="note"))
    t = g["thesis"]
    if t:
        if (t["argument"] or "").strip():
            out.append(Candidate("THESIS", t["id"], t["isin"], "argument", t["argument"],
                                 "USER_HYPOTHESIS", "SUPPORTING", _TIER_THESIS, t["recorded_at"],
                                 belief="user", record_kind="thesis"))
        if (t["anti_thesis"] or "").strip():
            out.append(Candidate("THESIS", t["id"], t["isin"], "anti_thesis", t["anti_thesis"],
                                 "USER_HYPOTHESIS", "CONTRARY", _TIER_THESIS, t["recorded_at"],
                                 record_kind="thesis"))
        if (t["intended_holding_period"] or "").strip():
            out.append(Candidate("THESIS", t["id"], t["isin"], "intended_holding_period",
                                 t["intended_holding_period"], "USER_STATEMENT", "UNLINKED",
                                 _TIER_THESIS, t["recorded_at"], record_kind="thesis"))
    for n in g["linked"]:
        trust = ("ACCEPTED_ASSISTANT_TEXT" if n["authorship"] == "ACCEPTED_AGENT"
                 else "USER_HYPOTHESIS" if n["kind"] in ("INTERPRETATION", "THESIS_NOTE")
                 else "USER_STATEMENT")
        stance = "CONTRARY" if n["relation"] == "CONTRADICTS" else "SUPPORTING"
        out.append(Candidate("NOTE", n["id"], n["isin"], "body", n["body"] or n["title"], trust,
                             stance, _TIER_LINKED, n["recorded_at"], record_kind="note"))
    for (kind, rid), rec in sorted(g["remembered"].items(), key=lambda kv: kv[1]["recorded_at"]):
        if kind == "THESIS" and t and rid == t["id"]:
            continue
        if not (rec["text"] or "").strip():
            continue
        author = {"ACCEPTED_ASSISTANT_TEXT": "accepted_assistant",
                  "ASSISTANT_SUGGESTION": "assistant"}.get(rec["attribution"], "user")
        trust = {"ACCEPTED_ASSISTANT_TEXT": "ACCEPTED_ASSISTANT_TEXT",
                 "ASSISTANT_SUGGESTION": "ASSISTANT_SUGGESTION",
                 "USER_HYPOTHESIS": "USER_HYPOTHESIS", "USER_BELIEF": "USER_HYPOTHESIS"}.get(
                     rec["attribution"], "USER_STATEMENT")
        record_kind = {"NOTE": "note", "ANSWER": "answer", "THESIS": "thesis",
                       "CHAT_MESSAGE": "chat"}[kind]
        field_name = {"NOTE": "body", "ANSWER": "answer", "THESIS": "argument",
                      "CHAT_MESSAGE": "text"}[kind]
        out.append(Candidate(kind, rid, rec["isin"], field_name, rec["text"], trust, "UNLINKED",
                             _TIER_MEMORY, rec["recorded_at"], belief=author,
                             record_kind=record_kind, remembered=True))
    return out


def _pack(candidates: list[Candidate], budget: int) -> tuple[list[Candidate], bool, bool, bool]:
    """Choose excerpts within the budget. Returns (chosen, truncated, contrary_dropped, partial).

    Within a tier CONTRARY and SUPPORTING alternate; a CONTRARY excerpt that does not fit marks
    the packet incomplete rather than letting supporting text take its place.
    """
    chosen: list[Candidate] = []
    used = 0
    truncated = contrary_dropped = partial = False
    by_tier: dict[int, list[Candidate]] = {}
    for c in candidates:
        by_tier.setdefault(c.tier, []).append(c)
    for tier in sorted(by_tier):
        contra = [c for c in by_tier[tier] if c.stance == "CONTRARY"]
        other = [c for c in by_tier[tier] if c.stance != "CONTRARY"]
        order: list[Candidate] = []
        while contra or other:
            if contra:
                order.append(contra.pop(0))
            if other:
                order.append(other.pop(0))
        for c in order:
            text, _, _, cut = _cut(c.full_text)
            cost = len(text) + 200  # metadata overhead, conservatively
            if len(chosen) >= bp.MAX_EXCERPTS or used + cost > budget:
                partial = True
                if c.stance == "CONTRARY":
                    contrary_dropped = True
                continue
            chosen.append(c)
            used += cost
            truncated = truncated or cut
    return chosen, truncated, contrary_dropped, partial


def assemble(
    investor_id: str, g: dict[str, Any], as_of: datetime, intent: str,
    budget: int = bp.MAX_PACKET_CHARS, lessons_enabled: bool = True,
) -> SealedPacket:
    """Build the butler.context.v1 packet and its server-side authority map. Pure."""
    if intent not in INTENTS:
        raise ValueError(f"unknown intent {intent!r}")
    authority: dict[str, AuthorityRef] = {}
    record_refs: dict[tuple[str, str, str | None], str] = {}

    def rref(kind: str, rid: str, isin: str, field_name: str | None = None,
             full: str | None = None) -> str:
        key = (kind, rid, field_name)
        if key not in record_refs:
            ref = f"r{len(record_refs) + 1}"
            record_refs[key] = ref
            authority[ref] = AuthorityRef(kind, rid, isin, field_name,
                                          _sha(full) if full is not None else None)
        return record_refs[key]

    chosen, truncated, contrary_dropped, partial = _pack(_candidates(g), budget)
    excerpts, beliefs = [], []
    for i, c in enumerate(chosen, start=1):
        text, start, end, _ = _cut(c.full_text)
        xid = f"x{i}"
        excerpts.append({
            "id": xid,
            "source_version_id": rref(c.kind, c.record_id, c.isin, c.field, c.full_text),
            "locator": {"start": start, "end": end},
            "text": text,
            "content_hash": _sha(text),
            "trust": c.trust,
            "stance": c.stance,
            "recorded_at": c.recorded_at.astimezone(UTC).isoformat(),
            "published_on": None,
        })
        authority[xid] = AuthorityRef(c.kind, c.record_id, c.isin, c.field, _sha(c.full_text))
        if c.belief:
            beliefs.append({
                "id": f"b{len(beliefs) + 1}",
                "author_kind": c.belief,
                "record_kind": c.record_kind,
                "recorded_at": c.recorded_at.astimezone(UTC).isoformat(),
                "excerpt_id": xid,
                "explicitly_remembered": c.remembered,
            })

    observations: list[dict[str, Any]] = []

    def observe(kind: str, metric: str, value: str, unit: str, basis: str, verification: str,
                prov: str, known_at: datetime, window: tuple[datetime, datetime] | None = None,
                period: tuple[Any, Any] | None = None) -> None:
        if len(observations) >= bp.MAX_OBSERVATIONS:
            return
        oid = f"o{len(observations) + 1}"
        observations.append({
            "id": oid, "kind": kind, "metric_id": metric, "value": value, "unit": unit,
            "basis": basis,
            "window_start": window[0].astimezone(UTC).isoformat() if window else None,
            "window_end": window[1].astimezone(UTC).isoformat() if window else None,
            "period_start": period[0].isoformat() if period else None,
            "period_end": period[1].isoformat() if period else None,
            "verification": verification, "provenance_ref": prov,
            "known_at": known_at.astimezone(UTC).isoformat(),
        })
        authority[oid] = authority[prov]

    window = g["activity_window"]
    act_ref = rref("ACTIVITY", f"activity:{g['isin']}", g["isin"])
    for metric in ("notes_saved", "evidence_linked", "answers_recorded", "open_questions"):
        observe("recorded_activity", metric, str(int(g["activity"].get(metric, 0))), "count",
                "saved_workspace_records", "RECORDED", act_ref, window[1], window=window)
    for s in g["sources"]:
        observe("source_available", "source_saved", s["source_type"], "document",
                "saved_workspace_records", "RECORDED", rref("SOURCE", s["id"], s["isin"]),
                s["recorded_at"], period=(s["publication_date"], s["publication_date"]))
    for f in g["facts"]:
        value = f["numeric_value"] if f["numeric_value"] is not None else f["boolean_value"]
        observe("sourced_fact", _token(f["metric"].lower(), r"[^a-z0-9_.:-]", 64),
                _decimal_string(value), _token(f["unit"], r"[^A-Za-z0-9_%/ .-]", 32),
                _token(f"{f['basis']}:{f['cadence']}", r"[^A-Za-z0-9_ .:-]", 64),
                f["verification"],
                rref("FACT", f["id"], f["isin"]), f["recorded_at"],
                period=(f["period_start"], f["period_end"]))
    if g["evaluation"]:
        e = g["evaluation"]
        observe("evaluation_state", "thesis_rules", e["state"], "state", "deterministic_engine",
                "ENGINE", rref("EVALUATION", e["id"], g["isin"]), e["evaluated_at"])

    limitations = ["OUTSIDE_WORKSPACE_RESEARCH_UNKNOWN", "NO_EXTERNAL_FEED_CONFIGURED",
                   "PRICE_NOT_INCLUDED", "TIME_SPENT_NOT_TRACKED"]
    if g["sources"]:
        limitations.append("SOURCE_TEXT_NOT_DISCLOSED")
    if truncated or partial:
        limitations.append("TRUNCATED_TO_BUDGET")
    if contrary_dropped:
        limitations.append("COUNTEREVIDENCE_DID_NOT_FIT")
    if g["retracted_count"]:
        limitations.append("RETRACTED_SOURCE_EXCLUDED")
    if g["forgotten_count"]:
        limitations.append("FORGOTTEN_MEMORY_EXCLUDED")
    if g["thesis"] is None:
        limitations.append("NO_ACTIVE_THESIS")

    available = {bp.REQUIRES_COVERAGE_GAP}
    if beliefs:
        available.add(bp.REQUIRES_BELIEF)
    if any(c.field == "intended_holding_period" for c in chosen):
        available.add(bp.REQUIRES_HORIZON)
    if len(excerpts) >= 2:
        available.add(bp.REQUIRES_PAIR)
    # BT 09: offer only the lessons whose conditions this packet's own records meet, and none
    # when the person turned lessons off. Never inferred from tone or sentiment.
    lesson_available: set[str] = set()
    activity = g["activity"]
    if observations:
        lesson_available.add(bp.LESSON_ACTIVITY)
    sourced = sum(1 for o in observations if o["kind"] in ("sourced_fact", "evaluation_state"))
    if len(excerpts) >= 2 or sourced >= 2:
        lesson_available.add(bp.LESSON_TWO_SIGNALS)
    if any(int(activity.get(m, 0)) > 0 for m in ("notes_saved", "answers_recorded",
                                                  "evidence_linked")):
        lesson_available.add(bp.LESSON_PRIOR_EFFORT)
    allowed_lessons = ([x for x in bp.LESSONS if bp.lesson_allowed(x, lesson_available)]
                       if lessons_enabled else [])
    ident = g["identity"]
    packet: dict[str, Any] = {
        "schema_version": bp.CONTEXT_SCHEMA,
        "packet_id": "",
        "as_of": as_of.astimezone(UTC).isoformat(),
        "intent": intent,
        "subject": {"security_isin": g["isin"], "symbol": ident.get("symbol"),
                    "name": ident.get("name")},
        "belief_refs": beliefs,
        "excerpts": excerpts,
        "observations": observations,
        "coverage": {
            "saved_workspace_records": "PARTIAL" if partial else "COMPLETE_FOR_QUERY",
            "external_sources": "NOT_CHECKED",
            "source_text": "NOT_DISCLOSED",
            "truncated": truncated or partial,
            "limitations": limitations,
        },
        "allowed_question_ids": [q for q in bp.QUESTIONS if bp.question_allowed(q, available)],
        "allowed_lesson_ids": allowed_lessons,
        "allowed_output": bp.PROPOSAL_SCHEMA,
    }
    body = dict(packet)
    body.pop("packet_id")
    digest = canonical.digest({
        "investor": investor_id, "packet": body,
        "authority": {k: [v.kind, v.record_id, v.field, v.field_hash]
                      for k, v in sorted(authority.items())},
        "catalogue": bp.CATALOGUE_VERSION,
    })
    packet["packet_id"] = f"packet-{digest[:24]}"
    now = datetime.now(UTC)
    return SealedPacket(
        packet=packet, authority=authority, investor_id=investor_id, digest=digest,
        sealed_at=now, expires_at=now + bp.PACKET_TTL, complete=not contrary_dropped,
        versions={"context": bp.CONTEXT_SCHEMA, "catalogue": bp.CATALOGUE_VERSION,
                  "rules": bp.RULE_VERSION},
    )


def build_packet(
    conn: Any, investor_id: str, isin: str, as_of: datetime, intent: str,
    budget: int = bp.MAX_PACKET_CHARS, lessons_enabled: bool = True,
) -> SealedPacket:
    """Authorized retrieval, then assembly. The connection's scope is the authority."""
    return assemble(investor_id, gather(conn, investor_id, isin, as_of), as_of, intent, budget,
                    lessons_enabled)


def packet_size(packet: dict[str, Any]) -> int:
    return len(canonical.canonical_json(packet))
