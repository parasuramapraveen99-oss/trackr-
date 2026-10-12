-- 012_market_cap.sql -- market-cap bucketing, the other half of "Sector / market-cap
-- breakdown" (docs/FEATURE_BACKLOG.md's Phase 2 table). Sector shipped in Phase 2
-- proper; this closes the remaining line.
--
-- Shaped exactly like security.sector (001_core.sql): a nullable enrichment column,
-- refreshed periodically by a standalone loader (app.db.load_market_cap), never written
-- by the engine. NULL means "not yet classified", never "small cap" -- the same
-- never-default rule CLAUDE.md rule 4 states for prices applies to reference data here.
--
-- Source is AMFI's SEBI-mandated "Categorisation of Large, Mid and Small Cap Stocks"
-- (https://www.amfiindia.com/otherdata/categorisation-of-stocks), refreshed twice
-- yearly. Its own classification carries exactly these three values (verified against a
-- live pull covering 5,427 ISINs) -- a CHECK constraint on security.exchange's pattern,
-- not a new ENUM type, since a 3-value set on one column doesn't need CREATE TYPE's
-- cross-migration ceremony.
ALTER TABLE security ADD COLUMN market_cap_bucket text
    CHECK (market_cap_bucket IN ('Large Cap', 'Mid Cap', 'Small Cap'));
COMMENT ON COLUMN security.market_cap_bucket IS
    'AMFI/SEBI large/mid/small-cap classification, refreshed twice yearly by '
    'app.db.load_market_cap. NULL for any ISIN outside AMFI''s list rather than guessed '
    '-- shown as its own "Unclassified" group, never merged into a real bucket.';
