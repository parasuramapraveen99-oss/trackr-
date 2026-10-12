-- Public reference ingestion state is independent of DEBUG and of any paid provider.
CREATE TABLE security_reference (
    isin text PRIMARY KEY,
    issuer text NOT NULL,
    description text NOT NULL,
    instrument text NOT NULL,
    status text NOT NULL,
    source_url text NOT NULL,
    response_sha256 text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    raw_payload jsonb NOT NULL
);

CREATE TABLE reference_sync_task (
    kind text NOT NULL CHECK (kind IN ('NSDL', 'NSE', 'IDENTITY', 'ACTION', 'REPLAY', 'SNAPSHOT')),
    task_key text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'retry', 'done')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    last_error text,
    PRIMARY KEY (kind, task_key)
);
CREATE INDEX reference_sync_task_due ON reference_sync_task(available_at)
    WHERE state != 'done';

CREATE TABLE reference_sync_dependency (
    account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    kind text NOT NULL,
    task_key text NOT NULL,
    PRIMARY KEY (account_id, kind, task_key),
    FOREIGN KEY (kind, task_key) REFERENCES reference_sync_task(kind, task_key)
);

CREATE TABLE security_reference_history (
    isin text NOT NULL REFERENCES security_reference(isin),
    record_sha256 text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    source_url text NOT NULL,
    raw_payload jsonb NOT NULL,
    PRIMARY KEY (isin, record_sha256)
);

CREATE TABLE reference_sync_import (
    account_id uuid PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    scheduled_at timestamptz
);

CREATE FUNCTION request_reference_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source != 'CORP_ACTION' THEN
        INSERT INTO reference_sync_import(account_id) VALUES (NEW.account_id)
        ON CONFLICT (account_id) DO UPDATE SET requested_at = clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER transaction_reference_sync AFTER INSERT ON transaction
    FOR EACH ROW EXECUTE FUNCTION request_reference_sync();

-- Existing portfolios are enrolled too, without modifying their original transactions.
INSERT INTO reference_sync_import(account_id)
    SELECT DISTINCT account_id FROM transaction;

-- Persist failed documents so they can be retried after the listing window moves on.
ALTER TABLE isin_change_circular ADD COLUMN next_retry_at timestamptz;
ALTER TABLE isin_change_circular ADD COLUMN attempts integer NOT NULL DEFAULT 0;
ALTER TABLE isin_change_circular ADD COLUMN pdf_sha256 text;

-- An explicit old/new statement or a dated exchange identity observation is required
-- for unattended promotion. Ledger arithmetic alone is not evidence of identity.
CREATE TABLE security_alias_evidence (
    old_isin text PRIMARY KEY REFERENCES security_alias(old_isin),
    current_isin text NOT NULL REFERENCES security(isin),
    effective_date date NOT NULL,
    evidence jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reference_bhavcopy_day (
    price_date date PRIMARY KEY,
    source_url text NOT NULL,
    payload_sha256 text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE reference_bhavcopy_security (
    price_date date REFERENCES reference_bhavcopy_day(price_date),
    isin text NOT NULL,
    symbol text NOT NULL,
    series text NOT NULL,
    close numeric NOT NULL CHECK (close > 0),
    PRIMARY KEY (price_date, isin, series)
);

CREATE TABLE reference_notice (
    notice_hash text PRIMARY KEY,
    isin text NOT NULL,
    symbol text NOT NULL,
    ex_date text NOT NULL,
    subject text NOT NULL,
    source_url text NOT NULL,
    payload jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reference_action_evidence (
    action_id uuid PRIMARY KEY REFERENCES corporate_action(id),
    evidence jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reference_account_issue (
    account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    action_id uuid NOT NULL REFERENCES corporate_action(id),
    isin text NOT NULL REFERENCES security(isin),
    reason text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, action_id)
);
