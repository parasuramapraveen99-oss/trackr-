-- 047_workspace_receipts.sql -- durable, scoped idempotency receipts for Workspace writes.
--
-- CLOSES BE-V10 (BLUEPRINT_BACKEND.md section 7.1, "Company-link receipt"), and generalises
-- it to every research WRITE route rather than solving it once for `company` only.
--
-- THE PROBLEM. Every WRITE route takes a UUID `Idempotency-Key`. A retry with the same key
-- and the same canonical payload must return the ORIGINAL response; the same key with a
-- DIFFERENT payload must be refused (409 IDEMPOTENCY_CONFLICT) -- including after a process
-- restart, so an in-memory cache is not an answer. Root rows carry `dedupe_key`, which proves
-- a key was used but not WHAT it was used for: `company` has no dedupe_key at all, and
-- uniqueness of (investor, isin) cannot tell "retry" from "same key reused for another ISIN".
--
-- WHY A TABLE AND NOT audit_log METADATA, which the blueprint named as the preferred route.
-- The workspace capability roles hold INSERT on audit_log and deliberately NO SELECT (045):
-- the audit log is append-and-forget for them. Reading receipts back out of it would need
-- either a table-wide SELECT grant on every tenant's audit history -- far broader than a
-- receipt lookup -- or a SECURITY DEFINER function, which 045 explicitly refuses to introduce
-- to paper over a missing permission. A dedicated tenant-scoped table under the same RLS,
-- append-only and insert guards as the bundle is the narrow version of the same idea.
--
-- WHAT A RECEIPT HOLDS, AND WHAT IT MUST NEVER HOLD. The operation name, its parent scope
-- (an ISIN or a parent row id), the key, a SHA-256 of the canonical request, the HTTP status
-- of the original success, and the IDENTIFIERS of what was created -- nothing else. No request
-- body, no note text, no answer, no source URL. `result` is enforced to be a flat object of
-- strings so that a future caller cannot quietly start storing prose in it.
--
-- NOT A FINANCIAL RECORD. Nothing here touches the ledger.

CREATE TABLE ws_receipt (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  -- The server operation, e.g. 'company.link' or 'note.revise'. A closed, code-owned
  -- vocabulary; the pattern keeps it from carrying anything else.
  operation text NOT NULL CHECK (operation ~ '^[a-z][a-z_.]{2,63}$'),
  -- The parent the operation acted under: an ISIN, a parent row id, or '-' for none.
  scope text NOT NULL CHECK (scope ~ '^[A-Za-z0-9-]{1,64}$'),
  idem_key uuid NOT NULL,
  payload_digest text NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
  status_code smallint NOT NULL CHECK (status_code IN (200,201)),
  -- Identifiers only. Flat object, every value a string (or an array of strings).
  result jsonb NOT NULL CHECK (
    jsonb_typeof(result) = 'object' AND ws_string_numbers(result)),
  actor_uid uuid REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  -- The namespace BLUEPRINT_BACKEND.md 7.1 fixes: investor + operation + parent + key.
  UNIQUE (investor_id,operation,scope,idem_key)
);

COMMENT ON TABLE ws_receipt IS
    'Durable idempotency receipt for a Workspace research write: operation, parent scope, '
    'key, SHA-256 of the canonical request and the identifiers it produced. Never the '
    'request body or any research prose. Lets a retry after a restart replay the original '
    'response and lets a reused key with a changed payload be refused. Append-only and '
    'tenant-scoped exactly like the 040-045 bundle.';

-- Same three guards 044 installs on the bundle's twenty-five tables. ws_insert_guard's
-- generic prefix takes the tenant advisory lock, checks tenant and human attribution and
-- stamps recorded_at/creation_xid; no table-specific branch is needed.
CREATE TRIGGER ws_insert BEFORE INSERT ON ws_receipt
  FOR EACH ROW EXECUTE FUNCTION ws_insert_guard();
CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON ws_receipt
  FOR EACH ROW EXECUTE FUNCTION ws_append_only();
CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON ws_receipt
  FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate();

-- Same tenant policy and grant shape as 045.
ALTER TABLE ws_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE ws_receipt FORCE ROW LEVEL SECURITY;
CREATE POLICY ws_tenant_rows ON ws_receipt
  USING (investor_id = ws_tenant()) WITH CHECK (investor_id = ws_tenant());
REVOKE ALL ON ws_receipt FROM PUBLIC;
GRANT SELECT ON ws_receipt TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,
  trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser;
GRANT DELETE ON ws_receipt TO trackr_ws_eraser;
-- Human writes (notes, rituals, answers, theses, facts, sources) and engine writes
-- (evaluation receipts) are the two paths that take an Idempotency-Key today.
GRANT INSERT ON ws_receipt TO trackr_ws_human,trackr_ws_engine;
