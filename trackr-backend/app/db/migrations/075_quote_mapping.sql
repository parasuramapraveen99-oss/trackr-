-- 075_quote_mapping.sql -- which provider ticker is THIS ISIN, and has that been checked.
--
-- An indicative (intraday) quote is fetched by ticker, and a ticker is a symbol: symbols
-- are unique within an exchange, not across them. Guessing the exchange put a BSE-only
-- holding (Kalyani Cast-Tech, INE0N6U01018) onto NSE, where KALYANI is a different company,
-- and valued it at a seventh of its price (2026-10-10). So the ticker is read from the
-- exchange's own bhavcopy row for the ISIN (seen_on), and checked every evening against
-- that day's official close (app.prices.quote_check): VERIFIED, or BLOCKED and never used.
--
-- Reference data: rebuilt from bhavcopy, read only by app.prices.live. No ledger table and
-- no valuation of record depends on it -- an ISIN without a usable mapping is simply
-- valued at its official close.
CREATE TABLE quote_mapping (
    isin            text PRIMARY KEY REFERENCES security (isin),
    exchange        text NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    symbol          text NOT NULL,
    ticker          text NOT NULL,
    seen_on         date NOT NULL,
    status          text NOT NULL DEFAULT 'UNVERIFIED'
                    CHECK (status IN ('UNVERIFIED', 'VERIFIED', 'BLOCKED')),
    checked_on      date,
    provider_close  numeric(20,6),
    official_close  numeric(20,6),
    note            text
);

-- The trading day a cached quote belongs to (the provider bar's own date). A quote is
-- indicative for TODAY only when this is today: on a weekend or holiday the provider's
-- newest bar is the last session's, whose official close is already the price.
ALTER TABLE live_price ADD COLUMN session_date date;
