-- 024_live_price_table.sql -- a cache for intraday prices (app.prices.yfinance_live),
-- entirely separate from `price` (bhavcopy, the ledger's authoritative source).
--
-- docs/ENGINE_SPEC.md's live-pricing addendum: this table backs "today, before tonight's
-- bhavcopy settles" only. One row per ISIN, no history -- unlike `price`, which is keyed
-- (isin, price_date) because every date's close must stay reproducible forever, this is
-- overwritten in place because only the current value is ever meaningful. Never read by
-- anything outside app.prices.live / app.db.live_prices; never joined into a historical
-- as_of query.
CREATE TABLE live_price (
    isin        text PRIMARY KEY REFERENCES security (isin),
    last_price  numeric(20,6) NOT NULL,
    fetched_at  timestamptz NOT NULL
);
