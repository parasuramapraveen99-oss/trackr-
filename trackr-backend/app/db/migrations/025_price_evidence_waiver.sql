-- 025_price_evidence_waiver.sql -- an explicit, audited escape hatch for a BONUS/SPLIT
-- whose ex_date predates app.prices.pipe.NSE_BHAVCOPY_EARLIEST (2024-01-01).
--
-- WHY THIS EXISTS. app.engine.corporate_actions.check_price_consistency requires
-- price_cum/price_ex before a BONUS/SPLIT can auto-apply -- the only automatic defence
-- against an inverted ratio (docs/ENGINE_SPEC.md section 7). For an action whose ex_date
-- is before this system's bhavcopy archive begins, that evidence is not merely missing,
-- it is PERMANENTLY UNAVAILABLE to the automated pipe -- no future feed run or backfill
-- will ever populate it. Without this column, a real, well-sourced, multiply-corroborated
-- pre-2024 action (the KAMAHOLD/GENSOL case this was built for) sits in Exceptions
-- forever with no legitimate way out except silently editing price_cum/price_ex to values
-- nobody can verify, which is worse than the gap it would close.
--
-- A waiver is NOT a bypass of sourcing or verification -- app.db.corporate_actions
-- .waive_price_evidence enforces ex_date < NSE_BHAVCOPY_EARLIEST (refusing any action
-- price evidence *could* still corroborate) and requires an explicit human-authored
-- reason, audited the same way verify/dispute already are. Everything else in
-- app.engine.corporate_actions.screen() -- check_sourced, check_verified -- still applies
-- unchanged.
ALTER TABLE corporate_action
    ADD COLUMN price_waived boolean NOT NULL DEFAULT false,
    ADD COLUMN price_waiver_reason text;

COMMENT ON COLUMN corporate_action.price_waived IS
    'True only via app.db.corporate_actions.waive_price_evidence, which refuses any '
    'action whose ex_date is not before NSE_BHAVCOPY_EARLIEST -- price evidence that '
    'could still be obtained must be, never waived.';
