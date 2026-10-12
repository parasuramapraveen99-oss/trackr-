-- Corporate-action source observations, provider revisions, observed coverage ranges,
-- exchange-tagged bhavcopy evidence, and the reference-row columns they need.
-- docs/CORPORATE_ACTIONS_COMPLETION_PLAN.md §3. These are GLOBAL reference tables:
-- written only by the reference worker and ops/cron routes, never by an investor route.
-- Per-account effects are derived on read from each account's own ledger and are not
-- stored here. No user attestation of history completeness exists (OD-3).

-- Preflight for the unique index at the end of this file. CREATE UNIQUE INDEX fails on
-- existing duplicates; fail first with a message that names them. Nothing is deleted or
-- rewritten -- the ledger is append-only, so duplicates need a human decision.
DO $$
DECLARE
    duplicates text;
BEGIN
    SELECT string_agg(format('account %s action %s (%s rows)', account_id, action_id, n), '; ')
      INTO duplicates
      FROM (SELECT account_id, raw_payload->>'auto_corporate_action_id' AS action_id,
                   count(*) AS n
              FROM transaction
             WHERE source = 'CORP_ACTION' AND raw_payload ? 'auto_corporate_action_id'
             GROUP BY 1, 2 HAVING count(*) > 1) d;
    IF duplicates IS NOT NULL THEN
        RAISE EXCEPTION 'migration 061: resolve duplicate derived corporate-action rows first: %',
            duplicates;
    END IF;
END $$;

-- One row per distinct source payload (idempotent on provider + bytes + parser version).
-- Classification is detection only (invariant 3); it never authorizes a write.
CREATE TABLE corporate_action_observation (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    payload_sha256 text NOT NULL,
    raw_payload jsonb NOT NULL,
    parser_version text NOT NULL,
    rights_reference text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    classification text NOT NULL CHECK (classification IN (
        'BONUS','SPLIT','DIVIDEND','RIGHTS','BUYBACK','MERGER','DEMERGER',
        'CAPITAL_REDUCTION','CONSOLIDATION','IDENTITY','UNKNOWN_ADJUSTMENT','UNKNOWN')),
    result text NOT NULL CHECK (result IN ('PENDING','ACCEPTED','REVIEW','REJECTED','CONFLICT')),
    reason_code text NOT NULL,
    reason text,
    isin text,
    ex_date date,
    event_id text,
    component_id text,
    action_id uuid REFERENCES corporate_action(id),
    dividend_id uuid REFERENCES dividend_event(id),
    UNIQUE (provider, payload_sha256, parser_version)
);
CREATE INDEX corporate_action_observation_isin ON corporate_action_observation (isin, ex_date);

-- Only where a provider event ID exists (the operator file import). NSE has none.
CREATE TABLE corporate_action_revision (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    event_id text NOT NULL,
    component_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    observation_id uuid NOT NULL REFERENCES corporate_action_observation(id),
    terms jsonb NOT NULL,
    verification_evidence jsonb,
    action_id uuid REFERENCES corporate_action(id),
    dividend_id uuid REFERENCES dividend_event(id),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, event_id, component_id, revision)
);
CREATE INDEX corporate_action_revision_latest ON corporate_action_revision
    (provider, event_id, component_id, revision DESC);

-- Observed ranges only. A row here never claims the range is complete.
CREATE TABLE corporate_action_coverage (
    provider text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL CHECK (end_date >= start_date),
    exchanges text[] NOT NULL,
    parser_version text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, start_date, end_date, parser_version)
);

-- BSE price evidence reuses the existing parsed-close tables (no raw bytes stored).
-- Every existing row came from the NSE archive (identity_evidence._day was NSE-only).
ALTER TABLE reference_bhavcopy_security DROP CONSTRAINT reference_bhavcopy_security_price_date_fkey;
ALTER TABLE reference_bhavcopy_security DROP CONSTRAINT reference_bhavcopy_security_pkey;
ALTER TABLE reference_bhavcopy_day DROP CONSTRAINT reference_bhavcopy_day_pkey;
ALTER TABLE reference_bhavcopy_day
    ADD COLUMN exchange text NOT NULL DEFAULT 'NSE' CHECK (exchange IN ('NSE','BSE'));
ALTER TABLE reference_bhavcopy_security
    ADD COLUMN exchange text NOT NULL DEFAULT 'NSE' CHECK (exchange IN ('NSE','BSE'));
ALTER TABLE reference_bhavcopy_day ADD PRIMARY KEY (exchange, price_date);
ALTER TABLE reference_bhavcopy_security ADD PRIMARY KEY (exchange, price_date, isin, series);
ALTER TABLE reference_bhavcopy_security ADD FOREIGN KEY (exchange, price_date)
    REFERENCES reference_bhavcopy_day (exchange, price_date);

ALTER TABLE corporate_action ADD COLUMN record_date date;
ALTER TABLE corporate_action ADD COLUMN allotment_date date;
ALTER TABLE corporate_action ADD COLUMN cancelled boolean NOT NULL DEFAULT false;
ALTER TABLE corporate_action ADD COLUMN automatic_application boolean NOT NULL DEFAULT true;
ALTER TABLE corporate_action ADD CONSTRAINT corporate_action_allotment_date
    CHECK (allotment_date IS NULL OR allotment_date >= ex_date);

-- One derived entitlement per account per action, independent of descriptive strings.
-- Makes retry and concurrent application safe (invariant 12).
CREATE UNIQUE INDEX transaction_auto_action_account ON transaction
    (account_id, (raw_payload->>'auto_corporate_action_id'))
    WHERE source = 'CORP_ACTION' AND raw_payload ? 'auto_corporate_action_id';
