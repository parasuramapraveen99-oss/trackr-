-- 017_cost_transfer_column.sql -- persists what app.engine.demerger already computes.
--
-- docs/ENGINE_SPEC.md section 11 (demerger cost-ratio allocation) and schema/
-- transaction.schema.json both already document `cost_transfer`: the absolute cost-basis
-- amount moving between the original and demerged-in security, carried on a
-- DEMERGER_COST_OUT / DEMERGER_COST_IN transaction pair, used only when quantity and
-- price are both 0. app.engine.types.Transaction has carried this field since Phase 3's
-- demerger session -- but no migration ever added the column, so a DEMERGER_COST_OUT/IN
-- pair saved via app.db.repository.save_transactions silently wrote everything except the
-- one field that makes the correction meaningful, and reloaded as a no-op adjustment
-- forever after. This closes that gap; nothing in app/engine/ changes.
--
-- Same shape as `stt` (014_stt_column.sql) deliberately: a plain NUMERIC(20,6) subset
-- field with a default of 0, "absent" and "explicitly zero" both spelled the same way as
-- every other optional money field on this table (`charges`, `dividend_amount`).
--
-- The `txn_type` enum itself has the same gap, one level up: `DEMERGER_IN` was added
-- (it is a Phase 1 corporate-action type, quantity up / pool unchanged), but
-- `DEMERGER_COST_OUT`/`DEMERGER_COST_IN` -- schema/transaction.schema.json's enum,
-- app.engine.types.TxnType, and app.engine.demerger's whole point -- were never added
-- to the database's own txn_type type. Without this, ANY attempt to save a demerger
-- cost-ratio correction fails with `invalid input value for enum txn_type`, not just a
-- silently-dropped cost_transfer. Confirmed against a real database, not assumed: this
-- surfaced only once tests/test_db_transactions.py actually ran against Postgres for the
-- first time (see docs/PHASE2_PROMPTS.md session 2 -- this environment previously had no
-- initdb/pg_ctl, so every test needing a real database silently skipped).
--
-- ADD VALUE cannot run in the same transaction that USES the new value (PostgreSQL
-- restriction, harmless here -- this migration only adds the values, nothing in this
-- file inserts a row using them).
ALTER TYPE txn_type ADD VALUE IF NOT EXISTS 'DEMERGER_COST_OUT';
ALTER TYPE txn_type ADD VALUE IF NOT EXISTS 'DEMERGER_COST_IN';

ALTER TABLE transaction ADD COLUMN cost_transfer numeric(20,6) NOT NULL DEFAULT 0;

COMMENT ON COLUMN transaction.cost_transfer IS
    'Used only when txn_type is DEMERGER_COST_OUT or DEMERGER_COST_IN, where quantity '
    'and price are both 0. The absolute cost-basis amount moving between the original '
    'and demerged security per the scheme document''s cost ratio (docs/ENGINE_SPEC.md '
    'section 11) -- a separate field rather than overloading `price`, for the same '
    'reason as `dividend_amount`: this is a capital-basis movement, not a per-unit '
    'trade. 0 on every other txn_type is absence of data, not a real transfer of 0.';
