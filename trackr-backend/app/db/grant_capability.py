"""Grant or revoke a product capability for one login -- the operator command.

    DATABASE_URL=postgresql:///portfolio python -m app.db.grant_capability --list
    DATABASE_URL=... python -m app.db.grant_capability --email you@example.com --grant workspace
    DATABASE_URL=... python -m app.db.grant_capability --email you@example.com --revoke workspace

**NO LONGER THE ONLY WAY A LOGIN GETS WORKSPACE.** Signup grants both capabilities since
2026-09-27, when the owner reversed the operator-gate decision -- see
``app.db.capabilities.DEFAULT_CAPABILITIES`` and migration 046. This command is now for
**revoking** a capability, restoring one that was revoked, and inspecting what an account
actually holds.

It was not deleted with the gate, and should not be: a capability that could be granted but
never withdrawn would make "revocation blocks new work" -- a property ``has_capability`` is
built around -- untestable. There is still deliberately no HTTP endpoint, no plan field and
no payment-provider webhook; when billing arrives it calls ``app.db.capabilities.grant``, it
does not replace it.

WHY A CLI AND NOT A ROUTE. A grant endpoint needs an admin role, and an admin role needs
its own authentication, audit and blast-radius story -- all of which is real work that
buys nothing while the operator is a person with a shell on the box. A command that
cannot be reached from the internet at all is a smaller thing to get right than one that
can.

Identity is by ``--email`` or ``--user-id``. Email is what an operator actually has to
hand; the id is there for scripts and for the case where two accounts differ only by
something that does not survive a copy-paste. Neither is guessed: an unknown user is an
error, never a silently created account.

Idempotent. Running a grant twice is not an error and writes one audit row, not two --
see ``app.db.capabilities.grant``. This matters because the realistic failure mode is an
operator who is not sure whether the first command took effect.

``--actor`` records who is doing this, for ``user_capability.granted_by`` and the audit
row's ``diff``. It defaults to the OS user and host so an unattended run is still
attributable, but an operator acting for someone else should say so explicitly.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
from typing import Any

from app.auth.repository import get_user_by_email
from app.db import capabilities
from app.db.connection import connect, database_url


def _default_actor() -> str:
    """Who ran this, when nobody said. ``user@host`` -- enough to find the shell it came
    from, and not something anyone would mistake for a verified identity."""
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # noqa: BLE001 -- attribution must never fail the command
        return "unknown-operator"


def _resolve_user(conn: Any, email: str | None, user_id: str | None) -> dict[str, Any] | None:
    """The account this command is about, or None. Never creates one."""
    if email:
        return get_user_by_email(conn, email.strip().lower())
    from app.auth.repository import get_user_by_id

    return get_user_by_id(conn, user_id) if user_id else None


def _print_listing(conn: Any) -> None:
    """Every login and its EFFECTIVE capabilities, plus the raw grants behind them.

    Both, because they differ in a way that matters: a user granted only ``workspace``
    is effectively ``portfolio, workspace``, and an operator deciding what to revoke
    needs to see which row actually exists.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, email FROM app_user ORDER BY email")
        users = [(str(row[0]), row[1]) for row in cur.fetchall()]

    if not users:
        print("no app_user rows")
        return

    width = max(len(email) for _, email in users)
    print(f"{'EMAIL'.ljust(width)}  {'EFFECTIVE':<24}  GRANTED")
    for user_id, email in users:
        effective = ", ".join(capabilities.effective_capabilities(conn, user_id)) or "-"
        granted = ", ".join(sorted(capabilities.active_capabilities(conn, user_id))) or "-"
        print(f"{email.ljust(width)}  {effective:<24}  {granted}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grant or revoke a product capability for one login.",
        epilog=(
            "Signup grants both capabilities (migration 046). This command revokes, restores "
            "and inspects; it is not the way access is first given."
        ),
    )
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--email", help="the app_user email to change")
    identity.add_argument("--user-id", help="the app_user id to change, for scripts")

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--grant",
        choices=sorted(capabilities.ALLOWED_CAPABILITIES),
        help="give this capability",
    )
    action.add_argument(
        "--revoke",
        choices=sorted(capabilities.ALLOWED_CAPABILITIES),
        help="take this capability away. Not a deletion: research data and privacy "
        "rights survive, and re-granting restores access.",
    )

    parser.add_argument("--list", action="store_true", help="show all logins, change nothing")
    parser.add_argument(
        "--actor",
        default=None,
        help="who is performing this change (default: the OS user and host)",
    )
    args = parser.parse_args(argv)

    if not args.list and not (args.grant or args.revoke):
        parser.error("say what to do: --grant, --revoke, or --list")
    if (args.grant or args.revoke) and not (args.email or args.user_id):
        parser.error("--grant/--revoke need --email or --user-id")

    with connect(database_url()) as conn:
        if args.list:
            _print_listing(conn)
            return 0

        user = _resolve_user(conn, args.email, args.user_id)
        if user is None:
            named = args.email or args.user_id
            print(f"no such user: {named}", file=sys.stderr)
            return 1

        user_id = str(user["id"])
        actor = args.actor or _default_actor()
        capability = args.grant or args.revoke

        if args.grant:
            changed = capabilities.grant(
                conn, user_id, capability, granted_by=actor, actor_uid=user_id
            )
            verb = "granted" if changed else "already had"
        else:
            changed = capabilities.revoke(
                conn, user_id, capability, revoked_by=actor, actor_uid=user_id
            )
            verb = "revoked" if changed else "did not have"

        effective = ", ".join(capabilities.effective_capabilities(conn, user_id)) or "none"
        print(f"{user['email']}: {verb} {capability}")
        print(f"  effective capabilities now: {effective}")
        if args.revoke and changed:
            # Say the part an operator is most likely to be unsure about, at the moment
            # they are most likely to want it.
            print("  research data is retained; export and erasure still work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
