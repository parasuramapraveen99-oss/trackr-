-- 059_price_day_closed.sql -- a weekday on which neither exchange published a bhavcopy.
--
-- WHY. The price-history backfill (app.prices.history) walks unsettled trading days
-- newest-first, a bounded number per run. An exchange holiday has no file, so nothing is
-- ever stored for it and it never becomes "settled" -- and a bounded newest-first walk
-- would then re-fetch the same holidays on every run and never reach older days.
--
-- WHY NOT price_absent. That table means "the archive answered and this security was not
-- in it", and app.db.prices.absent_on_or_before reads any such row as a CONFIRMED absence
-- -- a retired ISIN. Recording a holiday there would mark every holding retired from that
-- date on. A closed market is a fact about the DAY, not about any security, so it lives
-- here, and nothing that values a holding reads this table.
--
-- Written only when BOTH exchanges answered "nothing published" (no transport failure)
-- for a date at least a week old, so a late-published file or an outage cannot settle a
-- real trading day. Idempotent; never updated.
CREATE TABLE IF NOT EXISTS price_day_closed (
    price_date  date PRIMARY KEY,
    observed_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE price_day_closed IS
    'Weekdays on which neither NSE nor BSE published a bhavcopy (holidays). Read only by '
    'the price-history backfill to skip them; never by valuation.';
