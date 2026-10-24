-- Retain each imported observation set separately: same-date documents must not merge.
CREATE TABLE reconciliation_statement (
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    batch_id uuid REFERENCES import_batch(id) ON DELETE CASCADE,
    snap_date date NOT NULL,
    source text NOT NULL,
    quantities jsonb NOT NULL,
    fingerprint text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reconciliation_statement_account_date_idx
    ON reconciliation_statement(account_id, snap_date DESC, created_at DESC);

-- Immutable evidence and results of a comparison, never a source for holdings.
CREATE TABLE reconciliation_run (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    account_id uuid NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    request jsonb NOT NULL,
    report jsonb NOT NULL
);
CREATE INDEX reconciliation_run_investor_created_idx
    ON reconciliation_run(investor_id, created_at DESC, id);
CREATE FUNCTION reject_reconciliation_run_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Reconciliation runs are immutable; create a new run';
END;
$$;
CREATE TRIGGER reconciliation_run_no_update BEFORE UPDATE ON reconciliation_run
    FOR EACH ROW EXECUTE FUNCTION reject_reconciliation_run_update();
COMMENT ON TABLE reconciliation_run IS
    'Account-scoped comparison inputs and results at run time. Does not certify later '
    'ledger state. Deleted with the investor/account under the existing erasure flow.';

CREATE TRIGGER reconciliation_statement_no_update BEFORE UPDATE ON reconciliation_statement
    FOR EACH ROW EXECUTE FUNCTION reject_reconciliation_run_update();
