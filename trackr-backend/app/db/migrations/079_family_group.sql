-- 079_family_group.sql -- named families for advisers who manage many clients.
--
-- investor.family_group_id has existed since 001_core.sql ("HNI portfolios are family
-- units") but pointed at nothing: a bare uuid with no name, so a family could not be
-- shown, renamed or chosen from a list. This gives it a row of its own, owned by the login
-- that manages the clients, and makes the column a real reference.
--
-- Grouping only. Nothing financial lives here: holdings stay computed on read from the
-- ledger, and a family's totals are summed from its members at read time.

CREATE TABLE IF NOT EXISTS family_group (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    name          text NOT NULL CHECK (btrim(name) <> ''),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, name)
);

COMMENT ON TABLE family_group IS
    'A named group of investors (self, spouse, HUF, kids) owned by one login. Grouping '
    'only; totals are summed from the members on read.';

-- Any family id already on an investor gets a row, so the reference below holds. An id on
-- an unowned investor (fixture seeds) cannot be given an owner and is cleared instead.
UPDATE investor SET family_group_id = NULL
WHERE family_group_id IS NOT NULL AND owner_user_id IS NULL;

INSERT INTO family_group (id, owner_user_id, name)
SELECT DISTINCT ON (family_group_id)
       family_group_id, owner_user_id, 'Unnamed family ' || left(family_group_id::text, 8)
FROM investor
WHERE family_group_id IS NOT NULL
ORDER BY family_group_id, created_at
ON CONFLICT (id) DO NOTHING;

ALTER TABLE investor DROP CONSTRAINT IF EXISTS investor_family_group_fk;
ALTER TABLE investor ADD CONSTRAINT investor_family_group_fk
    FOREIGN KEY (family_group_id) REFERENCES family_group(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS investor_family_group_idx ON investor (family_group_id);
