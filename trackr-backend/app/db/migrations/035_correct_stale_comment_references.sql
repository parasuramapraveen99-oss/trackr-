-- 035_correct_stale_comment_references.sql -- re-issue one column comment whose text
-- names a function that no longer exists.
--
-- WHY A MIGRATION AND NOT JUST AN EDIT. 009_investor_onboarding.sql's COMMENT ON names a
-- router helper `_period_xirr`. It was renamed to `_window_xirr` and the old name now
-- exists nowhere in the tree -- a reader who greps for the name the
-- database itself gave them finds nothing. Correcting 009 on disk fixes a database built
-- from scratch tomorrow and does nothing for one migrated months ago, because
-- app.db.migrate records applied VERSIONS and never re-runs a file it has already seen
-- (app.db.migrate.applied_versions/migrate). Leaving those two to disagree is the same
-- file-versus-database divergence check_drift was written to catch, so the comment is
-- re-issued here instead.
--
-- Comments only. No DDL, no data, and the resulting schema is byte-identical.
COMMENT ON COLUMN investor.onboarding_date IS
    'When the advisory relationship began. NULL for most investors today -- the metric '
    'is simply omitted (app.routers.analytical._window_xirr), the same honest-omission '
    'pattern compute_fy_xirr uses for a missing 1-April price. Never defaulted to '
    'created_at or any other stand-in.';
