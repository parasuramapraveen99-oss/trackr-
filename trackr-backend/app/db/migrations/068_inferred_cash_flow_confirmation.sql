-- 068_inferred_cash_flow_confirmation.sql -- the investor's confirmation that a cash flow
-- the pipeline inferred (an AUTO_RESOLVED rights or IPO allotment) is theirs.
--
-- Confirm only. There is no reject: a rejected inference would leave sells in the ledger
-- with no shares behind them, which is a ledger problem, not a confirmation state. A row
-- here only records that the investor has looked at the inference. It never changes a
-- transaction or any figure, so the ledger stays append-only and the returns are not
-- affected by confirming.
--
-- Keyed by the transaction's own identity (ISIN, date, type, source reference), so a
-- confirmation survives re-reads and cannot attach to a different inferred row.

CREATE TABLE inferred_cash_flow_confirmation (
    investor_id  uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    isin         text NOT NULL,
    txn_date     date NOT NULL,
    txn_type     text NOT NULL,
    source_ref   text NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, isin, txn_date, txn_type, source_ref)
);
