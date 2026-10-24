-- Analytics cache markers: per investor, and only for rows that actually changed.
--
-- WHY (24-10-2026, measured on scratch). Migration 089's statement trigger wrote one
-- GLOBAL marker for any write to any input table, including:
--   * a statement that changed no rows (Postgres fires statement triggers regardless), so
--     the catch-up cycle re-upserting five unchanged index_level days invalidated every
--     cached report on every /sync call;
--   * one investor's portfolio_snapshot rows, which invalidated every OTHER investor; and
--   * the reference_sync_* queue tables, which the report never reads.
-- With the import screen polling /sync every few seconds, the 6-7 s advisor report was
-- recomputed on nearly every Analytics page load.
--
-- NOW. Three row-aware statement triggers per input table (INSERT / UPDATE / DELETE; a
-- transition table needs one event per trigger) read the changed rows:
--   * no changed rows                  -> no marker;
--   * a table scoped to one investor   -> one marker per (transaction, investor), with
--     investor_id set; resolved via investor_id, account_id -> account, or investor.id;
--   * a row whose investor cannot be resolved (account already deleted, NULL owner) or
--     any shared table (prices, securities, index levels, actions...) -> one global
--     marker (investor_id NULL) per transaction, which invalidates everyone, as before.
-- app/db/analytics_cache.data_version(conn, investor_id) reads the global markers plus
-- that investor's own. The atomicity argument of 089 is unchanged: every marker is
-- written inside the transaction that wrote the data it describes.
--
-- TRUNCATE keeps 089's global statement trigger (no transition table exists for it).

ALTER TABLE analytics_input_change ADD COLUMN investor_id uuid;
CREATE INDEX analytics_input_change_scope ON analytics_input_change (investor_id, id);

COMMENT ON COLUMN analytics_input_change.investor_id IS
    'NULL: a shared input changed (every report is stale). Set: only this investor''s '
    'inputs changed. See migration 093.';

CREATE FUNCTION analytics_input_rows_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    xact text := pg_current_xact_id()::text;
    scope text := TG_ARGV[0];
    ids uuid[] := '{}';
    inv uuid;
    flag text;
BEGIN
    -- Transition tables: changed_rows (INSERT, UPDATE new), old_rows (UPDATE old, DELETE).
    -- PL/pgSQL plans each statement on first use per table, so a branch naming a column
    -- the table lacks is never planned for it.
    IF scope = 'shared' THEN
        IF TG_OP = 'DELETE' THEN
            IF NOT EXISTS (SELECT 1 FROM old_rows) THEN RETURN NULL; END IF;
        ELSIF NOT EXISTS (SELECT 1 FROM changed_rows) THEN
            RETURN NULL;
        END IF;
        ids := ARRAY[NULL::uuid];
    ELSIF scope = 'investor_id' THEN
        IF TG_OP <> 'DELETE' THEN
            ids := ids || ARRAY(SELECT DISTINCT investor_id FROM changed_rows);
        END IF;
        IF TG_OP <> 'INSERT' THEN
            ids := ids || ARRAY(SELECT DISTINCT investor_id FROM old_rows);
        END IF;
    ELSIF scope = 'account_id' THEN
        IF TG_OP <> 'DELETE' THEN
            ids := ids || ARRAY(
                SELECT DISTINCT a.investor_id FROM changed_rows r
                LEFT JOIN account a ON a.id = r.account_id);
        END IF;
        IF TG_OP <> 'INSERT' THEN
            ids := ids || ARRAY(
                SELECT DISTINCT a.investor_id FROM old_rows r
                LEFT JOIN account a ON a.id = r.account_id);
        END IF;
    ELSIF scope = 'investor' THEN
        IF TG_OP <> 'DELETE' THEN
            ids := ids || ARRAY(SELECT DISTINCT id FROM changed_rows);
        END IF;
        IF TG_OP <> 'INSERT' THEN
            ids := ids || ARRAY(SELECT DISTINCT id FROM old_rows);
        END IF;
    ELSE
        RAISE EXCEPTION 'analytics_input_rows_changed: unknown scope % on %', scope, TG_TABLE_NAME;
    END IF;

    IF cardinality(ids) = 0 THEN
        RETURN NULL;
    END IF;
    IF array_position(ids, NULL) IS NOT NULL THEN
        -- Shared input, or an owner that cannot be resolved: invalidate everyone.
        PERFORM set_config('trackr.analytics_input_xact', xact, true);
        INSERT INTO analytics_input_change (source_table) VALUES (TG_TABLE_NAME);
        RETURN NULL;
    END IF;
    FOREACH inv IN ARRAY ARRAY(SELECT DISTINCT x FROM unnest(ids) x) LOOP
        flag := 'trackr.aic_' || replace(inv::text, '-', '');
        IF current_setting(flag, true) IS DISTINCT FROM xact THEN
            PERFORM set_config(flag, xact, true);
            INSERT INTO analytics_input_change (source_table, investor_id)
            VALUES (TG_TABLE_NAME, inv);
        END IF;
    END LOOP;
    RETURN NULL;
END
$$;

-- Same name and signature as 089, so later migrations that call it (090, 092) and any
-- future one get the row-aware, scoped triggers.
CREATE OR REPLACE FUNCTION attach_analytics_input_trigger(target regclass) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    scope text;
    -- Once a global marker exists in this transaction every report is already stale:
    -- skip the rest of the transaction's statements without reading their rows.
    gate text := 'WHEN (current_setting(''trackr.analytics_input_xact'', true) '
                 'IS DISTINCT FROM pg_current_xact_id()::text)';
BEGIN
    IF target = 'public.investor'::regclass THEN
        scope := 'investor';
    ELSIF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = target
                  AND attname = 'investor_id' AND NOT attisdropped) THEN
        scope := 'investor_id';
    ELSIF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = target
                  AND attname = 'account_id' AND NOT attisdropped) THEN
        scope := 'account_id';
    ELSE
        scope := 'shared';
    END IF;
    EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_changed ON %s', target);
    EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_inserted ON %s', target);
    EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_updated ON %s', target);
    EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_deleted ON %s', target);
    EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_truncated ON %s', target);
    EXECUTE format(
        'CREATE TRIGGER analytics_input_inserted AFTER INSERT ON %s '
        'REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT %s '
        'EXECUTE FUNCTION analytics_input_rows_changed(%L)', target, gate, scope);
    EXECUTE format(
        'CREATE TRIGGER analytics_input_updated AFTER UPDATE ON %s '
        'REFERENCING OLD TABLE AS old_rows NEW TABLE AS changed_rows FOR EACH STATEMENT %s '
        'EXECUTE FUNCTION analytics_input_rows_changed(%L)', target, gate, scope);
    EXECUTE format(
        'CREATE TRIGGER analytics_input_deleted AFTER DELETE ON %s '
        'REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT %s '
        'EXECUTE FUNCTION analytics_input_rows_changed(%L)', target, gate, scope);
    EXECUTE format(
        'CREATE TRIGGER analytics_input_truncated AFTER TRUNCATE ON %s '
        'FOR EACH STATEMENT EXECUTE FUNCTION analytics_input_changed()', target);
END
$$;

-- Queue bookkeeping is not an analytics input: the report reads no reference_sync_* table.
-- (Kept identical to app/db/analytics_cache.py EXCLUDED_TABLES.)
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['reference_sync_task', 'reference_sync_dependency',
                             'reference_sync_import'] LOOP
        IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
            EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_changed ON public.%I', t);
            EXECUTE format('DROP TRIGGER IF EXISTS analytics_input_truncated ON public.%I', t);
        END IF;
    END LOOP;
END
$$;

-- Re-attach every table that carried 089's trigger, now scoped and row-aware.
DO $$
DECLARE
    t regclass;
BEGIN
    FOR t IN
        SELECT DISTINCT tg.tgrelid::regclass FROM pg_trigger tg
        WHERE tg.tgname = 'analytics_input_changed' AND NOT tg.tgisinternal
    LOOP
        PERFORM attach_analytics_input_trigger(t);
    END LOOP;
END
$$;

-- One-time cleanup of redundant snapshot rebuilds queued before tasks recorded the
-- queuing transaction (see app/db/snapshots.settle_rebuild_tasks). A full replay deletes
-- an investor's snapshots and queues one task per deleted day; the range catch-up then
-- refilled those days without closing the tasks, so the worker re-valued thousands of
-- already-correct days, 64 at a time, and held the investor's status at "processing".
-- A task is closed only when its day was rebuilt more than an hour after it was queued:
-- far beyond any writer's read-to-write span (a cycle is budgeted at 210 s, Vercel kills
-- at 300 s), so the stored row cannot predate the change that queued the task.
UPDATE reference_sync_task t
SET state = 'done', last_error = NULL, attempts = 0,
    updated_at = clock_timestamp(), last_success_at = clock_timestamp()
FROM portfolio_snapshot p
WHERE t.kind = 'SNAPSHOT' AND t.state IN ('pending', 'retry')
  AND NOT (t.payload ? 'queued_xid')
  AND p.investor_id = (t.payload->>'investor_id')::uuid
  AND p.as_of = (t.payload->>'on')::date
  AND p.advised_only = (t.payload->>'advised_only')::boolean
  AND p.computed_at > t.available_at + interval '1 hour';
