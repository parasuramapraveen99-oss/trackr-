"""Deterministic evaluation of a thesis's own stated conditions over sourced facts. Pure.

docs/ENGINE_SPEC.md section 14 states these rules (from BLUEPRINT_BACKEND.md sections 6.1-6.2).
This is research logic and is kept apart from every portfolio equation: nothing here reads
or produces a holding, price, transaction or return.

INPUTS ARE EXPLICIT AND COMPLETE. Rules, typed fact candidates with provenance, the evidence
cutoff, a versioned metric registry, a versioned reporting calendar and the identity family
all arrive as arguments. There is no database, network, model, environment lookup or clock.
The same complete input always produces the same result and the same digest.

WHAT IT DOES NOT DO, deliberately:

* **Approve anything.** A metric the registry does not define is UNKNOWN/METRIC_UNAVAILABLE;
  a cadence the calendar does not cover is UNKNOWN/CALENDAR_UNKNOWN. No formula, filing
  deadline or fiscal calendar is inferred here (BE-Q03/Q04 open).
* **Substitute evidence.** "N consecutive periods" means N ADJACENT required windows ending
  at the calendar's required latest period -- never the last N rows that happen to exist.
  A missing window is UNKNOWN, never NOT_MET and never zero.
* **Resolve disagreement.** Two eligible unsuperseded verified values that differ in one
  required window are CONFLICT with both citations. Never an average, never latest-wins.
* **Convert.** A different basis or unit is a different series. No conversion by guess.

Decimal throughout; an IEEE double never appears here (`make lint` enforces it for app/engine/).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.engine.thesis_versions import BOOLEAN_OPERATORS, Rule

ENGINE_VERSION = "research_rules/1"
#: Canonical-input schema version recorded in every replay manifest.
INPUT_SCHEMA_VERSION = "1"

#: India has observed a fixed +05:30 offset with no DST since 1945 (see migration 040).
IST = timezone(timedelta(hours=5, minutes=30))

#: NUMERIC(20,6) bounds: 14 integer digits, 6 fractional.
_MAX_ABS = Decimal("99999999999999.999999")

# --- states and reason codes -------------------------------------------------------------
MET, NOT_MET, UNKNOWN, CONFLICT = "MET", "NOT_MET", "UNKNOWN", "CONFLICT"
TRIGGERED, NOT_TRIGGERED = "TRIGGERED", "NOT_TRIGGERED"

MISSING_PERIOD = "MISSING_PERIOD"
UNVERIFIED = "UNVERIFIED"
BASIS_MISMATCH = "BASIS_MISMATCH"
SOURCE_CONFLICT = "SOURCE_CONFLICT"
CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
STALE = "STALE"
INVALID_INPUT = "INVALID_INPUT"
#: Added to the blueprint's list: the registry has no approved definition for this
#: (metric, basis, unit). CALENDAR_UNKNOWN would misstate it and INVALID_INPUT would call a
#: well-formed rule malformed.
METRIC_UNAVAILABLE = "METRIC_UNAVAILABLE"
#: Added: an otherwise-matching fact was recorded, retrieved or published after the cutoff.
FUTURE_EVIDENCE = "FUTURE_EVIDENCE"


class EvaluationInputError(ValueError):
    """The input itself is malformed -- an error, distinct from UNKNOWN uncertainty."""


# --- definitions -------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDefinition:
    metric: str
    basis: str
    unit: str
    #: NUMERIC | BOOLEAN. Text is never a comparable type.
    value_type: str
    cadences: tuple[str, ...]
    #: The filed statement this series is read from (BALANCE_SHEET | PROFIT_AND_LOSS |
    #: CASH_FLOW | NOTES | SEGMENTS | SHAREHOLDING), part of the approved definition (BE 30).
    #: Optional; omitted from ``canonical`` when unset so existing registry digests are stable.
    statement: str | None = None


@dataclass(frozen=True)
class MetricRegistry:
    version: str
    metrics: tuple[MetricDefinition, ...] = ()

    def lookup(self, metric: str, basis: str, unit: str) -> MetricDefinition | None:
        for definition in self.metrics:
            if (definition.metric, definition.basis, definition.unit) == (metric, basis, unit):
                return definition
        return None

    def canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "metrics": sorted(
                ({"metric": m.metric, "basis": m.basis, "unit": m.unit,
                  "value_type": m.value_type, "cadences": sorted(m.cadences),
                  **({"statement": m.statement} if m.statement else {})}
                 for m in self.metrics),
                key=lambda m: (m["metric"], m["basis"], m["unit"]),
            ),
        }

    @classmethod
    def from_canonical(cls, body: Mapping[str, Any]) -> MetricRegistry:
        return cls(
            version=str(body["version"]),
            metrics=tuple(
                MetricDefinition(m["metric"], m["basis"], m["unit"], m["value_type"],
                                 tuple(m["cadences"]), m.get("statement"))
                for m in body.get("metrics", [])
            ),
        )


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def as_dict(self) -> dict[str, str]:
        return {"period_start": self.start.isoformat(), "period_end": self.end.isoformat()}


@dataclass(frozen=True)
class CadenceCalendar:
    """One company's fiscal periods for one cadence, ascending, adjacent, non-overlapping,
    and the most recent period whose report is DUE at the cutoff (None: nothing due yet)."""

    periods: tuple[Window, ...]
    required_latest: Window | None


@dataclass(frozen=True)
class ReportingCalendar:
    version: str
    cadences: Mapping[str, CadenceCalendar] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cadences": {
                cadence: {
                    "periods": [w.as_dict() for w in cal.periods],
                    "required_latest": cal.required_latest.as_dict()
                    if cal.required_latest else None,
                }
                for cadence, cal in sorted(self.cadences.items())
            },
        }

    @classmethod
    def from_canonical(cls, body: Mapping[str, Any]) -> ReportingCalendar:
        def window(d: Mapping[str, str] | None) -> Window | None:
            if d is None:
                return None
            return Window(date.fromisoformat(d["period_start"]),
                          date.fromisoformat(d["period_end"]))

        return cls(
            version=str(body["version"]),
            cadences={
                cadence: CadenceCalendar(
                    periods=tuple(w for w in (window(p) for p in cal["periods"]) if w),
                    required_latest=window(cal.get("required_latest")),
                )
                for cadence, cal in body.get("cadences", {}).items()
            },
        )


@dataclass(frozen=True)
class IdentityScope:
    """The verified ISIN family the repository prepared. Never looked up here."""

    isins: tuple[str, ...]
    version: str

    def canonical(self) -> dict[str, Any]:
        return {"isins": sorted(self.isins), "version": self.version}


@dataclass(frozen=True)
class FactCandidate:
    id: str
    isin: str
    metric: str
    basis: str
    unit: str
    cadence: str
    period_start: date
    period_end: date
    numeric_value: Decimal | None
    boolean_value: bool | None
    text_value: str | None
    verification: str
    source_id: str
    publication_date: date
    retrieved_at: datetime
    recorded_at: datetime
    supersedes_id: str | None

    def canonical(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "isin": self.isin,
            "metric": self.metric,
            "basis": self.basis,
            "unit": self.unit,
            "cadence": self.cadence,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "numeric_value": format(self.numeric_value, "f")
            if self.numeric_value is not None and self.numeric_value.is_finite()
            else (str(self.numeric_value) if self.numeric_value is not None else None),
            "boolean_value": self.boolean_value,
            "text_value": self.text_value,
            "verification": self.verification,
            "source_id": self.source_id,
            "publication_date": self.publication_date.isoformat(),
            "retrieved_at": _utc(self.retrieved_at),
            "recorded_at": _utc(self.recorded_at),
            "supersedes_id": self.supersedes_id,
        }


def _utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise EvaluationInputError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


# --- results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowResult:
    window: Window
    state: str  # VALUE | UNKNOWN | CONFLICT
    value: Decimal | bool | None
    fact_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    publication_date: date | None
    reason: str | None
    comparison: bool | None


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    kind: str  # VARIABLE | EXIT
    state: str
    required_windows: tuple[Window, ...]
    windows: tuple[WindowResult, ...]
    fact_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    comparator: str
    threshold: str
    latest: Decimal | bool | None
    latest_publication_date: date | None
    met_on: date | None
    reason_codes: tuple[str, ...]

    @property
    def ok(self) -> bool | None:
        """Variable display: true/false for MET/NOT_MET, null otherwise."""
        return {MET: True, NOT_MET: False}.get(self.state)


@dataclass(frozen=True)
class Evaluation:
    thesis_id: str
    state: str
    variables: tuple[RuleResult, ...]
    exits: tuple[RuleResult, ...]
    evidence_cutoff: datetime
    engine_version: str
    registry_version: str
    calendar_version: str
    input_digest: str
    considered_fact_ids: tuple[str, ...]


# --- the algorithm -----------------------------------------------------------------------


def required_windows(rule: Rule, calendar: ReportingCalendar) -> tuple[Window, ...] | None:
    """N adjacent fiscal windows ending at the required latest period, or None (unknown).

    Raises ``EvaluationInputError`` for a calendar whose periods overlap or leave gaps -- a
    malformed calendar is an error, not uncertainty.
    """
    cal = calendar.cadences.get(rule.cadence)
    if cal is None or cal.required_latest is None:
        return None
    periods = cal.periods
    for earlier, later in zip(periods, periods[1:], strict=False):
        if later.start != earlier.end + timedelta(days=1):
            raise EvaluationInputError(
                f"calendar {calendar.version} {rule.cadence} periods are not adjacent"
            )
    for period in periods:
        if period.end < period.start:
            raise EvaluationInputError("calendar period ends before it starts")
    if cal.required_latest not in periods:
        return None
    end_index = periods.index(cal.required_latest)
    count = rule.period_count
    if end_index + 1 < count:
        return None
    return tuple(periods[end_index - count + 1:end_index + 1])


def _eligible_time(fact: FactCandidate, cutoff: datetime) -> bool:
    cutoff_ist_date = cutoff.astimezone(IST).date()
    return (fact.publication_date <= cutoff_ist_date
            and fact.retrieved_at <= cutoff and fact.recorded_at <= cutoff)


def _value(
    fact: FactCandidate, definition: MetricDefinition
) -> tuple[Decimal | bool | None, str | None]:
    if definition.value_type == "NUMERIC":
        value = fact.numeric_value
        if value is None:
            return None, INVALID_INPUT
        if not value.is_finite() or abs(value) > _MAX_ABS:
            return None, INVALID_INPUT
        return value, None
    if definition.value_type == "BOOLEAN":
        if fact.boolean_value is None:
            # A text opinion is not a verified boolean.
            return None, UNVERIFIED if fact.text_value is not None else INVALID_INPUT
        return fact.boolean_value, None
    return None, INVALID_INPUT


def _compare(op: str, value: Decimal | bool, threshold: Decimal | None) -> bool:
    if op == "IS_TRUE":
        return value is True
    if op == "IS_FALSE":
        return value is False
    assert isinstance(value, Decimal) and threshold is not None
    return {
        "LT": value < threshold, "LE": value <= threshold, "EQ": value == threshold,
        "GE": value >= threshold, "GT": value > threshold,
    }[op]


def _evaluate_window(
    rule: Rule,
    definition: MetricDefinition,
    window: Window,
    is_newest: bool,
    facts: Sequence[FactCandidate],
    family: frozenset[str],
    cutoff: datetime,
) -> WindowResult:
    def unknown(reason: str) -> WindowResult:
        return WindowResult(window, UNKNOWN, None, (), (), None, reason, None)

    same_period = [f for f in facts
                   if f.isin in family and f.metric == rule.metric and f.cadence == rule.cadence
                   and f.period_start == window.start and f.period_end == window.end]
    series = [f for f in same_period if f.basis == rule.basis and f.unit == rule.unit]
    if not series:
        if same_period:
            return unknown(BASIS_MISMATCH)
        return unknown(STALE if is_newest else MISSING_PERIOD)

    timely = [f for f in series if _eligible_time(f, cutoff)]
    if not timely:
        return unknown(FUTURE_EVIDENCE)

    # Supersession as of the cutoff: only a successor that was itself known by then counts.
    superseded = {f.supersedes_id for f in timely if f.supersedes_id}
    heads = [f for f in timely if f.id not in superseded]
    verified = [f for f in heads if f.verification == "VERIFIED"]
    if not verified:
        # Declared policy: an UNVERIFIED same-source correction supersedes the verified value
        # it corrects, so the window is UNKNOWN/UNVERIFIED -- neither the old value nor the
        # unverified new one is used.
        return unknown(UNVERIFIED)

    typed: list[tuple[FactCandidate, Decimal | bool]] = []
    for fact in verified:
        value, reason = _value(fact, definition)
        if reason is not None:
            return unknown(reason)
        typed.append((fact, value))

    distinct = {(type(v).__name__, v) for _, v in typed}
    fact_ids = tuple(sorted(f.id for f, _ in typed))
    source_ids = tuple(sorted({f.source_id for f, _ in typed}))
    publication = max(f.publication_date for f, _ in typed)
    if len(distinct) > 1:
        return WindowResult(window, CONFLICT, None, fact_ids, source_ids, publication,
                            SOURCE_CONFLICT, None)
    value = typed[0][1]
    return WindowResult(window, "VALUE", value, fact_ids, source_ids, publication, None,
                        _compare(rule.op, value, rule.threshold_decimal))


def evaluate_rule(
    rule: Rule,
    kind: str,
    facts: Sequence[FactCandidate],
    cutoff: datetime,
    registry: MetricRegistry,
    calendar: ReportingCalendar,
    identity: IdentityScope,
) -> RuleResult:
    """One rule over every required window, with the fixed precedence:
    any CONFLICT -> CONFLICT; else any unknown window -> UNKNOWN; else all true -> MET;
    else NOT_MET. An incomplete rule is never NOT_MET because one known period fails."""

    def result(state: str, reasons: tuple[str, ...], windows: tuple[Window, ...] = (),
               window_results: tuple[WindowResult, ...] = ()) -> RuleResult:
        proving = [w for w in window_results if w.state == "VALUE"]
        latest = window_results[-1] if window_results else None
        return RuleResult(
            rule_id=rule.id, kind=kind, state=state, required_windows=windows,
            windows=window_results,
            fact_ids=tuple(sorted({i for w in window_results for i in w.fact_ids})),
            source_ids=tuple(sorted({i for w in window_results for i in w.source_ids})),
            comparator=rule.op, threshold=rule.threshold,
            latest=latest.value if latest and latest.state == "VALUE" else None,
            latest_publication_date=latest.publication_date if latest else None,
            met_on=max((w.publication_date for w in proving if w.publication_date),
                       default=None) if state == MET else None,
            reason_codes=reasons,
        )

    definition = registry.lookup(rule.metric, rule.basis, rule.unit)
    if definition is None:
        return result(UNKNOWN, (METRIC_UNAVAILABLE,))
    numeric_rule = rule.op not in BOOLEAN_OPERATORS
    if (rule.cadence not in definition.cadences
            or (definition.value_type == "NUMERIC") != numeric_rule):
        return result(UNKNOWN, (INVALID_INPUT,))
    windows = required_windows(rule, calendar)
    if windows is None:
        return result(UNKNOWN, (CALENDAR_UNKNOWN,))

    family = frozenset(identity.isins)
    evaluated = tuple(
        _evaluate_window(rule, definition, window, index == len(windows) - 1, facts, family,
                         cutoff)
        for index, window in enumerate(windows)
    )
    reasons = tuple(dict.fromkeys(w.reason for w in evaluated if w.reason))
    if any(w.state == CONFLICT for w in evaluated):
        return result(CONFLICT, reasons, windows, evaluated)
    if any(w.state == UNKNOWN for w in evaluated):
        return result(UNKNOWN, reasons, windows, evaluated)
    if all(w.comparison for w in evaluated):
        return result(MET, (), windows, evaluated)
    return result(NOT_MET, (), windows, evaluated)


def aggregate_exits(exits: Sequence[RuleResult]) -> str:
    """OR over exits: any MET -> TRIGGERED; else any CONFLICT -> CONFLICT; else any
    UNKNOWN -> UNKNOWN; else NOT_TRIGGERED. No exits at all is UNKNOWN."""
    states = [r.state for r in exits]
    if MET in states:
        return TRIGGERED
    if CONFLICT in states:
        return CONFLICT
    if UNKNOWN in states or not states:
        return UNKNOWN
    return NOT_TRIGGERED


def _dedupe_facts(facts: Sequence[FactCandidate]) -> tuple[FactCandidate, ...]:
    by_id: dict[str, FactCandidate] = {}
    for fact in facts:
        prior = by_id.get(fact.id)
        if prior is not None and prior.canonical() != fact.canonical():
            raise EvaluationInputError(f"fact {fact.id} supplied twice with different contents")
        by_id[fact.id] = fact
    return tuple(by_id[i] for i in sorted(by_id))


def canonical_input(
    thesis_id: str,
    variables: Sequence[Rule],
    exits: Sequence[Rule],
    facts: Sequence[FactCandidate],
    cutoff: datetime,
    registry: MetricRegistry,
    calendar: ReportingCalendar,
    identity: IdentityScope,
) -> dict[str, Any]:
    """Every input that can change the result, in an order-independent canonical form."""
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "thesis_id": thesis_id,
        "evidence_cutoff": _utc(cutoff),
        "variables": [r.as_dict() for r in variables],
        "exits": [r.as_dict() for r in exits],
        "facts": [f.canonical() for f in _dedupe_facts(facts)],
        "registry": registry.canonical(),
        "calendar": calendar.canonical(),
        "identity": identity.canonical(),
    }


def evaluate(
    thesis_id: str,
    status: str,
    variables: Sequence[Rule],
    exits: Sequence[Rule],
    facts: Sequence[FactCandidate],
    cutoff: datetime,
    registry: MetricRegistry,
    calendar: ReportingCalendar,
    identity: IdentityScope,
    digest: Any,
) -> Evaluation:
    """Evaluate an ACTIVE commitment. ``digest`` is the canonical-JSON SHA-256 function
    (``app/research/canonical.py::digest``), passed in so this module stays import-pure.

    A DRAFT or RETIRED thesis cannot be evaluated as a live trigger; the caller must not
    pass one, and this refuses rather than returning a result that looks like one.
    """
    if status != "ACTIVE":
        raise EvaluationInputError("only an ACTIVE commitment can be evaluated")
    if cutoff.tzinfo is None:
        raise EvaluationInputError("evidence cutoff must be timezone-aware")
    unique = _dedupe_facts(facts)
    variable_results = tuple(
        evaluate_rule(r, "VARIABLE", unique, cutoff, registry, calendar, identity)
        for r in variables
    )
    exit_results = tuple(
        evaluate_rule(r, "EXIT", unique, cutoff, registry, calendar, identity) for r in exits
    )
    input_digest = digest(canonical_input(thesis_id, variables, exits, unique, cutoff,
                                          registry, calendar, identity))
    return Evaluation(
        thesis_id=thesis_id,
        state=aggregate_exits(exit_results),
        variables=variable_results,
        exits=exit_results,
        evidence_cutoff=cutoff,
        engine_version=ENGINE_VERSION,
        registry_version=registry.version,
        calendar_version=calendar.version,
        input_digest=input_digest,
        considered_fact_ids=tuple(f.id for f in unique),
    )


# --- serialisation -----------------------------------------------------------------------


def _scalar(value: Decimal | bool | None) -> str | bool | None:
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def rule_result_dict(result: RuleResult) -> dict[str, Any]:
    """String-only JSON form (booleans excepted) -- what trigger_evaluation.rule_results stores."""
    return {
        "rule_id": result.rule_id,
        "kind": result.kind,
        "state": result.state,
        "ok": result.ok if result.kind == "VARIABLE" else None,
        "required_windows": [w.as_dict() for w in result.required_windows],
        "windows": [
            {**w.window.as_dict(), "state": w.state, "value": _scalar(w.value),
             "fact_ids": list(w.fact_ids), "source_ids": list(w.source_ids),
             "publication_date": w.publication_date.isoformat() if w.publication_date else None,
             "reason": w.reason, "comparison": w.comparison}
            for w in result.windows
        ],
        "fact_ids": list(result.fact_ids),
        "source_ids": list(result.source_ids),
        "comparator": result.comparator,
        "threshold": result.threshold,
        "latest": _scalar(result.latest),
        "latest_publication_date": result.latest_publication_date.isoformat()
        if result.latest_publication_date else None,
        "met_on": result.met_on.isoformat() if result.met_on else None,
        "reason_codes": list(result.reason_codes),
    }


def evaluation_dict(evaluation: Evaluation) -> dict[str, Any]:
    return {
        "thesis_id": evaluation.thesis_id,
        "state": evaluation.state,
        "variables": [rule_result_dict(r) for r in evaluation.variables],
        "exits": [rule_result_dict(r) for r in evaluation.exits],
        "evidence_cutoff": _utc(evaluation.evidence_cutoff),
        "engine_version": evaluation.engine_version,
        "registry_version": evaluation.registry_version,
        "calendar_version": evaluation.calendar_version,
        "input_digest": evaluation.input_digest,
        "considered_fact_ids": list(evaluation.considered_fact_ids),
    }
