-- 031_isin_change_circulars.sql -- an automated SOURCE of security_alias evidence.
--
-- WHY THIS EXISTS. security_alias (027) already holds the trust model: an alias is an
-- assertion, structural checks reject but never create, and DRAFT is inert until a human
-- (or app.db.security_alias.reconcile_after_alias) promotes it. What has been missing is
-- an automated writer of DRAFT rows. NSE publishes a "Change in ISIN" circular whenever
-- one is retired -- measured: 58 in 2022, 56 in 2023, 73 in 2024, 59 in 2025, via
-- www.nseindia.com/api/circulars. app.ingest.isin_change_feed turns those into rows here,
-- and this migration adds NO COLUMNS to security_alias itself -- see that module's
-- docstring for why its existing shape already anticipates this writer.
--
-- WHY A SEPARATE TABLE AND NOT security_alias DIRECTLY. A circular is not an identity
-- assertion by itself: it names a symbol and a NEW isin, never the old one, so "which
-- ISIN does this retire" is an inference this codebase makes (by joining the symbol
-- against `security`), not something NSE stated. Every circular this feed sees is worth
-- recording -- including the ones for a company nobody holds, or whose symbol join is
-- ambiguous -- and that record must never be confused with an identity claim. Precedent:
-- dividend_event (029) exists for exactly this reason, to avoid forcing a different kind
-- of fact into an existing trust table.
--
-- WHAT THIS TABLE IS NOT. It is not itself evidence of anything about a security's
-- identity -- old_isin/alias_created only report what app.ingest.isin_change_feed did
-- with the circular, not a fact this table asserts on its own. The identity assertion,
-- if one was made, lives in security_alias as its own DRAFT row, sourced back to
-- source_url here.
CREATE TABLE isin_change_circular (
    -- NSE's own circular id (e.g. '76380' from 'NSE/CML/76380'). The dedupe key: a
    -- re-run of the poll against an overlapping date range must be a no-op, same rule as
    -- dedupe_key everywhere else in ingestion.
    circular_number   text PRIMARY KEY,
    circular_ref      text NOT NULL,               -- 'NSE/CML/76380', kept for the audit trail
    circular_date     date NOT NULL,

    -- The circulars-LIST endpoint's own subject line, e.g. "Change in ISIN -- Oriana
    -- Power Limited (ORIANA)". The one field always known, because it comes from the
    -- list response itself -- unlike every column below, which depends on successfully
    -- fetching and parsing that circular's PDF and so is NULLABLE.
    subject_raw       text NOT NULL,

    symbol            text,
    new_isin          text CHECK (new_isin IS NULL OR new_isin ~ '^IN[A-Z0-9]{10}$'),

    -- The circular's free-text reason, e.g. "Sub-Division from Rs. 10/- to Rs. 2/-".
    -- Kept verbatim -- see app.engine.corporate_actions's "No ratio strings" note on why
    -- this codebase never trusts notation like this at face value for a NUMBER. It is
    -- read here only to classify the kind of event, never to derive a ratio.
    remarks_raw       text,

    -- A closed-set classification of remarks_raw (SUB_DIVISION, CONSOLIDATION,
    -- MERGER_OR_SCHEME, DEMERGER, RENAME, OTHER). ALWAYS NULL TODAY: the classifier
    -- that populated it was removed on 2026-09-22 and nothing replaced it, so
    -- app.ingest.isin_change_feed.ParsedCircular hard-codes reason_class=None. The
    -- columns are kept because they were only ever a review-queue enrichment --
    -- nothing downstream branches on them, so an always-NULL column changes no
    -- number and a future classifier needs no migration.
    reason_class      text,
    reason_confidence numeric(4,3),

    -- Two dates, because the circulars state two and they can disagree (confirmed: Tata
    -- Steel's ISIN changed "w.e.f. July 29, 2022" but was effective for trades "on and
    -- from the Ex-date i.e., July 28, 2022"). wef_date is kept for the audit trail only;
    -- ex_date is what security_alias.effective_date is set to, because that is the date
    -- app.db.repository.load_transactions must start applying the alias from.
    wef_date          date,
    ex_date           date,

    source_url        text NOT NULL,
    fetched_at        timestamptz NOT NULL DEFAULT now(),

    -- The symbol-join result. NULL means unresolved -- see skip_reason. A REFERENCE
    -- rather than a free column so this table cannot silently drift from `security`.
    old_isin          text REFERENCES security(isin),
    alias_created     boolean NOT NULL DEFAULT false,

    -- Set exactly when alias_created is false and old_isin resolution or add_alias
    -- itself refused the row: 'symbol_unknown', 'multiple_isins_for_symbol', or the
    -- message from a caught SecurityAliasError (self-alias, cross-issuer, cycle). NOT an
    -- enum -- SecurityAliasError's text is open-ended and this column exists for a human
    -- reading the review queue, not for code to branch on.
    skip_reason       text,

    CONSTRAINT isin_change_circular_outcome CHECK (
        (alias_created AND skip_reason IS NULL) OR (NOT alias_created)
    ),
    -- An alias can only have been created from a circular this module actually parsed.
    CONSTRAINT isin_change_circular_alias_needs_parse CHECK (
        NOT alias_created OR (new_isin IS NOT NULL AND ex_date IS NOT NULL)
    )
);

CREATE INDEX idx_isin_change_circular_symbol ON isin_change_circular (symbol);
-- The review queue's own ordering: unresolved rows, most recent first.
CREATE INDEX idx_isin_change_circular_unresolved ON isin_change_circular (circular_date DESC)
    WHERE NOT alias_created;

COMMENT ON TABLE isin_change_circular IS
    'One row per NSE "Change in ISIN" circular seen by app.ingest.isin_change_feed. '
    'Records the circular whether or not it produced a security_alias row -- old_isin '
    'and alias_created report the outcome of the symbol join and add_alias call, not an '
    'identity claim this table makes on its own. See app.db.security_alias for the '
    'actual identity assertion, DRAFT and sourced back to source_url here.';

-- Generic per-feed poll state: consecutive-failure count and circuit-breaker window.
-- Not specific to this feed -- app.ingest.corporate_action_feed and
-- app.db.dividend_events.refresh_dividend_feed poll on the same "never raise on a
-- routine failure" posture
-- today with no persisted failure count at all; this table is reusable by them without a
-- further migration once they want a breaker of their own.
CREATE TABLE feed_poll_state (
    feed                  text PRIMARY KEY,        -- e.g. 'nse_isin_change_circulars'
    consecutive_failures  integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_attempt_at       timestamptz,
    last_success_at       timestamptz,
    -- NULL = breaker closed, poll normally. Set = skip every poll until this timestamp,
    -- with zero HTTP calls attempted -- checked before the feed's own opener runs at all.
    breaker_open_until    timestamptz
);

COMMENT ON TABLE feed_poll_state IS
    'One row per polled feed: consecutive failures and an optional circuit-breaker '
    'window. A poll that finds breaker_open_until in the future must skip entirely -- '
    'not attempt a request and catch the failure -- see app.ingest.isin_change_feed.';
