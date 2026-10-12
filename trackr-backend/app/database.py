"""Connection handling and the JSON conventions, in one place.

Thin deliberately. ``app/db/connection.py`` already owns the connection lifecycle; this
adds the two things the web layer needs on top of it -- a per-request connection sourced
from ``app/config.py`` rather than a bare environment read, and the rule for how a
Decimal crosses the JSON boundary. Moved here verbatim from ``api/deps.py``, logic
unchanged -- see docs/BACKEND_REUSE_AND_GAP_ANALYSIS.md section 1.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.db.connection import (
    WorkspaceCapability,
    WorkspaceConnectionError,
    WorkspaceConnectionNotConfiguredError,
    connect,
    redact,
)

#: Capability -> the ``Settings`` field holding its DSN. The only place the two
#: vocabularies meet. A new capability requires a row here, a role in
#: scripts/provision_workspace_roles.sql and a grant in migration 045 -- three edits, so
#: that adding one silently is not possible.
_WORKSPACE_URL_FIELDS: dict[WorkspaceCapability, str] = {
    WorkspaceCapability.HUMAN: "workspace_human_database_url",
    WorkspaceCapability.RUNNER: "workspace_runner_database_url",
    WorkspaceCapability.ENGINE: "workspace_engine_database_url",
    WorkspaceCapability.SCHEDULER: "workspace_scheduler_database_url",
    WorkspaceCapability.READER: "workspace_reader_database_url",
    WorkspaceCapability.ERASER: "workspace_eraser_database_url",
}


def get_conn() -> Iterator[Any]:
    """FastAPI dependency: one connection per request. No pool.

    Given the load this serves -- an adviser with tens of clients, refreshed on trade
    days -- a pool would be a moving part bought with no measured problem. Add one when
    profiling says to, not before.
    """
    settings = get_settings()
    with connect(settings.database_url) as connection:
        yield connection


def workspace_url(capability: WorkspaceCapability) -> str:
    """The configured DSN for one capability, or refuse.

    Raises:
        WorkspaceConnectionNotConfiguredError: that login is not configured. No fallback
            is attempted -- see that exception's docstring for why a fallback would be
            worse than the outage.
    """
    field = _WORKSPACE_URL_FIELDS[capability]
    url = (getattr(get_settings(), field, None) or "").strip()
    if not url:
        raise WorkspaceConnectionNotConfiguredError(capability)
    return url


@contextmanager
def workspace_connection(capability: WorkspaceCapability) -> Iterator[Any]:
    """One connection, as one capability, for the duration of one server operation.

    Same lifecycle as ``get_conn`` and for the same reason: commit on success, roll back
    on any exception, close either way. Workspace writes need that more than the financial
    routes do, not less -- an accepted agent note, its links, its review row and its audit
    row are one atomic human act (migration 044 enforces it with a deferred constraint),
    so a partial commit is not a degraded success, it is a forged acceptance.

    STILL NO POOL. ``get_conn``'s reasoning is unchanged by this: a pool is a moving part
    bought with no measured problem. It would also be a wrong-shaped one here, because a
    pooled connection carries session state and every workspace transaction sets
    transaction-local tenant and actor settings (``app/db/workspace_context.py``). Pooling
    those is how one investor's context is read by the next request.

    A failure to CONNECT is re-raised as ``WorkspaceConnectionError`` naming the capability
    and a redacted DSN, because psycopg's own ``OperationalError`` quotes the connection
    string it failed on, password included. Exceptions raised by the caller's own work are
    deliberately left untouched -- wrapping those would change the type a caller catches
    and turn a ``LegalHoldError`` into a connection problem.
    """
    url = workspace_url(capability)
    manager = connect(url)
    try:
        connection = manager.__enter__()
    except Exception as exc:
        raise WorkspaceConnectionError(
            f"cannot connect for workspace capability {capability.value!r} "
            f"on {redact(url)}: {exc}"
        ) from None
    try:
        yield connection
    except BaseException as exc:
        # Hand the exception to ``connect``'s own handler so it rolls back and closes,
        # exactly as the ``with`` statement would have.
        if not manager.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        manager.__exit__(None, None, None)


class NonFiniteFinancialValueError(ValueError):
    """A financial value that cannot cross the JSON boundary as an exact decimal.

    NaN and the infinities are the three Decimal values with no decimal-string form.
    They reach here only from a defect -- a zero divisor the caller failed to guard, or a
    Decimal built from a float that was already Inf -- and the honest answer to a defect
    is an error, not a figure. Serialising one as ``null`` would claim the value is
    unknown, and as ``0`` would claim it is zero; both are inventions, which
    docs/ARCHITECTURE_INVARIANTS.md forbids. ``app/main.py`` answers this with a 500.
    """

    def __init__(self, value: Decimal, field: str | None = None) -> None:
        where = f" for {field}" if field else ""
        super().__init__(
            f"non-finite financial value{where}: {value!r} has no exact decimal form"
        )
        self.value = value
        self.field = field


def _exact(value: Decimal, field: str | None = None) -> str:
    """One Decimal -> the exact decimal string, in plain (never exponent) notation.

    ``format(value, "f")`` rather than ``str(value)`` because Decimal's own repr switches
    to scientific notation outside a narrow exponent band, and
    ``schema/transaction.schema.json#/$defs/decimal`` is ``^-?[0-9]+(\.[0-9]+)?$``. The
    two agree for every quantised money value the engine produces; they differ for a raw
    engine rate, which is exactly the value this boundary must not mangle.

    No quantisation. A rate solved to twenty significant digits crosses with twenty.
    ``NUMERIC(20,6)`` is the storage domain for MONEY, and using it to round a computed
    ratio would be losing precision to a constraint that does not apply to it.
    """
    if not value.is_finite():
        raise NonFiniteFinancialValueError(value, field)
    return format(value, "f")


def money(value: Decimal | None, field: str | None = None) -> str | None:
    """Money crosses as a STRING, per schema/transaction.schema.json#/$defs/decimal.

    A JSON number is an IEEE double by the time any client parses it, and a system that
    keeps Decimal end-to-end through the engine and NUMERIC(20,6) in Postgres would be
    throwing that away at the last step. The consumer formats the string; it never does
    arithmetic on it.
    """
    return None if value is None else _exact(value, field)


def rate(value: Decimal | None, field: str | None = None) -> str | None:
    """A rate or ratio crosses as a STRING TOO -- the same rule as ``money``.

    IT DID NOT USED TO. This function returned ``float(value)`` on the argument that a
    display ratio never needs to be exact, and holding/performance DTOs declared their
    rate fields ``float`` to match. That was wrong in a way a display argument cannot
    excuse: ``Decimal("0.1234567890123456789")`` left the server as
    ``0.12345678901234568``, so the ONE number the whole pipeline exists to compute --
    XIRR -- was the one value rounded on its way out, and a client that wanted to show
    or compare it could never recover the digits. Reconstructing them client-side is
    impossible; ``str()`` on the float only preserves the damage.

    Rates keep their UNITS as well as their digits: a fractional rate stays fractional,
    so ``"0.123"`` still means 12.3%. This change is about precision alone -- no equation,
    no unit and no null in ``docs/ENGINE_SPEC.md`` moves because of it.
    """
    return None if value is None else _exact(value, field)
