"""Private, investor-scoped reconciliation evidence. No financial ledger writes."""

import json

from app.db import audit


def save_run(conn, investor_id, account_id, request, report):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reconciliation_run (investor_id, account_id, request, report) "
            "VALUES (%s, %s, %s, %s) RETURNING id, created_at",
            (investor_id, account_id, json.dumps(request), json.dumps(report)),
        )
        run_id, created_at = cur.fetchone()
    audit.record(
        conn,
        "RECONCILIATION",
        "reconciliation_run",
        str(run_id),
        diff={"investor_id": investor_id, "account_id": account_id},
    )
    return {"id": str(run_id), "created_at": created_at.isoformat(), **report}


def list_runs(conn, investor_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, account_id, request->>'as_of', request->>'mode' "
            "FROM reconciliation_run WHERE investor_id=%s ORDER BY created_at DESC, id LIMIT 100",
            (investor_id,),
        )
        return [
            {
                "id": str(i),
                "created_at": dt.isoformat(),
                "account_id": str(a),
                "as_of": day,
                "mode": mode,
            }
            for i, dt, a, day, mode in cur.fetchall()
        ]


def get_run(conn, investor_id, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_at, report FROM reconciliation_run WHERE investor_id=%s AND id=%s",
            (investor_id, run_id),
        )
        row = cur.fetchone()
    return None if row is None else {"id": str(run_id), "created_at": row[0].isoformat(), **row[1]}
