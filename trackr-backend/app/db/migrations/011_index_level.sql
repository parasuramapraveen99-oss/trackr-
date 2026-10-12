-- 011_index_level.sql -- raw external index-level data for benchmark comparison
-- (docs/ENGINE_SPEC.md section 6). app/routers/analytical.py's performance() reads this
-- to replicate the portfolio's own cash flows into an index -- "what if I had bought the
-- index on exactly the days I bought stocks". Written by
-- app.prices.index_sources.fetch_and_store, the single ingestion path, mirroring how
-- app.prices.pipe writes `price`.
--
-- DELIBERATELY NOT `price`, and not a variant of it:
--   * `price` is keyed by (isin, price_date) and isin is a FOREIGN KEY into `security`.
--     `index_level` has NO such key and NO such foreign key -- an index level cannot be
--     joined against a security by construction, so it cannot be mistaken for one
--     anywhere in the schema, and it never enters app/engine/valuation.py's per-holding
--     path (that function takes a `dict[isin, price]`, not an index name).
--   * `index_name` is a fixed, checked enum, not a free-text symbol -- there is no
--     ambiguity about what a row represents the way a ticker could be.
--
-- Like `price`, this is RAW EXTERNAL DATA, not a derivation -- CLAUDE.md's "holdings
-- table written by anything but a rebuild" ban does not apply here, the same way it
-- does not apply to `price`: nothing about an index's own published level is computed
-- from this system's transaction ledger.
CREATE TABLE index_level (
    index_name  text NOT NULL CHECK (index_name IN ('NIFTY50_TRI', 'SENSEX_TRI')),
    level_date  date NOT NULL,
    close       numeric(20,6) NOT NULL,
    source      text NOT NULL,
    PRIMARY KEY (index_name, level_date)
);
COMMENT ON TABLE index_level IS
    'Published TRI (total-return) index levels, e.g. NIFTY 50 TRI. NOT a security price '
    '-- no FK to security, never joined against an ISIN. TRI only, never a price index '
    '(docs/ENGINE_SPEC.md section 6: a price index excludes dividends and understates '
    'the benchmark). Written exclusively by app.prices.index_sources.fetch_and_store.';
COMMENT ON COLUMN index_level.index_name IS
    'Fixed enum, not a free-text symbol -- SENSEX_TRI is a valid value seeded here even '
    'though no adapter fetches it yet (docs/PHASE2_PROMPTS.md session 3): no reliable '
    'free, machine-fetchable source exists, so the router rejects it explicitly (400) '
    'rather than this table ever holding a guessed or approximated row for it.';

CREATE INDEX index_level_name_date_desc_idx ON index_level (index_name, level_date DESC);
