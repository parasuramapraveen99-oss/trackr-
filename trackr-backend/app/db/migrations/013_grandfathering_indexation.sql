-- 013_grandfathering_indexation.sql -- reference data Phase 3 needs before Sessions 3
-- (grandfathering) and 4 (indexation) can be written: docs/TAX_ENGINE.md's
-- "Grandfathering" and "Indexation" sections respectively. Schema-only -- no real FMV
-- or CII values are seeded here; that's sourcing work for those later sessions
-- (docs/PHASE3_PROMPTS.md sessions 3 and 4). No change needed to
-- schema/asset_classes.json: EQ already carries long_term_days=365, which STCG/LTCG
-- classification (session 2) reads directly.

-- Shaped exactly like security.sector (001_core.sql) and security.market_cap_bucket
-- (012_market_cap.sql): a nullable enrichment column, sourced once, never written by
-- the engine. NULL means "not sourced" for most securities (anything listed after
-- 31 Jan 2018 never needs one) -- never coerced to 0, which is exactly the "missing
-- price defaults to zero" failure CLAUDE.md rule 4 bans, applied to this constant the
-- same way it applies to `price`.
ALTER TABLE security ADD COLUMN fmv_31jan2018 numeric(20,6)
    CHECK (fmv_31jan2018 IS NULL OR fmv_31jan2018 > 0);
COMMENT ON COLUMN security.fmv_31jan2018 IS
    'Fair market value on 31 Jan 2018, the equity grandfathering constant '
    '(docs/TAX_ENGINE.md: cost_of_acquisition = max(buy_price, min(fmv, sell_price))). '
    'One-time historical fact sourced once from exchange archives -- NULL for any '
    'security with no verified value, never 0. Populated by a future loader, not this '
    'migration.';

-- Static CBDT reference table for indexation (docs/TAX_ENGINE.md: indexed_cost = cost
-- x (CII_sale_year / CII_purchase_year)). Modelled on index_level, not on `price`: a
-- CII value is published once a year by a government body, is never a per-security
-- fact, and must never be joined against `security` -- CLAUDE.md rule 4 ("a missing
-- price raises, never defaults to zero") applies here too: a lookup miss in this table
-- must raise in the engine layer, never fall back to 1.0.
CREATE TABLE cost_inflation_index (
    financial_year text PRIMARY KEY CHECK (financial_year ~ '^\d{4}-\d{2}$'),
    cii_value      numeric(10,2) NOT NULL CHECK (cii_value > 0),
    source         text NOT NULL
);
COMMENT ON TABLE cost_inflation_index IS
    'CBDT Cost Inflation Index, one row per Indian financial year (e.g. "2023-24"), '
    'used only for indexation of pre-Apr-2023 debt-MF (and, per docs/TAX_ENGINE.md, '
    'property and unlisted-asset) acquisitions. Static external reference data, not '
    'derived from the transaction ledger -- same category as index_level, deliberately '
    'not a column on `price`, which is keyed by (isin, price_date) and FK''d to '
    '`security`; a CII value has no ISIN and must never be joinable against one. Real '
    'CBDT figures are sourced and loaded in a later Phase 3 session -- this migration '
    'seeds the shape only, per CLAUDE.md''s asset_classes.json precedent.';
COMMENT ON COLUMN cost_inflation_index.financial_year IS
    'Indian FY, April-to-March, "YYYY-YY" e.g. "2023-24" for 1 Apr 2023 - 31 Mar 2024 '
    '-- same string convention app.routers.transactions._financial_year_bounds parses '
    'and schema/dividend.schema.json''s by_fy keys already use.';
COMMENT ON COLUMN cost_inflation_index.cii_value IS
    'CBDT-published index number for the year, e.g. 348 for FY2023-24. Always a whole '
    'number historically; numeric(10,2) leaves headroom without NUMERIC(20,6)''s '
    '6-decimal money precision, since this is an index, not a currency amount.';
