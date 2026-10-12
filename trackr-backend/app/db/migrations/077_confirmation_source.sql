-- 077_confirmation_source.sql -- who confirmed an inferred cash flow.
--
-- 068's confirmation was only ever the investor's click. Rule 20(f) adds a second, stronger
-- confirmer: a CAS the investor uploads whose quantity matches the ledger WITH the
-- auto-resolved row (app.db.auto_reversal). The screen says which ("Matches your CAS").
-- Additive and re-runnable; existing rows were all the investor's.
ALTER TABLE inferred_cash_flow_confirmation
    ADD COLUMN IF NOT EXISTS confirmed_by text NOT NULL DEFAULT 'INVESTOR'
    CHECK (confirmed_by IN ('INVESTOR', 'CAS'));
