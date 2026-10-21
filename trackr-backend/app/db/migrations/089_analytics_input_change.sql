-- Transactional change markers for analytics_report_cache invalidation.
--
-- WHY NOT pg_stat_user_tables (tried first, 21-10-2026): those counters are published
-- asynchronously after commit. Measured on scratch, a request issued right after an
-- in-place price correction still saw the old counters and was served the old report.
-- A marker row written INSIDE the writing transaction becomes visible atomically with
-- the data it describes, so a reader can never see new data with an old version.
--
-- COST: one row per writing TRANSACTION, not per statement or row. A statement trigger
-- fires per statement (executemany price upserts are one statement per row), but its
-- WHEN clause skips every statement after the first in a transaction by comparing a
-- transaction-local setting with the current transaction id; the trigger function itself
-- runs once per transaction, whichever input table is written first. Any marker changes
-- the version, so which table it names is informational only. Measured on scratch:
-- 20,000 price upserts took 1.22-1.36 s with the trigger versus 1.08 s without.
--
-- COVERAGE: every public table except those that cannot affect the advisor report
-- (kept identical to app/db/analytics_cache.py EXCLUDED_TABLES / EXCLUDED_PREFIXES; a
-- test fails if a non-excluded table lacks the trigger, so a table added later is caught).
CREATE TABLE analytics_input_change (
    id            bigserial PRIMARY KEY,
    source_table  text NOT NULL,
    changed_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE analytics_input_change IS
    'One row per transaction that wrote an analytics input table. Read as (count, min, max) '
    'by app/db/analytics_cache.data_version. Rows older than a week are pruned.';

CREATE FUNCTION analytics_input_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('trackr.analytics_input_xact', pg_current_xact_id()::text, true);
    INSERT INTO analytics_input_change (source_table) VALUES (TG_TABLE_NAME);
    RETURN NULL;
END
$$;

CREATE FUNCTION attach_analytics_input_trigger(target regclass) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format(
        'CREATE TRIGGER analytics_input_changed AFTER INSERT OR UPDATE OR DELETE ON %s '
        'FOR EACH STATEMENT WHEN (current_setting(''trackr.analytics_input_xact'', true) '
        'IS DISTINCT FROM pg_current_xact_id()::text) '
        'EXECUTE FUNCTION analytics_input_changed()', target);
    EXECUTE format(
        'CREATE TRIGGER analytics_input_truncated AFTER TRUNCATE ON %s '
        'FOR EACH STATEMENT EXECUTE FUNCTION analytics_input_changed()', target);
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
        AND tablename NOT IN (
            'analytics_input_change', 'analytics_report_cache', 'job', 'app_session',
            'audit_log', 'ws_receipt', 'feed_poll_state', 'document', 'document_password',
            'import_batch', 'import_row', 'agent', 'skill', 'ritual', 'trigger_evaluation',
            'evaluation_fact', 'user_capability', 'grievance', 'legal_hold',
            'retention_policy', 'schema_migrations', 'scratch_database_tag')
        AND tablename NOT LIKE 'butler\_%' AND tablename NOT LIKE 'chat\_%'
        AND tablename NOT LIKE 'email\_%' AND tablename NOT LIKE 'hosted\_job%'
        AND tablename NOT LIKE 'market\_assistant\_%' AND tablename NOT LIKE 'monitor\_%'
        AND tablename NOT LIKE 'narrative\_%' AND tablename NOT LIKE 'note%'
        AND tablename NOT LIKE 'question%' AND tablename NOT LIKE 'report\_%'
        AND tablename NOT LIKE 'research\_%' AND tablename NOT LIKE 'task%'
        AND tablename NOT LIKE 'thesis%'
    LOOP
        PERFORM attach_analytics_input_trigger(format('public.%I', t)::regclass);
    END LOOP;
END
$$;
