"""The code-owned metric registry and reporting calendar the research routes validate against.

BE-Q03 was decided on 2026-09-29 (implementer-defined under owner delegation): the LIVE
registry is ``app/engine/research_formulas.LIVE_METRIC_CATALOGUE`` and the LIVE calendar is
SEBI LODR Reg 33 for Indian (``INE``) issuers. Consequences:

* ``POST /companies/{isin}/facts`` accepts exactly the catalogue's (metric, basis, unit,
  cadence) series and refuses anything else with 422 METRIC_NOT_APPROVED.
* A thesis rule on an approved series evaluates against the fiscal windows the calendar says
  are due; a foreign or mixed identity family still has no calendar and is UNKNOWN.

Tests supply their own versioned registry and calendar through the FastAPI dependencies
``get_metric_registry`` / ``get_calendar_provider`` in ``app/routers/workspace_deps.py``. A
registry change is a NEW version string; an old fact keeps the ``registry_version`` it was
accepted under (migration 049), and an old evaluation receipt keeps the exact definition body
it used (migration 050's ``research_definition``), so updating this never changes what a
historical record meant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.engine.research_formulas import LIVE_METRIC_CATALOGUE, india_lodr_calendar
from app.engine.research_rules import IST, IdentityScope, MetricRegistry, ReportingCalendar

#: The approved catalogue (BE-Q03, 2026-09-29). See docs/RESEARCH_CALCULATIONS.md.
LIVE_REGISTRY: MetricRegistry = LIVE_METRIC_CATALOGUE

#: The identity-scope policy version: VERIFIED security_alias edges only (migration 044's
#: ws_same_security). Bump it if that policy ever changes.
IDENTITY_POLICY_VERSION = "verified-alias-1"


class CalendarProvider:
    """Supplies one company's reporting calendar as of a cutoff.

    The base provider knows no calendar; ``approved`` is False. Tests subclass it.
    """

    approved = False

    def calendar_for(self, isins: Sequence[str], cutoff: datetime) -> ReportingCalendar:
        return ReportingCalendar(version="live-0", cadences={})


class LodrCalendarProvider(CalendarProvider):
    """SEBI LODR Reg 33 calendar for Indian issuers, evaluated on the cutoff's IST date."""

    approved = True

    def calendar_for(self, isins: Sequence[str], cutoff: datetime) -> ReportingCalendar:
        return india_lodr_calendar(tuple(sorted(isins)), cutoff.astimezone(IST).date())


LIVE_CALENDAR: CalendarProvider = LodrCalendarProvider()


def identity_scope(isins: Sequence[str]) -> IdentityScope:
    return IdentityScope(isins=tuple(sorted(isins)), version=IDENTITY_POLICY_VERSION)


@dataclass(frozen=True)
class FactTypeCheck:
    """Result of checking a FactCreate against the registry."""

    registry_version: str
    value_type: str


def check_fact_against_registry(
    registry: MetricRegistry,
    metric: str,
    basis: str,
    unit: str,
    cadence: str,
) -> FactTypeCheck | None:
    """The approved definition for this series, or None when it is not approved."""
    definition = registry.lookup(metric, basis, unit)
    if definition is None or cadence not in definition.cadences:
        return None
    return FactTypeCheck(registry_version=registry.version, value_type=definition.value_type)
