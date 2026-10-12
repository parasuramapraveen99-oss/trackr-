-- Daily levels of a money-market rate index, used only as the risk-free return in
-- Sharpe and Sortino (app.analytics.risk). Kept out of index_level on purpose: that
-- table holds equity TOTAL-RETURN indices only (migration 011), and a rate index is not
-- a benchmark anyone can be compared against.
--
-- Source: NSE Indices' Nifty 1D Rate Index (overnight TREPS), niftyindices.com -- the
-- same publisher as the TRI levels and index P/E already stored. The risk-free rate for
-- a window is the index's own annualised growth between that window's first and last
-- dates: the return cash actually earned over exactly that period, never an assumption.
CREATE TABLE risk_free_level (
    index_name  text NOT NULL,
    level_date  date NOT NULL,
    close       numeric(20,6) NOT NULL CHECK (close > 0),
    source_url  text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (index_name, level_date)
);
