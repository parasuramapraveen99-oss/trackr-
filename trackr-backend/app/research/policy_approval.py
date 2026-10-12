"""One-step owner approval of the drafted research policy (BE-Q04 source rights, BE-Q05 retention).

The owner asked for approval to be automated rather than entry-by-entry (2026-09-29). The
compromise that keeps the gate honest: the implementer DRAFTS ``policy/proposal.json``; the
owner approves the WHOLE proposal in one command, and the approval is bound to the file's exact
SHA-256. Until ``policy/approval.json`` exists and names that digest, ``approved_policy()`` is
None and every live verdict stays UNDECIDED (which denies) -- editing a single byte of the
proposal silently un-approves it rather than silently approving the edit.

    make approve-research-policy BY="Owner Name"     # writes policy/approval.json
    make research-policy-status                      # APPROVED | NOT_APPROVED | STALE

Pure file I/O, no database, no network. Retention rows the proposal names are seeded by
migration 058; ``workspace_privacy.readiness_blockers`` checks the DB values match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

POLICY_DIR = Path(__file__).with_name("policy")
PROPOSAL_PATH = POLICY_DIR / "proposal.json"
APPROVAL_PATH = POLICY_DIR / "approval.json"


@dataclass(frozen=True)
class ApprovedPolicy:
    version: str
    proposal_sha256: str
    approved_by: str
    approved_on: str
    source_rights: dict[str, Any]
    retention: dict[str, Any]


def proposal_digest(path: Path = PROPOSAL_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status(proposal: Path = PROPOSAL_PATH, approval: Path = APPROVAL_PATH) -> str:
    if not approval.exists():
        return "NOT_APPROVED"
    record = json.loads(approval.read_text())
    return "APPROVED" if record.get("proposal_sha256") == proposal_digest(proposal) else "STALE"


def load(proposal: Path = PROPOSAL_PATH, approval: Path = APPROVAL_PATH) -> ApprovedPolicy | None:
    """The approved policy, or None. A stale or malformed approval is None, never partial."""
    if status(proposal, approval) != "APPROVED":
        return None
    record = json.loads(approval.read_text())
    if not str(record.get("approved_by", "")).strip() or not record.get("approved_on"):
        return None
    body = json.loads(proposal.read_text())
    return ApprovedPolicy(
        version=f"{body['version']}@{record['proposal_sha256'][:12]}",
        proposal_sha256=record["proposal_sha256"],
        approved_by=record["approved_by"], approved_on=record["approved_on"],
        source_rights={k: v for k, v in body["source_rights"].items() if not k.startswith("_")},
        retention={k: v for k, v in body["retention"].items() if not k.startswith("_")},
    )


@lru_cache(maxsize=1)
def approved_policy() -> ApprovedPolicy | None:
    """Process-wide view, read once at import of the consumers. Restart after approving."""
    return load()


def approve(by: str, on: str | None = None) -> dict[str, Any]:
    if not by.strip():
        raise SystemExit("approval needs the approver's name: BY=\"Name\"")
    record = {
        "proposal_sha256": proposal_digest(),
        "approved_by": by.strip(),
        "approved_on": on or date.today().isoformat(),
        "scope": ["BE-Q04", "BE-Q05"],
        "statement": "I approve every source-rights verdict, condition and retention mapping in "
                     "proposal.json at this digest, as the product owner.",
    }
    APPROVAL_PATH.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.research.policy_approval")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("approve")
    ap.add_argument("--by", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.cmd == "approve":
        print(json.dumps(approve(args.by), indent=2))
        print("Approved. Apply migration 058 and restart the API so the policy loads.")
        return 0
    print(f"{status()}  proposal sha256={proposal_digest()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
