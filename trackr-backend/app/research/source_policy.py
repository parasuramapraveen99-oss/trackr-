"""Per-source-type, per-use rights verdicts for research material (BE-Q04). Open question.

A licensing verdict is per source AND per use. This module covers the four uses session 12
performs -- recording citation METADATA, STORING bytes, EXTRACTING text and READING retained
text back. Export is ``app/db/workspace_privacy.py::EXPORT_VERDICTS``; disclosure to a model
is ``USE_MODEL_DISCLOSE`` (BE 18/20). They are separate permissions and none implies another.

**UNTIL THE OWNER APPROVES ``policy/proposal.json``, EVERY LIVE VERDICT IS UNDECIDED, AND
UNDECIDED DENIES** (one-step approval: ``make approve-research-policy``, 2026-09-29 design).
Until then nobody has recorded that the system may store an exchange filing, a transcript or
even the investor's own upload, so the live policy refuses each use with 403
SOURCE_USE_DENIED. That is the honest state, not a
placeholder: live research writes are separately blocked by
``workspace_privacy.readiness_blockers`` anyway. Tests supply a synthetic ``SourcePolicy``
through the ``get_source_policy`` dependency; no verdict may be added here without the
owner's decision recorded in BACKEND_OPEN_QUESTIONS.md BE-Q04.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.research.errors import WorkspaceError

USE_METADATA = "METADATA"
USE_STORE = "STORE"
USE_EXTRACT = "EXTRACT"
USE_READ = "READ"
#: Releasing retained text to the person's chosen model endpoint (BE 18/20). A separate
#: permission: internal READ rights say nothing about disclosure to a third-party provider.
USE_MODEL_DISCLOSE = "MODEL_DISCLOSE"
USES = (USE_METADATA, USE_STORE, USE_EXTRACT, USE_READ, USE_MODEL_DISCLOSE)

ALLOWED = "ALLOWED"
DENIED = "DENIED"
UNDECIDED = "UNDECIDED"

SOURCE_TYPES = (
    "EXCHANGE_FILING", "ANNUAL_REPORT", "RESULTS", "PRESENTATION", "TRANSCRIPT",
    "SHAREHOLDING", "NEWS", "MANAGEMENT_COMMENTARY", "USER_DOCUMENT", "BOOK",
)


@dataclass(frozen=True)
class SourcePolicy:
    """(source_type, use) -> verdict. Anything absent is UNDECIDED."""

    version: str
    verdicts: Mapping[tuple[str, str], str] = field(default_factory=dict)

    def verdict(self, source_type: str, use: str) -> str:
        return self.verdicts.get((source_type, use), UNDECIDED)

    def require(self, source_type: str, use: str) -> None:
        verdict = self.verdict(source_type, use)
        if verdict != ALLOWED:
            raise WorkspaceError(
                403,
                "SOURCE_USE_DENIED",
                f"{use.lower()} is not permitted for {source_type} sources "
                f"(verdict {verdict}; BE-Q04)",
                {"source_type": verdict},
            )

    def allows(self, source_type: str, use: str) -> bool:
        return self.verdict(source_type, use) == ALLOWED


def _live_policy() -> SourcePolicy:
    """The owner-approved verdicts (``app/research/policy_approval.py``), or none at all.

    Only an approval bound to the proposal's exact digest loads; otherwise every use of every
    source stays UNDECIDED, which denies. A verdict string outside ALLOWED/DENIED is refused
    rather than read as permission."""
    from app.research.policy_approval import approved_policy

    policy = approved_policy()
    if policy is None:
        return SourcePolicy(version="live-0", verdicts={})
    verdicts: dict[tuple[str, str], str] = {}
    for source_type, entry in policy.source_rights.items():
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"approved policy names unknown source type {source_type!r}")
        for use, verdict in entry["uses"].items():
            if use not in USES or verdict not in (ALLOWED, DENIED):
                raise ValueError(f"approved policy has invalid {source_type}/{use}={verdict}")
            verdicts[(source_type, use)] = verdict
    return SourcePolicy(version=policy.version, verdicts=verdicts)


#: Live verdicts: empty until the owner approves the drafted proposal (BE-Q04).
LIVE_SOURCE_POLICY = _live_policy()
