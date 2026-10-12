-- 022_price_absent.sql -- "we looked, and it wasn't there" is an answer worth keeping.
--
-- THE BUG THIS FIXES. A backfill skipped any date that already had price rows
-- (app.db.prices.price_dates_settled, then named price_dates_present). That treats one
-- question -- "have we fetched this day's bhavcopy?" -- as answered by a different one:
-- "does SOME security have a price on this day?". The two agree only while the set of
-- securities we care about never changes.
--
-- It changes every time somebody buys something. Buy a new stock today and every
-- historical date already has prices for the OTHER holdings, so every date is "present",
-- so bhavcopy is never re-fetched, so the new holding never acquires any history at all.
-- Observed live: GSMFOILS (bought 2026-08-20) and ORIRAIL (bought 2026-08-25) had exactly
-- two price rows each, both from the days the daily job happened to run after the
-- purchase. Every snapshot from the purchase date onward was then skipped for want of a
-- price, each one logging a full traceback, on every single ./start.sh.
--
-- WHY A TABLE AND NOT A CLEVERER QUERY. The absence of a price row is ambiguous between
-- "we never fetched that day" and "we fetched it and this scrip wasn't trading". Nothing
-- in the schema could tell those apart, and they need opposite responses -- fetch it, or
-- stop asking. This table records the second one, so the question becomes decidable.
--
-- Without it the alternative is unbounded: a security that is legitimately absent from a
-- day's bhavcopy (suspended, pre-listing, an SME scrip that simply did not trade) would
-- leave that date permanently unsettled and re-fetched on every run, forever.
--
-- NOT A CACHE, and nothing is derived from it. It is a log of what an external archive
-- said on a date, which is exactly the kind of fact that cannot be recomputed later --
-- the bhavcopy for 2024-03-11 is not going to change its mind. Holdings, valuations and
-- every figure shown to a user remain derived from the ledger as always (CLAUDE.md).

CREATE TABLE price_absent (
    isin       text NOT NULL REFERENCES security(isin),
    price_date date NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, price_date)
);

COMMENT ON TABLE price_absent IS
    'One row per (security, date) where the bhavcopy WAS fetched and did not contain '
    'that security. Together with `price`, this makes "do we already have an answer for '
    'this security on this date?" decidable - which is what stops a backfill either '
    'skipping a date it still needs (the bug) or re-fetching one forever (the naive fix).';

COMMENT ON COLUMN price_absent.recorded_at IS
    'When we established the absence. Diagnostic only: nothing reads it to make a '
    'decision, and re-recording an absence leaves the original timestamp alone.';

-- The backfill asks "which dates are settled", scanning by date across all securities.
CREATE INDEX price_absent_date_idx ON price_absent (price_date);
