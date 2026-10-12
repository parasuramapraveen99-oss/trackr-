-- 063_fno_reference.sql -- the equity F&O contract master and its daily prices.
--
-- docs/FNO_PLAN.md Piece 2, docs/DATA_MODEL.md "Equity F&O", docs/ARCHITECTURE_INVARIANTS.md
-- rule 21. Reference data only, written by the F&O bhavcopy pipe
-- (app.prices.fno_sources -> app.db.fno_reference). No investor data lives here; the
-- append-only fno_trade ledger is migration 064.
--
-- WHY A CONTRACT TABLE AND NOT security. A derivative has no ISIN (the bhavcopy ISIN column
-- is empty for every F&O row), and security is keyed by ISIN. A contract is keyed by its
-- economic identity (exchange, underlying, instrument_type, expiry, strike, option_type).
-- exchange_token (FinInstrmId) is stored but is NOT the key: tokens are re-used.
--
-- NULLS ARE EQUAL IN THE IDENTITY. A future has no strike and no option type; a plain
-- UNIQUE would treat every NULL as distinct and let the same future be inserted twice. The
-- unique index therefore COALESCEs both to sentinels that a real option can never carry
-- (strike > 0 and option_type IN ('CE','PE') are enforced below). An expression index
-- rather than NULLS NOT DISTINCT so it does not depend on the server being PG15+.
--
-- Additive and re-runnable.

CREATE TABLE IF NOT EXISTS fno_contract (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange        text NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    instrument_type text NOT NULL CHECK (instrument_type IN ('IDF', 'IDO', 'STF', 'STO')),
    underlying      text NOT NULL CHECK (underlying <> ''),
    expiry          date NOT NULL,
    strike          numeric(20,6),
    option_type     text CHECK (option_type IN ('CE', 'PE')),
    -- Latest NewBrdLotQty seen. NULL when the contract is known only from the pre-UDiFF
    -- NSE archive, which carries no lot size; never inferred from value / contracts.
    lot_size        integer CHECK (lot_size > 0),
    exchange_token  text,
    first_seen      date NOT NULL,
    last_seen       date NOT NULL,
    CHECK (first_seen <= last_seen),
    -- A future has neither strike nor type; an option has both.
    CHECK (
        (instrument_type IN ('IDF', 'STF') AND strike IS NULL AND option_type IS NULL)
        OR (instrument_type IN ('IDO', 'STO') AND strike > 0 AND option_type IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS fno_contract_identity_idx ON fno_contract (
    exchange, underlying, instrument_type, expiry, COALESCE(strike, -1), COALESCE(option_type, '')
);

COMMENT ON TABLE fno_contract IS
    'Equity F&O contract master from the NSE/BSE F&O bhavcopy. Keyed by contract identity, '
    'never by display symbol or exchange token. A contract listed on NSE and on BSE is two '
    'rows. docs/ARCHITECTURE_INVARIANTS.md rule 21(a).';

CREATE TABLE IF NOT EXISTS fno_price (
    contract_id      uuid NOT NULL REFERENCES fno_contract(id),
    price_date       date NOT NULL,
    -- ClsPric / SttlmPric / UndrlygPric. NULL when the file carries none (blank or 0):
    -- a missing price is absent, never zero (CLAUDE.md rule 4).
    close            numeric(20,6) CHECK (close > 0),
    settlement_price numeric(20,6) CHECK (settlement_price > 0),
    underlying_close numeric(20,6) CHECK (underlying_close > 0),
    open_interest    numeric(20,6) CHECK (open_interest >= 0),
    -- That day's NewBrdLotQty. Lot sizes change; the per-day value is the record.
    lot_size         integer CHECK (lot_size > 0),
    source           text NOT NULL CHECK (source IN ('NSE_FO_BHAVCOPY', 'BSE_FO_BHAVCOPY')),
    -- Provenance: source_type, source_url, sha256 of the fetched file, publication_date,
    -- period and retrieved_at.
    receipt          jsonb NOT NULL,
    PRIMARY KEY (contract_id, price_date),
    CHECK (close IS NOT NULL OR settlement_price IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS fno_price_date_idx ON fno_price (price_date);

COMMENT ON TABLE fno_price IS
    'Daily F&O close / settlement / underlying close / open interest / lot size per '
    'contract, from the exchange F&O bhavcopy only (rule 21(c)). Upserted: a republished '
    'file carries its correction through, as for price.';
