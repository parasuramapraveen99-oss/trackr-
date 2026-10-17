-- Dated direct-fund balances from CAS. Source observations only: no ledger writes,
-- acquisition dates, cost basis, or calculated returns.
CREATE UNIQUE INDEX import_batch_id_investor_idx ON import_batch(id, investor_id);
CREATE TABLE cas_fund_snapshot (
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    batch_id uuid NOT NULL,
    folio_key text NOT NULL,
    folio_label text NOT NULL,
    isin text NOT NULL REFERENCES security(isin),
    amc_name text NOT NULL,
    scheme_name text NOT NULL,
    snap_date date NOT NULL,
    quantity numeric(28,8) NOT NULL CHECK (quantity >= 0),
    nav numeric(28,8) NOT NULL CHECK (nav > 0),
    market_value numeric(20,6) NOT NULL CHECK (market_value >= 0),
    PRIMARY KEY (batch_id, folio_key, isin),
    FOREIGN KEY (batch_id, investor_id) REFERENCES import_batch(id, investor_id) ON DELETE CASCADE
);
CREATE INDEX cas_fund_snapshot_investor_date_idx
    ON cas_fund_snapshot(investor_id, snap_date);
COMMENT ON TABLE cas_fund_snapshot IS
    'CAS-reported direct fund balances, shown with their statement date and source. '
    'Not computed holdings, cost basis, cash flows, or an input to performance.';
COMMENT ON COLUMN cas_fund_snapshot.folio_key IS
    'Investor- and AMC-scoped SHA-256 identity for deduplication. Full folio numbers '
    'remain only in the private source document; API exposes a masked label.';
