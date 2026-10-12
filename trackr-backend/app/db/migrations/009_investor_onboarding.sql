-- 009_investor_onboarding.sql -- adviser-onboarding date, for compute_period_xirr's
-- start_date (app/engine/xirr.py) and the "Period XIRR" line in
-- docs/FEATURE_BACKLOG.md's Phase 1 table, distinct from fy_xirr (always 1 April).
--
-- WHY ON investor, NOT account. account.is_advised already scopes WHICH TRADES count
-- toward advisory performance. GET /investors/{investor_id}/performance aggregates
-- across ALL of an investor's accounts; there is no per-account performance endpoint,
-- and a client routinely has more than one account (docs/DATA_MODEL.md). A per-account
-- onboarding_date would leave no spec-sanctioned way to pick whose date backs the
-- aggregate metric. "Adviser onboarding" is when the relationship with the CLIENT
-- began -- a second account joining the mandate later must not reset it.
ALTER TABLE investor ADD COLUMN onboarding_date date;

COMMENT ON COLUMN investor.onboarding_date IS
    'When the advisory relationship began. NULL for most investors today -- the metric '
    'is simply omitted (app.routers.analytical._window_xirr), the same honest-omission '
    'pattern fy_xirr uses for a missing 1-April price. Never defaulted to created_at or '
    'any other stand-in.';

ALTER TABLE investor ADD CONSTRAINT investor_onboarding_date_not_in_the_future
    CHECK (onboarding_date IS NULL OR onboarding_date <= CURRENT_DATE);
