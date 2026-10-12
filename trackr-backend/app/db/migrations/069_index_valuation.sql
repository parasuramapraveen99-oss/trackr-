-- Published index characteristics, separate from total-return index levels.
-- Reference observations only; no derived portfolio totals are persisted.
CREATE TABLE index_valuation (
    index_name text NOT NULL,
    valuation_date date NOT NULL,
    pe numeric(20,6),
    pb numeric(20,6),
    dividend_yield numeric(20,6), -- fraction, not publisher's percent
    source_url text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (index_name, valuation_date)
);
