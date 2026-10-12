"""Approved research metric and valuation-scenario calculators over sourced facts. Pure.

BE 27 (metrics) and BE 28 (scenarios). docs/RESEARCH_CALCULATIONS.md is the human-readable
registry; this module is its executable form. Every number a research method, a Butler card or
a company report later quotes is produced here, by an approved formula IDENTIFIER and VERSION,
never by model prose.

BE-Q03 WAS DECIDED ON 2026-09-29 (implementer-defined under owner delegation). ``LIVE_FORMULAS``
is built from ``app/engine/research_formulas.py``, whose every entry carries that approval and
a registry version. A formula id/version not in it answers UNKNOWN / ``FORMULA_NOT_APPROVED``.
The machinery is also exercised against a SYNTHETIC registry (``fixtures/research_metrics.json``)
whose invented test formulas can never be loaded as the live registry. No equation, weight or
constant exists outside a registered definition; a caller can never supply one.

WHAT A CALCULATION IS:

* A formula is a closed expression tree over NAMED inputs (sourced verified facts) and, for a
  scenario, NAMED assumptions (the person's own stated values). The tree is part of the approved
  definition. A caller supplies an identifier, a version, fact ids and -- for a scenario --
  assumption values. It can never supply an expression, operator, operand or computed value.
* Every input is validated against the definition before arithmetic: VERIFIED, not superseded,
  exact metric, basis, unit and cadence, and a period the definition permits. A mismatch is
  UNKNOWN with its reason, never a conversion. A factor-of-100 slip between a fraction and
  percentage points is exactly what the unit check exists to stop.
* Two current verified values for one input are CONFLICT with both citations. Never a mean,
  never latest-wins, never a management-adjusted substitute.
* Undefined arithmetic is a state, not an exception: a zero denominator, a non-finite input or
  a result outside NUMERIC(20,6) is UNKNOWN with its reason code. Precision and rounding come
  from the definition's policy inside an explicit Decimal context, never the ambient default.
* The result carries a complete trace -- expression, substituted inputs with fact ids and
  provenance, assumptions with their human origin, unit and period -- so a person can re-derive
  it by hand from filed evidence. A value without that trace is not an output of this module.

A SCENARIO IS ARITHMETIC, NOT ADVICE. Assumptions are structurally separate from evidence (their
own list, their own label, their own origin) and each is required: there is no default discount
rate, growth rate, terminal value, margin, tax rate or horizon. A scenario definition whose
output would be a per-share price, a fair value, a target, an expected return, a rating or a
recommendation cannot be constructed at all, whatever it is called (``ProhibitedScenarioOutput``).

ISOLATION. Reads fact candidates passed in as arguments. No ledger, no holding, no price, no
position size, no database, no network, no clock, no model. Nothing computed here is stored as
a fact. Decimal throughout; ``make lint`` forbids an IEEE double in app/engine/.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any

from app.engine import research_formulas as _approved
from app.engine.research_rules import FactCandidate

ENGINE_VERSION = "research_metrics/1"

OK, UNKNOWN, CONFLICT = "OK", "UNKNOWN", "CONFLICT"

# --- reason codes (stable; the UI and tools key on them) ---------------------------------
FORMULA_NOT_APPROVED = "FORMULA_NOT_APPROVED"
OUTPUT_CONTRACT_NOT_APPROVED = "OUTPUT_CONTRACT_NOT_APPROVED"
FACT_NOT_FOUND = "FACT_NOT_FOUND"
FACT_NOT_REQUIRED = "FACT_NOT_REQUIRED"
UNVERIFIED = "UNVERIFIED"
SUPERSEDED = "SUPERSEDED"
NOT_NUMERIC = "NOT_NUMERIC"
BASIS_MISMATCH = "BASIS_MISMATCH"
UNIT_MISMATCH = "UNIT_MISMATCH"
CADENCE_MISMATCH = "CADENCE_MISMATCH"
PERIOD_MISMATCH = "PERIOD_MISMATCH"
PERIOD_NOT_PERMITTED = "PERIOD_NOT_PERMITTED"
ISIN_MISMATCH = "ISIN_MISMATCH"
MISSING_INPUT = "MISSING_INPUT"
SOURCE_CONFLICT = "SOURCE_CONFLICT"
NON_FINITE = "NON_FINITE"
OVERFLOW = "OVERFLOW"
ZERO_DENOMINATOR = "ZERO_DENOMINATOR"
UNDEFINED_ARITHMETIC = "UNDEFINED_ARITHMETIC"
RESULT_OUT_OF_RANGE = "RESULT_OUT_OF_RANGE"
MISSING_ASSUMPTION = "MISSING_ASSUMPTION"
ASSUMPTION_UNIT_MISMATCH = "ASSUMPTION_UNIT_MISMATCH"
ASSUMPTION_OUT_OF_RANGE = "ASSUMPTION_OUT_OF_RANGE"
ASSUMPTION_NOT_DECLARED = "ASSUMPTION_NOT_DECLARED"
ASSUMPTION_INVALID = "ASSUMPTION_INVALID"
ASSUMPTION_WITHOUT_REASON = "ASSUMPTION_WITHOUT_REASON"

#: NUMERIC(20,6) bounds: 14 integer digits, 6 fractional -- the same bound every stored
#: research value obeys (migration 040).
MAX_ABS = Decimal("99999999999999.999999")

#: Every intermediate is computed in this explicit context. 34 significant digits is IEEE
#: decimal128; ample for NUMERIC(20,6) operands, and stated rather than inherited.
_PRECISION = 34

ROUNDINGS = {"ROUND_HALF_EVEN": ROUND_HALF_EVEN, "ROUND_HALF_UP": ROUND_HALF_UP}

#: Input period roles. CURRENT: every CURRENT input shares one exact period. PRIOR: the
#: period ending the day before the CURRENT period starts, with the same cadence. Nothing
#: else is expressible -- no "latest available", no "nearest", no trailing window guessed
#: from whatever rows exist.
PERIOD_ROLES = ("CURRENT", "PRIOR")

_OPS = ("add", "sub", "mul", "div")


class DefinitionError(ValueError):
    """A formula/scenario definition is malformed. A registry build error, never a result."""


class ProhibitedScenarioOutput(DefinitionError):
    """A scenario definition whose output is a target, fair value, rating or recommendation.

    Raised when the registry is BUILT, so a prohibited shape cannot exist to be called. If an
    approved BE-Q03 entry ever trips this, that is a conflict to record under BE-Q03, not a
    check to loosen."""


# =========================================================================================
# definitions
# =========================================================================================


@dataclass(frozen=True)
class Approval:
    """Who approved a definition and where that is recorded. SYNTHETIC entries carry
    ``authority='SYNTHETIC_FIXTURE'`` and are refused as live definitions."""

    authority: str
    decided_on: str
    reference: str


@dataclass(frozen=True)
class InputSpec:
    name: str
    metric: str
    basis: str
    unit: str
    cadence: str
    period_role: str = "CURRENT"


@dataclass(frozen=True)
class AssumptionSpec:
    """A value the PERSON states. Range bounds are inclusive decimal strings, or None for an
    unbounded side (the definition says so explicitly; nothing is defaulted)."""

    name: str
    unit: str
    minimum: str | None
    maximum: str | None
    description: str


@dataclass(frozen=True)
class Precision:
    #: e.g. "0.000001" -- the exponent of the result.
    quantum: str
    rounding: str = "ROUND_HALF_EVEN"


@dataclass(frozen=True)
class FormulaDefinition:
    formula_id: str
    version: str
    title: str
    #: Closed expression tree: ["input", name] | ["assumption", name] | ["const", "decimal"]
    #: | [op, left, right] with op in add/sub/mul/div.
    expression: tuple[Any, ...]
    inputs: tuple[InputSpec, ...]
    output_unit: str
    precision: Precision
    approval: Approval
    #: Scenario only (BE 28). Empty for a metric.
    assumptions: tuple[AssumptionSpec, ...] = ()
    #: Scenario only: the approved non-recommendation output shape.
    output_kind: str | None = None
    output_contract: str | None = None
    kind: str = "METRIC"  # METRIC | SCENARIO

    @property
    def key(self) -> tuple[str, str]:
        return (self.formula_id, self.version)

    def canonical(self) -> dict[str, Any]:
        return {
            "formula_id": self.formula_id, "version": self.version, "kind": self.kind,
            "title": self.title, "expression": _tree_json(self.expression),
            "inputs": [vars(i) for i in self.inputs],
            "assumptions": [vars(a) for a in self.assumptions],
            "output_unit": self.output_unit, "output_kind": self.output_kind,
            "output_contract": self.output_contract,
            "precision": vars(self.precision), "approval": vars(self.approval),
        }

    def digest(self) -> str:
        body = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


def _tree_json(node: Any) -> Any:
    return [_tree_json(n) if isinstance(n, tuple) else n for n in node]


#: Scenario output kinds a definition may declare. Company-level, conditional quantities only.
PERMITTED_OUTPUT_KINDS = frozenset({"COMPANY_LEVEL_AMOUNT", "COMPANY_LEVEL_RATIO",
                                    "CONDITIONAL_SERIES_VALUE"})

#: What a scenario may never produce, however named (BE 28 rule 4).
PROHIBITED_OUTPUT_KINDS = frozenset({
    "PER_SHARE_VALUE", "PRICE_TARGET", "TARGET_PRICE", "FAIR_VALUE", "INTRINSIC_VALUE",
    "IMPLIED_PRICE", "EXPECTED_RETURN", "UPSIDE", "DOWNSIDE", "RATING", "RECOMMENDATION",
    "BUY_SELL_HOLD", "CONVICTION", "POSITION_VALUE", "RANGE_TARGET", "ILLUSTRATIVE_VALUE",
})
_PROHIBITED_UNIT_MARKERS = ("PER_SHARE", "/SHARE", "PER SHARE", "_PS")

#: Approved non-recommendation output contracts (BE-Q03 scenario boundary, decided 2026-09-29;
#: see ``app/engine/research_formulas.py``). A scenario whose contract is not listed stays
#: UNKNOWN / OUTPUT_CONTRACT_NOT_APPROVED even where its arithmetic is registered.
LIVE_OUTPUT_CONTRACTS: frozenset[str] = frozenset(_approved.OUTPUT_CONTRACTS)


def _check_tree(node: Any, inputs: set[str], assumptions: set[str], used: set[str]) -> None:
    if not isinstance(node, tuple) or not node:
        raise DefinitionError("expression node must be a non-empty tuple")
    tag = node[0]
    if tag == "input" and len(node) == 2:
        if node[1] not in inputs:
            raise DefinitionError(f"undeclared input {node[1]!r}")
        used.add(f"input:{node[1]}")
    elif tag == "assumption" and len(node) == 2:
        if node[1] not in assumptions:
            raise DefinitionError(f"undeclared assumption {node[1]!r}")
        used.add(f"assumption:{node[1]}")
    elif tag == "const" and len(node) == 2:
        try:
            d = Decimal(node[1])
        except InvalidOperation as exc:
            raise DefinitionError("constant must be a decimal string") from exc
        if not isinstance(node[1], str) or not d.is_finite():
            raise DefinitionError("constant must be a finite decimal string")
    elif tag in _OPS and len(node) == 3:
        _check_tree(node[1], inputs, assumptions, used)
        _check_tree(node[2], inputs, assumptions, used)
    else:
        raise DefinitionError(f"unknown expression node {tag!r}")


def validate_definition(d: FormulaDefinition) -> None:
    """Structural checks every entry must pass before it can sit in a registry."""
    names = [i.name for i in d.inputs]
    anames = [a.name for a in d.assumptions]
    if len(set(names)) != len(names) or len(set(anames)) != len(anames):
        raise DefinitionError("duplicate operand name")
    if set(names) & set(anames):
        raise DefinitionError("an operand cannot be both evidence and an assumption")
    for i in d.inputs:
        if i.period_role not in PERIOD_ROLES:
            raise DefinitionError(f"unknown period role {i.period_role!r}")
    if d.inputs and not any(i.period_role == "CURRENT" for i in d.inputs):
        raise DefinitionError("a formula needs at least one CURRENT input")
    if d.precision.rounding not in ROUNDINGS:
        raise DefinitionError("rounding must be named explicitly")
    try:
        q = Decimal(d.precision.quantum)
    except InvalidOperation as exc:
        raise DefinitionError("quantum must be a decimal string") from exc
    if not q.is_finite() or q <= 0 or q.as_tuple().exponent < -6:
        raise DefinitionError("quantum must be positive with at most 6 fractional digits")
    used: set[str] = set()
    _check_tree(d.expression, set(names), set(anames), used)
    declared = {f"input:{n}" for n in names} | {f"assumption:{n}" for n in anames}
    if used != declared:
        raise DefinitionError("every declared operand must appear in the expression")
    if d.kind == "METRIC":
        if d.assumptions or d.output_kind or d.output_contract:
            raise DefinitionError("a metric takes no assumptions and no scenario contract")
    elif d.kind == "SCENARIO":
        kind = (d.output_kind or "").upper()
        unit = d.output_unit.upper()
        if kind in PROHIBITED_OUTPUT_KINDS or any(m in unit for m in _PROHIBITED_UNIT_MARKERS):
            raise ProhibitedScenarioOutput(
                f"{d.formula_id}: a scenario cannot output {kind or unit} (BE-Q03 conflict)")
        if kind not in PERMITTED_OUTPUT_KINDS:
            raise ProhibitedScenarioOutput(f"{d.formula_id}: undeclared output kind {kind!r}")
        if not d.assumptions:
            raise DefinitionError("a scenario is defined by the person's assumptions")
    else:
        raise DefinitionError("kind must be METRIC or SCENARIO")


@dataclass(frozen=True)
class FormulaRegistry:
    """Approved definitions keyed by (id, version). There is no 'latest' lookup."""

    version: str
    formulas: tuple[FormulaDefinition, ...] = ()
    output_contracts: frozenset[str] = frozenset()
    synthetic: bool = False

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        for d in self.formulas:
            validate_definition(d)
            if d.key in seen:
                raise DefinitionError(f"duplicate formula {d.key}")
            seen.add(d.key)
            if not self.synthetic and d.approval.authority == "SYNTHETIC_FIXTURE":
                raise DefinitionError("a synthetic fixture formula cannot be a live definition")

    def get(self, formula_id: str, version: str) -> FormulaDefinition | None:
        for d in self.formulas:
            if d.key == (formula_id, version):
                return d
        return None

    def listing(self) -> list[dict[str, Any]]:
        return [{"formula_id": d.formula_id, "version": d.version, "kind": d.kind,
                 "title": d.title, "digest": d.digest(),
                 "output_contract_approved": d.kind == "METRIC"
                 or d.output_contract in self.output_contracts}
                for d in self.formulas]




# =========================================================================================
# results
# =========================================================================================


@dataclass(frozen=True)
class Assumption:
    """One person-stated scenario input. ``origin`` is who stated it (a user id); a model never
    reaches this type -- the tool refuses task-scoped (model-driven) scenario calls."""

    name: str
    value: str
    unit: str
    reason: str
    origin: str


@dataclass
class CalcResult:
    status: str
    formula_id: str
    formula_version: str
    kind: str = "METRIC"
    value: str | None = None
    unit: str | None = None
    period: dict[str, str] | None = None
    expression: str | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    definition_digest: str | None = None
    registry_version: str | None = None
    precision: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine_version": ENGINE_VERSION, "status": self.status, "kind": self.kind,
            "formula_id": self.formula_id, "formula_version": self.formula_version,
            "registry_version": self.registry_version,
            "definition_digest": self.definition_digest,
            "value": self.value, "unit": self.unit, "period": self.period,
            "expression": self.expression, "precision": self.precision,
            "inputs": self.inputs, "assumptions": self.assumptions,
            "reasons": self.reasons, "conflicts": self.conflicts,
        }


def _reason(code: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, **{k: v for k, v in detail.items() if v is not None}}


def _fact_trace(f: FactCandidate, name: str) -> dict[str, Any]:
    return {
        "operand": name, "label": "FILED_FACT", "fact_id": f.id, "source_id": f.source_id,
        "metric": f.metric, "basis": f.basis, "unit": f.unit, "cadence": f.cadence,
        "period_start": f.period_start.isoformat(), "period_end": f.period_end.isoformat(),
        "value": format(f.numeric_value, "f") if f.numeric_value is not None
        and f.numeric_value.is_finite() else None,
        "publication_date": f.publication_date.isoformat(),
        "verification": f.verification,
    }


# =========================================================================================
# evaluation
# =========================================================================================


def _finite_bounded(value: Decimal | None) -> str | None:
    if value is None:
        return NOT_NUMERIC
    if not value.is_finite():
        return NON_FINITE
    if abs(value) > MAX_ABS:
        return OVERFLOW
    return None


def _render(node: tuple[Any, ...], values: Mapping[str, Decimal]) -> str:
    tag = node[0]
    if tag in ("input", "assumption"):
        return f"{node[1]}[{format(values[f'{tag}:{node[1]}'], 'f')}]"
    if tag == "const":
        return node[1]
    symbol = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[tag]
    return f"({_render(node[1], values)} {symbol} {_render(node[2], values)})"


class _Undefined(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _eval(node: tuple[Any, ...], values: Mapping[str, Decimal], ctx: Context) -> Decimal:
    tag = node[0]
    if tag in ("input", "assumption"):
        return values[f"{tag}:{node[1]}"]
    if tag == "const":
        return Decimal(node[1])
    left = _eval(node[1], values, ctx)
    right = _eval(node[2], values, ctx)
    if tag == "div" and right == 0:
        raise _Undefined(ZERO_DENOMINATOR)
    try:
        if tag == "add":
            return ctx.add(left, right)
        if tag == "sub":
            return ctx.subtract(left, right)
        if tag == "mul":
            return ctx.multiply(left, right)
        return ctx.divide(left, right)
    except DivisionByZero as exc:  # pragma: no cover - guarded above
        raise _Undefined(ZERO_DENOMINATOR) from exc
    except Overflow as exc:
        raise _Undefined(OVERFLOW) from exc
    except InvalidOperation as exc:
        raise _Undefined(UNDEFINED_ARITHMETIC) from exc


def _prior_of(current: tuple[date, date], candidate: tuple[date, date]) -> bool:
    """PRIOR means the period ending the day before CURRENT starts. Mechanical adjacency,
    not a fiscal calendar: a gap or an overlap is not a prior period."""
    return candidate[1] + timedelta(days=1) == current[0]


def _assign_facts(
    d: FormulaDefinition,
    facts: Sequence[FactCandidate],
    requested_ids: Sequence[str],
    superseded_by: Mapping[str, str],
    family: frozenset[str] | None,
    result: CalcResult,
) -> dict[str, FactCandidate] | None:
    """Validate and bind each fact to exactly one declared input, or record why not."""
    by_id = {f.id: f for f in facts}
    for fid in requested_ids:
        if fid not in by_id:
            result.reasons.append(_reason(FACT_NOT_FOUND, fact_id=fid))
    metrics = {i.metric for i in d.inputs}
    candidates: dict[str, list[FactCandidate]] = {i.name: [] for i in d.inputs}
    for f in facts:
        if family is not None and f.isin not in family:
            result.reasons.append(_reason(ISIN_MISMATCH, fact_id=f.id))
            continue
        if f.metric not in metrics:
            result.reasons.append(_reason(FACT_NOT_REQUIRED, fact_id=f.id, metric=f.metric))
            continue
        if f.verification != "VERIFIED":
            result.reasons.append(_reason(UNVERIFIED, fact_id=f.id))
            continue
        if f.id in superseded_by:
            result.reasons.append(_reason(SUPERSEDED, fact_id=f.id,
                                          superseded_by=superseded_by[f.id]))
            continue
        problem = _finite_bounded(f.numeric_value)
        if problem is not None:
            result.reasons.append(_reason(problem, fact_id=f.id))
            continue
        matched = False
        for spec in d.inputs:
            if spec.metric != f.metric:
                continue
            mismatch = None
            if f.basis != spec.basis:
                mismatch = BASIS_MISMATCH
            elif f.unit != spec.unit:
                mismatch = UNIT_MISMATCH
            elif f.cadence != spec.cadence:
                mismatch = CADENCE_MISMATCH
            if mismatch:
                result.reasons.append(_reason(mismatch, fact_id=f.id, operand=spec.name,
                                              expected=f"{spec.basis}/{spec.unit}/"
                                              f"{spec.cadence}",
                                              got=f"{f.basis}/{f.unit}/{f.cadence}"))
                matched = True
                continue
            candidates[spec.name].append(f)
            matched = True
        if not matched:  # pragma: no cover - metric filtered above
            result.reasons.append(_reason(FACT_NOT_REQUIRED, fact_id=f.id))

    # A metric used under two roles (CURRENT and PRIOR) receives both candidates above; split
    # them by period once the CURRENT period is known.
    current_specs = [s for s in d.inputs if s.period_role == "CURRENT"]
    current_periods: set[tuple[date, date]] = set()
    for spec in current_specs:
        for f in candidates[spec.name]:
            current_periods.add((f.period_start, f.period_end))
    prior_metrics = {s.metric for s in d.inputs if s.period_role == "PRIOR"}
    if prior_metrics & {s.metric for s in current_specs} and len(current_periods) > 1:
        # Same metric in both roles: the CURRENT period is the latest-ENDING one only if the
        # other is exactly its prior. Anything else is ambiguous and refused.
        latest = max(current_periods, key=lambda p: p[1])
        others = current_periods - {latest}
        if all(_prior_of(latest, p) for p in others):
            current_periods = {latest}
    if len(current_periods) > 1:
        result.reasons.append(_reason(PERIOD_MISMATCH, periods=sorted(
            f"{a.isoformat()}..{b.isoformat()}" for a, b in current_periods)))
        return None
    current = next(iter(current_periods)) if current_periods else None

    bound: dict[str, FactCandidate] = {}
    placed: set[str] = set()
    offered: dict[str, str] = {}
    for spec in d.inputs:
        pool = candidates[spec.name]
        for f in pool:
            offered.setdefault(f.id, spec.name)
        if current is not None:
            if spec.period_role == "CURRENT":
                pool = [f for f in pool if (f.period_start, f.period_end) == current]
            else:
                pool = [f for f in pool if _prior_of(current, (f.period_start, f.period_end))]
        placed.update(f.id for f in pool)
        if not pool:
            result.reasons.append(_reason(MISSING_INPUT, operand=spec.name, metric=spec.metric,
                                          basis=spec.basis, unit=spec.unit,
                                          cadence=spec.cadence, role=spec.period_role))
            continue
        values = {f.numeric_value for f in pool}
        if len(values) > 1:
            result.conflicts.append({
                "operand": spec.name, "code": SOURCE_CONFLICT,
                "fact_ids": sorted(f.id for f in pool),
                "source_ids": sorted({f.source_id for f in pool}),
                "values": sorted(format(v, "f") for v in values if v is not None)})
            continue
        # Identical values from more than one fact: one number, every citation kept. This is
        # not claimed as independent corroboration.
        bound[spec.name] = sorted(pool, key=lambda f: f.id)[0]
        for extra in sorted(pool, key=lambda f: f.id)[1:]:
            result.inputs.append({**_fact_trace(extra, spec.name), "role": "SAME_VALUE_CITATION"})
    # A validated fact that fits no role's permitted period is refused by name, never dropped.
    for fid, operand in sorted(offered.items()):
        if fid not in placed:
            result.reasons.append(_reason(PERIOD_NOT_PERMITTED, fact_id=fid, operand=operand))
    return bound


def _check_assumptions(d: FormulaDefinition, supplied: Sequence[Assumption],
                       result: CalcResult) -> dict[str, Decimal]:
    by_name: dict[str, Assumption] = {}
    for a in supplied:
        if a.name in by_name:
            result.reasons.append(_reason(ASSUMPTION_INVALID, name=a.name, detail="DUPLICATE"))
            continue
        by_name[a.name] = a
    declared = {s.name for s in d.assumptions}
    for name in sorted(set(by_name) - declared):
        result.reasons.append(_reason(ASSUMPTION_NOT_DECLARED, name=name))
    values: dict[str, Decimal] = {}
    for spec in d.assumptions:
        a = by_name.get(spec.name)
        if a is None:
            result.reasons.append(_reason(MISSING_ASSUMPTION, name=spec.name, unit=spec.unit))
            continue
        if not a.reason or not a.reason.strip():
            result.reasons.append(_reason(ASSUMPTION_WITHOUT_REASON, name=spec.name))
            continue
        if a.unit != spec.unit:
            result.reasons.append(_reason(ASSUMPTION_UNIT_MISMATCH, name=spec.name,
                                          expected=spec.unit, got=a.unit))
            continue
        try:
            v = Decimal(a.value)
        except (InvalidOperation, TypeError):
            result.reasons.append(_reason(ASSUMPTION_INVALID, name=spec.name))
            continue
        problem = _finite_bounded(v)
        if problem is not None:
            result.reasons.append(_reason(problem, name=spec.name))
            continue
        if (spec.minimum is not None and v < Decimal(spec.minimum)) or \
                (spec.maximum is not None and v > Decimal(spec.maximum)):
            result.reasons.append(_reason(ASSUMPTION_OUT_OF_RANGE, name=spec.name,
                                          minimum=spec.minimum, maximum=spec.maximum))
            continue
        values[spec.name] = v
        result.assumptions.append({"operand": spec.name, "label": "ASSUMPTION",
                                   "value": a.value, "unit": a.unit, "reason": a.reason,
                                   "stated_by": a.origin})
    return values


def calculate(
    registry: FormulaRegistry,
    formula_id: str,
    formula_version: str,
    facts: Sequence[FactCandidate],
    requested_ids: Sequence[str] | None = None,
    superseded_by: Mapping[str, str] | None = None,
    assumptions: Sequence[Assumption] | None = None,
    family: Sequence[str] | None = None,
    expect_kind: str | None = None,
) -> CalcResult:
    """Evaluate one approved definition over explicit facts (and assumptions). Never raises for
    a content problem: every refusal is a status with reasons.

    ``facts`` are the candidates the caller loaded for ``requested_ids``; an id with no loaded
    fact is FACT_NOT_FOUND. ``superseded_by`` maps a fact id to its successor so a stale
    citation is refused rather than computed. ``family`` is the verified ISIN family; a fact
    for another company is refused.
    """
    result = CalcResult(status=UNKNOWN, formula_id=formula_id, formula_version=formula_version,
                        registry_version=registry.version)
    d = registry.get(formula_id, formula_version)
    if d is None or (expect_kind is not None and d.kind != expect_kind):
        result.reasons.append(_reason(FORMULA_NOT_APPROVED, formula_id=formula_id,
                                      formula_version=formula_version,
                                      gate="BE-Q03"))
        return result
    result.kind = d.kind
    result.definition_digest = d.digest()
    result.unit = d.output_unit
    result.precision = vars(d.precision)
    if d.kind == "SCENARIO" and d.output_contract not in registry.output_contracts:
        result.reasons.append(_reason(OUTPUT_CONTRACT_NOT_APPROVED, formula_id=formula_id,
                                      output_contract=d.output_contract, gate="BE-Q03"))
        # FORMULA_NOT_APPROVED as well, so a client keying on the single gate code sees
        # "not approved" rather than an evidence gap.
        result.reasons.append(_reason(FORMULA_NOT_APPROVED, formula_id=formula_id,
                                      formula_version=formula_version, gate="BE-Q03"))
        return result

    requested = list(requested_ids) if requested_ids is not None else [f.id for f in facts]
    bound = _assign_facts(d, facts, requested, superseded_by or {},
                          frozenset(family) if family is not None else None, result)
    assumed = _check_assumptions(d, assumptions or (), result) if d.kind == "SCENARIO" \
        else {}
    if d.kind == "METRIC" and assumptions:
        result.reasons.append(_reason(ASSUMPTION_NOT_DECLARED, detail="METRIC_TAKES_NONE"))

    if bound is not None:
        primary = [{**_fact_trace(bound[s.name], s.name), "role": s.period_role}
                   for s in d.inputs if s.name in bound]
        result.inputs[:0] = primary
    if result.conflicts:
        result.status = CONFLICT
        return result
    if bound is None or result.reasons or len(bound) != len(d.inputs) \
            or len(assumed) != len(d.assumptions):
        result.status = UNKNOWN
        return result

    values: dict[str, Decimal] = {f"input:{k}": v.numeric_value for k, v in bound.items()
                                  if v.numeric_value is not None}
    values.update({f"assumption:{k}": v for k, v in assumed.items()})
    current = next((bound[s.name] for s in d.inputs if s.period_role == "CURRENT"), None)
    if current is not None:
        result.period = {"period_start": current.period_start.isoformat(),
                         "period_end": current.period_end.isoformat(),
                         "cadence": current.cadence}
    result.expression = _render(d.expression, values)

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ctx.rounding = ROUNDINGS[d.precision.rounding]
        ctx.traps[InvalidOperation] = True
        ctx.traps[DivisionByZero] = True
        ctx.traps[Overflow] = True
        try:
            raw = _eval(d.expression, values, ctx)
            if not raw.is_finite():
                raise _Undefined(NON_FINITE)
            out = raw.quantize(Decimal(d.precision.quantum),
                               rounding=ROUNDINGS[d.precision.rounding], context=ctx)
        except _Undefined as exc:
            result.reasons.append(_reason(exc.code))
            return result
        except InvalidOperation:
            result.reasons.append(_reason(RESULT_OUT_OF_RANGE))
            return result
    if abs(out) > MAX_ABS:
        result.reasons.append(_reason(RESULT_OUT_OF_RANGE))
        return result
    result.value = format(out, "f")
    result.expression = f"{result.expression} = {format(raw, 'f')} -> {result.value}"
    result.status = OK
    return result


def evaluate_operands(d: FormulaDefinition, operands: Mapping[str, Decimal]
                      ) -> tuple[str, str | None, str | None]:
    """Re-run ONLY the arithmetic of an approved definition over already-validated operand
    values (keyed ``input:<name>`` / ``assumption:<name>``). Used by the report contract to
    reproduce a published calculated cell from its input cells -- the same expression, context
    and precision policy as ``calculate``, never a second implementation.

    Returns (status, value, reason_code)."""
    names = {f"input:{i.name}" for i in d.inputs} | {f"assumption:{a.name}"
                                                    for a in d.assumptions}
    if set(operands) != names:
        return UNKNOWN, None, MISSING_INPUT
    for v in operands.values():
        problem = _finite_bounded(v)
        if problem is not None:
            return UNKNOWN, None, problem
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ctx.rounding = ROUNDINGS[d.precision.rounding]
        ctx.traps[InvalidOperation] = True
        ctx.traps[DivisionByZero] = True
        ctx.traps[Overflow] = True
        try:
            raw = _eval(d.expression, operands, ctx)
            out = raw.quantize(Decimal(d.precision.quantum),
                               rounding=ROUNDINGS[d.precision.rounding], context=ctx)
        except _Undefined as exc:
            return UNKNOWN, None, exc.code
        except InvalidOperation:
            return UNKNOWN, None, RESULT_OUT_OF_RANGE
    if abs(out) > MAX_ABS:
        return UNKNOWN, None, RESULT_OUT_OF_RANGE
    return OK, format(out, "f"), None


# =========================================================================================
# loading a registry from JSON (fixtures, and later an approved registry file)
# =========================================================================================


def _tree(node: Any) -> tuple[Any, ...]:
    if not isinstance(node, list):
        raise DefinitionError("expression nodes are JSON arrays")
    return tuple(_tree(n) if isinstance(n, list) else n for n in node)


def registry_from_json(body: Mapping[str, Any], *, synthetic: bool) -> FormulaRegistry:
    """Build a registry from its JSON form. Numbers must be strings; a JSON number anywhere in
    a definition is refused (it would already be a binary double)."""
    def no_numbers(v: Any) -> None:
        if isinstance(v, bool) or v is None or isinstance(v, str):
            return
        if isinstance(v, list):
            for x in v:
                no_numbers(x)
        elif isinstance(v, dict):
            for x in v.values():
                no_numbers(x)
        else:
            # Any other scalar is a JSON number, i.e. already a binary value: refused.
            raise DefinitionError("numbers in a formula definition must be decimal strings")

    no_numbers(body.get("formulas", []))
    formulas = []
    for f in body.get("formulas", []):
        formulas.append(FormulaDefinition(
            formula_id=f["formula_id"], version=f["version"], title=f["title"],
            kind=f.get("kind", "METRIC"), expression=_tree(f["expression"]),
            inputs=tuple(InputSpec(**i) for i in f["inputs"]),
            assumptions=tuple(AssumptionSpec(**a) for a in f.get("assumptions", [])),
            output_unit=f["output_unit"], output_kind=f.get("output_kind"),
            output_contract=f.get("output_contract"),
            precision=Precision(**f["precision"]), approval=Approval(**f["approval"])))
    return FormulaRegistry(version=str(body["version"]), formulas=tuple(formulas),
                           output_contracts=frozenset(body.get("output_contracts", [])),
                           synthetic=synthetic)


#: The approved live registry (BE-Q03 decided 2026-09-29 under owner delegation). Definitions
#: live in ``app/engine/research_formulas.py``; docs/RESEARCH_CALCULATIONS.md is their prose.
#: Built here, after ``registry_from_json``, and validated on import like any registry.
LIVE_FORMULAS = registry_from_json(_approved.registry_body(), synthetic=False)
if LIVE_FORMULAS.output_contracts != LIVE_OUTPUT_CONTRACTS:  # pragma: no cover - wiring guard
    raise DefinitionError("live formula contracts diverged from LIVE_OUTPUT_CONTRACTS")
