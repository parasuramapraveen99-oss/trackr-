"""The nine internal read-only research tools behind one allowlisted dispatch (BE 18).

``POST /investors/{id}/workspace/tools/{tool_name}`` reaches ``dispatch`` and nothing else.

THE ALLOWLIST IS CHECKED FIRST AND LITERALLY. ``tool_name`` must be an exact key of
``HANDLERS`` or ``skills.UNAVAILABLE_TOOLS``. There is no ``getattr``, no case folding, no path
handling, no prefix matching: an unknown, mixed-case, path-traversing or model-invented name is
404 before any argument is parsed or any row is read. A method's own ``permitted_tools`` narrows
this further for task-scoped calls.

FAILURE CLASSES STAY DISTINCT (the specific misreport this prevents: an authorisation failure
answered as UNKNOWN teaches a method the company "has no filings"):

    unowned investor   403   (route dependency, before this module)
    missing scoped id  404   NOT_FOUND
    locked company     409   RITUAL_REQUIRED
    malformed args     422   INVALID_INPUT
    bounded operation  429   RATE_LIMITED
    genuinely absent   200   status UNKNOWN, with a notice saying why

NOTHING HERE CAN WRITE. Handlers receive a READER-capability connection (migration 045 grants
that role no INSERT anywhere) plus the caller's existing financial connection for the position
read. They call repository READ functions only; ``get_trigger_status`` runs the pure engine and
stores no receipt. No handler seeds a company, skill, ritual or fact.

TASK SCOPE NEVER WIDENS. With ``task_id`` a call sees only that task's pinned references at its
cutoff; ``task_id`` cannot reach a record the task was not given.

UNAVAILABLE IS SAID, NOT FAKED. The two calculators (BE 27-28) compute only formulas in the
approved registry (``app/engine/research_metrics.py``); every other id answers UNKNOWN /
FORMULA_NOT_APPROVED (the live registry is BE-Q03's 2026-09-29 decision). The three source reads
(BE 29) release only held material whose per-source verdicts allow reading and model
disclosure; the live policy allows none (BE-Q04). Neither ever fetches, estimates or falls back.
A calculator takes an identifier, a version and fact ids -- never an expression or a value --
and a scenario's assumptions must come from a person (see ``app/research/calculator.py``).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.db import research as rs
from app.db import workspace as ws
from app.engine import research_metrics as rm
from app.research import calculator as calc
from app.research import skills as sk
from app.research.errors import (
    CONTEXT_DRIFT,
    WorkspaceError,
    conflict,
    invalid,
    not_found,
)

UNKNOWN_TOOL = "UNKNOWN_TOOL"
RATE_LIMITED = "RATE_LIMITED"
NOT_IN_TASK_SCOPE = "NOT_IN_TASK_SCOPE"
TOOL_NOT_PERMITTED = "TOOL_NOT_PERMITTED"

#: In-process bound per investor. A single-process guard, stated as such: it bounds one
#: worker's load, not a distributed quota.
RATE_WINDOW_NS = 60 * 1_000_000_000
RATE_MAX_CALLS = 120
_calls: dict[str, deque[int]] = defaultdict(deque)
_calls_lock = threading.Lock()


def _check_rate(investor_id: str, now: int | None = None) -> None:
    now = time.monotonic_ns() if now is None else now
    with _calls_lock:
        window = _calls[investor_id]
        while window and now - window[0] > RATE_WINDOW_NS:
            window.popleft()
        if len(window) >= RATE_MAX_CALLS:
            raise WorkspaceError(429, RATE_LIMITED,
                                 f"at most {RATE_MAX_CALLS} tool calls per minute")
        window.append(now)


@dataclass(frozen=True)
class TaskScope:
    """What a task-scoped call may see. Built by ``app/db/tasks.py::task_scope``."""

    task_id: str
    isin: str
    cutoff: datetime
    pinned: dict[str, frozenset[str]]
    thesis_id: str | None
    position_query: dict[str, Any] | None
    permitted_tools: frozenset[str]
    #: Human-declared calculation specs pinned at task creation (formula, version, facts,
    #: assumptions) and the person who pinned them -- the only assumption origin a task has.
    calculation_specs: tuple[dict[str, Any], ...] = ()
    created_by: str | None = None


@dataclass
class Ctx:
    conn: Any
    fin_conn: Any
    investor_id: str
    isin: str
    family: list[str]
    scope: TaskScope | None
    registry: Any
    calendar: Any
    formulas: Any = None
    policy: Any = None
    actor_uid: str | None = None


def _response(status: str = "OK", data: Any = None, references: list | None = None,
              conflicts: list | None = None, notices: list | None = None,
              calculation: dict | None = None) -> dict[str, Any]:
    return {"status": status, "data": data, "references": references or [],
            "conflicts": conflicts or [], "notices": notices or [], "calculation": calculation}


def _notice(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


# --- argument parsing ------------------------------------------------------------------------


def _only(args: dict[str, Any], allowed: set[str]) -> None:
    if not isinstance(args, dict):
        raise invalid("args must be an object", {"args": "TYPE"})
    extra = set(args) - allowed
    if extra:
        raise invalid("unknown tool arguments", {k: "UNKNOWN" for k in sorted(extra)})


def _instant(args: dict[str, Any], name: str) -> datetime:
    raw = args.get(name)
    if not isinstance(raw, str):
        raise invalid(f"{name} is required as an ISO instant", {name: "REQUIRED"})
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise invalid(f"{name} is not an ISO instant", {name: "FORMAT"}) from exc
    if value.tzinfo is None:
        raise invalid(f"{name} needs a UTC offset", {name: "NAIVE"})
    if value > datetime.now(UTC):
        raise invalid(f"{name} cannot be in the future", {name: "FUTURE"})
    return value


def _date(args: dict[str, Any], name: str) -> date:
    raw = args.get(name)
    if not isinstance(raw, str):
        raise invalid(f"{name} is required as YYYY-MM-DD", {name: "REQUIRED"})
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise invalid(f"{name} is not a date", {name: "FORMAT"}) from exc


def _bool(args: dict[str, Any], name: str, default: bool | None = None) -> bool:
    raw = args.get(name, default)
    if not isinstance(raw, bool):
        raise invalid(f"{name} must be true or false", {name: "TYPE"})
    return raw


def _opt_str(args: dict[str, Any], name: str, max_len: int = 512) -> str | None:
    raw = args.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > max_len:
        raise invalid(f"{name} must be a string", {name: "TYPE"})
    return raw


def _opt_limit(args: dict[str, Any]) -> int | None:
    raw = args.get("limit")
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise invalid("limit must be an integer", {"limit": "TYPE"})
    return raw


def _thesis_id(args: dict[str, Any]) -> str:
    raw = args.get("thesis_id")
    if not isinstance(raw, str):
        raise invalid("thesis_id is required", {"thesis_id": "REQUIRED"})
    return raw


# --- scope helpers ---------------------------------------------------------------------------


def _pinned(ctx: Ctx, kind: str, ref_id: str) -> bool:
    return ctx.scope is None or ref_id in ctx.scope.pinned.get(kind, frozenset())


def _thesis_in_company(ctx: Ctx, thesis_id: str) -> dict[str, Any]:
    row = rs.thesis_row(ctx.conn, ctx.investor_id, thesis_id)  # 404 / 409 gate
    if row["isin"] not in ctx.family:
        raise not_found("thesis")
    if not _pinned(ctx, "thesis", row["id"]):
        raise WorkspaceError(404, NOT_IN_TASK_SCOPE, "this thesis is not pinned in the task")
    return row


def _cap_cutoff(ctx: Ctx, at: datetime, name: str) -> None:
    if ctx.scope is not None and at > ctx.scope.cutoff:
        raise invalid(f"{name} is after the task's context cutoff", {name: "AFTER_CUTOFF"})


def _filter_page(ctx: Ctx, page: dict[str, Any], kind: str) -> dict[str, Any]:
    if ctx.scope is None:
        return page
    items = [i for i in page["items"] if _pinned(ctx, kind, i["id"])
             and i["recorded_at"] <= ctx.scope.cutoff]
    return {"items": items, "next_cursor": page["next_cursor"]}


# --- the nine handlers -----------------------------------------------------------------------


def get_active_thesis(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"at"})
    at = _instant(args, "at")
    _cap_cutoff(ctx, at, "at")
    selection, _ = rs.select_in_force(ctx.conn, ctx.investor_id, ctx.isin, at)
    if selection.state == "CONFLICT":
        ids = [v.id for v in selection.candidates]
        return _response("CONFLICT", None, [{"kind": "thesis", "id": i} for i in ids],
                         conflicts=[{"claim": "active_commitment", "source_ids": ids,
                                     "description": "verified aliases hold more than one "
                                     "active commitment"}])
    if selection.state == "NONE":
        return _response("UNKNOWN", None,
                         notices=[_notice("NO_COMMITMENT_IN_FORCE",
                                          "no active commitment is in force at this instant")])
    thesis = rs.thesis_by_id(ctx.conn, ctx.investor_id, selection.thesis.id)
    if not _pinned(ctx, "thesis", thesis["id"]):
        return _response("UNKNOWN", None,
                         notices=[_notice(NOT_IN_TASK_SCOPE,
                                          "the commitment in force is not pinned in this task")])
    return _response("OK", thesis, [{"kind": "thesis", "id": thesis["id"]}])


def get_thesis_history(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"cursor", "limit"})
    page = rs.list_theses(ctx.conn, ctx.investor_id, ctx.isin, _opt_str(args, "cursor"),
                          _opt_limit(args))
    page = _filter_page(ctx, page, "thesis")
    return _response("OK", page, [{"kind": "thesis", "id": t["id"]} for t in page["items"]])


def get_exit_plan(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"thesis_id"})
    row = _thesis_in_company(ctx, _thesis_id(args))
    return _response("OK", {"thesis_id": row["id"], "version": row["version"],
                            "exits": row["exits"]}, [{"kind": "thesis", "id": row["id"]}])


def get_pre_mortem(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"thesis_id"})
    row = _thesis_in_company(ctx, _thesis_id(args))
    return _response("OK", {"text": row["pre_mortem"], "written_on": row["version_on"],
                            "thesis_id": row["id"], "version": row["version"]},
                     [{"kind": "thesis", "id": row["id"]}])


def get_decision_history(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"cursor", "limit"})
    notes = ws.list_notes(ctx.conn, ctx.investor_id, ctx.isin, "DECISION", False,
                          _opt_str(args, "cursor"), _opt_limit(args))
    notes = _filter_page(ctx, notes, "note")
    theses = ws._rows(ctx.conn, "SELECT id FROM thesis WHERE investor_id = %s "
                                "AND isin = ANY(%s::text[])", (ctx.investor_id, ctx.family))
    ids = [t["id"] for t in theses if _pinned(ctx, "thesis", t["id"])]
    events = [e for tid, evs in rs._events_for(ctx.conn, ctx.investor_id, ids).items()
              for e in evs
              if ctx.scope is None or e["recorded_at"] <= ctx.scope.cutoff]
    notices = []
    if len(events) > 100:
        events = events[-100:]
        notices.append(_notice("EVENTS_BOUNDED", "showing the latest 100 thesis events"))
    refs = ([{"kind": "note", "id": n["id"]} for n in notes["items"]]
            + [{"kind": "thesis_event", "id": e["id"]} for e in events])
    return _response("OK", {"notes": notes, "events": {"items": events, "next_cursor": None}},
                     refs, notices=notices)


def get_open_questions(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"cursor", "limit"})
    page = ws.list_questions(ctx.conn, ctx.investor_id, ctx.isin, "OPEN", None,
                             _opt_str(args, "cursor"), _opt_limit(args))
    if ctx.scope is not None:
        page = {"items": [q for q in page["items"]
                          if _pinned(ctx, "note", q["note_id"])
                          and q["recorded_at"] <= ctx.scope.cutoff],
                "next_cursor": page["next_cursor"]}
    return _response("OK", page, [{"kind": "question", "id": q["id"]} for q in page["items"]])


def get_notes(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"kind", "cursor", "limit"})
    page = ws.list_notes(ctx.conn, ctx.investor_id, ctx.isin, _opt_str(args, "kind", 32),
                         False, _opt_str(args, "cursor"), _opt_limit(args))
    page = _filter_page(ctx, page, "note")
    return _response("OK", page, [{"kind": "note", "id": n["id"]} for n in page["items"]])


def get_position_state(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """The existing holdings engine, unchanged, on the caller's financial connection.

    A missing price stays the engine's own partition: the holding is named under
    ``unpriced`` and excluded from every value, never zeroed, carried or dropped. An engine
    failure is an error (5xx), not UNKNOWN. Nothing is stored."""
    _only(args, {"as_of", "advised_only"})
    as_of = _date(args, "as_of")
    advised_only = _bool(args, "advised_only", False)
    if ctx.scope is not None:
        query = ctx.scope.position_query
        if query is None:
            raise WorkspaceError(404, NOT_IN_TASK_SCOPE, "this task pinned no position query")
        if query["as_of"] != as_of.isoformat() or query["advised_only"] != advised_only:
            raise WorkspaceError(404, NOT_IN_TASK_SCOPE,
                                 "only the task's pinned position query may be read")
        from app.research.context import ledger_watermark

        if ledger_watermark(ctx.fin_conn, ctx.investor_id, as_of, advised_only) \
                != query["ledger_watermark"]:
            raise conflict(CONTEXT_DRIFT, "the ledger has changed since this task was sealed; "
                           "prepare a new task")
    from app.routers.holdings import _portfolio, _row

    book = _portfolio(ctx.fin_conn, ctx.investor_id, as_of, advised_only=advised_only)
    fam = set(ctx.family)
    held = [_row(book, h) for h in book["valued"] if h.isin in fam]
    unpriced = [{"isin": h.isin, "invested": format(h.invested, "f")}
                for h in book["unpriced"] if h.isin in fam]
    notices = []
    if unpriced:
        notices.append(_notice("PRICE_MISSING",
                               "no price is available; the holding is not valued"))
    if not held and not unpriced:
        notices.append(_notice("NO_POSITION", "the ledger shows no holding of this company "
                                              "on this date"))
    data = {"as_of": book["as_of"].isoformat(), "advised_only": advised_only,
            "holdings": held, "unpriced": unpriced}
    return _response("OK", data, [{"kind": "position", "id": f"position:{as_of.isoformat()}"}],
                     notices=notices)


def get_trigger_status(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """Display evaluation from the pure engine. No receipt, no evaluation_fact, no write."""
    _only(args, {"thesis_id", "evidence_cutoff"})
    row = _thesis_in_company(ctx, _thesis_id(args))
    cutoff = _instant(args, "evidence_cutoff")
    _cap_cutoff(ctx, cutoff, "evidence_cutoff")
    evaluation = rs.evaluate_readonly(ctx.conn, ctx.investor_id, row["id"], cutoff,
                                      ctx.registry, ctx.calendar)
    status = {"CONFLICT": "CONFLICT", "UNKNOWN": "UNKNOWN"}.get(evaluation["state"], "OK")
    refs = [{"kind": "thesis", "id": row["id"]}] + [
        {"kind": "fact", "id": f} for f in evaluation["fact_ids"]]
    return _response(status, evaluation, refs)


# --- BE 27-28 calculators --------------------------------------------------------------------


def _calc_response(result: rm.CalcResult) -> dict[str, Any]:
    refs = [{"kind": "fact", "id": i["fact_id"]} for i in result.inputs]
    notices = [_notice(r["code"], _reason_text(r)) for r in result.reasons]
    conflicts = [{"claim": f"input:{c['operand']}", "source_ids": c["source_ids"],
                  "description": "verified inputs disagree for one operand; both are cited and "
                                 "nothing was averaged or preferred"}
                 for c in result.conflicts]
    return _response(result.status, None, refs, conflicts, notices, result.as_dict())


def _reason_text(r: dict[str, Any]) -> str:
    detail = ", ".join(f"{k}={v}" for k, v in r.items() if k != "code")
    return f"{r['code']}" + (f" ({detail})" if detail else "")


def _scoped_fact_ids(ctx: Ctx, ids: list[str]) -> None:
    if ctx.scope is None:
        return
    unpinned = [i for i in ids if i not in ctx.scope.pinned.get("fact", frozenset())]
    if unpinned:
        raise WorkspaceError(404, NOT_IN_TASK_SCOPE, "a fact is not pinned in the task")


def calculate_financial_metrics(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """BE 27. {formula_id, formula_version, fact_ids} -> value with a full trace, or UNKNOWN /
    CONFLICT with reasons. An ``expression``, ``value`` or operand argument is refused (422)."""
    _only(args, {"formula_id", "formula_version", "fact_ids"})
    fid, version = calc.parse_formula(args)
    ids = calc.parse_fact_ids(args)
    _scoped_fact_ids(ctx, ids)
    result = calc.run(ctx.conn, ctx.investor_id, ctx.family, ctx.formulas, fid, version, ids,
                      None, None, ctx.scope.cutoff if ctx.scope else None, kind="METRIC")
    return _calc_response(result)


def calculate_valuation(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """BE 28. Scenario arithmetic over sourced facts and the PERSON's stated assumptions.

    Direct call: the authenticated person is the assumptions' origin. Task-scoped call (a model
    driving the tool): only an exact scenario a person pinned when creating the task may run,
    with that person as origin. Anything else is 403 MODEL_ASSUMPTION_REFUSED."""
    _only(args, {"formula_id", "formula_version", "fact_ids", "assumptions"})
    fid, version = calc.parse_formula(args)
    ids = calc.parse_fact_ids(args)
    stated = calc.parse_assumptions(args.get("assumptions", []))
    _scoped_fact_ids(ctx, ids)
    if ctx.scope is not None:
        key = calc.spec_key(fid, version, ids, stated)
        pinned = {calc.spec_key(s["formula_id"], s["version"], list(s.get("fact_ids", [])),
                                list(s.get("assumptions", [])))
                  for s in ctx.scope.calculation_specs}
        if key not in pinned or not ctx.scope.created_by:
            raise WorkspaceError(403, calc.MODEL_ASSUMPTION_REFUSED,
                                 "inside a task only a scenario the person pinned may run; "
                                 "assumptions cannot come from the model")
        origin = ctx.scope.created_by
    else:
        origin = ctx.actor_uid
    result = calc.run(ctx.conn, ctx.investor_id, ctx.family, ctx.formulas, fid, version, ids,
                      stated, origin, ctx.scope.cutoff if ctx.scope else None, kind="SCENARIO")
    return _calc_response(result)


# --- BE 29 source reads -----------------------------------------------------------------------
#
# Held material only: nothing is fetched on demand and nothing is written. A tool response is
# released to the caller's model, so every item passes its source type's MODEL_DISCLOSE verdict
# (and METADATA for the citation itself); a denied or undecided item is EXCLUDED with a notice
# naming how many and why -- never silently dropped, never shown anyway.


def _released(ctx: Ctx, source_type: str) -> bool:
    from app.research.source_policy import USE_METADATA, USE_MODEL_DISCLOSE

    return (ctx.policy.allows(source_type, USE_METADATA)
            and ctx.policy.allows(source_type, USE_MODEL_DISCLOSE))


def _exclusion_notice(excluded: dict[str, int]) -> list[dict[str, str]]:
    return [_notice("SOURCE_DISCLOSURE_NOT_APPROVED",
                    f"{n} item(s) of type {t} withheld: no recorded verdict permits disclosing "
                    "them to a model (BE-Q04)") for t, n in sorted(excluded.items())]


def get_filings(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """Held sources for this company (origin, publication and retrieval dates, period, content
    hash, extraction pin, revision links). Missing is UNKNOWN."""
    from app.db import research_evidence as ev

    _only(args, {"limit"})
    limit = min(max(_opt_limit(args) or 100, 1), 200)
    rows = ev.held_sources(ctx.conn, ctx.investor_id, ctx.family,
                           ctx.scope.cutoff if ctx.scope else None, limit)
    excluded: dict[str, int] = defaultdict(int)
    items = []
    for r in rows:
        if not _pinned(ctx, "source", r["id"]):
            continue
        if not _released(ctx, r["source_type"]):
            excluded[r["source_type"]] += 1
            continue
        items.append(r)
    notices = _exclusion_notice(excluded)
    if not items:
        notices.append(_notice("NO_HELD_FILINGS", "no releasable filing is held for this "
                                                  "company; nothing was fetched"))
        return _response("UNKNOWN", None, notices=notices)
    refs = [{"kind": "source", "id": r["id"], "publication_date": r["publication_date"],
             "retrieved_at": r["retrieved_at"]} for r in items]
    return _response("OK", {"items": items}, refs, notices=notices)


def get_financial_facts(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """Held facts with verification state, successors and conflicts. No hidden write, no
    on-demand fetch, no preference between disagreeing sources."""
    from app.db import research_evidence as ev

    _only(args, {"metric", "period_start", "period_end"})
    metric = _opt_str(args, "metric", 128)
    ps = _date(args, "period_start") if args.get("period_start") is not None else None
    pe = _date(args, "period_end") if args.get("period_end") is not None else None
    rows = ev.held_facts(ctx.conn, ctx.investor_id, ctx.family, metric, ps, pe,
                         ctx.scope.cutoff if ctx.scope else None)
    excluded: dict[str, int] = defaultdict(int)
    items = []
    for r in rows:
        if not _pinned(ctx, "fact", r["id"]):
            continue
        if not _released(ctx, r["source_type"]):
            excluded[r["source_type"]] += 1
            continue
        items.append(r)
    notices = _exclusion_notice(excluded)
    released = {r["id"] for r in items}
    conflicts = [{"claim": f"{r['metric']}:{r['basis']}:{r['unit']}:{r['period_start']}.."
                           f"{r['period_end']}",
                  "source_ids": sorted({r["source_id"]} | {o["source_id"] for o in items
                                                           if o["id"] in r["conflicts_with"]}),
                  "description": "current values from different sources differ; both are "
                                 "shown and neither is preferred"}
                 for r in items if r["conflicts_with"]
                 and all(c in released for c in r["conflicts_with"])]
    if not items:
        notices.append(_notice("NO_HELD_FACTS", "no releasable fact is held for this query"))
        return _response("UNKNOWN", None, notices=notices)
    if any(r["verification"] != "VERIFIED" for r in items):
        notices.append(_notice("UNVERIFIED_PRESENT", "some facts are unverified extractions; "
                                                     "they are not evidence until a person "
                                                     "rechecks them"))
    return _response("CONFLICT" if conflicts else "OK", {"items": items},
                     [{"kind": "fact", "id": r["id"]} for r in items], conflicts, notices)


def get_announcements(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """Held dated events with exact identity. SCHEDULED, OBSERVED, CLAIMED and CANCELLED are
    distinct states; ``classification_verified`` is true only after a person's recheck."""
    from app.db import research_evidence as ev

    _only(args, set())
    installed = ws._one(ctx.conn, "SELECT to_regclass('research_event') IS NOT NULL AS ok",
                        None)
    if not installed or not installed["ok"]:
        return _response("UNKNOWN", None, notices=[_notice(
            "EVENTS_NOT_INSTALLED", "event storage (migration 055) is not installed")])
    rows = ev.list_events(ctx.conn, ctx.investor_id, ctx.family,
                          ctx.scope.cutoff if ctx.scope else None)
    excluded: dict[str, int] = defaultdict(int)
    items = []
    for r in rows:
        if not _pinned(ctx, "source", r["source_id"]):
            continue
        if not _released(ctx, r["source_type"]):
            excluded[r["source_type"]] += 1
            continue
        items.append(r)
    notices = _exclusion_notice(excluded)
    if not items:
        notices.append(_notice("NO_HELD_EVENTS", "no releasable announcement is held"))
        return _response("UNKNOWN", None, notices=notices)
    return _response("OK", {"items": items},
                     [{"kind": "source", "id": r["source_id"]} for r in items], notices=notices)


EVIDENCE_HANDLERS: dict[str, Callable[[Ctx, dict[str, Any]], dict[str, Any]]] = {
    "calculate_financial_metrics": calculate_financial_metrics,
    "calculate_valuation": calculate_valuation,
    "get_filings": get_filings,
    "get_financial_facts": get_financial_facts,
    "get_announcements": get_announcements,
}


HANDLERS: dict[str, Callable[[Ctx, dict[str, Any]], dict[str, Any]]] = {
    "get_active_thesis": get_active_thesis,
    "get_thesis_history": get_thesis_history,
    "get_exit_plan": get_exit_plan,
    "get_pre_mortem": get_pre_mortem,
    "get_decision_history": get_decision_history,
    "get_open_questions": get_open_questions,
    "get_notes": get_notes,
    "get_position_state": get_position_state,
    "get_trigger_status": get_trigger_status,
}
assert tuple(HANDLERS) == sk.INTERNAL_TOOLS


def _unavailable(tool_name: str) -> dict[str, Any]:
    code, gate = sk.GATED_TOOLS[tool_name]
    kind = "calculator" if code == "FORMULA_NOT_APPROVED" else "source adapter"
    return _response("UNKNOWN", None, notices=[_notice(
        code, f"no approved {kind} exists for {tool_name} ({gate}); nothing was computed or "
              "fetched")])


def resolve_name(tool_name: str) -> str:
    """Exact allowlist membership, before anything else. 404 otherwise."""
    if tool_name in HANDLERS or tool_name in sk.GATED_TOOLS:
        return tool_name
    raise WorkspaceError(404, UNKNOWN_TOOL, "no such tool")


def dispatch(
    tool_name: str,
    *,
    conn: Any,
    fin_conn: Any,
    investor_id: str,
    isin: str,
    args: dict[str, Any],
    scope: TaskScope | None,
    registry: Any,
    calendar: Any,
    formulas: Any = None,
    policy: Any = None,
    actor_uid: str | None = None,
) -> dict[str, Any]:
    from app.research.source_policy import LIVE_SOURCE_POLICY

    formulas = formulas if formulas is not None else rm.LIVE_FORMULAS
    policy = policy if policy is not None else LIVE_SOURCE_POLICY
    name = resolve_name(tool_name)
    if scope is not None and name not in scope.permitted_tools:
        raise WorkspaceError(403, TOOL_NOT_PERMITTED,
                             "the task's pinned method does not permit this tool")
    _check_rate(investor_id)
    isin = ws.require_unlocked(conn, investor_id, isin)
    if scope is not None and isin not in ws.isin_family(conn, scope.isin):
        raise WorkspaceError(404, NOT_IN_TASK_SCOPE, "this task concerns another company")
    ctx = Ctx(conn=conn, fin_conn=fin_conn, investor_id=investor_id, isin=isin,
              family=ws.isin_family(conn, isin), scope=scope, registry=registry,
              calendar=calendar, formulas=formulas, policy=policy, actor_uid=actor_uid)
    if name in EVIDENCE_HANDLERS:
        return EVIDENCE_HANDLERS[name](ctx, args)
    if name in sk.GATED_TOOLS:
        _only(args, set(args))  # shape only; nothing to compute
        return _unavailable(name)
    return HANDLERS[name](ctx, args)
