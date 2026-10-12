"""The transaction-local tenant and actor scope every workspace statement runs inside.

Migrations 044 and 045 ask PostgreSQL two questions on every workspace row: which investor
is this transaction acting for (``app.workspace_investor``, read by ``ws_tenant()``), and
which authenticated human is attributed (``app.workspace_actor``, read by ``ws_actor()``).
This module is the only place that answers them.

**WHAT THIS IS NOT.** It is not authentication, and RLS is not authentication either.
Row-level security restricts a transaction to the tenant this module SELECTED; it has no
opinion about whether that selection was legitimate. The authority to select an investor
comes from ``app/auth/dependencies.py`` -- ``require_owned_investor`` compares
``investor.owner_user_id`` to the authenticated user and returns 403/404 -- and this
function requires that it already happened. Calling ``enter_workspace`` with an
investor id read straight off a request, unchecked, would set the tenant to whatever the
caller asked for and RLS would faithfully enforce the attacker's choice.

The ownership re-check below is therefore defence in depth on a check that must already
have passed, not the check itself.

Three rules, each load-bearing:

1. **Values are BOUND, never interpolated.** ``set_config($1, $2, true)`` with parameters,
   not ``SET LOCAL app.workspace_investor = '...'``. ``SET`` does not take parameters at
   all in PostgreSQL, so the interpolating version is the only one that compiles -- which
   is exactly why it is the version people write. A tenant identifier that reaches SQL as
   text is a tenant identifier an attacker can extend.
2. **The third argument is ``true``: transaction-local.** The setting dies at COMMIT or
   ROLLBACK with no cleanup code to forget. This is what makes a reused connection safe
   between two investors; see ``clear_workspace``.
3. **``app.erasure_authorised`` is NOT set here.** Ordinary workspace access has nothing
   to do with deletion. That flag belongs to the privacy workflow alone, after its own
   hold and ownership checks (``app/db/privacy.py``), and is set by ``authorise_erasure``
   below so that the one path that needs it is greppable and the ordinary path cannot
   acquire it by accident.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from app.db.connection import WorkspaceCapability

#: Transaction-local setting names, matching migration 044's ``ws_tenant()`` and
#: ``ws_actor()`` exactly. Changing either name is a schema change, not a rename.
SETTING_INVESTOR = "app.workspace_investor"
SETTING_ACTOR = "app.workspace_actor"

#: The erasure flag, already in use by ``app/db/privacy.py`` and 001_core.sql's
#: append-only trigger. Named here because migration 044's ``ws_append_only()`` reads the
#: same setting, so the financial ledger and the workspace tables agree on one word for
#: "this transaction is an authorised erasure" rather than having two.
SETTING_ERASURE = "app.erasure_authorised"

#: Capabilities that represent a person acting. ``ws_human()`` in the database is the
#: authority; this mirrors it so a Python caller passing no actor for a human operation
#: fails here with a clear message instead of inside a trigger.
_HUMAN_CAPABILITIES = frozenset({WorkspaceCapability.HUMAN})


class WorkspaceContextError(RuntimeError):
    """The tenant/actor scope could not be established, so nothing was run.

    Every failure in this module raises before any workspace statement executes. Failing
    closed matters more here than a useful partial result: migration 044's insert guard
    refuses a row whose ``investor_id`` does not equal ``ws_tenant()``, so a missing
    context does not read as "unscoped", it reads as "no tenant", and every write is
    refused. That is the correct outcome, but an exception naming the missing input is a
    much better diagnostic than a trigger message from four layers down.
    """


def _as_uuid(value: Any, label: str) -> str:
    """Canonical UUID text, or refuse.

    Parsed rather than trusted even though the value is bound as a parameter. ``set_config``
    takes text, so ``'whatever'`` would be stored happily and only fail later when
    ``ws_tenant()`` casts it -- inside a trigger, on the first insert, with a message about
    invalid input syntax for type uuid. Rejecting it here names the argument instead.
    """
    if value is None:
        raise WorkspaceContextError(f"{label} is required")
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkspaceContextError(f"{label} is not a UUID: {value!r}") from exc


def _assert_owned(conn: Any, investor_id: str, actor_uid: str | None) -> None:
    """Re-check that this investor exists and that ``actor_uid`` owns it.

    Deliberately duplicates ``app/auth/dependencies.py::_authorise``, which has already
    run for any HTTP caller. The duplication buys two things a route guard cannot:
    coverage of NON-HTTP callers (the scheduler, the expiry worker, a management command,
    which have no request and therefore no dependency to have passed), and a failure that
    happens on the SAME CONNECTION the writes will use, so there is no window in which
    ownership was true on one connection and the tenant was set on another.

    Uses no capability to decide anything. A capability says which product a login may
    open; it never widens whose data may be read (``app/db/capabilities.py``).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT owner_user_id FROM investor WHERE id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None:
        raise WorkspaceContextError(f"no such investor: {investor_id}")
    owner = str(row[0]) if row[0] else None
    if owner is None:
        raise WorkspaceContextError(
            f"investor {investor_id} is unclaimed; workspace access requires an owner"
        )
    if actor_uid is not None and owner != actor_uid:
        raise WorkspaceContextError(
            f"investor {investor_id} belongs to another account"
        )


@contextmanager
def enter_workspace(
    conn: Any,
    capability: WorkspaceCapability,
    investor_id: str,
    actor_uid: str | None = None,
) -> Iterator[Any]:
    """Scope ``conn``'s current transaction to one investor, and optionally one human.

    Args:
        conn: a connection already opened for ``capability``
            (``app/database.py::workspace_connection``). This function does not choose a
            connection, because a function that chose one from an argument would be a
            function a caller could ask for the eraser.
        capability: the capability the SERVER OPERATION needs. Passed so this boundary can
            check the actor requirement that goes with it; it is not read from a DTO,
            header, query parameter or anything else a request can influence. The database
            does not trust this argument either -- it reads ``current_user``.
        investor_id: an investor whose ownership has ALREADY been verified by
            ``require_owned_investor`` or an equivalent.
        actor_uid: the authenticated human, required for ``HUMAN``. ``None`` for the
            runner, engine, scheduler and reader, which act as the system: migration 044
            only demands an actor when ``ws_human()`` is true, and supplying a person's id
            for a background job would attribute machine work to them.

    Yields:
        The same connection, with the scope set for the remainder of this transaction.

    Raises:
        WorkspaceContextError: any missing or inconsistent input, before any workspace
            statement runs.
    """
    tenant = _as_uuid(investor_id, "investor_id")
    actor = _as_uuid(actor_uid, "actor_uid") if actor_uid is not None else None

    if capability in _HUMAN_CAPABILITIES and actor is None:
        raise WorkspaceContextError(
            f"capability {capability.value!r} represents a person and requires actor_uid; "
            "migration 044 refuses a human write with no attribution"
        )
    if capability not in _HUMAN_CAPABILITIES and actor is not None:
        # Not merely tidiness. A background capability that set an actor would be claiming
        # a person did something they did not do, and ``ws_insert_guard`` compares a row's
        # attribution to ``ws_actor()`` -- so a scheduler that set one could insert rows
        # attributed to the owner. The guard's own defence is that ``ws_human()`` is false
        # for the scheduler; this is the near-side half of the same rule.
        raise WorkspaceContextError(
            f"capability {capability.value!r} acts as the system and must not carry an "
            f"actor_uid (got {actor})"
        )

    _assert_owned(conn, tenant, actor)

    with conn.cursor() as cur:
        # BOUND PARAMETERS. set_config(setting, value, is_local) is the parameterisable
        # form of SET LOCAL; `SET LOCAL app.workspace_investor = %s` does not exist.
        cur.execute("SELECT set_config(%s, %s, true)", (SETTING_INVESTOR, tenant))
        cur.execute("SELECT set_config(%s, %s, true)", (SETTING_ACTOR, actor or ""))
    yield conn


@contextmanager
def authorise_erasure(conn: Any, investor_id: str) -> Iterator[Any]:
    """Additionally set ``app.erasure_authorised`` for this transaction. Erasure only.

    The one caller is the privacy workflow, after it has checked ownership and confirmed
    no legal hold is open. Kept in this module rather than inline in ``privacy.py`` so
    that ``grep -rn erasure_authorised`` finds every place authority is granted, and kept
    OUT of ``enter_workspace`` so that no ordinary read or write can acquire it by being
    routed through the normal helper.

    This grants nothing by itself. ``ws_append_only()`` permits a DELETE only when all
    four hold: the connected role is a member of ``trackr_ws_eraser``, this flag is on,
    the row's ``investor_id`` equals ``ws_tenant()``, and no unreleased ``legal_hold`` row
    covers that investor. A normal role setting this flag is still refused -- which is a
    case worth testing, because it is the shape a compromised request handler would take.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, %s, true)", (SETTING_ERASURE, "on"))
    yield conn


def current_scope(conn: Any) -> dict[str, str | None]:
    """What the database currently believes the tenant and actor are.

    For diagnostics and for tests that need to prove a scope was cleared. Reads through
    ``current_setting(..., true)``, the missing_ok form, so an unset setting is ``None``
    rather than an error -- the same call migration 044's helpers make.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nullif(current_setting(%s, true), ''), "
            "       nullif(current_setting(%s, true), ''), "
            "       nullif(current_setting(%s, true), '')",
            (SETTING_INVESTOR, SETTING_ACTOR, SETTING_ERASURE),
        )
        investor, actor, erasure = cur.fetchone()
    return {"investor_id": investor, "actor_uid": actor, "erasure": erasure}


def clear_workspace(conn: Any) -> None:
    """Drop the scope now, without ending the transaction.

    NOT THE NORMAL WAY THE SCOPE ENDS, and calling it is not required. ``set_config(...,
    true)`` is transaction-local, so COMMIT and ROLLBACK both clear all three settings
    with no cleanup code that could be skipped on an exception path. That property is the
    reason the scope is transaction-local rather than session-local, and it is what makes
    a connection safe to hand to a second tenant.

    This exists for the one case that property does not cover: code that deliberately runs
    two tenants in one transaction -- an operator script, a fixture -- and wants the second
    to start from nothing rather than inherit the first. Setting an empty string is how a
    transaction-local setting is unset; ``RESET`` would restore the session value, which is
    a different and wrong thing.
    """
    with conn.cursor() as cur:
        for setting in (SETTING_INVESTOR, SETTING_ACTOR, SETTING_ERASURE):
            cur.execute("SELECT set_config(%s, %s, true)", (setting, ""))
