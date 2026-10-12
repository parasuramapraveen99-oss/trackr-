-- 014_stt_column.sql -- Phase 3 Session 10, docs/PHASE3_SESSION10_STT_FIX.md.
-- Income Tax Act Section 48 disallows STT specifically as a deductible expense when
-- computing capital gains on STT-paid equity (both the buy and sell leg) -- see
-- docs/TAX_ENGINE.md's Session 10 status note. `charges` already includes STT and keeps
-- its current meaning unchanged (real cash paid, used by holdings.py's weighted-average
-- invested/avg_cost); `stt` is tracked ALONGSIDE it, as a SUBSET, not an addition, purely
-- so app.engine.realised.tax_cost/tax_proceeds can exclude it for the tax-facing Schedule
-- 112A / ITR capital-gains figures without touching Phase 1's display numbers.
--
-- Same shape as `charges` (001_core.sql) deliberately, including the absence of a CHECK
-- constraint -- `charges` itself carries none, and this is the same kind of field.
ALTER TABLE transaction ADD COLUMN stt numeric(20,6) NOT NULL DEFAULT 0;
COMMENT ON COLUMN transaction.stt IS
    'Securities Transaction Tax, ALREADY INCLUDED inside charges (a subset, not '
    'additive). Populated only by the contract-note adapter, pro-rata-by-turnover '
    'apportioned the same way charges is; 0 elsewhere (tradebook, manual entry) is '
    'absence of data, never evidence no STT was paid. app.engine.realised.tax_cost/'
    'tax_proceeds use this to exclude STT from tax-facing cost of acquisition / full '
    'value of consideration per Income Tax Act Section 48 -- docs/TAX_ENGINE.md.';
