"""Persistence for the generated narrative and the fact packs behind it (migration 034).

Narrow and typed, like ``app/db/snapshots.py``: rows in, rows out, no opinion about when
anything should be generated. That orchestration lives in ``app.routers.narrative``,
which is the only caller permitted to write here.

Both tables are caches of a derivation and both are safe to truncate -- the analytics
page renders every figure without either. Nothing in this module ever returns a FIGURE
to a caller: the artifact holds fact ids and prose, and the values a reader sees always
come from a live ledger replay.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any


def upsert_factpack(
    conn: Any,
    investor_id: str,
    as_of: date,
    *,
    advised_only: bool,
    benchmark: str,
    universe: str,
    facts: dict[str, Any],
    sha256: str,
) -> None:
    """Store one quantised pack. Idempotent, and a rerun carries the better number
    through -- same ``DO UPDATE`` reasoning as ``upsert_snapshot``, because a republished
    bhavcopy changes what the day's figures were."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO narrative_factpack
                (investor_id, as_of, advised_only, benchmark, universe,
                 facts, factpack_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (investor_id, as_of, advised_only, benchmark, universe) DO UPDATE
            SET facts = EXCLUDED.facts,
                factpack_sha256 = EXCLUDED.factpack_sha256,
                computed_at = now()
            """,
            (
                investor_id, as_of, advised_only, benchmark, universe,
                json.dumps(facts, sort_keys=True, default=str), sha256,
            ),
        )


def get_previous_factpack(
    conn: Any,
    investor_id: str,
    as_of: date,
    *,
    advised_only: bool,
    benchmark: str,
    universe: str,
) -> tuple[date, dict[str, Any]] | None:
    """The most recent stored pack STRICTLY BEFORE ``as_of``, or None.

    Strictly before, and whatever date that turns out to be -- never "yesterday". There
    is no row for a weekend or a holiday, and a comparison that assumed one would either
    find nothing or, worse, silently compare across a gap it did not mention. The caller
    gets the date back so a change can say which day it is measured from.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT as_of, facts
            FROM narrative_factpack
            WHERE investor_id = %s AND as_of < %s AND advised_only = %s
              AND benchmark = %s AND universe = %s
            ORDER BY as_of DESC
            LIMIT 1
            """,
            (investor_id, as_of, advised_only, benchmark, universe),
        )
        row = cur.fetchone()
    if row is None:
        return None
    facts = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    return row[0], facts


def get_artifact(
    conn: Any,
    investor_id: str,
    surface: str,
    as_of: date,
    *,
    advised_only: bool,
    benchmark: str,
    universe: str,
    factpack_sha256: str,
    rule_version: str,
    template_version: str,
    definition_pack_sha: str,
    model: str,
) -> list[dict[str, Any]] | None:
    """The stored claims for this EXACT combination of facts and prompt, or None.

    Every input that could change the answer is in the lookup, not only the facts. A
    miss on a changed definition card or a bumped prompt version is the point: serving
    output produced under a different prompt as though it came from this one is the
    failure this key exists to prevent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT claims
            FROM narrative_artifact
            WHERE investor_id = %s AND surface = %s AND as_of = %s
              AND advised_only = %s AND benchmark = %s AND universe = %s
              AND factpack_sha256 = %s AND rule_version = %s
              AND template_version = %s AND definition_pack_sha = %s AND model = %s
            """,
            (
                investor_id, surface, as_of, advised_only, benchmark, universe,
                factpack_sha256, rule_version, template_version,
                definition_pack_sha, model,
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row[0], list) else json.loads(row[0])


def upsert_artifact(
    conn: Any,
    investor_id: str,
    surface: str,
    as_of: date,
    *,
    advised_only: bool,
    benchmark: str,
    universe: str,
    factpack_sha256: str,
    rule_version: str,
    template_version: str,
    definition_pack_sha: str,
    model: str,
    claims: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO narrative_artifact
                (investor_id, surface, as_of, advised_only, benchmark, universe,
                 factpack_sha256, rule_version, template_version, definition_pack_sha,
                 model, claims, rejections)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (investor_id, surface, as_of, advised_only, benchmark, universe,
                         factpack_sha256, rule_version, template_version,
                         definition_pack_sha, model) DO UPDATE
            SET claims = EXCLUDED.claims,
                rejections = EXCLUDED.rejections,
                generated_at = now()
            """,
            (
                investor_id, surface, as_of, advised_only, benchmark, universe,
                factpack_sha256, rule_version, template_version, definition_pack_sha,
                model, json.dumps(claims, default=str), json.dumps(rejections, default=str),
            ),
        )


def count_artifacts(conn: Any, investor_id: str | None = None) -> int:
    """Row count, for verifying that a cron actually ran.

    Exists because of a documented failure in this repo: the dividend routes were
    registered, green on every test run, and named in no cron -- so nothing called them
    and the tables stayed empty for weeks. A green suite is not evidence that a job ran;
    a row count is.
    """
    with conn.cursor() as cur:
        if investor_id is None:
            cur.execute("SELECT count(*) FROM narrative_artifact")
        else:
            cur.execute(
                "SELECT count(*) FROM narrative_artifact WHERE investor_id = %s",
                (investor_id,),
            )
        return int(cur.fetchone()[0])
