-- 028_price_waiver_basis.sql -- WHY a price-evidence waiver was granted, structured.
--
-- WHY THIS EXISTS. 025 recorded only a free-text `price_waiver_reason`, which made the
-- two very different grounds for a waiver indistinguishable after the fact:
--
--   ABSENCE_CONFIRMED   the archive ANSWERED and did not carry the security. Permanent:
--                       a retired or never-listed identifier no future fetch produces.
--   BELOW_ARCHIVE_FLOOR the ex_date precedes app.prices.pipe.NSE_BHAVCOPY_EARLIEST, so
--                       no fetch is ever ATTEMPTED and an absence can never be recorded.
--
-- Only the first is permanently true. The second is true ONLY RELATIVE TO A CONSTANT
-- THAT IS DESIGNED TO MOVE -- that floor is this system's verified floor, not the
-- archives' real limit, and lowering it is explicitly a supported follow-up. Lower it and
-- every BELOW_ARCHIVE_FLOOR waiver keeps asserting "unobtainable" about data that has
-- become fetchable, with nothing able to notice. A waiver silently outliving its own
-- premise is exactly the failure docs/ARCHITECTURE_INVARIANTS.md #5 exists to prevent.
--
-- So the basis is recorded, and for the floor case the floor IN EFFECT AT THE TIME is
-- recorded with it. app.db.corporate_actions.stale_price_waivers compares the two and
-- surfaces any waiver whose premise has expired; the pending-row retry re-examines them
-- rather than leaving them standing.
ALTER TABLE corporate_action
    ADD COLUMN price_waiver_basis text,
    ADD COLUMN price_waiver_floor date,
    ADD CONSTRAINT corporate_action_waiver_basis_known CHECK (
        price_waiver_basis IS NULL
        OR price_waiver_basis IN ('ABSENCE_CONFIRMED', 'BELOW_ARCHIVE_FLOOR')
    ),
    -- A floor-based waiver without the floor it rested on cannot be re-examined later,
    -- which would defeat the entire point of this migration.
    ADD CONSTRAINT corporate_action_waiver_floor_recorded CHECK (
        price_waiver_basis IS DISTINCT FROM 'BELOW_ARCHIVE_FLOOR'
        OR price_waiver_floor IS NOT NULL
    );

COMMENT ON COLUMN corporate_action.price_waiver_basis IS
    'Which finding justified waiving price evidence. ABSENCE_CONFIRMED is permanent; '
    'BELOW_ARCHIVE_FLOOR is true only relative to price_waiver_floor and must be '
    're-examined whenever that floor is lowered.';

COMMENT ON COLUMN corporate_action.price_waiver_floor IS
    'The NSE_BHAVCOPY_EARLIEST value in effect when a BELOW_ARCHIVE_FLOOR waiver was '
    'granted. If the floor later drops to or below this row''s ex_date, the waiver''s '
    'premise has expired -- see app.db.corporate_actions.stale_price_waivers.';
