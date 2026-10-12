"""Named families: a login's own grouping of the investors it manages (migration 079).

Grouping only. A family holds no money and no holdings; its totals are summed from its
members at read time (``app.routers.investors.overview``). Every function is scoped by
the caller's login: a family or investor that belongs to someone else is reported as not
found, exactly like a missing one, so ids cannot be probed.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db import audit


class FamilyNotFoundError(LookupError):
    """No such family for this login (absent, or owned by someone else)."""


class DuplicateFamilyNameError(ValueError):
    """This login already has a family with that name."""


# Audit rows carry ids only: the audit log is widely readable (app.db.audit) and a family
# name is a person's surname.


def _row(row: tuple) -> dict[str, Any]:
    return {"id": str(row[0]), "name": row[1], "created_at": row[2]}


def _id(family_id: str) -> str:
    """A malformed id names no family: answer not-found, not a database error."""
    try:
        return str(uuid.UUID(str(family_id)))
    except ValueError:
        raise FamilyNotFoundError(family_id) from None


def _clean(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValueError("A family needs a name.")
    return cleaned


def list_families(conn: Any, user_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, created_at FROM family_group WHERE owner_user_id = %s "
            "ORDER BY lower(name), id",
            (user_id,),
        )
        return [_row(r) for r in cur.fetchall()]


def assert_owned(conn: Any, user_id: str, family_id: str) -> None:
    family_id = _id(family_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM family_group WHERE id = %s AND owner_user_id = %s",
            (family_id, user_id),
        )
        if cur.fetchone() is None:
            raise FamilyNotFoundError(family_id)


def create_family(conn: Any, user_id: str, name: str) -> dict[str, Any]:
    import psycopg

    cleaned = _clean(name)
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO family_group (owner_user_id, name) VALUES (%s, %s) "
                "RETURNING id, name, created_at",
                (user_id, cleaned),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateFamilyNameError(cleaned) from exc
        family = _row(cur.fetchone())
    audit.record(
        conn, action=audit.ACTION_INSERT, entity_type="family_group",
        entity_id=family["id"], actor_uid=user_id, diff=None,
    )
    return family


def rename_family(conn: Any, user_id: str, family_id: str, name: str) -> dict[str, Any]:
    import psycopg

    family_id = _id(family_id)
    cleaned = _clean(name)
    with conn.cursor() as cur:
        try:
            cur.execute(
                "UPDATE family_group SET name = %s WHERE id = %s AND owner_user_id = %s "
                "RETURNING id, name, created_at",
                (cleaned, family_id, user_id),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateFamilyNameError(cleaned) from exc
        row = cur.fetchone()
    if row is None:
        raise FamilyNotFoundError(family_id)
    audit.record(
        conn, action=audit.ACTION_UPDATE, entity_type="family_group",
        entity_id=family_id, actor_uid=user_id, diff=None,
    )
    return _row(row)


def delete_family(conn: Any, user_id: str, family_id: str) -> None:
    """Remove the grouping. Its members stay, unassigned (the FK is ON DELETE SET NULL)."""
    family_id = _id(family_id)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM family_group WHERE id = %s AND owner_user_id = %s",
            (family_id, user_id),
        )
        if cur.rowcount == 0:
            raise FamilyNotFoundError(family_id)
    audit.record(
        conn, action=audit.ACTION_DELETE, entity_type="family_group",
        entity_id=family_id, actor_uid=user_id, diff=None,
    )


def set_investor_family(
    conn: Any, user_id: str, investor_id: str, family_id: str | None
) -> None:
    """Put an investor in a family, or take it out (``family_id`` None). The investor's
    ownership is checked by the router (``require_owned_investor``); the family's here."""
    if family_id is not None:
        assert_owned(conn, user_id, family_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE investor SET family_group_id = %s WHERE id = %s AND owner_user_id = %s",
            (family_id, investor_id, user_id),
        )
    audit.record(
        conn, action=audit.ACTION_UPDATE, entity_type="investor",
        entity_id=investor_id, actor_uid=user_id, diff={"family_group_id": family_id},
    )


def delete_if_empty(conn: Any, family_id: str) -> None:
    """Called after an investor is erased: a family with no members left is the last
    trace of that person's family name, so it goes too (DPDP erasure)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM family_group f WHERE f.id = %s AND NOT EXISTS "
            "(SELECT 1 FROM investor i WHERE i.family_group_id = f.id)",
            (family_id,),
        )
