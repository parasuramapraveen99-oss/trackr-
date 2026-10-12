"""The connection an ad hoc script is allowed to open -- and nothing else.

WHY THIS EXISTS. ``app.db.connection.connect()`` reads ``DATABASE_URL`` and connects to
whatever it names. That is correct for the application, which is supposed to talk to the
real database. It is the wrong default for the other thing that opens connections in this
project constantly: the one-off script. A probe written to answer a question -- "is that
row test residue?", "did the feed record anything?" -- inherits production by default,
and the only thing standing between a read-only probe and a destructive one is that
whoever wrote it did not make a mistake. Every such script in this project's history has
been read-only by intent; none was read-only by construction.

This module is the construction. A script imports ``connect_scratch`` instead of
``connect``, and gets a connection ONLY to a database that has been deliberately,
separately marked as disposable. Anything else raises ``NotATestDatabaseError``. There is
no flag to relax it and no fallback to ``DATABASE_URL`` -- a fallback is the entire bug.

THE GUARDS, and which of them actually proves anything. Same shape as
``docs/ARCHITECTURE_INVARIANTS.md`` rule 10 ("structural checks reject; they never
vouch"), for the same reason: a name is a claim, not evidence.

  1. The URL must come from ``TRACKR_SCRATCH_DATABASE_URL`` or be passed explicitly.
     ``DATABASE_URL`` is never consulted as a source. REFUSES.
  2. If ``DATABASE_URL`` is exported and the resolved URL equals it, refuse. Belt and
     braces: a script run without ``.env`` exported leaves this unable to fire, which is
     why it is not the guard anything depends on. REFUSES.
  3. The database NAME must end in ``_test``, ``_scratch`` or ``_throwaway``. This is the
     check that stops ``.../portfolio`` dead, and it is the reason guard 2 being blind is
     survivable. It PROVES NOTHING on its own -- anyone can name a database
     ``foo_test`` -- and is refusal-only.
  4. The connected database must contain the ``scratch_database_tag`` table with a row.
     This is the ONLY guard that vouches, and it exists nowhere in ``app/db/migrations/``
     ON PURPOSE: migrating it would put the tag in every database including the real one,
     which is the guard defeating itself. It gets there only via ``tag_as_scratch``.
  5. The tag must name the database it is sitting in. Catches the one way guard 4 can be
     inherited rather than granted: ``pg_dump`` of a tagged scratch database restored
     into something else carries the tag row with it.

WHAT THIS CANNOT DO, stated plainly rather than discovered later. It cannot stop a script
that calls ``psycopg.connect`` itself, and no runtime guard can -- the import is right
there. What covers that is ``make lint``'s "no ad hoc script opens its own connection"
rule, which reads every ``.py`` outside ``trackr-backend/app/`` and ``tests/`` and fails
on a direct connect. A script in ``/tmp`` is outside both reaches; for that one, this
module is a tool offered, not a gate imposed, and the honest summary is that it makes the
safe path the easy path rather than making the unsafe path impossible.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from app.db.connection import DATABASE_URL_ENV

#: Where a script's disposable database is named. Deliberately NOT ``DATABASE_URL``: two
#: different variables is what lets a shell hold both at once without either shadowing
#: the other, so running a probe never involves editing the app's own configuration.
SCRATCH_URL_ENV = "TRACKR_SCRATCH_DATABASE_URL"

#: Guard 4's table. Never in a migration -- see the module docstring.
TAG_TABLE = "scratch_database_tag"

#: Guard 3. Refusal-only; passing it is not evidence of anything.
_SCRATCH_NAME_RE = re.compile(r"_(test|scratch|throwaway)$")


class NotATestDatabaseError(RuntimeError):
    """A script asked for a scratch connection and the target did not prove it was one.

    Deliberately a hard error and not a warning. The failure this prevents is silent by
    nature: a connection that works is indistinguishable from a connection that should
    not have been allowed, right up until the script writes something.
    """


def _database_name(url: str) -> str:
    """The database a libpq URL names, or ``""``. Path-only, so a query string carrying
    ``?options=`` or ``?sslmode=`` cannot smuggle characters past guard 3."""
    return urlsplit(url).path.lstrip("/")


def _resolve_url(url: str | None) -> str:
    """Guards 1-3, before any connection is attempted."""
    resolved = url or os.environ.get(SCRATCH_URL_ENV)
    if not resolved:
        raise NotATestDatabaseError(
            f"{SCRATCH_URL_ENV} is not set and no url was passed. A one-off script does "
            f"not get to inherit {DATABASE_URL_ENV}: point this at a disposable database "
            f"and tag it first (python -m app.db.scratch --tag --url ... --note ...)."
        )

    configured = os.environ.get(DATABASE_URL_ENV)
    if configured and resolved == configured:
        raise NotATestDatabaseError(
            f"refusing to open a scratch connection to {DATABASE_URL_ENV} itself. That "
            "is the application's own database; a script that wants to read it should "
            "say so explicitly with app.db.connection.connect and be reviewed as such."
        )

    name = _database_name(resolved)
    if not _SCRATCH_NAME_RE.search(name):
        raise NotATestDatabaseError(
            f"database {name!r} is not named as disposable. A scratch database's name "
            "must end in _test, _scratch or _throwaway, so that a mistyped host or a "
            "stale shell variable cannot silently land a probe on something real. "
            "(Renaming a real database to pass this check is not a workaround; guard 4 "
            "still demands an in-database tag it will not have.)"
        )
    return resolved


def _assert_tagged(conn: Any, url: str) -> None:
    """Guards 4 and 5, on an open connection. The only vouching step."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{TAG_TABLE}",))
        if cur.fetchone()[0] is None:
            raise NotATestDatabaseError(
                f"{_database_name(url)!r} has no {TAG_TABLE} table, so nothing here has "
                "ever declared it disposable. A database name is a claim; this table is "
                "the evidence. Tag it deliberately if that is what it is:\n"
                f"  python -m app.db.scratch --tag --url {url!r} --note 'what it is for'"
            )
        cur.execute(f"SELECT database_name, note, tagged_at FROM {TAG_TABLE} LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise NotATestDatabaseError(
                f"{TAG_TABLE} exists in {_database_name(url)!r} but is empty -- a "
                "truncated tag is not a tag. Re-tag before using this database."
            )
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]

    tagged_for = row[0]
    if tagged_for != actual:
        raise NotATestDatabaseError(
            f"the {TAG_TABLE} row in {actual!r} was written for database "
            f"{tagged_for!r}. A tag that travelled -- a pg_dump of a scratch database "
            "restored somewhere else brings this row along, and the restored database "
            "has not itself been declared disposable by anyone. Re-tag it on purpose, "
            "having checked what it actually is."
        )


@contextmanager
def connect_scratch(purpose: str, url: str | None = None) -> Iterator[Any]:
    """A connection to a tagged, disposable database. Raises rather than guessing.

    ``purpose`` is required and unused by any logic -- it exists so the refusal message a
    script triggers says what the script was trying to do, and so writing the call forces
    a sentence about why this connection is being opened at all.

    Commits on success, rolls back on any exception, same all-or-nothing posture as
    ``app.db.connection.connect``. A scratch database is disposable, not exempt: a probe
    that half-writes is still a probe whose output cannot be trusted.
    """
    import psycopg

    resolved = _resolve_url(url)
    conn = psycopg.connect(resolved)
    try:
        _assert_tagged(conn, resolved)
    except Exception:
        conn.close()
        raise
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def tag_as_scratch(url: str, note: str) -> str:
    """Declare one database disposable. A separate, deliberate act, never implicit.

    Applies guards 2 and 3 itself, so the obvious mistake -- tagging the application's
    own database to make a script stop complaining -- refuses here rather than succeeding
    and disarming guard 4 permanently. What it cannot prevent is someone unsetting
    ``DATABASE_URL`` and tagging a database they have named ``something_test`` that is in
    fact real. That requires actively misrepresenting the target, which is a different
    failure from the accident this module exists to stop, and no in-process check
    distinguishes a lie from a fact.

    Idempotent: re-tagging an already-tagged database replaces the note.
    """
    import psycopg

    resolved = _resolve_url(url)
    if not note.strip():
        raise ValueError("a scratch tag needs a note saying what the database is for")

    with psycopg.connect(resolved) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            actual = cur.fetchone()[0]
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TAG_TABLE} (
                    database_name text PRIMARY KEY,
                    note          text NOT NULL,
                    tagged_at     timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(f"DELETE FROM {TAG_TABLE}")
            cur.execute(
                f"INSERT INTO {TAG_TABLE} (database_name, note) VALUES (%s, %s)",
                (actual, note.strip()),
            )
        conn.commit()
    return actual


def main(argv: list[str] | None = None) -> int:
    """``python -m app.db.scratch --tag --url ... --note ...``.

    A CLI rather than a library-only function because tagging should be a thing somebody
    typed, visible in a shell history, not a line buried in a script that runs itself.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", action="store_true", help="declare the database disposable")
    parser.add_argument("--url", default=None, help=f"libpq URL (default: ${SCRATCH_URL_ENV})")
    parser.add_argument("--note", default="", help="what this scratch database is for")
    args = parser.parse_args(argv)

    if not args.tag:
        parser.error("nothing to do: pass --tag")

    try:
        tagged = tag_as_scratch(args.url or os.environ.get(SCRATCH_URL_ENV, ""), args.note)
    except (NotATestDatabaseError, ValueError) as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"tagged {tagged!r} as a scratch database: {args.note.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
