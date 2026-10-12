-- 064_fno_trade.sql -- the append-only equity F&O ledger, and F&O rows in staging.
--
-- docs/FNO_PLAN.md Piece 1, docs/DATA_MODEL.md "Equity F&O", docs/ARCHITECTURE_INVARIANTS.md
-- rule 21(b). F&O trades never enter `transaction` (every column there is keyed by ISIN; a
-- derivative has none). Same discipline as `transaction`: append-only by trigger, a UNIQUE
-- dedupe_key, and the one DPDP erasure escape hatch. NO CORRECTION MECHANISM EXISTS YET:
-- there is no F&O reversal path (no reversing kind, no writer), so a wrong fno_trade row
-- cannot currently be corrected -- only erased with the investor. The trigger message below
-- names reversing rows as the intended pattern, not a built one.
--
-- STAGING: a discriminator on import_row, not a second staging table. An F&O row from a
-- contract note rides the same import_batch, the same statuses (STAGED / PROMOTED /
-- DUPLICATE / REJECTED), the same review and the same attributed promote as the equity
-- rows of that note; only the ledger it lands in differs. segment = 'FNO' rows carry an
-- F&O payload whose contract was resolved at staging (exactly one fno_contract match) or
-- are REJECTED with the candidates named.

CREATE TABLE IF NOT EXISTS fno_trade (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Created order. Two trades on one date replay in the order they were written
    -- (app.engine.fno_types.FnoTrade.seq); created_at is the same for a whole promote.
    seq         bigserial NOT NULL,
    account_id  uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    contract_id uuid NOT NULL REFERENCES fno_contract(id),
    trade_date  date NOT NULL,
    side        text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    -- Units (lots x lot size), always positive; direction comes from side.
    quantity    numeric(20,6) NOT NULL CHECK (quantity > 0),
    price       numeric(20,6) NOT NULL CHECK (price >= 0),
    -- This trade's share of the note's F&O charges (apportioned by turnover); stt is a
    -- subset of charges.
    charges     numeric(20,6) NOT NULL DEFAULT 0 CHECK (charges >= 0),
    stt         numeric(20,6) NOT NULL DEFAULT 0 CHECK (stt >= 0 AND stt <= charges),
    kind        text NOT NULL DEFAULT 'TRADE' CHECK (kind IN ('TRADE', 'EXCHANGE_SETTLEMENT')),
    source      txn_source NOT NULL,
    source_ref  text,
    dedupe_key  text NOT NULL,
    raw_payload jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE fno_trade IS
    'APPEND-ONLY equity F&O ledger. No UPDATE, no DELETE -- enforced by trigger below. '
    'Positions are computed on read and never stored. docs/ARCHITECTURE_INVARIANTS.md '
    'rule 21(b).';

-- Re-importing the same note (or its PDF and XLSX forms) must be a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS fno_trade_dedupe_key_uniq ON fno_trade (dedupe_key);
CREATE INDEX IF NOT EXISTS fno_trade_account_contract_idx
    ON fno_trade (account_id, contract_id, trade_date);

-- Same enforcement and the same single escape hatch as transaction_append_only (001):
-- DPDP erasure sets app.erasure_authorised LOCAL and cascades from investor.
CREATE OR REPLACE FUNCTION fno_trade_append_only() RETURNS trigger AS $$
BEGIN
    IF current_setting('app.erasure_authorised', true) = 'on' THEN
        RETURN COALESCE(OLD, NEW);
    END IF;
    RAISE EXCEPTION
        'fno_trade is append-only (attempted %). Corrections are new reversing rows; '
        'erasure must set app.erasure_authorised.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fno_trade_no_update_or_delete ON fno_trade;
CREATE TRIGGER fno_trade_no_update_or_delete
    BEFORE UPDATE OR DELETE ON fno_trade
    FOR EACH ROW EXECUTE FUNCTION fno_trade_append_only();

ALTER TABLE import_row
    ADD COLUMN IF NOT EXISTS segment text NOT NULL DEFAULT 'EQ'
        CHECK (segment IN ('EQ', 'FNO'));

COMMENT ON COLUMN import_row.segment IS
    'EQ: payload.transaction, promoted into transaction. FNO: payload.fno_trade, promoted '
    'into fno_trade and never into transaction (rule 21(b)).';
