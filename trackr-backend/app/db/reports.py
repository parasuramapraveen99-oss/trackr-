"""Published report documents, artifacts and retractions (RP 05-07). Raw SQL, append-only.

Same contract as ``app/db/workspace.py``: every function takes a connection already scoped by
``enter_workspace``. Writes of documents/artifacts are the ENGINE's (the worker); a person can
only append an OWNER_REQUEST retraction. Migration 056 refuses output for a cancelled, expired
or failed job and any artifact for a retracted document.

Imports nothing from the investor notebook (``make lint``).
"""

from __future__ import annotations

import json
from typing import Any

from app.db import workspace as ws
from app.research.errors import not_found

_rows = ws._rows
_one = ws._one


def insert_document(conn: Any, investor_id: str, job_id: str, isin: str, report_version: int,
                    status: str, contract_version: str, document_json: str, document_sha: str,
                    ledger_json: str, ledger_sha: str, published: int, withheld: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_document (investor_id, job_id, isin, report_version, status,
                contract_version, document_json, document_sha256, ledger_json, ledger_sha256,
                claims_published, claims_withheld)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, job_id, isin, report_version, status, contract_version,
             document_json, document_sha, ledger_json, ledger_sha, published, withheld))
        return str(cur.fetchone()[0])


def insert_artifact(conn: Any, investor_id: str, report_document_id: str, fmt: str, sha: str,
                    size: int, pages: int | None, document_id: str, renderer: str,
                    manifest: dict[str, Any], artifact_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_artifact (investor_id, id, report_document_id, format, sha256,
                byte_size, page_count, document_id, renderer, manifest_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (investor_id, artifact_id, report_document_id, fmt, sha, size, pages, document_id,
             renderer, json.dumps(manifest, sort_keys=True)))
        return str(cur.fetchone()[0])


def document_for_job(conn: Any, investor_id: str, job_id: str) -> dict[str, Any] | None:
    return _one(conn, """
        SELECT id, job_id, isin, report_version, status, contract_version, document_json,
               document_sha256, ledger_json, claims_published, claims_withheld, recorded_at
          FROM report_document WHERE investor_id = %s AND job_id = %s""",
                (investor_id, job_id))


def artifacts(conn: Any, investor_id: str, report_document_id: str) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT id, format, sha256, byte_size, page_count, document_id, renderer, recorded_at
          FROM report_artifact WHERE investor_id = %s AND report_document_id = %s
         ORDER BY format""", (investor_id, report_document_id))


def artifact(conn: Any, investor_id: str, report_document_id: str, artifact_id: str
             ) -> dict[str, Any]:
    row = _one(conn, """
        SELECT id, format, sha256, byte_size, page_count, document_id, renderer, manifest_json
          FROM report_artifact WHERE investor_id = %s AND report_document_id = %s AND id = %s""",
               (investor_id, report_document_id, ws._uuid(artifact_id, "artifact")))
    if row is None:
        raise not_found("artifact")
    return row


def retractions(conn: Any, investor_id: str, report_document_id: str) -> list[dict[str, Any]]:
    return _rows(conn, """
        SELECT id, reason, detail, actor_uid, recorded_at FROM report_retraction
         WHERE investor_id = %s AND report_document_id = %s ORDER BY seq""",
                 (investor_id, report_document_id))


def retract(conn: Any, investor_id: str, report_document_id: str, reason: str,
            detail: str | None, actor_uid: str | None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO report_retraction (investor_id, report_document_id, reason, detail,
                                           actor_uid)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
            """, (investor_id, report_document_id, reason, detail, actor_uid))
        return str(cur.fetchone()[0])


def report_version(conn: Any, investor_id: str, job: dict[str, Any]) -> int:
    """1 + the number of earlier jobs in this job's revision chain."""
    n, cur = 1, job.get("revision_of")
    while cur is not None and n < 1000:
        n += 1
        row = _one(conn, "SELECT revision_of FROM hosted_job WHERE investor_id = %s AND id = %s",
                   (investor_id, cur))
        cur = row["revision_of"] if row else None
    return n
