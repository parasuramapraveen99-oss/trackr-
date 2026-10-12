-- 019_nifty500_index_name.sql -- admit NIFTY 500 TRI to `index_level`'s checked enum.
--
-- 011_index_level.sql's COMMENT explains why index_name is a fixed CHECK rather than a
-- free-text symbol: a row's meaning must never be ambiguous the way a ticker could be.
-- That is exactly why adding an index is a migration and not a code-only change -- the
-- constraint is the schema's own statement of which series this table is allowed to hold.
--
-- NIFTY 500 TRI backs the Dashboard's benchmark line, where a broad-market index is the
-- honest comparator for a portfolio that is rarely large-cap-only; NIFTY 50 TRI stays the
-- benchmark for docs/ENGINE_SPEC.md section 6's single-figure XIRR comparison. Both come
-- from the same niftyindices TRI endpoint (app.prices.index_sources.TRI_INDICES), and TRI
-- ONLY in both cases -- a price index excludes dividends and understates the comparison.
--
-- SENSEX_TRI stays in the enum and stays unfetched: still no reliable free, machine-
-- fetchable source, so app.routers.analytical refuses it with a 400 rather than this
-- table ever holding a guessed row for it.
ALTER TABLE index_level DROP CONSTRAINT index_level_index_name_check;
ALTER TABLE index_level ADD CONSTRAINT index_level_index_name_check
    CHECK (index_name IN ('NIFTY50_TRI', 'NIFTY500_TRI', 'SENSEX_TRI'));

COMMENT ON COLUMN index_level.index_name IS
    'Fixed enum, not a free-text symbol. NIFTY50_TRI backs section 6''s benchmark XIRR; '
    'NIFTY500_TRI backs the Dashboard''s benchmark line. SENSEX_TRI is a valid value with '
    'no adapter behind it -- no reliable free, machine-fetchable source exists, so the '
    'router rejects it explicitly (400) rather than this table ever holding a guessed row.';
