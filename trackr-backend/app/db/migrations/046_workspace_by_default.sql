-- 046_workspace_by_default.sql -- Workspace is granted at signup. There is no operator gate.
--
-- A PRODUCT DECISION WAS REVERSED, 2026-09-27, BY THE OWNER. Until today
-- `docs/PRODUCT_ARCHITECTURE.md` section 4 read:
--
--     "Ordinary signup grants portfolio only. Workspace access initially uses an explicit
--      audited operator command."
--
-- The owner's decision is that there is no operator command, and that either product must be
-- usable by whoever signs up. `app/db/capabilities.py::DEFAULT_CAPABILITIES` now grants both at
-- registration, and the contract has been rewritten to match rather than left contradicting the
-- code.
--
-- WHY THIS IS A NEW MIGRATION AND NOT AN EDIT TO 039. 039 is applied -- to the local development
-- database on 2026-09-26, and to anything else that has run it. CLAUDE.md's rule is absolute:
-- never rewrite an applied migration. Its file keeps its original text, which is the honest
-- record of what was true when it ran; this file records what changed and when. The same pattern
-- as `035_correct_stale_comment_references.sql`, which exists for exactly this reason.
--
-- WHAT THIS FILE DOES NOT DO: add a column, a constraint, a table or an index. The schema is
-- unchanged. It backfills rows and corrects two `COMMENT ON` strings that now describe a rule
-- that no longer exists -- and a comment in a live database asserting an operator gate that is
-- not enforced is worse than no comment, because the next person reads it as the design.

-- ---------------------------------------------------------------------------------
-- 1. Backfill. Every existing login gets Workspace.
-- ---------------------------------------------------------------------------------
--
-- Mirrors 039's own portfolio backfill, and for the same reason: doing it in the migration makes
-- it true for every environment the migration reaches, including one nobody remembers to run a
-- script against. Without this, accounts created before today would be the only ones that still
-- needed the operator command the owner has just abolished -- which is precisely the confusing
-- half-state to avoid.
--
-- `ON CONFLICT (user_id, capability) DO UPDATE ... WHERE revoked_at IS NOT NULL` reactivates a
-- row that was explicitly revoked. That is deliberate: this is a reset to the new default, not a
-- respectful merge with the old policy's decisions. There are no production revocations to
-- preserve -- the capability existed for one day and only as an operator gate.
--
-- `granted_by` names this migration, so the audit question "who gave this account Workspace?"
-- has a truthful answer rather than looking like a person did it.
INSERT INTO user_capability (user_id, capability, granted_by)
SELECT id, 'workspace', 'migration:046_workspace_by_default'
FROM app_user
ON CONFLICT (user_id, capability) DO UPDATE
   SET revoked_at = NULL,
       granted_at = now(),
       granted_by = EXCLUDED.granted_by
 WHERE user_capability.revoked_at IS NOT NULL;

-- ---------------------------------------------------------------------------------
-- 2. Correct the comments 039 left in the database.
-- ---------------------------------------------------------------------------------

COMMENT ON TABLE user_capability IS
    'Which products a login may use. A grant is a product capability, never authority '
    'over another account''s data -- investor.owner_user_id (015) remains the only thing '
    'that decides whose ledger a request may read. Since migration 046 both capabilities '
    'are granted at signup: there is no operator gate on Workspace, and either product is '
    'usable by whoever registers.';

COMMENT ON COLUMN user_capability.capability IS
    'portfolio | workspace. Checked in the database as well as in Python: the allowed set '
    'is small, closed, and worth being unable to typo. Effective capabilities are resolved '
    'in app/db/capabilities.py -- workspace IMPLIES portfolio, and that implication lives '
    'in one function rather than in every caller. Both are granted at registration by '
    'DEFAULT_CAPABILITIES; neither is ever inferred from a request Origin, header, email '
    'domain or frontend build.';

COMMENT ON COLUMN user_capability.granted_by IS
    'Who or what performed the last change: ''registration'' for a signup, '
    '''migration:0NN_...'' for a backfill, or the operator''s name from '
    'app/db/grant_capability.py -- which remains the way a capability is REVOKED, and '
    'restored afterwards. The full history is in audit_log under '
    'entity_type = ''user_capability''.';

-- NOT REMOVED: `app/db/grant_capability.py`. Abolishing the operator gate on signup does not
-- abolish the ability to revoke, which is the same command and is still audited. A capability
-- that could be granted but never withdrawn would make "revocation blocks new work" -- a
-- property `has_capability` is built around and `docs/PRODUCT_ARCHITECTURE.md` still requires --
-- untestable.
