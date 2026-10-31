-- 095_mf_statement_import.sql -- CAMS/KFintech statement transactions into the ledger.
--
-- docs/MF_ETF_PLAN.md section 6: "Authoritative amounts are required before transaction
-- launch." A registrar prints the rupees that moved; units x NAV is only a check on them,
-- because units are rounded to 3 decimals. Measured on the first real detailed CAS
-- (26-Oct-2026): every purchase's units x NAV missed the printed amount by a fraction of a
-- paisa, while every redemption matched units x NAV - STT exactly.
--
-- THREE ADDITIVE CHANGES, nothing altered or narrowed:
--
-- 1. txn_source RTA_STATEMENT. A registrar's statement is evidence of an allotment and a
--    payment, which none of the existing sources says (CAS is a depository's DECLARED
--    quantities; MANUAL is a human's assertion). ADD VALUE only -- the first row using it
--    is written by the application in a later transaction (see 033's note).
--
-- 2. import_row.segment 'MF'. A fund row is staged like an equity row but promoted by its
--    own path (app.db.imports._promote_mf), which writes the detail row below in the same
--    transaction as the ledger row.
--
-- 3. mf_transaction_detail. One row per promoted fund transaction: the statement's own
--    facts. ``cash_amount`` is the authoritative rupees moved and is the ONLY column the
--    engine reads (app.db.repository.load_transactions joins it onto Transaction); the
--    rest is provenance and the charge breakdown. Append-only, like the ledger it
--    describes -- a corrected statement is a reversal, never an edit.
--
-- FOLIOS ARE ACCOUNTS. One ``account`` per (investor, AMC-scoped folio), broker
-- 'MF_FOLIO', client_id the hashed folio key from app.db.cas_folios -- never the printed
-- folio number. Per-account FIFO then keeps lots and tax per folio, which the plan
-- requires, without an engine change.

ALTER TYPE txn_source ADD VALUE IF NOT EXISTS 'RTA_STATEMENT';

ALTER TABLE import_row DROP CONSTRAINT IF EXISTS import_row_segment_check;
ALTER TABLE import_row
    ADD CONSTRAINT import_row_segment_check CHECK (segment IN ('EQ', 'FNO', 'MF'));

COMMENT ON COLUMN import_row.segment IS
    'EQ: payload.transaction, promoted into transaction. FNO: payload.fno_trade, promoted '
    'into fno_trade and never into transaction (rule 21(b)). MF: payload.transaction plus '
    'payload.mf_detail, promoted into transaction and mf_transaction_detail together.';

CREATE UNIQUE INDEX IF NOT EXISTS account_mf_folio_uniq
    ON account (investor_id, client_id) WHERE broker = 'MF_FOLIO';

CREATE TABLE mf_transaction_detail (
    transaction_id   uuid PRIMARY KEY REFERENCES transaction (id) ON DELETE CASCADE,
    investor_id      uuid NOT NULL REFERENCES investor (id) ON DELETE CASCADE,
    folio_key        text NOT NULL,
    registrar        text NOT NULL,
    statement_kind   text NOT NULL,
    printed_amount   numeric(20, 6) NOT NULL,
    cash_amount      numeric(20, 6) NOT NULL CHECK (cash_amount >= 0),
    stamp_duty       numeric(20, 6),
    stt              numeric(20, 6),
    -- NULL is UNKNOWN, not zero (same rule as transaction.tds_amount).
    tds              numeric(20, 6),
    source_sequence  integer NOT NULL,
    batch_id         uuid REFERENCES import_batch (id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX mf_transaction_detail_investor_idx ON mf_transaction_detail (investor_id);

COMMENT ON TABLE mf_transaction_detail IS
    'A registrar statement''s own facts for one fund ledger row. cash_amount is the '
    'authoritative rupees moved (purchase: printed amount + stamp duty; redemption: printed '
    'net amount) and overrides units x price in every cash reader. Append-only.';

CREATE OR REPLACE FUNCTION mf_transaction_detail_append_only() RETURNS trigger AS $$
BEGIN
    IF current_setting('app.erasure_authorised', true) = 'on' THEN
        RETURN COALESCE(OLD, NEW);
    END IF;
    RAISE EXCEPTION
        'mf_transaction_detail is append-only (attempted %). Corrections are new reversing '
        'rows; erasure must set app.erasure_authorised.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER mf_transaction_detail_no_update_or_delete
    BEFORE UPDATE OR DELETE ON mf_transaction_detail
    FOR EACH ROW EXECUTE FUNCTION mf_transaction_detail_append_only();

SELECT attach_analytics_input_trigger('public.mf_transaction_detail'::regclass);
