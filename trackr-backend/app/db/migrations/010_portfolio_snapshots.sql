-- 010_portfolio_snapshots.sql -- persisted compute_snapshot() output (docs/ENGINE_SPEC.md
-- section 8). app/routers/analytical.py's fy-XIRR path (app.engine.xirr.compute_fy_xirr)
-- and its trailing-window path (_window_xirr) currently call compute_snapshot() live, on
-- every /performance request, against historical prices, just to get an honest
-- opening_value for 1-April / onboarding_date. This table is that result,
-- cached. It is a CACHE OF A DERIVATION, never a source of truth: every row must always be
-- reproducible by calling compute_snapshot() again against the same ledger and prices.
--
-- Written ONLY by the rebuild job (app.routers.analytical._run_snapshots, via
-- POST /ops/snapshots/run). No UPDATE of a stored total by hand, ever -- a correction is a
-- rerun of the job for that date, same as a republished bhavcopy corrects app.db.prices.
--
-- PORTFOLIO-LEVEL TOTALS ONLY. No per-ISIN quantities. CLAUDE.md's "a holdings table
-- written by anything but a rebuild" ban is about a table standing in for
-- compute_holdings()'s per-position output; this table cannot answer "what does the
-- investor hold" and was never meant to.
CREATE TABLE portfolio_snapshot (
    investor_id      uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    as_of            date NOT NULL,
    -- Part of the grain, not a filter applied after the fact. performance() calls
    -- load_transactions(..., advised_only=advised_only) BEFORE computing anything, so an
    -- investor with a mix of advised/unadvised accounts has two genuinely different
    -- ledgers and therefore two genuinely different snapshots. Storing only the
    -- unfiltered view would silently hand a SEBI-registered adviser the wrong opening
    -- value on the one query parameter that exists specifically for their reporting.
    advised_only     boolean NOT NULL DEFAULT false,
    invested         numeric(20,6) NOT NULL,
    market_value     numeric(20,6) NOT NULL,
    unrealised       numeric(20,6) NOT NULL,
    realised_to_date numeric(20,6) NOT NULL,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, as_of, advised_only)
);
COMMENT ON TABLE portfolio_snapshot IS
    'A cache of compute_snapshot(), never a source of truth (ENGINE_SPEC.md section 8). '
    'Always recomputable from the transaction ledger and the price table as they stood on '
    'as_of. Written exclusively by app.routers.analytical._run_snapshots.';
COMMENT ON COLUMN portfolio_snapshot.advised_only IS
    'Mirrors GET /investors/{id}/performance''s advised_only parameter, which restricts to '
    'account.is_advised accounts before computing anything -- a materially different '
    'ledger, so a materially different number, not just a display filter.';

-- Named portfolio_snapshot, not snapshot: holding_snapshot already exists (001_core.sql)
-- and is a different concept entirely -- CAS/broker-declared per-ISIN quantities used to
-- check the engine, not portfolio totals. Keeping the names visually distinct matters.

ALTER TABLE portfolio_snapshot ADD CONSTRAINT portfolio_snapshot_as_of_not_in_the_future
    CHECK (as_of <= CURRENT_DATE);

-- (investor_id, advised_only, as_of) rather than the PK's own column order: range reads
-- for one investor's one series ("chart the last 3 years") are the read pattern Session 2
-- needs, and the PK's (investor_id, as_of, advised_only) ordering does not serve that
-- efficiently once advised_only sits between investor_id and the date range.
CREATE INDEX portfolio_snapshot_range_idx
    ON portfolio_snapshot (investor_id, advised_only, as_of);
