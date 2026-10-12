-- 039_user_capability.sql -- which PRODUCTS a login may use. Not which data it may read.
--
-- Two separate questions, and conflating them is the mistake this table exists to avoid:
--
--   "may this login open Trackr Workspace?"      <- THIS TABLE (a product capability)
--   "may this login read investor X's ledger?"   <- investor.owner_user_id (015), unchanged
--
-- A capability never widens ownership and ownership never implies a capability. Every
-- research route added later requires BOTH, and `require_workspace_access` is deliberately
-- a separate dependency from `require_owned_investor` so neither can be mistaken for the
-- other at a call site. See docs/PRODUCT_ARCHITECTURE.md section 4.
--
-- NOT a billing table. There is no price, plan, period, invoice or payment-provider id
-- here, and no public endpoint writes it. Workspace access is provisioned by an audited
-- operator command (app/db/grant_capability.py). When billing arrives it belongs in its
-- own table that GRANTS through this one; it does not belong in these columns.
--
-- ONE ROW PER (user, capability), FOREVER. Granting a capability that was revoked clears
-- `revoked_at` on the existing row rather than inserting a second one, so "is this active"
-- is a column read and never a fold over history. The audit_log carries the history --
-- who granted, who revoked, when -- because that is the table already trusted for it
-- (app/db/audit.py), and a second private history here would be one more thing to keep
-- consistent with it.

CREATE TABLE user_capability (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    capability  text NOT NULL,
    granted_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz,
    -- Free text naming the human or process that last changed this row, for the case
    -- where the operator is not itself an app_user (a shell on the box, a deploy job).
    -- Deliberately not an FK: a nullable FK to app_user would quietly become NULL for
    -- exactly the operators who most need attributing.
    granted_by  text,

    CONSTRAINT user_capability_known CHECK (capability IN ('portfolio', 'workspace')),
    CONSTRAINT user_capability_unique UNIQUE (user_id, capability)
);

COMMENT ON TABLE user_capability IS
    'Which products a login may use. A grant is a product capability, never authority '
    'over another account''s data -- investor.owner_user_id (015) remains the only '
    'thing that decides whose ledger a request may read.';
COMMENT ON COLUMN user_capability.capability IS
    'portfolio | workspace. Checked in the database as well as in Python: the allowed '
    'set is small, closed, and worth being unable to typo. Effective capabilities are '
    'resolved in app/db/capabilities.py -- workspace IMPLIES portfolio, and that '
    'implication lives in one function rather than in every caller.';
COMMENT ON COLUMN user_capability.revoked_at IS
    'NULL means active. Revocation is a timestamp, not a DELETE: losing a capability '
    'must never look like never having had one, and DPDP export/erasure rights survive '
    'a downgrade (docs/PRODUCT_ARCHITECTURE.md section 4). Research rows are NOT '
    'deleted when this is set.';
COMMENT ON COLUMN user_capability.granted_by IS
    'Who performed the last change, as the operator command recorded it. The full '
    'history is in audit_log under entity_type = ''user_capability''.';

-- The hot path is "what can THIS user do, right now", once per authenticated request
-- that needs it. Partial on revoked_at IS NULL because a revoked row is never the
-- answer to that question and there is no reason to carry it in the index.
CREATE INDEX user_capability_active_idx
    ON user_capability (user_id)
    WHERE revoked_at IS NULL;

-- BACKFILL. Every login that exists today keeps working exactly as it did, which means
-- portfolio for everyone and workspace for no one. Doing this in the migration rather
-- than in application startup is what makes it true for every environment the migration
-- reaches, including one nobody remembers to run a script against.
--
-- ON CONFLICT DO NOTHING so re-running against a database that somehow already has rows
-- is a no-op rather than an error -- the same posture as every other idempotent insert
-- in this schema. Nothing here grants workspace: there is deliberately no environment in
-- which this migration alone turns Workspace on for anybody.
INSERT INTO user_capability (user_id, capability, granted_by)
SELECT id, 'portfolio', 'migration:039_user_capability'
FROM app_user
ON CONFLICT (user_id, capability) DO NOTHING;
