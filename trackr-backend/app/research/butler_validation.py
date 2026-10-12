"""Butler validation and the trusted card renderer (BT 04).

docs/BUTLER_ARCHITECTURE.md sections 4-5; docs/BUTLER_CHARACTER.md sections 6-7.

THE RULE THIS MODULE EXISTS TO ENFORCE: no unvalidated factual assertion reaches the trusted
Butler presentation path. Two inputs are rendered here and nothing else:

1. **Deterministic inbox items** (``render_item``) -- stored as template id + typed arguments,
   re-read against the CURRENT records, rights and tombstones every time they are shown.
2. **Model proposals** (``validate_proposal``) -- ids and enums only, checked against a sealed
   packet AND a packet rebuilt under the caller's current access. A proposal that fails any
   step is rejected WHOLE with fixed reason codes; nothing from it is shown, streamed or
   partially rendered. No model is connected in this phase (BT 08); this path exists so its
   contract is fixed and tested before one is.

Every displayed sentence comes from ``butler_policy`` templates. Values are identifiers, enums,
dates and decimal strings from stored records; quotes are exact substrings with their hashes.
Nothing here parses a number into a float, follows a URL, or executes text: a filing that says
"ignore previous instructions" is a quoted string like any other.

VALIDATION ORDER (section 5), fail closed at each step:
  1 access (router + ``current is None``)  2 binding/expiry/versions  3 strict shape and
  membership  4 excerpt text and hashes  5 observation eligibility  6 comparison compatibility
  7 freshness/coverage  8 question premises. A second model can never waive a failed step.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.research import butler_policy as bp
from app.research.butler_context import SealedPacket
from app.research.source_policy import ALLOWED, DENIED, USE_METADATA, SourcePolicy

# --- fixed reason codes -------------------------------------------------------------------
ACCESS_REVOKED = "ACCESS_REVOKED"
PACKET_MISMATCH = "PACKET_MISMATCH"
PACKET_EXPIRED = "PACKET_EXPIRED"
STALE_INPUT = "STALE_INPUT"
SCHEMA_INVALID = "SCHEMA_INVALID"
UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
EMPTY_SELECTION = "EMPTY_SELECTION"
ABSTAIN_WITH_SELECTION = "ABSTAIN_WITH_SELECTION"
QUESTION_NOT_ALLOWED = "QUESTION_NOT_ALLOWED"
LESSON_NOT_ALLOWED = "LESSON_NOT_ALLOWED"
PREMISE_UNSUPPORTED = "PREMISE_UNSUPPORTED"
HASH_MISMATCH = "HASH_MISMATCH"
OFFSET_INVALID = "OFFSET_INVALID"
INCOMPATIBLE_COMPARISON = "INCOMPATIBLE_COMPARISON"
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
SOURCE_RETRACTED = "SOURCE_RETRACTED"
SOURCE_RIGHTS_UNDECIDED = "SOURCE_RIGHTS_UNDECIDED"
SOURCE_RIGHTS_WITHDRAWN = "SOURCE_RIGHTS_WITHDRAWN"
MEMORY_FORGOTTEN = "MEMORY_FORGOTTEN"
RITUAL_REQUIRED = "RITUAL_REQUIRED"

#: What an invalidated card says instead of its content. Fixed text; no private input.
INVALID_TEXT = {
    SOURCE_RETRACTED: "The source this card cited was marked as retracted, corrected or a "
    "duplicate. Its details are withdrawn.",
    SOURCE_RIGHTS_UNDECIDED: "Use of this kind of source has not been approved yet, so its "
    "details are withheld.",
    SOURCE_RIGHTS_WITHDRAWN: "Use of this kind of source is no longer permitted, so its "
    "details are withheld.",
    EVIDENCE_UNAVAILABLE: "The records this card cited are no longer available.",
    MEMORY_FORGOTTEN: "You asked Butler to forget a record this card quoted.",
    RITUAL_REQUIRED: "Record your own questions for this company before its research is shown.",
    STALE_INPUT: "The record this card described no longer matches what is stored.",
}

_PACKET_ID = re.compile(r"^packet-[a-f0-9]{24}$")
_OBS_ID = re.compile(r"^o[0-9]{1,3}$")
_EXC_ID = re.compile(r"^x[0-9]{1,3}$")
_PROPOSAL_KEYS = frozenset({"schema_version", "packet_id", "observation_refs",
                            "comparison_candidates", "question_ids", "lesson_ids",
                            "abstain_code"})
_MAX_PROPOSAL_CHARS = 4096

#: Only an https URL with a plain host and path. No userinfo, query or fragment -- where
#: credentials and signatures live -- and no other scheme (javascript:, data:, http:, file:).
_SAFE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/%-]*)?$")


def safe_href(url: str | None) -> str | None:
    """A clickable link resolved from an AUTHORIZED STORED source, or None. A model-supplied
    URL never reaches this function: the proposal schema has no URL field at all."""
    if not url or not _SAFE_URL.match(url):
        return None
    return url


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _day(value: Any, tz: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(tz).date().isoformat() if tz else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _fill(template: str, args: dict[str, Any]) -> str:
    """Fill a reviewed template. Every placeholder must be supplied; nothing else is inserted."""
    names = set(re.findall(r"{([a-z_]+)}", template))
    missing = names - set(args)
    if missing:
        raise KeyError(f"template argument(s) missing: {sorted(missing)}")
    return template.format(**{k: str(v) for k, v in args.items() if k in names})


@dataclass
class ValidationOutcome:
    accepted: bool
    reason_codes: list[str] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    abstain_code: str | None = None
    coverage: dict[str, Any] | None = None


def _reject(*codes: str) -> ValidationOutcome:
    return ValidationOutcome(accepted=False, reason_codes=sorted(set(codes)))


# =========================================================================================
# step 3 -- strict shape (recursive: nothing but the declared keys and scalar types)
# =========================================================================================


def check_shape(proposal: Any) -> list[str]:
    """Fixed reason codes for a malformed proposal; empty when the shape is exactly right.

    Written out rather than delegated to a JSON-schema library so the refusal is total and
    reviewable here: extra keys at ANY depth, a number where a string belongs, a bool where a
    list belongs, oversize payloads and unknown enum values are all refused.
    """
    import json

    if not isinstance(proposal, dict) or set(proposal) != _PROPOSAL_KEYS:
        return [SCHEMA_INVALID]
    try:
        size = len(json.dumps(proposal, ensure_ascii=False))
    except (TypeError, ValueError):
        return [SCHEMA_INVALID]
    if size > _MAX_PROPOSAL_CHARS:
        return [LIMIT_EXCEEDED]
    codes: list[str] = []
    if proposal["schema_version"] != bp.PROPOSAL_SCHEMA:
        codes.append(SCHEMA_INVALID)
    if not isinstance(proposal["packet_id"], str) or not _PACKET_ID.match(proposal["packet_id"]):
        codes.append(SCHEMA_INVALID)
    obs = proposal["observation_refs"]
    if not isinstance(obs, list) or not all(isinstance(o, str) and _OBS_ID.match(o) for o in obs):
        codes.append(SCHEMA_INVALID)
    elif len(obs) > bp.MAX_OBSERVATION_REFS:
        codes.append(LIMIT_EXCEEDED)
    elif len(set(obs)) != len(obs):
        codes.append(SCHEMA_INVALID)
    pairs = proposal["comparison_candidates"]
    if not isinstance(pairs, list) or not all(
        isinstance(p, dict) and set(p) == {"left_excerpt_id", "right_excerpt_id"}
        and all(isinstance(p[k], str) and _EXC_ID.match(p[k]) for k in p)
        for p in pairs
    ):
        codes.append(SCHEMA_INVALID)
    elif len(pairs) > bp.MAX_COMPARISONS:
        codes.append(LIMIT_EXCEEDED)
    questions = proposal["question_ids"]
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        codes.append(SCHEMA_INVALID)
    elif len(questions) > bp.MAX_QUESTIONS:
        codes.append(LIMIT_EXCEEDED)
    elif any(q not in bp.QUESTIONS for q in questions):
        codes.append(QUESTION_NOT_ALLOWED)
    lessons = proposal["lesson_ids"]
    if not isinstance(lessons, list) or not all(isinstance(x, str) for x in lessons):
        codes.append(SCHEMA_INVALID)
    elif len(lessons) > bp.MAX_LESSONS:
        codes.append(LIMIT_EXCEEDED)
    elif any(x not in bp.LESSONS for x in lessons):
        codes.append(LESSON_NOT_ALLOWED)
    abstain = proposal["abstain_code"]
    if abstain is not None and abstain not in bp.ABSTAIN_CODES:
        codes.append(SCHEMA_INVALID)
    return sorted(set(codes))


# =========================================================================================
# proposal validation and rendering
# =========================================================================================

_ACTIVITY_LABELS = {
    "notes_saved": "notes saved",
    "evidence_linked": "evidence links recorded",
    "answers_recorded": "answers recorded",
    "open_questions": "questions with no saved answer",
}


def _observation_statement(o: dict[str, Any]) -> tuple[str, str]:
    """(statement, basis) for one observation, from reviewed wording only."""
    kind = o["kind"]
    if kind == "recorded_activity":
        return (
            f"Recorded in this workspace between {o['window_start'][:10]} and "
            f"{o['window_end'][:10]}: {_ACTIVITY_LABELS.get(o['metric_id'], o['metric_id'])} "
            f"= {o['value']}. This counts saved records, not time spent or research elsewhere.",
            "RECORDED_ACTIVITY",
        )
    if kind == "sourced_fact":
        verified = ("verified by a named check" if o["verification"] == "VERIFIED"
                    else "not verified")
        return (
            f"A saved source record states {o['metric_id']} ({o['basis']}) for "
            f"{o['period_start']} to {o['period_end']}: {o['value']} {o['unit']} "
            f"({verified}). This is what the source says, not a confirmed fact about the "
            "company.",
            "SOURCE_STATEMENT",
        )
    if kind == "evaluation_state":
        return (
            "The deterministic evaluation of your recorded thesis conditions is "
            f"{bp.label(bp.EVALUATION_STATE_LABELS, o['value'])}.",
            "ENGINE_EVALUATION",
        )
    return (
        f"A {bp.label(bp.SOURCE_TYPE_LABELS, o['value'])} dated {o['period_start']} is saved "
        "in this workspace.",
        "SOURCE_METADATA",
    )


def _incompatible(observations: list[dict[str, Any]]) -> bool:
    """Two sourced facts for the same metric may be shown together only on the same unit,
    basis and cadence. Otherwise juxtaposing them implies a comparison nobody approved."""
    seen: dict[str, tuple[str, str]] = {}
    for o in observations:
        if o["kind"] != "sourced_fact":
            continue
        shape = (o["unit"], o["basis"])
        if o["metric_id"] in seen and seen[o["metric_id"]] != shape:
            return True
        seen[o["metric_id"]] = shape
    return False


def _coverage_card(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_sources": packet["coverage"]["external_sources"],
        "source_text": packet["coverage"]["source_text"],
        "truncated": packet["coverage"]["truncated"],
        "limitations": list(packet["coverage"]["limitations"]),
        "as_of": packet["as_of"],
    }


def validate_proposal(
    sealed: SealedPacket,
    proposal: Any,
    *,
    now: datetime,
    current: SealedPacket | None,
) -> ValidationOutcome:
    """Validate a model proposal against its sealed packet and the CURRENT rebuild.

    ``current`` is the packet rebuilt with the same cutoff under the caller's access at
    submission time, or None when access is gone (revoked capability, erased investor, ritual
    lock). Permission failures are never delegated to the model or turned into an abstention.
    """
    # 1. access
    if current is None:
        return _reject(ACCESS_REVOKED)
    # 3a. shape first, so nothing below indexes a malformed value
    shape = check_shape(proposal)
    if shape:
        return _reject(*shape)
    packet = sealed.packet
    # 2. binding, expiry, versions
    if proposal["packet_id"] != packet["packet_id"]:
        return _reject(PACKET_MISMATCH)
    if now > sealed.expires_at:
        return _reject(PACKET_EXPIRED)
    if current.digest != sealed.digest or current.investor_id != sealed.investor_id:
        missing = set(sealed.authority) - set(current.authority)
        return _reject(EVIDENCE_UNAVAILABLE if missing else STALE_INPUT)

    obs_ids = proposal["observation_refs"]
    pairs = proposal["comparison_candidates"]
    questions = proposal["question_ids"]
    lessons = proposal["lesson_ids"]
    abstain = proposal["abstain_code"]
    selected = bool(obs_ids or pairs or questions or lessons)
    if abstain is None and not selected:
        return _reject(EMPTY_SELECTION)
    if abstain is not None and selected:
        return _reject(ABSTAIN_WITH_SELECTION)
    if abstain is not None:
        return ValidationOutcome(accepted=True, abstain_code=abstain,
                                 coverage=_coverage_card(packet))

    # 3b. membership
    observations = {o["id"]: o for o in packet["observations"]}
    excerpts = {x["id"]: x for x in packet["excerpts"]}
    if any(o not in observations for o in obs_ids):
        return _reject(UNKNOWN_REFERENCE)
    if any(p[k] not in excerpts for p in pairs for k in p):
        return _reject(UNKNOWN_REFERENCE)
    if any(q not in packet["allowed_question_ids"] for q in questions):
        return _reject(QUESTION_NOT_ALLOWED)
    if any(x not in packet["allowed_lesson_ids"] for x in lessons):
        return _reject(LESSON_NOT_ALLOWED)

    # 4. excerpt text, offsets and hashes against the sealed authority
    used_excerpts = {p[k] for p in pairs for k in p}
    if questions and bp.REQUIRES_BELIEF in bp.QUESTIONS[questions[0]].requires:
        used_excerpts.update(b["excerpt_id"] for b in packet["belief_refs"][:1])
    for xid in used_excerpts:
        x = excerpts[xid]
        loc = x["locator"]
        if not (0 <= loc["start"] < loc["end"]) or loc["end"] - loc["start"] != len(x["text"]):
            return _reject(OFFSET_INVALID)
        if _sha(x["text"]) != x["content_hash"]:
            return _reject(HASH_MISMATCH)
        auth = sealed.authority.get(xid)
        if auth is None or current.authority.get(xid) != auth:
            return _reject(EVIDENCE_UNAVAILABLE)

    # 5-6. observation eligibility and comparison compatibility
    chosen = [observations[o] for o in obs_ids]
    if _incompatible(chosen):
        return _reject(INCOMPATIBLE_COMPARISON)
    for p in pairs:
        left, right = excerpts[p["left_excerpt_id"]], excerpts[p["right_excerpt_id"]]
        if left["id"] == right["id"] or (
                left["source_version_id"] == right["source_version_id"]
                and left["locator"] == right["locator"]):
            return _reject(INCOMPATIBLE_COMPARISON)

    # 7. freshness: nothing known after the packet's cutoff
    for o in chosen:
        if o["known_at"] > packet["as_of"]:
            return _reject(STALE_INPUT)

    # 8. premises
    available = {bp.REQUIRES_COVERAGE_GAP} if packet["coverage"]["limitations"] else set()
    if packet["belief_refs"]:
        available.add(bp.REQUIRES_BELIEF)
    if pairs:
        available.add(bp.REQUIRES_PAIR)
    if any(sealed.authority[x["id"]].field == "intended_holding_period"
           for x in packet["excerpts"]):
        available.add(bp.REQUIRES_HORIZON)
    for q in questions:
        if not bp.question_allowed(q, available):
            return _reject(PREMISE_UNSUPPORTED)

    # Render: reviewed templates, exact passages, typed values. Nothing else.
    cards: list[dict[str, Any]] = []
    for o in chosen:
        text, basis = _observation_statement(o)
        cards.append({"kind": "OBSERVATION", "statements": [{"text": text, "basis": basis}],
                      "passages": [], "question": None, "lesson": None,
                      "citation_refs": [o["provenance_ref"]]})
    for p in pairs:
        passages = [
            {"ref": excerpts[p[k]]["id"], "text": excerpts[p[k]]["text"],
             "trust": excerpts[p[k]]["trust"], "recorded_at": excerpts[p[k]]["recorded_at"]}
            for k in ("left_excerpt_id", "right_excerpt_id")
        ]
        cards.append({
            "kind": "PASSAGE_PAIR",
            "statements": [{"text": "Two dated passages, shown side by side. Showing them "
                                    "together does not claim that they contradict each other.",
                            "basis": "USER_QUOTE"}],
            "passages": passages,
            "question": _question("COMPARE_TWO_PASSAGES"),
            "lesson": None,
            "citation_refs": [excerpts[p[k]]["source_version_id"] for k in p],
        })
    for q in questions:
        if q == "COMPARE_TWO_PASSAGES" and pairs:
            continue
        belief = packet["belief_refs"][0] if packet["belief_refs"] and \
            bp.REQUIRES_BELIEF in bp.QUESTIONS[q].requires else None
        passages = ([{"ref": belief["excerpt_id"], "text": excerpts[belief["excerpt_id"]]["text"],
                      "trust": excerpts[belief["excerpt_id"]]["trust"],
                      "recorded_at": excerpts[belief["excerpt_id"]]["recorded_at"]}]
                    if belief else [])
        cards.append({"kind": "QUESTION", "statements": [], "passages": passages,
                      "question": _question(q), "lesson": None, "citation_refs": []})
    for x in lessons:
        cards.append({"kind": "LESSON", "statements": [], "passages": [], "question": None,
                      "lesson": _lesson(x), "citation_refs": []})
    return ValidationOutcome(accepted=True, cards=cards, coverage=_coverage_card(packet))


def _question(qid: str) -> dict[str, str]:
    q = bp.QUESTIONS[qid]
    return {"id": q.id, "version": q.version, "text": q.text}


def _lesson(lid: str) -> dict[str, str]:
    x = bp.LESSONS[lid]
    return {"id": x.id, "version": x.version, "text": x.text}


# =========================================================================================
# deterministic inbox cards
# =========================================================================================


def _source_citation(cid: str, s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cid,
        "kind": "SOURCE",
        "record_id": s["id"],
        "label": s["source"],
        "source_type": s["source_type"],
        "published_on": _iso(s["publication_date"]),
        "retrieved_at": _iso(s["retrieved_at"]),
        "recorded_at": _iso(s["recorded_at"]),
        "content_sha256": s.get("content_sha256"),
        "href": safe_href(s.get("source_url")),
    }


def _source_block(
    s: dict[str, Any] | None, retraction: dict[str, Any] | None, policy: SourcePolicy,
    visible: set[str], allow_retracted: bool = False,
) -> str | None:
    """Why a source-citing card cannot be shown, or None when it can."""
    if s is None or s["isin"] not in visible:
        return EVIDENCE_UNAVAILABLE
    if retraction is not None and not allow_retracted:
        return SOURCE_RETRACTED
    verdict = policy.verdict(s["source_type"], USE_METADATA)
    if verdict == DENIED:
        return SOURCE_RIGHTS_WITHDRAWN
    if verdict != ALLOWED:
        return SOURCE_RIGHTS_UNDECIDED
    return None


def _passage(text: str) -> tuple[str, bool]:
    if len(text) <= bp.MAX_EXCERPT_CHARS:
        return text, False
    return text[: bp.MAX_EXCERPT_CHARS], True


def render_item(
    item: dict[str, Any],
    inputs: dict[str, Any],
    policy: SourcePolicy,
    tz: Any,
    now: datetime,
    status: dict[str, Any],
) -> dict[str, Any]:
    """One stored inbox item as a typed card, rebuilt from CURRENT records.

    Anything the card cites that is now retracted, forgotten, rights-blocked, erased or locked
    turns the whole card into a withdrawn notice with a fixed reason -- the content is not
    shown, and it is never served from what the card said when it was created.
    """
    template = bp.CARDS[item["template_id"]]
    args = item["args"] or {}
    ident = inputs["identities"].get(item["isin"]) or {"isin": item["isin"], "symbol": None,
                                                         "name": None}
    company = ident.get("name") or ident.get("symbol") or item["isin"]
    fill: dict[str, Any] = {"company": company}
    citations: list[dict[str, Any]] = []
    passages: list[dict[str, Any]] = []
    blocked: str | None = None
    if item["isin"] not in inputs["visible"]:
        blocked = RITUAL_REQUIRED

    tid = item["template_id"]
    if blocked is None and tid in ("NEW_MATERIAL", "COVERAGE_TEXT_UNAVAILABLE",
                                   "SOURCE_CORRECTION"):
        s = inputs["sources"].get(args.get("source_id"))
        retraction = inputs["retractions"].get(args.get("source_id"))
        blocked = _source_block(s, retraction, policy, inputs["visible"],
                                allow_retracted=tid == "SOURCE_CORRECTION")
        if blocked is None:
            fill.update({
                "source_label": s["source"],
                "source_type": bp.label(bp.SOURCE_TYPE_LABELS, s["source_type"]),
                "period": s["period"],
                "publication_date": _iso(s["publication_date"]),
                "recorded_on": _day(s["recorded_at"], tz),
                "content_state": bp.label(bp.CONTENT_STATE_LABELS,
                                          s.get("extraction_status") or "METADATA_ONLY"),
                "reason": (bp.label(bp.RETRACTION_LABELS, retraction["reason_code"])
                           if tid == "SOURCE_CORRECTION" and retraction
                           else (s.get("unavailable_reason") or "UNKNOWN")),
            })
            if tid == "SOURCE_CORRECTION":
                if retraction is None:
                    blocked = STALE_INPUT
                else:
                    fill["recorded_on"] = _day(retraction["recorded_at"], tz)
            citations.append(_source_citation("c1", s))
    elif blocked is None and tid == "THESIS_CHANGED":
        old = inputs["theses"].get(args.get("old_thesis_id"))
        new = inputs["theses"].get(args.get("new_thesis_id"))
        if old is None or new is None:
            blocked = EVIDENCE_UNAVAILABLE
        elif {("THESIS", old["id"]), ("THESIS", new["id"])} & inputs["forgotten"]:
            blocked = MEMORY_FORGOTTEN
        else:
            fill.update({"old_version": old["version"], "old_version_on": _iso(old["version_on"]),
                         "new_version": new["version"], "new_version_on": _iso(new["version_on"])})
            for cid, t in (("c1", old), ("c2", new)):
                text, cut = _passage(t["argument"] or "")
                passages.append({"citation_id": cid, "text": text, "truncated": cut,
                                 "author_kind": "user", "trust": "USER_HYPOTHESIS",
                                 "label": f"Thesis version {t['version']} argument",
                                 "recorded_at": _iso(t["recorded_at"])})
                citations.append({"id": cid, "kind": "THESIS", "record_id": t["id"],
                                  "label": f"Thesis version {t['version']}",
                                  "published_on": None, "retrieved_at": None,
                                  "recorded_at": _iso(t["recorded_at"]),
                                  "content_sha256": _sha(t["argument"] or ""), "href": None})
    elif blocked is None and tid == "RULE_STATE_CHANGED":
        ev = inputs["evaluations"].get(args.get("evaluation_id"))
        if ev is None:
            blocked = EVIDENCE_UNAVAILABLE
        elif ev["state"] != args.get("state"):
            blocked = STALE_INPUT
        else:
            fill.update({
                "thesis_version": args.get("thesis_version", "UNKNOWN"),
                "evidence_cutoff": _iso(ev["evidence_cutoff"]),
                "state": bp.label(bp.EVALUATION_STATE_LABELS, ev["state"]),
                "previous_state": bp.label(bp.EVALUATION_STATE_LABELS,
                                           args.get("previous_state") or "NONE"),
            })
            citations.append({"id": "c1", "kind": "EVALUATION", "record_id": ev["id"],
                              "label": "Deterministic evaluation receipt",
                              "published_on": None, "retrieved_at": None,
                              "recorded_at": _iso(ev["evidence_cutoff"]),
                              "content_sha256": ev["input_digest"], "href": None})
    elif blocked is None and tid == "REVIEW_DUE":
        fill.update({k: args.get(k, "UNKNOWN") for k in (
            "due_on", "window_start", "window_end", "notes_saved", "evidence_linked",
            "answers_recorded", "open_questions")})

    header = {
        "id": item["id"],
        "kind": item["card_kind"],
        "template_id": tid,
        "template_version": item["template_version"],
        "rule_version": item["rule_version"],
        "priority": item["priority"],
        "isin": item["isin"],
        "identity": ident,
        "subscription_id": item.get("subscription_id"),
        "recorded_at": _iso(item["recorded_at"]),
        "deliver_after": _iso(item["deliver_after"]),
        "supersedes_item_id": item.get("supersedes_item_id"),
        "superseded_by_item_id": item.get("superseded_by_item_id"),
        "status": status,
        "coverage": {
            "saved_workspace_records": "CHECKED",
            "external_sources": "NOT_CHECKED",
            "note": "Only records saved in this workspace were checked.",
        },
        "model_involved": False,
    }
    if blocked is not None:
        return {**header, "state": "WITHDRAWN", "withdrawn_reason": blocked,
                "title": "This card was withdrawn",
                "statements": [{"text": INVALID_TEXT[blocked], "basis": "SYSTEM"}],
                "passages": [], "question": None, "lesson": None, "citations": []}
    return {
        **header,
        "state": "ACTIVE",
        "withdrawn_reason": None,
        "title": _fill(template.title, fill),
        "statements": [{"text": _fill(t, fill), "basis": _basis(tid)} for t in template.statements],
        "passages": passages,
        "question": _question(template.question_id) if template.question_id else None,
        "lesson": _lesson(template.lesson_id) if template.lesson_id else None,
        "citations": citations,
    }


def _basis(template_id: str) -> str:
    return {
        "NEW_MATERIAL": "SOURCE_METADATA",
        "COVERAGE_TEXT_UNAVAILABLE": "SOURCE_METADATA",
        "SOURCE_CORRECTION": "SOURCE_METADATA",
        "REVIEW_DUE": "RECORDED_ACTIVITY",
        "RULE_STATE_CHANGED": "ENGINE_EVALUATION",
        "THESIS_CHANGED": "USER_QUOTE",
        "COVERAGE_NO_FEED": "SYSTEM_COVERAGE",
    }.get(template_id, "SYSTEM")
