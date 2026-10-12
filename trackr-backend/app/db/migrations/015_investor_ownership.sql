-- 015_investor_ownership.sql -- every investor belongs to exactly one login.
--
-- Until now nothing connected app_user to investor: any authenticated caller could read
-- any investor's ledger by passing a different {investor_id}, and several read endpoints
-- took no token at all. This column is the ownership edge the API authorises against.
--
-- Nullable on purpose: rows seeded by load_golden / seed_demo predate logins, and DPDP
-- erasure must not be blocked by a deleted user. An unowned investor is simply
-- inaccessible through the API -- app.db.assign_owner claims such rows for a real login.

ALTER TABLE investor
    ADD COLUMN owner_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL;

CREATE INDEX investor_owner_idx ON investor (owner_user_id);

COMMENT ON COLUMN investor.owner_user_id IS
    'The login that onboarded this investor and is authorised to read its ledger. '
    'NULL = unclaimed (fixture seeds); unclaimed investors are refused by the API '
    'until claimed via app.db.assign_owner.';
