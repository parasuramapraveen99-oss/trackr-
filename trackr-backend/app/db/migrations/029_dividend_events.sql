-- 029_dividend_events.sql -- DECLARED dividends as reference data, beside corporate_action.
--
-- WHY A SEPARATE TABLE, AND NOT `DIVIDEND` ADDED TO corp_action_type. Four reasons, the
-- first of which is disqualifying on its own.
--
-- 1. IT WOULD CREATE A SILENT AUTO-APPLY PATH TO THE LEDGER.
--    app.db.corporate_actions.apply_known_actions_for_account is allowed to skip the
--    review-then-promote flow every other ledger write goes through, and its docstring
--    states exactly why: "three independent checks, not one" -- VERIFIED, sourced, and a
--    recorded ratio corroborated by the observed cum/ex price move. But
--    app.engine.corporate_actions.check_price_consistency returns None on its FIRST LINE
--    for any action_type that is not BONUS or SPLIT -- it PASSES VACUOUSLY. And
--    check_sourced only tests that two strings are non-empty. So a DIVIDEND member of
--    that enum would inherit an auto-apply path whose only surviving check is "somebody
--    typed a URL". That is not a theoretical risk: the enum member alone is enough,
--    because apply_known_actions_for_account iterates whatever screen() returns.
-- 2. UNIQUE (isin, action_type, ex_date) CANNOT HOLD THE REAL DATA. An interim and a
--    special dividend declared with one ex-date are two events with two amounts; that
--    key admits one. See this table's own UNIQUE, which adds the kind.
-- 3. THERE IS NOWHERE TO PUT THE AMOUNT. corporate_action's money columns are price_cum
--    and price_ex, which are CORROBORATION, not payload. A dividend's amount per share,
--    record date and payment date have no home there.
-- 4. THAT TABLE'S STATED SCOPE IS MULTIPLIER EVENTS. Migration 023 excludes
--    CONSOLIDATION and MERGER precisely because they do not fit its additive quantity
--    model. A dividend does not touch quantity AT ALL -- further outside that scope, not
--    nearer it.
--
-- The precedent followed here is therefore security_alias (027), not corporate_action: a
-- separate table that REUSES the existing trust machinery, built because fusing two
-- different kinds of fact into one record would force an invention.
--
-- WHAT THIS TABLE IS NOT. It is not a ledger, not a cash record, and not evidence that
-- anybody was paid. A row here says "this company declared this amount per share with
-- this ex-date", which is a fact about the COMPANY and exists independently of any
-- investor. Entitlement (what a given holder should receive) is DERIVED on read from
-- this table and the ledger -- see app.engine.dividends -- and is never stored, for the
-- same reason holdings are never stored. Receipt (that cash actually arrived) cannot be
-- established from this table at all, and neither of the two feeds that populate it
-- carries a payment amount or tax withheld.
--
-- THE RULE THAT MATTERS MOST, restated here because this is where a later reader will
-- look for it: NOTHING IN THIS TABLE MAY BECOME A `DIVIDEND` LEDGER ROW WITHOUT
-- EVIDENCE OF RECEIPT. A declared amount times an eligible quantity is a PROJECTION. The
-- path from projection to ledger row runs through the existing import_batch/promote flow
-- with a human or a credit advice in it -- never automatically, however well corroborated
-- the declaration is. "Expected 10,000" and "received 10,000" are different claims.

-- INTERIM/FINAL/SPECIAL are what Indian notices actually distinguish. UNSPECIFIED is not
-- a fourth kind of dividend -- it means THIS SOURCE DID NOT SAY, which is the normal case
-- for yfinance (Ticker.dividends carries an ex-date and an amount, nothing else) and for
-- NSE subjects whose free text omits it. It is deliberately a real enum value rather than
-- NULL so it participates in the UNIQUE below; see app.db.dividend_events for the
-- matching rule that stops an UNSPECIFIED candidate from becoming a duplicate of an
-- already-classified event.
CREATE TYPE dividend_kind AS ENUM ('INTERIM', 'FINAL', 'SPECIAL', 'UNSPECIFIED');

CREATE TABLE dividend_event (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    isin              text NOT NULL REFERENCES security(isin),

    -- The eligibility cutoff. app.engine.holdings.quantity_held_before(.., ex_date)
    -- answers "what was held at the close before this", which is the correct reference
    -- point for a dividend exactly as it is for a bonus: holding at that close is what
    -- puts a holder on the register at the record date. NOT NULL because without it
    -- there is no cutoff and so nothing computable -- NSE sends "-" for a date not yet
    -- fixed, and such a notice must produce no row at all rather than a row with a
    -- guessed date.
    ex_date           date NOT NULL,

    -- Stored for display and reconciliation, NEVER computed from. The legal entitlement
    -- date is the record date, but the quantity a holder had ON it is not observable from
    -- a ledger keyed by trade date -- ex_date is. Recording both and computing from one
    -- is what keeps that distinction honest instead of quietly picking whichever is
    -- present.
    record_date       date,
    payment_date      date,

    amount_per_share  numeric(20,6) NOT NULL CHECK (amount_per_share > 0),
    kind              dividend_kind NOT NULL DEFAULT 'UNSPECIFIED',

    -- Provenance. Same columns, same names and same meanings as corporate_action, so
    -- this system has ONE standard of evidence rather than two.
    source            text NOT NULL,
    source_url        text NOT NULL,

    -- CROSS-SOURCE CORROBORATION -- this table's substitute for the price check, and the
    -- reason those two columns exist at all.
    --
    -- A bonus or split mechanically divides the price by its multiplier, so
    -- check_price_consistency can corroborate a RATIO against an independent observation
    -- (the actual close). A dividend has NO SUCH ANALOGUE, and this is arithmetic rather
    -- than policy: the existing guard accepts agreement between 0.70 and 1.40 of the
    -- recorded multiplier, while a dividend moves the price by roughly amount/price --
    -- for a typical Indian large-cap 0.3%-2%, inside one day's ordinary noise. No
    -- tightening of that band recovers a signal that is not there.
    --
    -- So corroboration here means A SECOND SOURCE READ THE SAME FACT, recorded below.
    -- NOT "therefore correct": see app.engine.dividend_events.check_cross_sourced, which
    -- documents the provenance relationship between the two feeds and why their agreement
    -- is weaker evidence than a price check, not equivalent to it.
    corroborated_by   text,
    corroborated_url  text,

    verification      corp_action_verification NOT NULL DEFAULT 'DRAFT',
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),

    -- One event per (security, ex-date, kind). The kind is IN the key because an interim
    -- and a special dividend genuinely can share an ex-date and are genuinely two
    -- events -- without it, a special declared alongside an interim is silently lost.
    -- The amount is deliberately NOT in the key: if it were, a corrected amount would
    -- insert a SECOND event and leave two contradictory claims about one fact with
    -- nothing able to choose between them at read time -- exactly the failure
    -- security_alias's single-row primary key was designed to prevent. A changed amount
    -- updates the row; a disagreement between sources moves it to DISPUTED.
    UNIQUE (isin, ex_date, kind),

    -- Date sanity, enforced in SQL as well as in Python so the invariant survives
    -- anything that writes here without going through app.db.dividend_events -- the same
    -- belt-and-braces reasoning security_alias's same-issuer CHECK is written with.
    -- These catch an INVERTED OR MISREAD PARSE (a payment date read as an ex-date), which
    -- is the realistic failure; they are not a precise statement of market mechanics.
    CONSTRAINT dividend_event_record_after_ex CHECK (record_date IS NULL OR record_date >= ex_date),
    CONSTRAINT dividend_event_payment_after_ex CHECK (payment_date IS NULL OR payment_date >= ex_date),
    -- A corroborating source needs a URL for the same reason the primary one does: an
    -- assertion nobody can re-check is not provenance. Either both or neither.
    CONSTRAINT dividend_event_corroboration_complete CHECK (
        (corroborated_by IS NULL) = (corroborated_url IS NULL)
    ),
    -- A source cannot corroborate itself. Two runs of the same feed are one observation,
    -- and treating the second as independent confirmation is the precise mistake
    -- check_cross_sourced exists to prevent.
    CONSTRAINT dividend_event_corroboration_distinct CHECK (
        corroborated_by IS NULL OR corroborated_by <> source
    )
);

CREATE INDEX dividend_event_isin_idx ON dividend_event (isin, verification);
CREATE INDEX dividend_event_ex_date_idx ON dividend_event (ex_date);

COMMENT ON TABLE dividend_event IS
    'A sourced reference record for one DECLARED dividend -- a fact about the company, '
    'not about any investor. Never written to by an adapter, and NEVER applied to the '
    'ledger directly: a declared amount times an eligible quantity is a PROJECTION, and '
    'the path to a DIVIDEND ledger row runs through import_batch/promote with receipt '
    'evidence in it. Entitlement is derived on read (app.engine.dividends) and never '
    'stored, for the same reason holdings are never stored.';

COMMENT ON COLUMN dividend_event.ex_date IS
    'The eligibility cutoff -- quantity_held_before() reads the close BEFORE this. Never '
    'null: a notice with no fixed ex-date produces no row rather than a guessed one.';

COMMENT ON COLUMN dividend_event.record_date IS
    'Display and reconciliation only. The legal entitlement date, but not computable '
    'from a trade-dated ledger -- ex_date is what eligibility uses.';

COMMENT ON COLUMN dividend_event.kind IS
    'UNSPECIFIED means THIS SOURCE DID NOT SAY, not a fourth kind of dividend. In the '
    'UNIQUE key because interim and special dividends can share an ex-date.';

COMMENT ON COLUMN dividend_event.corroborated_by IS
    'The second, distinct source that read the same amount and ex-date. This table''s '
    'substitute for corporate_action''s price check, because a dividend''s ex-date price '
    'move is inside daily noise. Corroboration, NOT proof -- see '
    'app.engine.dividend_events.check_cross_sourced on the provenance relationship.';

COMMENT ON COLUMN dividend_event.verification IS
    'DRAFT is inert -- it changes no number anywhere. Only VERIFIED events are eligible '
    'to produce a projection. Same three-state vocabulary as corporate_action and '
    'security_alias, deliberately the same enum type rather than a parallel one.';
