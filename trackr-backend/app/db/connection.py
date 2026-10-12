"""Database connections.

Thin on purpose. The system is defined by ``contracts/openapi.yaml``,
``schema/*.schema.json`` and ``fixtures/*.json``; Postgres is the reference store, not the
specification. Keeping this to a connection factory is what leaves the stack swappable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any

#: Standard libpq URL. No default: a silent fallback to localhost is how a test run ends
#: up writing to something real.
DATABASE_URL_ENV = "DATABASE_URL"


class WorkspaceCapability(Enum):
    """Which workspace login a server operation runs as. A CLOSED set of five words.

    THE POINT OF AN ENUM HERE IS THAT A CALLER CANNOT NAME A CONNECTION. The alternative
    -- a function taking a DSN, or a role name, or a string read from a request -- is one
    header away from a browser choosing to be the eraser. A capability is selected by the
    server operation that needs it, in code, and the only values that exist are these
    six; there is no member for "whatever the caller asked for".

    Mapped to a configured DSN by ``app/database.py::workspace_url``, and to a database
    role by scripts/provision_workspace_roles.sql. The three lists are the same six names
    in the same order on purpose.
    """

    #: Explicit authenticated human mutations and review. The only capability the
    #: database will accept a ``task_review`` or a human chat turn from (migration 044).
    HUMAN = "human"
    #: Browser attempt/result submission and validation. Submits untrusted model output
    #: and must never be able to accept it.
    RUNNER = "runner"
    #: Deterministic evidence verification, evaluation receipts and report assembly.
    ENGINE = "engine"
    #: Due occurrences and bounded lease expiry. Can expire an attempt, never propose.
    SCHEDULER = "scheduler"
    #: Read-only corpus and tool access. Holds no INSERT anywhere.
    READER = "reader"
    #: The existing authorised privacy workflow, and the only holder of DELETE.
    ERASER = "eraser"


class DatabaseNotConfiguredError(RuntimeError):
    """No connection string. Raised rather than guessed at."""


class WorkspaceConnectionNotConfiguredError(RuntimeError):
    """A workspace capability was used but its login is not configured.

    Separate from ``DatabaseNotConfiguredError`` so the fail-closed case reads as itself.
    There is deliberately NO fallback behind this error -- not to the migration owner, not
    to ``DATABASE_URL``. Falling back would mean an unconfigured eraser erased as the
    schema owner, with RLS bypassed and every append-only trigger droppable, which is a
    strictly worse outcome than refusing.
    """

    def __init__(self, capability: WorkspaceCapability) -> None:
        self.capability = capability
        super().__init__(
            f"no connection configured for workspace capability {capability.value!r} "
            f"(set WORKSPACE_{capability.value.upper()}_DATABASE_URL). Refusing to fall "
            "back to the migration owner or the financial connection."
        )


class WorkspaceConnectionError(RuntimeError):
    """A configured workspace login could not be connected to.

    Distinct from ``WorkspaceConnectionNotConfiguredError``: that one means nobody
    supplied a credential, this one means the credential was supplied and the cluster
    refused it or was unreachable. An operator needs to tell those apart -- the first is a
    deployment that is not finished, the second is a password that has rotated.

    Always carries a DSN passed through ``redact``.
    """


#: Everything between ``//`` and the ``@`` of a libpq URL -- user and password.
_DSN_USERINFO = re.compile(r"(?<=//)[^/@\s]*@")


def redact(url: str | None) -> str:
    """A connection string safe to put in an exception, a log line or a test report.

    Passwords reach logs by accident, not by decision: a psycopg connection error quotes
    the DSN it failed on, and a startup diagnostic that prints "could not connect to
    <url>" prints the password with it. Every error in this module and in
    ``app/database.py`` goes through here first.

    Redacts the WHOLE userinfo segment including the username, not just the password
    after the colon. The username here is the capability -- ``trackr_ws_eraser_login`` --
    and which login a given deployment failed to authenticate as is exactly the sort of
    thing worth not publishing. Host, port and database survive, because those are what
    make the message diagnosable.
    """
    if not url:
        return "<unset>"
    return _DSN_USERINFO.sub("<redacted>@", url)


def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise DatabaseNotConfiguredError(
            f"{DATABASE_URL_ENV} is not set. Refusing to guess a connection string: "
            "defaulting to localhost is how a test run ends up writing to production."
        )
    return url


@contextmanager
def connect(url: str | None = None) -> Iterator[Any]:
    """Yield a connection, committing on success and rolling back on any exception.

    All-or-nothing is the point. A mutation and its audit row share one transaction, so a
    failure that loses the audit must lose the mutation too.
    """
    import psycopg

    conn = psycopg.connect(url or database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
