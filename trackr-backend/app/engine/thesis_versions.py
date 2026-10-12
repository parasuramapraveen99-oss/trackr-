"""Thesis versions: rule validation, DRAFT/ACTIVE/RETIRED classification, version order and
the commitment in force at a cutoff. Pure -- explicit inputs only.

docs/ENGINE_SPEC.md section 13 states these rules; BLUEPRINT_BACKEND.md sections 6.1 and 6.3
are their origin. This module is research logic, NOT a portfolio equation: nothing here
feeds holdings, XIRR or any figure derived from the ledger.

WHAT IT REFUSES, by construction:

* **No database, network, clock or model.** The observation cutoff is an argument. A
  function that read ``datetime.now()`` could not be replayed.
* **No rewriting of human prose.** Completeness is judged on a whitespace-stripped COPY;
  the stored argument, anti-thesis, pre-mortem and holding period are the caller's exact
  strings.
* **No guessing a malformed rule into a valid one.** The rule grammar mirrors migration
  041's ``ws_rules_valid`` exactly -- nine string keys, closed operator and cadence sets,
  a NUMERIC(20,6)-shaped decimal-string threshold, 1..99 periods, POINT means 1 period,
  unique ids -- and a violation raises ``RuleSyntaxError`` (HTTP 422). Incomplete PROSE is
  a DRAFT; a malformed EXECUTABLE rule is an error. Those are different things.
* **No choosing between alias histories.** If verified ISIN aliases expose two independent
  ACTIVE commitments, the result is CONFLICT, never the newer one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

RULE_KEYS = ("id", "label", "metric", "basis", "unit", "op", "threshold", "cadence", "periods")
OPERATORS = ("LT", "LE", "EQ", "GE", "GT", "IS_TRUE", "IS_FALSE")
BOOLEAN_OPERATORS = ("IS_TRUE", "IS_FALSE")
CADENCES = ("QUARTER", "YEAR", "POINT")
PROSE_FIELDS = ("argument", "anti_thesis", "pre_mortem", "intended_holding_period")

_DECIMAL = re.compile(r"^-?[0-9]{1,14}(\.[0-9]{1,6})?$")
_PERIODS = re.compile(r"^[1-9][0-9]?$")

STATUS_DRAFT = "DRAFT"
STATUS_ACTIVE = "ACTIVE"
STATUS_RETIRED = "RETIRED"

INTENT_SAVE = "SAVE"
INTENT_RETIRE = "RETIRE"


class RuleSyntaxError(ValueError):
    """A variable or exit rule is not a well-formed executable condition.

    ``reasons`` maps a JSON-pointer-ish location (``exits[0].threshold``) to a code.
    """

    def __init__(self, reasons: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in reasons.items()))
        self.reasons = dict(reasons)


class VersionConflictError(ValueError):
    """The caller's expected version or thesis id is not the branch's latest."""


class BlockingQuestionsOpenError(ValueError):
    """A blocking question in the verified ISIN family has no answer yet."""

    def __init__(self, question_ids: Sequence[str]) -> None:
        super().__init__(f"{len(question_ids)} blocking question(s) remain open")
        self.question_ids = tuple(question_ids)


@dataclass(frozen=True)
class Rule:
    """One exit or variable condition, exactly as the human supplied it. All strings."""

    id: str
    label: str
    metric: str
    basis: str
    unit: str
    op: str
    threshold: str
    cadence: str
    periods: str

    @property
    def threshold_decimal(self) -> Decimal | None:
        return None if self.op in BOOLEAN_OPERATORS else Decimal(self.threshold)

    @property
    def period_count(self) -> int:
        return int(self.periods)

    def as_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in RULE_KEYS}


def parse_rules(raw: object, field: str) -> tuple[Rule, ...]:
    """Validate a rules array against the closed grammar. Raises ``RuleSyntaxError``.

    Every scalar must already be a string: a JSON number, boolean or null is refused, not
    coerced -- ``10.5`` as a JSON number is an IEEE double and has no place in a condition
    that decides whether someone sells.
    """
    reasons: dict[str, str] = {}
    if not isinstance(raw, list):
        raise RuleSyntaxError({field: "NOT_AN_ARRAY"})
    seen: set[str] = set()
    rules: list[Rule] = []
    for index, item in enumerate(raw):
        where = f"{field}[{index}]"
        if not isinstance(item, dict):
            reasons[where] = "NOT_AN_OBJECT"
            continue
        keys = set(item)
        missing = [k for k in RULE_KEYS if k not in keys]
        extra = sorted(keys - set(RULE_KEYS))
        if missing:
            reasons[where] = "MISSING_KEYS:" + ",".join(missing)
            continue
        if extra:
            reasons[where] = "UNKNOWN_KEYS:" + ",".join(extra)
            continue
        bad_type = [k for k in RULE_KEYS if not isinstance(item[k], str)]
        if bad_type:
            for key in bad_type:
                reasons[f"{where}.{key}"] = "NOT_A_STRING"
            continue
        blank = [k for k in RULE_KEYS if not item[k].strip()]
        if blank:
            for key in blank:
                reasons[f"{where}.{key}"] = "BLANK"
            continue
        if item["op"] not in OPERATORS:
            reasons[f"{where}.op"] = "UNKNOWN_OPERATOR"
        if item["cadence"] not in CADENCES:
            reasons[f"{where}.cadence"] = "UNKNOWN_CADENCE"
        if not _PERIODS.match(item["periods"]):
            reasons[f"{where}.periods"] = "OUT_OF_RANGE"
        elif item["cadence"] == "POINT" and item["periods"] != "1":
            reasons[f"{where}.periods"] = "POINT_IS_ONE_PERIOD"
        if item["op"] in BOOLEAN_OPERATORS:
            if item["threshold"] != "boolean":
                reasons[f"{where}.threshold"] = "BOOLEAN_THRESHOLD_MUST_BE_LITERAL"
        elif not _DECIMAL.match(item["threshold"]):
            reasons[f"{where}.threshold"] = "NOT_A_DECIMAL"
        else:
            try:
                if not Decimal(item["threshold"]).is_finite():  # pragma: no cover - regex
                    reasons[f"{where}.threshold"] = "NOT_FINITE"
            except InvalidOperation:  # pragma: no cover - regex excludes
                reasons[f"{where}.threshold"] = "NOT_A_DECIMAL"
        if item["id"] in seen:
            reasons[f"{where}.id"] = "DUPLICATE_ID"
        seen.add(item["id"])
        rules.append(Rule(**{k: item[k] for k in RULE_KEYS}))
    if reasons:
        raise RuleSyntaxError(reasons)
    return tuple(rules)


@dataclass(frozen=True)
class FieldReason:
    field: str
    code: str

    def encoded(self) -> str:
        """The form stored in thesis.draft_reasons (text[])."""
        return f"{self.field}:{self.code}"


@dataclass(frozen=True)
class Classification:
    status: str
    draft_reasons: tuple[FieldReason, ...]


def assert_publishable(open_blocking_question_ids: Sequence[str]) -> None:
    """Refuse ANY save -- DRAFT included -- while a blocking question is unanswered."""
    if open_blocking_question_ids:
        raise BlockingQuestionsOpenError(open_blocking_question_ids)


def classify(
    intent: str,
    prose: Mapping[str, str],
    variables: Sequence[Rule],
    exits: Sequence[Rule],
    *,
    branch_exists: bool,
) -> Classification:
    """DRAFT with field reasons, ACTIVE, or RETIRED. The client never chooses.

    RETIRE is an explicit human request on an existing branch; it is never inferred from a
    sale, an exit condition or elapsed time.
    """
    if intent == INTENT_RETIRE:
        if not branch_exists:
            raise VersionConflictError("there is no thesis to retire")
        return Classification(STATUS_RETIRED, ())
    if intent != INTENT_SAVE:
        raise ValueError(f"unknown intent {intent!r}")
    reasons = [FieldReason(name, "MISSING") for name in PROSE_FIELDS
               if not (prose.get(name) or "").strip()]
    if not variables:
        reasons.append(FieldReason("variables", "EMPTY"))
    if not exits:
        reasons.append(FieldReason("exits", "EMPTY"))
    if reasons:
        return Classification(STATUS_DRAFT, tuple(reasons))
    return Classification(STATUS_ACTIVE, ())


@dataclass(frozen=True)
class VersionRecord:
    """One stored thesis version, as the selector needs it."""

    id: str
    isin: str
    version: int
    status: str
    formed_on: date
    version_on: date
    recorded_at: datetime


def next_version(
    existing: Sequence[VersionRecord], expected_version: int, supplied_id: str | None
) -> int:
    """The version number to allocate, or ``VersionConflictError``.

    ``expected_version`` must equal the branch's latest version (0 for a new branch) and,
    when a thesis id is supplied, that id must be the latest version's row. The database
    re-checks sequential allocation under the tenant lock (migration 044).
    """
    latest = max(existing, key=lambda v: v.version, default=None)
    current = latest.version if latest else 0
    if expected_version != current:
        raise VersionConflictError(
            f"expected version {expected_version}, but the latest is {current}"
        )
    if supplied_id is not None and (latest is None or latest.id != supplied_id):
        raise VersionConflictError("the supplied thesis is not the latest version")
    return current + 1


def formation_date(existing: Sequence[VersionRecord], proposed: date) -> date:
    """Version 1's formed_on, which every later version must carry unchanged."""
    first = min(existing, key=lambda v: v.version, default=None)
    if first is not None and first.formed_on != proposed:
        raise VersionConflictError("formed_on is fixed by the first version")
    return proposed


def in_force(versions: Sequence[VersionRecord], cutoff: datetime) -> VersionRecord | None:
    """The commitment in force at ``cutoff``: the latest non-DRAFT version RECEIVED by then.

    Version/receipt order is authoritative; a backdated ``version_on`` does not move a
    version earlier. ACTIVE is in force; RETIRED means nothing is. A later DRAFT neither
    displaces an ACTIVE commitment nor undoes a retirement.
    """
    known = sorted((v for v in versions if v.recorded_at <= cutoff),
                   key=lambda v: v.version, reverse=True)
    for version in known:
        if version.status == STATUS_DRAFT:
            continue
        return version if version.status == STATUS_ACTIVE else None
    return None


@dataclass(frozen=True)
class CommitmentSelection:
    """ACTIVE with one thesis, NONE, or CONFLICT with every competing in-force version."""

    state: str
    thesis: VersionRecord | None
    candidates: tuple[VersionRecord, ...]


def select_commitment(
    branches: Mapping[str, Sequence[VersionRecord]], cutoff: datetime
) -> CommitmentSelection:
    """In-force selection across a VERIFIED identity family's per-ISIN branches.

    Each original ISIN keeps its own branch. Two branches each with an ACTIVE commitment is
    a CONFLICT the person must resolve -- never a silent choice by timestamp or a merge.
    """
    active = [v for isin in sorted(branches)
              if (v := in_force(branches[isin], cutoff)) is not None]
    if len(active) > 1:
        return CommitmentSelection("CONFLICT", None, tuple(active))
    if len(active) == 1:
        return CommitmentSelection("ACTIVE", active[0], tuple(active))
    return CommitmentSelection("NONE", None, ())
