-- 032_analytics_and_universe.sql -- the reference data the analytics module reads.
--
-- Three tables, none of which holds a derived portfolio number. Holdings stay computed on
-- read; nothing here caches a valuation. What these store is REFERENCE data about
-- securities -- the same category as `security.sector` and `security.market_cap_bucket`,
-- which are already loaded from NSE and AMFI respectively.

-- ---------------------------------------------------------------------------------------
-- 1. security_symbol_change
--
-- A trading symbol moving is a corporate-action signal, and until now this system had
-- nowhere to record it. `security.symbol` is written once at import (app/db/imports.py)
-- and never refreshed, which is a live bug: INE155A01022 was Tata Motors Ltd / TATAMOTORS
-- and is now Tata Motors Passenger Vehicles Ltd / TMPV after the Oct-2026 CV demerger.
-- A yfinance lookup on the stale symbol 404s today, and once NSE reassigns TATAMOTORS to
-- the demerged CV entity the same lookup returns THE WRONG COMPANY, silently.
--
-- This is NOT security_alias (027). That table maps ISIN -> ISIN when an identifier is
-- superseded. This is a symbol change on an UNCHANGED ISIN -- a different fact, which is
-- why it gets its own table rather than being forced into that one. The ISIN never moves;
-- that is the whole point of keying on it.
CREATE TABLE security_symbol_change (
    isin        text NOT NULL REFERENCES security (isin),
    -- NULL when the security had no symbol recorded before (an ISIN created by the
    -- ledger-side INSERT ... ON CONFLICT DO NOTHING paths, which set isin only).
    old_symbol  text,
    new_symbol  text NOT NULL,
    -- The bhavcopy date the change was OBSERVED on, not the date the exchange made it.
    -- We learn about it when a file disagrees with what we hold; claiming to know the
    -- effective date would be inventing a fact the feed does not carry.
    observed_on date NOT NULL,
    PRIMARY KEY (isin, observed_on)
);
COMMENT ON TABLE security_symbol_change IS
    'Audit trail of trading-symbol changes observed in the bhavcopy. A symbol move is a '
    'corporate-action signal; the ISIN is unchanged (see security_alias for ISIN changes).';

-- ---------------------------------------------------------------------------------------
-- 2. universe_member
--
-- The cross-sectional ranking universe for factor percentiles. A factor score is a
-- PERCENTILE, and a percentile without a stated population is meaningless -- "momentum
-- 66/100" says nothing until you know 66th out of what. Ranking a portfolio against only
-- its own holdings is the failure mode this exists to prevent.
--
-- Members are priced from the bhavcopy we already download every day (it carries every
-- listed scrip; the pipe currently discards all but held ISINs), so widening the universe
-- costs no extra fetch -- only rows.
CREATE TABLE universe_member (
    isin       text PRIMARY KEY REFERENCES security (isin),
    -- Named, because there will be more than one: a micro-cap book ranked against NIFTY100
    -- is as misleading as no universe at all. The name travels with every score to the UI.
    universe   text NOT NULL,
    added_on   date NOT NULL,
    -- NULL = current member. Kept rather than deleted so a historical score stays
    -- explicable: index constituents change, and a rank computed last quarter was computed
    -- against last quarter's population.
    removed_on date
);
CREATE INDEX universe_member_current_idx ON universe_member (universe) WHERE removed_on IS NULL;

-- ---------------------------------------------------------------------------------------
-- 3. security_fundamentals
--
-- Vendor fundamentals, as-of dated. Keyed (isin, as_of) rather than one-row-per-ISIN
-- because a vendor RESTATES: a ratio that changes is either a new filing or a correction,
-- and collapsing the two loses the ability to tell them apart. Same shape as `price`,
-- for the same reason.
--
-- EVERY ratio column is nullable and a NULL means UNKNOWN, never zero -- the same
-- convention transaction.tds_amount documents. This matters more here than anywhere else
-- in the schema: the upstream feed returns null for returnOnEquity on most Indian scrips
-- (81% of a 24-holding test book by weight), and a zero-filled ROE would silently drag
-- every quality score and portfolio aggregate toward the floor.
CREATE TABLE security_fundamentals (
    isin             text NOT NULL REFERENCES security (isin),
    -- The date WE observed these values, not the fiscal period they describe. See
    -- fiscal_period below for that.
    as_of            date NOT NULL,

    pe               numeric(20,6),
    pb               numeric(20,6),
    roe              numeric(20,6),
    net_margin       numeric(20,6),
    debt_to_equity   numeric(20,6),
    interest_cover   numeric(20,6),
    dividend_yield   numeric(20,6),
    sales_growth_1y  numeric(20,6),
    profit_growth_1y numeric(20,6),
    market_cap       numeric(20,6),

    -- Provenance, per field, for the two fields that need it. "The vendor says ROE is 30%"
    -- and "we computed 30% from the FY26 annual statements because the vendor returned
    -- null" are DIFFERENT CLAIMS with different failure modes, and the UI must be able to
    -- say which one it is showing. 'vendor' | 'derived_statements'.
    roe_basis        text CHECK (roe_basis IN ('vendor', 'derived_statements')),
    -- The statement period any derived figure came from. NULL when nothing was derived.
    fiscal_period    date,

    source           text NOT NULL,
    fetched_at       timestamptz NOT NULL,
    PRIMARY KEY (isin, as_of)
);
CREATE INDEX security_fundamentals_isin_asof_desc_idx
    ON security_fundamentals (isin, as_of DESC);
COMMENT ON COLUMN security_fundamentals.roe_basis IS
    'vendor | derived_statements. A NULL ratio means UNKNOWN, never zero.';
