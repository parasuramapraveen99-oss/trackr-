"""Research method (skill) versions: publication and reads (BE 16-17, BLUEPRINT_BACKEND.md 7.3).

Same connection contract as ``app/db/workspace.py``: every function takes a connection already
scoped by ``enter_workspace``; writes take the tenant lock, check the idempotency receipt, then
write row + audit + receipt in one transaction.

NOTHING HERE SEEDS A TENANT. A GET that finds no skills returns an empty page and inserts
nothing; the catalogue's default contract reaches the database only through an explicit,
authenticated, idempotent POST. Version allocation is sequential under the tenant lock (migration
044 refuses a gap or a duplicate independently), disabling is a new version, and nothing is ever
updated or deleted -- an old task keeps pointing at the exact version that ran.
"""

from __future__ import annotations

from typing import Any

from app.db import workspace as ws
from app.research import canonical
from app.research import skills as sk
from app.research import text as tx
from app.research.errors import VERSION_CONFLICT, conflict, not_found

OP_SKILL_PUBLISH = "skill.publish"

_rows = ws._rows
_one = ws._one
_uuid = ws._uuid

_COLUMNS = """
s.id, s.version, s.name, s.summary, s.method, s.reads, s.citation_rules, s.permitted_tools,
s.forbidden_conclusions, s.output_contract, s.house_rules_version, s.enabled, s.recorded_at
"""


def skill_dto(row: dict[str, Any]) -> dict[str, Any]:
    """Declared business fields plus catalogue identity and a deterministic availability
    check. No readiness, provider or model field -- a method is not an agent."""
    entry = sk.entry_for(row["id"])
    out = {
        "id": row["id"],
        "version": row["version"],
        "name": row["name"],
        "summary": row["summary"],
        "method": row["method"],
        "reads": list(row["reads"]),
        "citation_rules": list(row["citation_rules"]),
        "permitted_tools": list(row["permitted_tools"]),
        "forbidden_conclusions": list(row["forbidden_conclusions"]),
        "output_contract": row["output_contract"],
        "house_rules_version": row["house_rules_version"],
        "enabled": row["enabled"],
        "recorded_at": row["recorded_at"],
        "method_key": entry.key if entry else None,
        "task_route": entry.task_route if entry else None,
        "availability": (sk.availability(entry, row["reads"], row["permitted_tools"])
                         if entry else {"state": "UNKNOWN",
                                        "missing": [{"id": "CATALOGUE_ENTRY", "kind": "METHOD",
                                                     "code": "NOT_IN_CATALOGUE",
                                                     "gate": None}]}),
    }
    return out


def skill_version(conn: Any, investor_id: str, skill_id: str, version: int) -> dict[str, Any]:
    """The exact immutable version, including disabled ones (history still cites them)."""
    skill_id = _uuid(skill_id, "skill")
    row = _one(conn, f"SELECT {_COLUMNS} FROM skill s "
                     "WHERE s.investor_id = %s AND s.id = %s AND s.version = %s",
               (investor_id, skill_id, version))
    if row is None:
        raise not_found("skill version")
    return skill_dto(row)


def list_skills(
    conn: Any, investor_id: str, history: bool, cursor: str | None, limit: int | None
) -> dict[str, Any]:
    """Latest version per method (enabled or disabled), or every version with ``history``."""
    size = canonical.clamp_limit(limit)
    scope = canonical.scope_digest(investor_id, "skills", {"history": history})
    after = canonical.decode_cursor(cursor, scope)
    latest_only = "" if history else """
      AND NOT EXISTS (SELECT 1 FROM skill n WHERE n.investor_id = s.investor_id
                       AND n.id = s.id AND n.version > s.version)"""
    rows = _rows(
        conn,
        f"""
        SELECT {_COLUMNS} FROM skill s
         WHERE s.investor_id = %(inv)s {latest_only}
           AND (%(k0)s::uuid IS NULL OR (s.id, s.version) > (%(k0)s::uuid, %(k1)s::int))
         ORDER BY s.id, s.version
         LIMIT %(lim)s
        """,
        {"inv": investor_id, "k0": after[0] if after else None,
         "k1": after[1] if after else None, "lim": size + 1},
    )
    return ws._page(conn, rows, size, scope, lambda page: [skill_dto(r) for r in page],
                    key=lambda r: [r["id"], r["version"]])


def _current_version(conn: Any, investor_id: str, skill_id: str) -> int:
    row = _one(conn, "SELECT COALESCE(max(version), 0) AS v FROM skill "
                     "WHERE investor_id = %s AND id = %s", (investor_id, skill_id))
    return int(row["v"]) if row else 0


def publish_skill(
    conn: Any, investor_id: str, actor_uid: str, skill_id: str, body: dict[str, Any],
    key: str, payload_digest: str,
) -> tuple[dict[str, Any], int]:
    """Append the next version of a KNOWN catalogue method.

    ``expected_version`` is the version the person saw (0 for a first publication). A retry
    with the same key replays the original even after later versions exist; a changed payload
    under the same key is 409. Unknown methods are 404: there is no path to invent one.
    """
    entry = sk.entry_for(skill_id)
    if entry is None:
        raise not_found("method")
    skill_id = entry.skill_id
    for field_name, limit in (("name", tx.TITLE_MAX_BYTES), ("summary", tx.REASON_MAX_BYTES),
                              ("method", tx.NOTE_BODY_MAX_BYTES)):
        tx.check_text(body[field_name], field_name, limit)
    lock_scope = skill_id
    ws.lock_tenant(conn, investor_id)
    receipt = ws.check_replay(conn, investor_id, OP_SKILL_PUBLISH, lock_scope, key,
                              payload_digest)
    if receipt is not None:
        return skill_version(conn, investor_id, receipt.result["skill_id"],
                             int(receipt.result["version"])), 200

    fields = sk.check_create(entry, body)
    current = _current_version(conn, investor_id, skill_id)
    if body["expected_version"] != current:
        raise conflict(VERSION_CONFLICT,
                       f"this method is at version {current}; reload before publishing")
    version = current + 1
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO skill (investor_id, id, version, name, summary, method, reads,
                               citation_rules, permitted_tools, forbidden_conclusions,
                               output_contract, house_rules_version, enabled, dedupe_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (investor_id, skill_id, version, fields["name"], fields["summary"],
             fields["method"], fields["reads"], fields["citation_rules"],
             fields["permitted_tools"], fields["forbidden_conclusions"],
             canonical.canonical_json(fields["output_contract"]),
             fields["house_rules_version"], fields["enabled"],
             ws.dedupe_key(OP_SKILL_PUBLISH, lock_scope, key)),
        )
    ws.audit_write(conn, actor_uid, OP_SKILL_PUBLISH, "skill", skill_id, version=version,
                   enabled=fields["enabled"], method=entry.key)
    ws.record_receipt(conn, investor_id, OP_SKILL_PUBLISH, lock_scope, key, payload_digest,
                      {"skill_id": skill_id, "version": str(version)}, actor_uid)
    return skill_version(conn, investor_id, skill_id, version), 201


def pinned_skill(conn: Any, investor_id: str, skill_id: str, version: int) -> dict[str, Any]:
    """The exact version a task or agent pins, with its catalogue entry. 404 if absent."""
    dto = skill_version(conn, investor_id, skill_id, version)
    dto["entry"] = sk.entry_for(dto["id"])
    return dto
