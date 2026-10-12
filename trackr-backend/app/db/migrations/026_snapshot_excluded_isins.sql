-- 026_snapshot_excluded_isins.sql -- what a snapshot's totals do NOT include.
--
-- WHY THIS EXISTS. compute_snapshot used the strict compute_holdings, so a single ISIN
-- whose own ledger segment does not reconcile (a NegativeHoldingError -- missing buy
-- history, not a short position) made the WHOLE portfolio unsnapshottable, for every
-- date, permanently. One broken holding out of twenty-four blanked the entire value and
-- benchmark chart, which is the worst of both worlds: no number at all, and no
-- explanation either.
--
-- The rule that shape was protecting is still right, and is NOT relaxed here: a total
-- computed while silently dropping a position is a confidently wrong number
-- (app.engine.holdings.compute_holdings_partitioned's own docstring). The fix is the
-- word SILENTLY. A snapshot may now exclude a non-reconciling ISIN, but only by
-- recording exactly which ones it excluded, so every reader can caveat the figure
-- instead of trusting it blindly -- and so the exclusion disappears from the record by
-- itself once the ledger gap is closed and the next rebuild replays cleanly.
--
-- This also makes stored snapshots CONSISTENT with the live view, which already works
-- this way: app.routers.holdings._portfolio partitions the same broken ISINs out of the
-- dashboard's own totals. Before this column, the dashboard's "current value" and the
-- chart's last point disagreed about which holdings exist -- one excluded them, the
-- other refused to plot at all.
ALTER TABLE portfolio_snapshot
    ADD COLUMN excluded_isins text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN portfolio_snapshot.excluded_isins IS
    'ISINs left out of this row''s totals because their ledger segment did not '
    'reconcile on this date (app.engine.holdings.compute_holdings_partitioned). Empty '
    'for a clean replay, which is the normal case. Never NULL: "no exclusions" and '
    '"exclusions unknown" must not look alike.';
