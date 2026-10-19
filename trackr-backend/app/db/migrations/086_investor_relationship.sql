-- A user's relationship is independent of an optional named family group.
ALTER TABLE investor ADD COLUMN relationship text NOT NULL DEFAULT 'client'
    CHECK (relationship IN ('family', 'client'));

UPDATE investor SET relationship = 'family' WHERE family_group_id IS NOT NULL;
