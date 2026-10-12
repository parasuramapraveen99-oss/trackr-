-- 001_core.sql -- the six tables of the MVP. Source of truth: docs/DATA_MODEL.md.
--
-- Money and quantity are NUMERIC(20,6). Never a binary numeric type: losing paise here
-- makes reconciliation fail in ways that take days to find.

CREATE TYPE asset_class AS ENUM (
    'EQ','MF_EQUITY','MF_DEBT','SIF','BND','NCD','SGB','FD','BNK','CD','LN','PPF_EPF',
    'NPS_ULIP','INS','PO','REIT','INVIT','GLD','SLV','JWL','PR','ART','AIF','PE','PMS',
    'US_EQ','INTL_PE','STOCK_FNO','OTHER_FNO','CRYPTO'
);
-- All 30 seeded so the enum never needs a migration. Phase 1 BUILDS EXACTLY ONE: EQ.
-- The list is an extension point, not a to-do list.

CREATE TYPE txn_type AS ENUM (
    'BUY','SELL','BONUS','SPLIT_ADJ','DEMERGER_IN','TRANSFER_IN','TRANSFER_OUT',
    'IPO_ALLOT','BUYBACK','DIVIDEND'
);

CREATE TYPE txn_source AS ENUM (
    'MANUAL','CSV','TRADEBOOK','CONTRACT_NOTE','EMAIL','CORP_ACTION'
);

CREATE TABLE investor (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text NOT NULL UNIQUE,
    display_name    text NOT NULL,
    family_group_id uuid,
    created_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN investor.code IS
    'Use this in files and logs, NEVER the name or PAN.';
COMMENT ON COLUMN investor.family_group_id IS
    'HNI portfolios are family units: self, spouse, HUF, kids. Modelled now because '
    'retrofitting it is a migration.';
-- NO PAN COLUMN HERE, deliberately. PAN lives encrypted in investor_secret (002).

CREATE TABLE account (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    broker      text NOT NULL,
    dp_id       text,
    client_id   text,
    is_advised  boolean NOT NULL DEFAULT true,
    label       text
);
COMMENT ON COLUMN account.is_advised IS
    'False = the client''s own punting; exclude from advisory performance. Exists so a '
    'SEBI-registered adviser can report on the mandated account only, cleanly, without '
    'arguing later about which trades were theirs.';

CREATE TABLE security (
    isin        text PRIMARY KEY CHECK (isin ~ '^IN[A-Z0-9]{10}$'),
    symbol      text,
    name        text,
    exchange    text CHECK (exchange IN ('NSE','BSE','BOTH')),
    asset_class asset_class NOT NULL DEFAULT 'EQ',
    sector      text
);
COMMENT ON COLUMN security.symbol IS
    'Display only. Tickers get renamed and reused; the ISIN never moves.';

CREATE TABLE transaction (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    isin            text NOT NULL REFERENCES security(isin),
    txn_date        date NOT NULL,
    txn_type        txn_type NOT NULL,
    quantity        numeric(20,6) NOT NULL CHECK (quantity >= 0),
    price           numeric(20,6) NOT NULL DEFAULT 0,
    charges         numeric(20,6) NOT NULL DEFAULT 0,
    dividend_amount numeric(20,6) NOT NULL DEFAULT 0,
    source          txn_source NOT NULL,
    source_ref      text,
    dedupe_key      text NOT NULL,
    raw_payload     jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE transaction IS
    'APPEND-ONLY. No UPDATE, no DELETE, ever - enforced by trigger below, not by '
    'convention. A correction is a new reversing row. Everything else in the system is '
    'derived from this table; holdings are computed on read and never stored.';
COMMENT ON COLUMN transaction.quantity IS
    'ALWAYS POSITIVE. Direction comes from txn_type, never from a sign here.';
COMMENT ON COLUMN transaction.txn_date IS 'TRADE date, not settlement date.';

-- THE SINGLE MOST IMPORTANT LINE IN THE SCHEMA.
-- Email forwarding WILL deliver the same contract note twice. The database must be the
-- thing that refuses it, not application logic that someone later forgets to call.
CREATE UNIQUE INDEX transaction_dedupe_key_uniq ON transaction (dedupe_key);

CREATE INDEX transaction_account_isin_date_idx ON transaction (account_id, isin, txn_date);
CREATE INDEX transaction_date_idx ON transaction (txn_date);

-- Append-only, enforced. Application discipline is the kind of thing someone later
-- forgets; a trigger is not.
--
-- DPDP erasure must still be able to delete, so there is exactly one escape hatch:
-- app.erasure_authorised, set LOCAL inside the erasure transaction. The normal path
-- cannot delete; the erasure path has to announce itself, and that announcement is
-- audited. See db/privacy.py.
CREATE FUNCTION transaction_append_only() RETURNS trigger AS $$
BEGIN
    IF current_setting('app.erasure_authorised', true) = 'on' THEN
        RETURN COALESCE(OLD, NEW);
    END IF;
    RAISE EXCEPTION
        'transaction is append-only (attempted %). Corrections are new reversing rows; '
        'erasure must set app.erasure_authorised.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER transaction_no_update_or_delete
    BEFORE UPDATE OR DELETE ON transaction
    FOR EACH ROW EXECUTE FUNCTION transaction_append_only();

CREATE TABLE price (
    isin       text NOT NULL REFERENCES security(isin),
    price_date date NOT NULL,
    close      numeric(20,6) NOT NULL,
    source     text NOT NULL,
    PRIMARY KEY (isin, price_date)
);
COMMENT ON TABLE price IS
    'Public data on its own schedule, independent of client transactions. NSE wins on '
    'conflict (deeper liquidity). BOTH exchanges must be fetched: some BSE-only scrips '
    'have no NSE symbol at all, and an unpriced holding makes XIRR uncomputable.';

CREATE INDEX price_isin_date_desc_idx ON price (isin, price_date DESC);

CREATE TABLE holding_snapshot (
    account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    isin       text NOT NULL REFERENCES security(isin),
    snap_date  date NOT NULL,
    quantity   numeric(20,6) NOT NULL,
    source     text NOT NULL,
    PRIMARY KEY (account_id, isin, snap_date)
);
COMMENT ON TABLE holding_snapshot IS
    'CAS/broker-declared quantities, used to CHECK the engine. NOT a source of truth for '
    'display. A mismatch against compute_holdings() means exactly one of: a corporate '
    'action, an off-market transfer, a missed contract note, or a bug.';

-- There is deliberately no `holdings` table. Holdings are computed on read, every time,
-- from the transaction ledger. If one appears in a later migration, the architecture has
-- been broken.
