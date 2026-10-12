"""Which products a login may use -- reads and writes of ``user_capability`` (039).

**This module answers one question and must never be asked another.** "May this login
open Workspace?" is a product question. "May this login read investor X?" is an ownership
question, and it is answered by ``investor.owner_user_id`` through
``app.auth.dependencies.require_owned_investor``. A research route needs BOTH answers and
gets them from two separate dependencies on purpose: a single combined guard is one
refactor away from someone deciding that holding a capability is good enough.

Raw SQL, same discipline as ``app/db/repository.py`` and ``app/auth/repository.py``.

THE IMPLICATION LIVES HERE, ONCE. ``workspace`` includes ``portfolio``. Every caller asks
``effective_capabilities`` or ``has_capability`` rather than reading rows and reasoning
about them, because a second copy of that rule is how a Workspace user eventually gets
refused a portfolio page on one code path and allowed it on another.

READ PER REQUEST, NEVER FROM THE TOKEN. Capabilities are deliberately absent from the JWT
payload (``app/auth/service.py::issue_token``). A claim baked into a token stays true
until the token expires, which would make revocation take up to ``jwt_expiry_seconds`` to
bite. Product access has to stop when the operator says it stops, so it is a row read on
the request's own connection.
"""

from __future__ import annotations

from typing import Any

from app.db import audit

#: A login that may use Trackr+. Every existing user was backfilled with this by 039,
#: and registration grants it in the same transaction that creates the account.
CAPABILITY_PORTFOLIO = "portfolio"

#: A login that may use Trackr Workspace. Granted at signup since 2026-09-27 (see
#: ``DEFAULT_CAPABILITIES``), and revocable by the operator command
#: (``app/db/grant_capability.py``). Still never inferred from a request header, an Origin, an
#: email domain or a frontend build -- the server decides, in one place.
CAPABILITY_WORKSPACE = "workspace"

#: The closed set, mirroring 039's CHECK constraint. Both are enforced: the database is
#: what makes a typo impossible, this is what makes it a clear error rather than an
#: IntegrityError from three layers down.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_PORTFOLIO, CAPABILITY_WORKSPACE})

#: Capability -> everything holding it also confers. Workspace is a superset of Plus:
#: docs/PRODUCT_ARCHITECTURE.md section 4, "a workspace grant includes portfolio".
_IMPLIES: dict[str, frozenset[str]] = {
    CAPABILITY_WORKSPACE: frozenset({CAPABILITY_PORTFOLIO}),
}

#: Stable wire order, so ``GET /auth/capabilities`` is byte-identical for equal state and
#: a client may compare responses. Least to most privileged, which is also the order the
#: product contract writes them in.
_WIRE_ORDER: tuple[str, ...] = (CAPABILITY_PORTFOLIO, CAPABILITY_WORKSPACE)

#: audit_log actions. Named like the existing ones in ``app/db/audit.py``.
ACTION_CAPABILITY_GRANTED = "CAPABILITY_GRANTED"
ACTION_CAPABILITY_REVOKED = "CAPABILITY_REVOKED"

#: audit_log.entity_type for these rows. The entity is the USER whose access changed --
#: not the grant row -- because that is what someone reading the log is looking for.
AUDIT_ENTITY = "user_capability"

#: The stable machine-readable code a denied research request carries. Clients branch on
#: this, never on the human sentence beside it, so it is part of the contract and must
#: not change with the wording.
WORKSPACE_REQUIRED_CODE = "workspace_capability_required"


class UnknownCapabilityError(ValueError):
    """Not one of ``ALLOWED_CAPABILITIES``. Raised before touching the database so a
    caller's typo reads as a caller's typo."""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(
            f"{capability!r} is not a known capability "
            f"(expected one of {sorted(ALLOWED_CAPABILITIES)})"
        )


class WorkspaceCapabilityRequiredError(PermissionError):
    """The caller is authenticated but has no active ``workspace`` capability.

    Carries ``code`` so ``app/main.py``'s handler can put a stable identifier in the
    error envelope beside the sentence. 403, never 404: the route exists and the request
    was understood -- pretending otherwise would be a different lie from the one 404
    tells about investor ids, and would make a genuine routing bug indistinguishable
    from a downgrade.
    """

    code = WORKSPACE_REQUIRED_CODE

    def __init__(self, user_id: str | None = None) -> None:
        self.user_id = user_id
        super().__init__("this account does not have Trackr Workspace access")


def _validate(capability: str) -> str:
    if capability not in ALLOWED_CAPABILITIES:
        raise UnknownCapabilityError(capability)
    return capability


def active_capabilities(conn: Any, user_id: str) -> set[str]:
    """The capabilities GRANTED to this user, exactly as stored. No implication applied.

    Almost every caller wants ``effective_capabilities`` instead. This exists for the
    operator command, which has to show what is actually in the table rather than what
    it adds up to -- revoking ``workspace`` from a user who never held ``portfolio``
    directly is a state worth being able to see.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capability FROM user_capability "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return {row[0] for row in cur.fetchall()}


def effective_capabilities(conn: Any, user_id: str) -> list[str]:
    """What this user can actually do, in stable wire order.

    Applies ``_IMPLIES``: a ``workspace`` grant yields ``["portfolio", "workspace"]``
    even if no ``portfolio`` row was ever written. This is the ONLY function that
    expands the implication, and ``GET /auth/capabilities`` returns exactly this list.

    An unknown user id, or one with no active rows, yields ``[]`` -- not an error and
    not a default grant. A login that somehow reaches here with nothing granted gets
    nothing; silently substituting ``portfolio`` would mean the resolver, rather than
    the operator, decided someone's access.
    """
    granted = active_capabilities(conn, user_id)
    effective = set(granted)
    for capability in granted:
        effective |= _IMPLIES.get(capability, frozenset())
    return [name for name in _WIRE_ORDER if name in effective]


def has_capability(conn: Any, user_id: str, capability: str) -> bool:
    """Does this user effectively hold ``capability``?

    The non-HTTP resolver. Workers -- monitor scheduling, report jobs, anything that
    prepares or executes work on a user's behalf outside a request -- call this before
    doing that work, so a revocation stops new work without any HTTP layer involved.
    Revocation blocks new work; it never deletes what was already recorded.
    """
    return _validate(capability) in effective_capabilities(conn, user_id)


def grant(
    conn: Any,
    user_id: str,
    capability: str,
    granted_by: str,
    actor_uid: str | None = None,
) -> bool:
    """Give ``user_id`` ``capability``. Returns True if this call changed anything.

    Idempotent: granting an already-active capability changes nothing, returns False,
    and writes no audit row -- an audit trail that records non-events is harder to read
    in the dispute it exists for. Granting a previously revoked capability clears
    ``revoked_at`` and re-stamps ``granted_at``, which is a real change and is audited.

    The ``WHERE user_capability.revoked_at IS NOT NULL`` on the conflict branch is what
    makes "did this change anything" a fact the database reports rather than one Python
    infers from a prior SELECT. A row comes back in exactly two cases -- the INSERT
    happened, or a revoked row was reactivated -- and in no other. A racing duplicate
    grant therefore audits once, not twice.

    Writes on the CALLER's connection, so the grant and its audit row share one
    transaction -- ``app/db/audit.py``'s rule, unchanged here.
    """
    _validate(capability)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_capability (user_id, capability, granted_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, capability) DO UPDATE
               SET revoked_at = NULL,
                   granted_at = now(),
                   granted_by = EXCLUDED.granted_by
             WHERE user_capability.revoked_at IS NOT NULL
            RETURNING id
            """,
            (user_id, capability, granted_by),
        )
        changed = cur.fetchone() is not None

    if changed:
        audit.record(
            conn,
            ACTION_CAPABILITY_GRANTED,
            AUDIT_ENTITY,
            entity_id=str(user_id),
            actor_uid=actor_uid,
            diff={"capability": capability, "granted_by": granted_by},
        )
    return changed


def revoke(
    conn: Any,
    user_id: str,
    capability: str,
    revoked_by: str,
    actor_uid: str | None = None,
) -> bool:
    """Take ``capability`` away from ``user_id``. Returns True if this call changed
    anything.

    Idempotent: revoking something already revoked, or never granted, is a no-op
    returning False with no audit row.

    **This is not a deletion.** The row stays, its research data stays, and the account's
    DPDP export/erasure rights are untouched -- ``docs/PRODUCT_ARCHITECTURE.md`` section
    4: "Loss of capability is not deletion and must not block privacy rights." A
    downgraded user can still export and erase everything they own.
    """
    _validate(capability)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_capability SET revoked_at = now(), granted_by = %s "
            "WHERE user_id = %s AND capability = %s AND revoked_at IS NULL",
            (revoked_by, user_id, capability),
        )
        changed = cur.rowcount > 0

    if changed:
        audit.record(
            conn,
            ACTION_CAPABILITY_REVOKED,
            AUDIT_ENTITY,
            entity_id=str(user_id),
            actor_uid=actor_uid,
            diff={"capability": capability, "revoked_by": revoked_by},
        )
    return changed


#: What a brand-new account gets. BOTH products, since 2026-09-27.
#:
#: **THIS REVERSED A PRODUCT DECISION, ON THE OWNER'S INSTRUCTION.** Until 2026-09-27
#: `docs/PRODUCT_ARCHITECTURE.md` section 4 read "Ordinary signup grants portfolio only.
#: Workspace access initially uses an explicit audited operator command." The owner's decision
#: is that there is no operator command and that either product must be usable by whoever signs
#: up, so signup grants both. The contract has been updated to match rather than left to
#: contradict the code.
#:
#: WHAT DID *NOT* CHANGE, AND MUST NOT. Capabilities are still granted BY THE SERVER, in one
#: place, and are still not inferred from which frontend asked. The obvious reading of "let
#: people sign up on Workspace" would be "grant workspace when the request comes from the
#: Workspace origin" -- and that is the one thing the old contract forbade for a reason that
#: survives the reversal: an Origin header is set by the client, and anything that calls the API
#: directly can claim to be either product. Granting both unconditionally needs no such claim,
#: so there is nothing to forge.
#:
#: Revocation still works and is still audited (`grant_capability.py`), so "everyone gets both"
#: is a default rather than a permanent property of an account.
DEFAULT_CAPABILITIES: tuple[str, ...] = (CAPABILITY_PORTFOLIO, CAPABILITY_WORKSPACE)


def grant_default_capabilities(conn: Any, user_id: str, granted_by: str = "registration") -> None:
    """What a brand-new account gets: ``portfolio`` AND ``workspace``.

    Called from inside registration's existing transaction
    (``app/routers/auth.py::register``) so an account cannot exist for even one statement
    without its capability rows.

    Identical whichever origin signed up, deliberately -- see ``DEFAULT_CAPABILITIES`` for why
    origin-based granting was not the change made here. Each grant audits separately, so the log
    shows both.
    """
    for capability in DEFAULT_CAPABILITIES:
        grant(conn, user_id, capability, granted_by=granted_by, actor_uid=str(user_id))
