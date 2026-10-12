"""Glue between stored facts and the pure calculator engine (BE 27-28). READ only.

``app/engine/research_metrics.py`` does the arithmetic; this module loads the requested facts
(respecting a task's cutoff), states who stated each scenario assumption, and shapes the result
for the tool response and the task packet. It never stores a result: a calculation is
recomputed from pinned facts every time it is read, so it can always be re-derived.

ASSUMPTIONS HAVE A HUMAN ORIGIN OR DO NOT EXIST. On the direct tool route the origin is the
authenticated person making the call. Inside a task, the model may only re-run a scenario whose
exact formula, facts and assumptions a person pinned when they created the task
(``calculation_specs``); an assumption the model typed is refused (403 MODEL_ASSUMPTION_REFUSED).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.db import research as rs
from app.engine import research_metrics as rm
from app.research.errors import WorkspaceError, invalid

MODEL_ASSUMPTION_REFUSED = "MODEL_ASSUMPTION_REFUSED"

_FORMULA_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_UNIT = re.compile(r"^\S{1,32}$")
_DECIMAL = re.compile(r"^-?[0-9]{1,14}(\.[0-9]{1,6})?$")
MAX_FACTS = 24
MAX_ASSUMPTIONS = 12


def parse_formula(args: dict[str, Any]) -> tuple[str, str]:
    fid, version = args.get("formula_id"), args.get("formula_version")
    if not isinstance(fid, str) or not _FORMULA_ID.match(fid):
        raise invalid("formula_id must be an approved identifier", {"formula_id": "FORMAT"})
    if not isinstance(version, str) or not _VERSION.match(version):
        # There is no "latest": the version is part of what the number means.
        raise invalid("formula_version is required; there is no implicit latest",
                      {"formula_version": "REQUIRED"})
    return fid, version


def parse_fact_ids(args: dict[str, Any]) -> list[str]:
    raw = args.get("fact_ids")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_FACTS \
            or not all(isinstance(f, str) and 0 < len(f) <= 64 for f in raw):
        raise invalid(f"fact_ids must be 1-{MAX_FACTS} fact ids", {"fact_ids": "FORMAT"})
    if len(set(raw)) != len(raw):
        raise invalid("fact_ids repeats an id", {"fact_ids": "DUPLICATE"})
    return list(raw)


def parse_assumptions(raw: Any) -> list[dict[str, str]]:
    """Strict shape: name, value (decimal STRING), unit, reason. Nothing else, no numbers."""
    if not isinstance(raw, list) or len(raw) > MAX_ASSUMPTIONS:
        raise invalid("assumptions must be a list", {"assumptions": "FORMAT"})
    out = []
    for i, a in enumerate(raw):
        if not isinstance(a, dict) or set(a) != {"name", "value", "unit", "reason"}:
            raise invalid("each assumption is exactly {name, value, unit, reason}",
                          {f"assumptions[{i}]": "SHAPE"})
        if not isinstance(a["name"], str) or not 0 < len(a["name"]) <= 120:
            raise invalid("assumption name is invalid", {f"assumptions[{i}].name": "FORMAT"})
        if not isinstance(a["value"], str) or not _DECIMAL.match(a["value"]):
            raise invalid("assumption value must be a decimal string",
                          {f"assumptions[{i}].value": "FORMAT"})
        if not isinstance(a["unit"], str) or not _UNIT.match(a["unit"]):
            raise invalid("assumption unit is invalid", {f"assumptions[{i}].unit": "FORMAT"})
        if not isinstance(a["reason"], str) or not 0 < len(a["reason"].strip()) <= 2000:
            raise invalid("every assumption needs the person's reason",
                          {f"assumptions[{i}].reason": "REQUIRED"})
        out.append({k: a[k] for k in ("name", "value", "unit", "reason")})
    return out


def spec_key(formula_id: str, version: str, fact_ids: list[str],
             assumptions: list[dict[str, str]]) -> tuple[Any, ...]:
    return (formula_id, version, tuple(sorted(fact_ids)),
            tuple(sorted(tuple(sorted(a.items())) for a in assumptions)))


def run(
    conn: Any,
    investor_id: str,
    family: list[str],
    formulas: rm.FormulaRegistry,
    formula_id: str,
    version: str,
    fact_ids: list[str],
    assumptions: list[dict[str, str]] | None,
    origin: str | None,
    cutoff: datetime | None = None,
    kind: str | None = None,
) -> rm.CalcResult:
    """Load, compute, return. Never persists. ``origin`` is the person who stated the
    assumptions (a user id); None is only valid when there are none."""
    if assumptions and not origin:
        raise WorkspaceError(403, MODEL_ASSUMPTION_REFUSED,
                             "scenario assumptions must be stated by a person")
    facts, superseded = rs.calculation_facts(conn, investor_id, fact_ids, cutoff)
    stated = [rm.Assumption(name=a["name"], value=a["value"], unit=a["unit"],
                            reason=a["reason"], origin=str(origin))
              for a in (assumptions or [])]
    return rm.calculate(formulas, formula_id, version, facts, fact_ids, superseded, stated,
                        family, expect_kind=kind)


def packet_result(calc_ref: str, result: rm.CalcResult) -> dict[str, Any]:
    """The task packet's ``calculation_results`` entry: values only from code."""
    body = result.as_dict()
    first = next((r["code"] for r in result.reasons), None)
    return {
        "calculation_ref": calc_ref,
        "formula_id": result.formula_id,
        "version": result.formula_version,
        "status": result.status,
        "value": result.value,
        "unit": result.unit if result.status == rm.OK else None,
        "period": result.period,
        "input_refs": [i["fact_id"] for i in result.inputs],
        "code": None if result.status == rm.OK else (
            first or ("SOURCE_CONFLICT" if result.conflicts else "UNKNOWN")),
        "trace": body,
    }
