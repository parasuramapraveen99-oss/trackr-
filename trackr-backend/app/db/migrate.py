"""Migration runner -- numbered .sql files, applied in order, recorded once.

Deliberately not an ORM migration framework. Six tables do not need one, and keeping the
DDL as plain SQL is what lets a Go or Node implementation read the same schema. See
``docs/ARCHITECTURE.md`` on swapping the stack.

Migrations are FORWARD-ONLY. There is no downgrade path, because a downgrade that drops a
column holding client data is not a thing anyone should be able to run by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.connection import DatabaseNotConfiguredError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

#: 001_core.sql -> version 1
VERSION_PATTERN = re.compile(r"^(\d+)_.*\.sql$")

#: The Trackr Workspace schema, which must land ALL AT ONCE or not at all.
#:
#: NUMBERED 040-045, NOT 031-036. The blueprint (BLUEPRINT_BACKEND.md section 4) proposed
#: 031-036 against a tree that ended at 030. Nine migrations landed in the meantime --
#: 031_isin_change_circulars through 039_user_capability -- so those numbers are taken and
#: recorded as applied in real databases. Renumbering the whole unapplied bundle together
#: is the only correct move: reusing a recorded version would make two different files
#: claim one row of ``schema_migrations``, and a database that had applied the old 031
#: would silently skip the new one.
#:
#: WHY A BUNDLE AT ALL. 040-043 define twenty-five tables with no triggers, no RLS and no
#: grants. 044 installs the append-only and insert guards; 045 installs tenant RLS and the
#: per-capability grants. A deployment sitting at 043 therefore has the entire research
#: schema present and completely unguarded -- writable without a tenant check, editable
#: after the fact, readable across investors by anything that can reach the tables. That
#: state must not be reachable by a normal deployment, which is what the fence below is
#: for. Sessions 04 and 05 author 040-043 and can only apply them through the explicit
#: disposable-database path.
WORKSPACE_BUNDLE: dict[int, str] = {
    40: "040_workspace_evidence.sql",
    41: "041_workspace_commitments.sql",
    42: "042_workspace_execution.sql",
    43: "043_workspace_monitor_chat.sql",
    44: "044_workspace_guards.sql",
    45: "045_workspace_access.sql",
}

#: The twenty-five tables the bundle creates, in the order migrations 044 and 045 install
#: guards and grants on them. Kept here so Python can check properties of the whole set --
#: ownership, in ``privileged_workspace_owners`` -- without re-deriving the list from SQL.
#: A table missing from this tuple, from 044's loop or from 045's loop is a table with no
#: append-only guard or no tenant isolation, so all three lists must stay identical.
#:
#: Followed by the tenant-scoped tables later forward migrations add on the same pattern
#: (same insert/append-only/truncate triggers, same RLS policy, same grant shape), each
#: installing its own guards in its own file: 047's ``ws_receipt`` and 048's
#: ``research_source_extraction``. They are listed here so the ownership check and the
#: privacy inventory (``app/db/workspace_privacy.py``) cover them exactly like the bundle.
WORKSPACE_TABLES: tuple[str, ...] = (
    "company", "research_source", "research_fact", "note", "note_link", "ritual", "question",
    "question_answer", "thesis", "trigger_evaluation", "evaluation_fact", "thesis_event",
    "skill", "agent", "task", "task_context", "task_attempt", "task_result", "result_citation",
    "task_review", "chat_thread", "chat_message", "monitor_schedule", "monitor_run",
    "monitor_report",
    "ws_receipt", "research_source_extraction",
    # 051_butler.sql -- Butler's explicit follows, memory references, outbox, inbox and
    # reviews (BT 01). Same guards, same RLS, same privacy inventory.
    "butler_preference", "butler_subscription", "butler_subscription_event", "butler_memory",
    "butler_memory_event", "butler_source_retraction", "butler_outbox", "butler_outbox_claim",
    "butler_outbox_effect", "butler_inbox_item", "butler_inbox_event", "butler_review",
    "butler_review_response", "butler_review_evidence", "butler_review_completion",
    "butler_tick",
    # 052_research_execution.sql -- BT 09's explicit teaching preferences and item feedback.
    "butler_teaching_preference", "butler_feedback",
    # 053_butler_notebook.sql -- BT 12-15's consented investor notebook. Same guards and RLS;
    # its SELECT grant is narrower (no runner, no scheduler), which is the purpose binding.
    "butler_learning_consent", "butler_self_description", "butler_episode", "butler_pattern",
    "butler_pattern_revision", "butler_notebook_event", "butler_notebook_exclusion",
    "butler_coaching_use",
    # 054_hosted_jobs.sql -- RP 00/01's scoped hosted jobs (company reports, reflection).
    "hosted_job", "hosted_job_event", "hosted_job_lease", "hosted_job_checkpoint",
    "hosted_job_charge",
    # 055_research_evidence.sql -- BE 29/34 dated corporate events with exact identity.
    "research_event",
    # 056_report_documents.sql -- RP 05/06 published report documents, artifacts, retractions.
    "report_document", "report_artifact", "report_retraction",
)

#: The notebook tables (053). Named separately because they are a SENSITIVE category: nothing
#: that assembles a company-report packet, a support-assistant context or a browser task packet
#: may read them. ``app/research/report_contract.py`` refuses these names as keys and
#: ``make lint`` refuses a report/job module that imports the notebook repository.
NOTEBOOK_TABLES: frozenset[str] = frozenset({
    "butler_learning_consent", "butler_self_description", "butler_episode", "butler_pattern",
    "butler_pattern_revision", "butler_notebook_event", "butler_notebook_exclusion",
    "butler_coaching_use",
})

#: The migration that installs Butler's tables. Butler routes and the worker check for it
#: explicitly: a Workspace-ready database that has not applied 051 answers 503, never a 500
#: from a missing relation.
BUTLER_MIGRATION: tuple[int, str] = (51, "051_butler.sql")

#: Runner idempotency receipts plus BT 09's teaching tables. The task/agent/chat routes (BE 19-25)
#: and Butler's teaching routes check for it the same way Butler checks for 051: a database that
#: has not applied it answers 503, never a runner write that fails on a missing grant.
EXECUTION_MIGRATION: tuple[int, str] = (52, "052_research_execution.sql")

#: BT 12-15's consented investor notebook.
NOTEBOOK_MIGRATION: tuple[int, str] = (53, "053_butler_notebook.sql")

#: RP 00/01's scoped hosted jobs (BT-P02): company-report and notebook-reflection jobs.
HOSTED_JOBS_MIGRATION: tuple[int, str] = (54, "054_hosted_jobs.sql")

#: BE 29/34 external-evidence provenance and research_event. Routes that write adapter sources
#: or events, and the announcement reader, check it; absent -> 503 / UNKNOWN, never a 500.
EVIDENCE_MIGRATION: tuple[int, str] = (55, "055_research_evidence.sql")

#: RP 05/06 published report documents and artifacts. The report pipeline stops before
#: publication (with a stated reason) and the document/artifact routes answer 503 until applied.
REPORT_DOCUMENTS_MIGRATION: tuple[int, str] = (56, "056_report_documents.sql")

#: BE 36/37 event identity on monitor_run and the EARNINGS_CALL event type. The monitor routes
#: and the monitor tick check it; absent -> 503 / SCHEMA_NOT_READY, never a 500.
MONITOR_EVENTS_MIGRATION: tuple[int, str] = (57, "057_monitor_event_identity.sql")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    filename    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Every migration on disk, in version order.

    Raises:
        ValueError: two files claim the same version. Ambiguous ordering is worse than
            no ordering, because it applies differently on different machines.
    """
    found: dict[int, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = VERSION_PATTERN.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {found[version]}, {path}")
        found[version] = path
    return sorted(found.items())


def applied_versions(conn: Any) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


class IncompleteWorkspaceBundleError(RuntimeError):
    """The workspace migrations on disk, or already recorded, are not a whole bundle.

    Raised BEFORE anything is applied, so a deployment that trips this is unchanged rather
    than half-migrated. The message names the specific files missing or in conflict,
    because "incomplete bundle" on its own sends an operator to read six migrations.
    """


class NotADisposableDatabaseError(RuntimeError):
    """``apply_through`` was pointed at something that is not a throwaway database.

    The partial-bundle path exists so sessions 04 and 05 can execute intermediate DDL
    against real PostgreSQL. It must not be usable as a way around the fence, so it demands
    proof of isolation rather than a promise of it.
    """


@dataclass(frozen=True)
class DisposableDatabase:
    """A database the CALLER created for this test and will drop afterwards.

    Constructed by a fixture naming the database it just created -- not read from a
    setting, an environment variable or the connection itself. That is the whole design:
    if this value could be derived from the connection, then any connection would satisfy
    it, and the partial path would be a bypass with a ceremony in front of it.

    ``name`` is checked against ``current_database()``, so a fixture that creates
    ``ws_partial_ab12`` and then hands over an inherited connection to ``portfolio``
    fails. Naming a database ``test`` proves nothing and is not accepted as proof;
    ``assert_disposable`` looks at what is actually in it.
    """

    name: str


def assert_disposable(conn: Any, target: DisposableDatabase) -> None:
    """Refuse unless ``conn`` really is on ``target`` and ``target`` really is disposable.

    Three checks, none of which a settings flag can satisfy:

    1. ``current_database()`` equals the name the caller declared. Catches the common
       fixture bug -- a freshly created database, and a connection to the old one.
    2. The database is not the one this deployment is configured to use. Read from the
       environment rather than ``app.config`` so that this function has no import cycle and
       works in a bare ``psql``-less test process; both ``DATABASE_URL`` and
       ``MIGRATION_DATABASE_URL`` are compared.
    3. There is no client data in it. An empty ``investor`` table is the strongest cheap
       signal available: every document, transaction, holding and workspace row in this
       schema hangs off an investor, so a database with none has nothing to lose. A
       database that HAS investors is somebody's, whatever it is called.

    ``workspace_enabled=false`` is deliberately not one of the checks. A feature flag
    describes what the application will serve; it says nothing about which database is on
    the other end of the socket, and treating it as isolation is how a disabled feature
    still corrupts a shared schema.
    """
    import os

    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]

    if actual != target.name:
        raise NotADisposableDatabaseError(
            f"connection is on database {actual!r} but the caller declared "
            f"{target.name!r}. Create the disposable database and connect to it before "
            "applying a partial bundle."
        )

    for env_name in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
        configured = os.environ.get(env_name, "")
        if configured and configured.rstrip("/").rsplit("/", 1)[-1].split("?")[0] == actual:
            raise NotADisposableDatabaseError(
                f"database {actual!r} is this deployment's {env_name}. The partial-bundle "
                "path applies unguarded workspace tables and must never touch it."
            )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'investor')"
        )
        has_investor_table = cur.fetchone()[0]
        if has_investor_table:
            cur.execute("SELECT count(*) FROM investor")
            investors = cur.fetchone()[0]
            if investors:
                raise NotADisposableDatabaseError(
                    f"database {actual!r} holds {investors} investor row(s), so it is not "
                    "a disposable test target. Every record in this schema hangs off an "
                    "investor; a database with some is somebody's."
                )


def check_workspace_bundle(conn: Any, directory: Path = MIGRATIONS_DIR) -> None:
    """Refuse to migrate unless the workspace bundle is whole -- on disk and on record.

    Called by ``migrate`` on every normal run, including when the workspace feature is
    disabled. A feature flag cannot make a half-installed schema safe: the tables exist in
    the database whether or not any route reads them, and 040-043 without 044-045 are
    twenty-five tables with no tenant isolation and no append-only guard.

    Both sides are inspected, because they fail differently:

    * **On disk.** A branch that has authored 040 and 041 but not the rest. Allowed to
      exist -- that is what sessions 04 and 05 produce -- but not to be deployed.
    * **On record.** ``schema_migrations`` rows claiming a subset was applied. This is the
      dangerous one: it means a partial bundle already reached this database, and applying
      the remainder would install guards over tables that have been writable by anything
      in the meantime. It is reported, never quietly completed.
    * **Conflicting filenames.** A recorded version 40 whose filename is not
      ``040_workspace_evidence.sql`` means this database applied something else as 40.
      Adopting a shared partial schema on a name collision is how two branches silently
      merge their DDL.

    Absent entirely is a normal, supported state -- every deployment is in it today.

    Raises:
        IncompleteWorkspaceBundleError: any of the above.
    """
    on_disk = {version for version, _ in discover(directory) if version in WORKSPACE_BUNDLE}
    disk_names = {
        version: path.name for version, path in discover(directory) if version in WORKSPACE_BUNDLE
    }

    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute(
            "SELECT version, filename FROM schema_migrations WHERE version = ANY(%s)",
            (sorted(WORKSPACE_BUNDLE),),
        )
        recorded = {row[0]: row[1] for row in cur.fetchall()}

    conflicts = [
        f"version {version} is recorded as {name!r} but the bundle expects "
        f"{WORKSPACE_BUNDLE[version]!r}"
        for version, name in sorted(recorded.items())
        if name != WORKSPACE_BUNDLE[version]
    ]
    conflicts += [
        f"version {version} on disk is {name!r} but the bundle expects "
        f"{WORKSPACE_BUNDLE[version]!r}"
        for version, name in sorted(disk_names.items())
        if name != WORKSPACE_BUNDLE[version]
    ]
    if conflicts:
        raise IncompleteWorkspaceBundleError(
            "workspace migration numbers conflict with something else: "
            + "; ".join(conflicts)
            + ". Renumber the unapplied bundle in app/db/migrate.py::WORKSPACE_BUNDLE and "
            "rename the files together; never reuse a recorded version."
        )

    if recorded and set(recorded) != set(WORKSPACE_BUNDLE):
        missing = sorted(set(WORKSPACE_BUNDLE) - set(recorded))
        raise IncompleteWorkspaceBundleError(
            f"this database has applied a PARTIAL workspace bundle: recorded "
            f"{sorted(recorded)}, missing {missing}. The tables from the recorded "
            "migrations exist without the guards from the missing ones. Do not complete "
            "the bundle over them -- investigate how a partial bundle was applied here "
            "(the only supported route is app/db/migrate.py::apply_through, which refuses "
            "any database holding investor rows)."
        )

    if on_disk and on_disk != set(WORKSPACE_BUNDLE):
        missing = sorted(WORKSPACE_BUNDLE[v] for v in set(WORKSPACE_BUNDLE) - on_disk)
        raise IncompleteWorkspaceBundleError(
            f"the workspace migration bundle is incomplete on disk: present "
            f"{sorted(WORKSPACE_BUNDLE[v] for v in on_disk)}, missing {missing}. These "
            "migrations deploy together or not at all: the first four define the tables "
            "and the last two make them tenant-scoped and append-only. Apply intermediate "
            "files through app/db/migrate.py::apply_through against a disposable database."
        )


def migrate(conn: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every unapplied migration. Returns the filenames applied.

    Idempotent: running twice applies nothing the second time. Each migration runs inside
    the caller's transaction, so a failure part-way leaves the schema untouched rather
    than half-built.

    THE CALLER'S TRANSACTION IS LOAD-BEARING, and now checked rather than assumed. This
    function never commits: ``start.sh`` and every documented deployment command commit
    once, after it returns, so the six workspace files and their six ``schema_migrations``
    rows are one atomic unit. On an autocommit connection each statement would commit as it
    executed, which would turn a failure in 045 into a permanent deployment sitting at 044
    -- tables installed, RLS absent -- and that is precisely the state
    ``check_workspace_bundle`` exists to make unreachable. An autocommit connection is
    therefore refused rather than worked around.

    Raises:
        IncompleteWorkspaceBundleError: the workspace bundle is partial on disk or on
            record. Checked first, so nothing is applied.
        RuntimeError: ``conn`` is in autocommit mode.
    """
    if getattr(conn, "autocommit", False):
        raise RuntimeError(
            "migrate() requires a transactional connection: on autocommit, a failure "
            "part-way through the 040-045 workspace bundle would leave its tables "
            "installed without the guards from 044/045 and could not be rolled back. "
            "Open the connection without autocommit and commit once after this returns."
        )

    check_workspace_bundle(conn, directory)

    already = applied_versions(conn)
    applied: list[str] = []

    for version, path in discover(directory):
        if version in already:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s)",
                (version, path.name),
            )
        applied.append(path.name)

    return applied


def apply_through(
    conn: Any,
    target: DisposableDatabase,
    through: int,
    directory: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply migrations up to and including ``through``, on a disposable database only.

    THE TEST-ONLY PARTIAL PATH, and the only supported way to execute an intermediate
    workspace migration. Sessions 04 and 05 author 040-043 and have to run them against
    real PostgreSQL -- a CHECK constraint that has never been executed is a comment -- but
    a deployment must still refuse the same files. Both facts hold because the bypass is a
    DIFFERENT FUNCTION requiring a DIFFERENT ARGUMENT, not a flag on ``migrate``.

    There is deliberately no environment variable, setting or request that reaches this.
    ``DEBUG``, ``workspace_enabled`` and a database named ``test`` are all irrelevant here;
    the caller must construct a ``DisposableDatabase`` naming a database it created, and
    ``assert_disposable`` then checks the connection is really on it and that it holds no
    client data.

    Does not commit, for the same reason ``migrate`` does not.

    Raises:
        NotADisposableDatabaseError: ``conn``/``target`` is not a fresh throwaway database.
    """
    if getattr(conn, "autocommit", False):
        raise RuntimeError("apply_through() requires a transactional connection")

    assert_disposable(conn, target)

    already = applied_versions(conn)
    applied: list[str] = []
    for version, path in discover(directory):
        if version > through or version in already:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s)",
                (version, path.name),
            )
        applied.append(path.name)
    return applied


def workspace_schema_ready(conn: Any) -> bool:
    """Is the complete workspace bundle recorded as applied to this database?

    The other half of ``Settings.workspace_enabled``. The flag says an operator WANTS
    Workspace served; this says the schema can actually support it. Both are required, and
    the flag alone must never be enough -- a route enabled over a schema missing 045 would
    read and write workspace tables with no row-level security at all.
    """
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = ANY(%s) AND filename = "
            "ANY(%s)",
            (sorted(WORKSPACE_BUNDLE), sorted(WORKSPACE_BUNDLE.values())),
        )
        return cur.fetchone()[0] == len(WORKSPACE_BUNDLE)


def butler_schema_ready(conn: Any) -> bool:
    """Is ``051_butler.sql`` recorded as applied? Checked by the Butler routes and worker."""
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s AND filename = %s",
            BUTLER_MIGRATION,
        )
        return cur.fetchone() is not None


def _applied(conn: Any, version: int, filename: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s AND filename = %s",
            (version, filename),
        )
        return cur.fetchone() is not None


def notebook_schema_ready(conn: Any) -> bool:
    """Is ``053_butler_notebook.sql`` recorded as applied? Checked by the notebook routes."""
    return _applied(conn, *NOTEBOOK_MIGRATION)


def hosted_jobs_schema_ready(conn: Any) -> bool:
    """Is ``054_hosted_jobs.sql`` recorded as applied? Checked by report routes and worker."""
    return _applied(conn, *HOSTED_JOBS_MIGRATION)


def report_documents_schema_ready(conn: Any) -> bool:
    """Is ``056_report_documents.sql`` recorded as applied?"""
    return _applied(conn, *REPORT_DOCUMENTS_MIGRATION)


def monitor_schema_ready(conn: Any) -> bool:
    """Are ``055_research_evidence.sql`` and ``057_monitor_event_identity.sql`` applied?"""
    return _applied(conn, *EVIDENCE_MIGRATION) and _applied(conn, *MONITOR_EVENTS_MIGRATION)


def evidence_schema_ready(conn: Any) -> bool:
    """Is ``055_research_evidence.sql`` recorded as applied?"""
    return _applied(conn, *EVIDENCE_MIGRATION)


def execution_schema_ready(conn: Any) -> bool:
    """Is ``052_research_execution.sql`` recorded as applied? Checked by the task routes."""
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s AND filename = %s",
            EXECUTION_MIGRATION,
        )
        return cur.fetchone() is not None


def privileged_workspace_owners(conn: Any) -> list[str]:
    """Workspace tables owned by a SUPERUSER or a role with BYPASSRLS. Empty is correct.

    THE ONE FAILURE MODE THAT MAKES MIGRATION 045 DECORATIVE. 045 sets ``FORCE ROW LEVEL
    SECURITY`` precisely so a table's owner is bound by its own tenant policy -- but nothing
    binds a superuser, and ``BYPASSRLS`` is named for what it does. Measured on a real
    cluster: with a superuser owner, ``SELECT count(*) FROM note`` with no tenant selected
    returned every tenant's rows; with a ``NOSUPERUSER NOBYPASSRLS`` owner it returned zero.

    Checked HERE rather than in scripts/provision_workspace_roles.sql because a disposable
    test cluster legitimately runs as its own superuser -- so the provisioning script warns
    and this refuses. A deployment reaches this function; a throwaway cluster does not, unless
    a test calls it deliberately. There is no flag that turns this off.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT r.rolname
            FROM pg_class c
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE ns.nspname = 'public'
              AND c.relname = ANY(%s)
              AND (r.rolsuper OR r.rolbypassrls)
            ORDER BY r.rolname
            """,
            (list(WORKSPACE_TABLES),),
        )
        return [row[0] for row in cur.fetchall()]


def assert_workspace_ready(conn: Any) -> None:
    """Raise unless this database can support workspace routes.

    Called at the point of enablement, not per request. Two independent requirements: the
    complete bundle is recorded as applied, and the tables it created are not owned by a role
    that row-level security cannot bind.

    Raises:
        IncompleteWorkspaceBundleError: the bundle is incomplete, or its tables have a
            privileged owner.
    """
    privileged = privileged_workspace_owners(conn)
    if privileged:
        raise IncompleteWorkspaceBundleError(
            f"the workspace tables are owned by {privileged}, which is a superuser or has "
            "BYPASSRLS. Migration 045's FORCE ROW LEVEL SECURITY cannot bind such a role, so "
            "tenant isolation would not hold for anything running as it. Reassign ownership to "
            "a dedicated NOSUPERUSER NOBYPASSRLS role before enabling Workspace -- "
            "docs/DEPLOYMENT.md section 4d."
        )
    if not workspace_schema_ready(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                (sorted(WORKSPACE_BUNDLE),),
            )
            present = {row[0] for row in cur.fetchall()}
        raise IncompleteWorkspaceBundleError(
            "WORKSPACE_ENABLED is set but this database has not applied the complete "
            f"workspace bundle: missing "
            f"{sorted(WORKSPACE_BUNDLE[v] for v in set(WORKSPACE_BUNDLE) - present)}. A "
            "feature flag cannot substitute for the row-level security and append-only "
            "guards those migrations install."
        )


def migration_url() -> tuple[str, bool]:
    """The connection migrations should run through, and whether it is the separate one.

    Returns ``(url, is_separate_identity)``. ``MIGRATION_DATABASE_URL`` when set, otherwise
    ``DATABASE_URL`` with ``False`` -- which every deployment and dev box does today and is
    why the fallback exists at all.

    THE BOOLEAN IS THE POINT. ``start.sh`` and the documented deployment commands print
    which identity was used, so the fallback is visible every time it happens rather than
    being discovered later by someone wondering why the web user owns the workspace tables.
    ``Settings._validate_workspace_identities`` then makes the fallback impossible once
    ``WORKSPACE_ENABLED`` is true: a table owner can drop the triggers that make its tables
    append-only, so the identity that runs DDL must not be the identity that serves
    requests.
    """
    import os

    explicit = (os.environ.get("MIGRATION_DATABASE_URL") or "").strip()
    if explicit:
        return explicit, True
    shared = (os.environ.get("DATABASE_URL") or "").strip()
    if not shared:
        raise DatabaseNotConfiguredError(
            "neither MIGRATION_DATABASE_URL nor DATABASE_URL is set; refusing to guess a "
            "connection string for a schema change."
        )
    return shared, False


def check_drift(conn: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Every migration on disk that has NOT been applied to ``conn``. Empty means this
    database exactly matches the repository.

    WHY THIS EXISTS. Six migrations (025 through 030 -- price-evidence waivers, snapshot
    exclusions, security_alias, price-waiver basis, and the two dividend migrations) sat
    fully written, fully tested, and fully committed while a real local database silently
    stayed six versions behind for an unknown number of sessions. The full pytest suite
    stayed green throughout, because ``tests/conftest_db.py`` migrates its own throwaway
    cluster automatically on every run -- a passing suite proves the MIGRATIONS ARE
    CORRECT, never that any particular real database has actually RUN them. Those are
    different claims, and nothing before this function ever checked the second one.

    This is consequently the one check in ``make lint`` that looks at a real database's
    state rather than at source text -- every other check in that target is static
    analysis (grep for a banned token, grep for a layer violation) and needs no service
    running. This one needs ``DATABASE_URL`` to point at something reachable, and the
    caller (``scripts/check_migration_drift.py``) is expected to skip cleanly, not fail,
    when it does not -- the same posture ``tests/conftest_db.py`` takes when ``initdb``/
    ``pg_ctl`` are absent. A skip here is a weaker signal than a check that ran and
    passed, exactly as CLAUDE.md already says about the Postgres-backed test suite; it is
    not a reason to make an unconfigured environment fail a gate it cannot evaluate.
    """
    applied = applied_versions(conn)
    return [path.name for version, path in discover(directory) if version not in applied]
