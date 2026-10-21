-- Computed advisor analytics responses, reproducible from the ledger and reference data.
--
-- WHY A CACHE EXISTS (CLAUDE.md: "a cache is permitted only once profiling proves it
-- necessary"). Measured 21-10-2026 on the e2e scratch portfolio (49 ISINs): 4-6 s per
-- request, ~65% replaying the ledger per snapshot day for mixed company/fund books, plus
-- O(holdings^2) Decimal correlation/covariance (40 holdings: 2.4 s, 60: 5.5 s).
--
-- NEVER A SOURCE OF TRUTH. A row is served only when `data_version` equals the current
-- write-counter fingerprint of every table the report reads and the code's method
-- version (app/db/analytics_cache.py). Any committed write to any of those tables changes
-- the fingerprint, so a stale row is recomputed, never shown. Deleting every row is
-- always safe: the next request recomputes.
CREATE TABLE analytics_report_cache (
    investor_id     uuid NOT NULL REFERENCES investor (id) ON DELETE CASCADE,
    as_of           date NOT NULL,
    advised_only    boolean NOT NULL,
    benchmark       text NOT NULL,
    universe        text NOT NULL,
    method_version  text NOT NULL,
    data_version    text NOT NULL,
    report          jsonb NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    compute_ms      integer NOT NULL CHECK (compute_ms >= 0),
    PRIMARY KEY (investor_id, as_of, advised_only, benchmark, universe)
);

CREATE INDEX analytics_report_cache_as_of_idx ON analytics_report_cache (as_of);

COMMENT ON TABLE analytics_report_cache IS
    'Derived advisor analytics responses. Served only on an exact data_version match; '
    'safe to truncate at any time.';
