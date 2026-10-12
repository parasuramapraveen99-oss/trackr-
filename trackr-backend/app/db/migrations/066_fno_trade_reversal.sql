-- 066_fno_trade_reversal.sql -- corrections to the F&O ledger are reversal rows.
--
-- docs/ENGINE_SPEC.md section 15 rule 15, docs/ARCHITECTURE_INVARIANTS.md rule 21(b). The
-- fno_trade ledger is append-only (migration 064's trigger): a wrong row is never edited or
-- deleted. A correction is a NEW row, kind = 'REVERSAL', whose reverses_id names the trade
-- it voids; the engine then drops both from matching. Written by
-- app.db.fno_corrections.reverse_fno_trade.
--
-- Schema only: no existing row is touched, so the append-only trigger is not involved.
--
-- 'CORRECTION' is added to txn_source for the reversal row's source. ADD VALUE is safe in
-- the migration transaction because nothing in this file uses the new value (see 033).
--
-- Additive and re-runnable.

ALTER TYPE txn_source ADD VALUE IF NOT EXISTS 'CORRECTION';

ALTER TABLE fno_trade ADD COLUMN IF NOT EXISTS reverses_id uuid REFERENCES fno_trade(id);

-- Migration 064 declared kind's CHECK inline, so PostgreSQL named it fno_trade_kind_check.
ALTER TABLE fno_trade DROP CONSTRAINT IF EXISTS fno_trade_kind_check;
ALTER TABLE fno_trade ADD CONSTRAINT fno_trade_kind_check
    CHECK (kind IN ('TRADE', 'EXCHANGE_SETTLEMENT', 'REVERSAL'));

-- A reversal always names its target, and nothing else may.
ALTER TABLE fno_trade DROP CONSTRAINT IF EXISTS fno_trade_reversal_target_check;
ALTER TABLE fno_trade ADD CONSTRAINT fno_trade_reversal_target_check
    CHECK ((kind = 'REVERSAL') = (reverses_id IS NOT NULL));

-- A trade is reversed at most once.
CREATE UNIQUE INDEX IF NOT EXISTS fno_trade_reverses_once_idx
    ON fno_trade (reverses_id) WHERE reverses_id IS NOT NULL;

COMMENT ON COLUMN fno_trade.reverses_id IS
    'Set only on a kind = REVERSAL row: the fno_trade it voids (ENGINE_SPEC section 15 '
    'rule 15). Unique: a trade is reversed at most once. The original is never edited.';
