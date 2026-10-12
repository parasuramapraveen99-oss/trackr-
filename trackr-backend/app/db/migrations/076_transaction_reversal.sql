-- 076_transaction_reversal.sql -- a correction to the equity ledger is a reversal row.
--
-- docs/ARCHITECTURE_INVARIANTS.md rules 2 and 20(f). The transaction ledger is append-only
-- (001's trigger): a wrong row is never edited or deleted. A correction is a NEW row whose
-- reverses_id names the row it voids; replay then drops both
-- (app.db.repository.load_transactions), so the engine never sees either. Same shape as
-- the F&O ledger's reversal (migration 066).
--
-- First writer: app.db.auto_reversal -- an AUTO_RESOLVED row (rule 20) that a document the
-- investor uploads contradicts, or no longer needs, is reversed automatically (20f).
--
-- Schema only: no existing row is touched, so the append-only trigger is not involved.
-- Additive and re-runnable.

ALTER TABLE transaction ADD COLUMN IF NOT EXISTS reverses_id uuid REFERENCES transaction(id);

-- A reversal never names itself.
ALTER TABLE transaction DROP CONSTRAINT IF EXISTS transaction_reverses_other_check;
ALTER TABLE transaction ADD CONSTRAINT transaction_reverses_other_check
    CHECK (reverses_id IS NULL OR reverses_id <> id);

-- A row is reversed at most once.
CREATE UNIQUE INDEX IF NOT EXISTS transaction_reverses_once_idx
    ON transaction (reverses_id) WHERE reverses_id IS NOT NULL;

COMMENT ON COLUMN transaction.reverses_id IS
    'Set only on a reversal row: the transaction it voids (rule 2; rule 20f). Unique: a row '
    'is reversed at most once. Replay drops both rows. The original is never edited.';
