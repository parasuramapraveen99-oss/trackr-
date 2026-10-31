-- 096_market_close.sql -- every bhavcopy row, kept, so an import never waits on a download.
--
-- WHY. `price` keeps only ISINs some ledger holds plus the ranking universe; every other
-- row of every bhavcopy was discarded. A stock nobody had held before therefore had no
-- history, and the first import holding it had to download every past day again. These
-- tables keep the whole merged file (NSE wins over BSE, exactly app.prices.pipe.merge),
-- so `price` can be filled from the database in one statement
-- (app.prices.market.hydrate_prices). Owner decision 2026-10-31, docs/INSTANT_IMPORT_PLAN.md.
--
-- `market_close` is shared public market data, NOT a list of securities Trackr+ knows:
-- it deliberately has no foreign key to `security`. The engine never reads it; it reads
-- `price`, as before. Nothing here is per-investor.

CREATE TABLE market_close_day (
    price_date    date PRIMARY KEY,
    -- Rows each exchange's file returned, e.g. {"NSE": 2840, "BSE": 4610}.
    fetched       jsonb NOT NULL,
    -- Exchange -> error text for any exchange that failed. Empty when both answered.
    failures      jsonb NOT NULL DEFAULT '{}'::jsonb,
    not_published text[] NOT NULL DEFAULT '{}',
    -- Both exchanges answered and rows came back: the only state in which a security's
    -- absence from the file may be recorded (the rule in app.prices.history).
    complete      boolean NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE market_close (
    isin       text NOT NULL CHECK (isin ~ '^IN[A-Z0-9]{10}$'),
    price_date date NOT NULL REFERENCES market_close_day(price_date),
    close      numeric(20,6) NOT NULL CHECK (close > 0),
    source     text NOT NULL,
    PRIMARY KEY (isin, price_date)
);

COMMENT ON TABLE market_close IS
    'Every merged bhavcopy close (all ISINs, NSE over BSE). Shared market data; the engine '
    'reads `price`, which is filled from here for ledger ISINs.';
COMMENT ON TABLE market_close_day IS
    'One row per stored bhavcopy day, recording which exchanges answered. `complete` '
    'gates absence recording.';
