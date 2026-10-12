"""Claim unowned investors for a login -- the one-time fix for rows that predate
investor ownership (015_investor_ownership.sql).

    DATABASE_URL=postgresql:///portfolio python -m app.db.assign_owner --email you@example.com

Every investor row with no ``owner_user_id`` is assigned to the named app_user. Rows
that already have an owner are left alone: ownership is not stolen, only granted where
there is none. Without a claim, an unowned investor is permanently 403 through the API.

    --list   show ownership state and exit; change nothing.
"""

from __future__ import annotations

import argparse
import sys

from app.auth.repository import get_user_by_email
from app.db.connection import connect, database_url


def run(conn, owner_user_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE investor SET owner_user_id = %s WHERE owner_user_id IS NULL",
            (owner_user_id,),
        )
        return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="the app_user email to claim unowned investors")
    parser.add_argument("--list", action="store_true", help="show ownership state, change nothing")
    args = parser.parse_args()

    with connect(database_url()) as conn:
        if args.list:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT code, display_name, COALESCE(owner_user_id::text, '-') "
                    "FROM investor ORDER BY code"
                )
                for code, name, owner in cur.fetchall():
                    print(f"{code}\t{name}\t{owner}")
            return 0

        if not args.email:
            parser.error("--email is required (or use --list)")
        user = get_user_by_email(conn, args.email.strip().lower())
        if user is None:
            print(f"no such user: {args.email}", file=sys.stderr)
            return 1
        claimed = run(conn, str(user["id"]))
        print(f"claimed {claimed} investor(s) for {user['email']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
