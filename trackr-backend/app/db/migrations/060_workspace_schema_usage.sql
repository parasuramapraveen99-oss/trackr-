-- 060_workspace_schema_usage.sql -- USAGE on schema public for the six capability roles.
--
-- WHY. Migration 045 grants every workspace capability its table and column privileges
-- (e.g. SELECT (id,owner_user_id) ON investor), but a table privilege is unreachable without
-- USAGE on the schema holding the table. 045 silently relied on the stock default, where
-- PUBLIC holds USAGE on `public`. A cluster whose `public` schema does not grant that --
-- the local trackr-postgres container is one -- makes every workspace login see every table
-- as nonexistent: the first statement of every research route, workspace_context's
-- ownership check, fails with `relation "investor" does not exist` and the route 500s.
--
-- WHY THE SIX ROLES, NOT PUBLIC. Granting to PUBLIC would repair this too, but would also
-- hand schema visibility to every role on the cluster. The capability roles are exactly the
-- identities 045 already authorises; the logins inherit through their single membership.
--
-- USAGE only, never CREATE: a runtime identity that could create objects in `public` could
-- shadow a table the guards resolve by name. Idempotent -- re-granting is a no-op.

GRANT USAGE ON SCHEMA public
  TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,trackr_ws_scheduler,
     trackr_ws_reader,trackr_ws_eraser;
