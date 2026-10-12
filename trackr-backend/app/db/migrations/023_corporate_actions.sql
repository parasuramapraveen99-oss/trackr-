-- 023_corporate_actions.sql -- verified corporate-action reference data.
--
-- docs/ENGINE_SPEC.md section 7 notices a quantity changed and gives a human a text
-- hypothesis ("ratio ~2 -> likely 1:1 bonus or 1:2 split"); nothing before this turned
-- that hypothesis into the actual ledger transaction. This table is a maintained,
-- sourced reference of verified corporate actions (bonus/split/demerger) that
-- app.engine.corporate_actions matches against a Discrepancy to propose the
-- correct BONUS/SPLIT_ADJ/DEMERGER_IN row -- staged for a human to confirm through the
-- same import_batch/import_row flow every file adapter already uses, never written to
-- the ledger directly. See app.db.corporate_actions.stage_corporate_actions().
--
-- NO RATIO STRINGS. Indian sources disagree on notation order for the same event (the
-- same Kama Holdings bonus is printed "4:1" by one provider and "1:4" by another, a 5x
-- difference with no error raised if you guess wrong), so each row stores the primitive
-- facts printed on the exchange notice and derives the multiplier itself -- see
-- app.engine.corporate_actions.CorporateAction.multiplier().

-- CONSOLIDATION (a reverse split -- face value raised, share count reduced) is
-- deliberately absent: app.engine.types.TxnType's free-share group (BONUS, SPLIT_ADJ,
-- DEMERGER_IN) is additive only ("quantity up, pool UNCHANGED" -- see that module's
-- docstring), and nothing in holdings.py can remove quantity while leaving the cost
-- pool untouched. Representing a consolidation needs a new ledger mechanism, which is
-- a separate engine/schema decision -- the same reasoning that excludes MERGER here.
CREATE TYPE corp_action_type AS ENUM ('BONUS', 'SPLIT', 'DEMERGER');
CREATE TYPE corp_action_verification AS ENUM ('DRAFT', 'VERIFIED', 'DISPUTED');

CREATE TABLE corporate_action (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    isin               text NOT NULL REFERENCES security(isin),
    action_type        corp_action_type NOT NULL,
    ex_date            date NOT NULL,

    -- BONUS: "N new shares for every M held".
    bonus_new          integer,
    bonus_for          integer,

    -- SPLIT: face value before/after -- printed on every exchange notice and not
    -- order-ambiguous, unlike a ratio string. face_value_before must exceed
    -- face_value_after (a forward split only -- see the CONSOLIDATION note above).
    face_value_before  numeric(20,6),
    face_value_after   numeric(20,6),

    -- DEMERGER: quantity leg only. The cost-ratio leg stays the existing, separate,
    -- manual step (app.engine.demerger.demerger_cost_split) -- it comes from the
    -- company's scheme document, which no feed carries, per docs/TAX_ENGINE.md
    -- "Corporate action tax nuances".
    resulting_isin     text REFERENCES security(isin),
    demerger_new       integer,
    demerger_for       integer,

    -- Price corroboration. A bonus or split mechanically divides the price by the
    -- multiplier; app.engine.corporate_actions.check_price_consistency rejects a record
    -- whose recorded ratio disagrees with the observed cum/ex price move. This is the
    -- only automatic defence against an inverted ratio, which is why these are
    -- mandatory for BONUS/SPLIT, not optional.
    price_cum          numeric(20,6),
    price_ex           numeric(20,6),

    source             text NOT NULL,
    source_url         text NOT NULL,
    verification       corp_action_verification NOT NULL DEFAULT 'DRAFT',
    notes              text,
    created_at         timestamptz NOT NULL DEFAULT now(),

    UNIQUE (isin, action_type, ex_date)
);

COMMENT ON TABLE corporate_action IS
    'A verified, sourced reference record for one bonus/split/demerger. '
    'Never written to by an adapter and never applied to the ledger directly -- '
    'app.engine.corporate_actions.screen() quarantines anything that fails its guards, '
    'and app.db.corporate_actions.stage_corporate_actions() only ever proposes a staged '
    'transaction through the same review-then-promote flow (import_batch/import_row) '
    'every file adapter uses.';

COMMENT ON COLUMN corporate_action.verification IS
    'DRAFT until a human has checked this row against the exchange notice. Only '
    'VERIFIED rows are eligible to match a Discrepancy -- see '
    'app.engine.corporate_actions.screen().';

CREATE INDEX corporate_action_isin_idx ON corporate_action (isin, verification);
