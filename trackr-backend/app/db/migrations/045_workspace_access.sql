-- 045_workspace_access.sql -- tenant row-level security and per-capability grants.
--
-- LAST OF SIX (040-045). After this file the workspace schema is complete and enforced, and
-- `app/db/migrate.py::workspace_schema_ready` starts returning true. That still does not turn
-- any route on: `WORKSPACE_ENABLED` is a separate operator decision and no research route
-- exists yet.
--
-- WHAT RLS IS FOR HERE, AND WHAT IT IS NOT. It restricts every workspace table to the investor
-- that `app/db/workspace_context.py` SELECTED for this transaction. It is defence in depth
-- behind `require_owned_investor`, which is what actually decides whether the authenticated
-- caller may select that investor at all. RLS has no opinion about whether the selection was
-- legitimate: a transaction that sets `app.workspace_investor` to a stranger's id gets a
-- faithfully enforced view of the stranger's rows. **RLS is not authentication.** It is what
-- makes a missing WHERE clause harmless instead of catastrophic, and that is worth a lot -- but
-- it is not the ownership check, and describing it as one would be the single most misleading
-- sentence anyone could write about this file.
--
-- BOTH `USING` AND `WITH CHECK`. USING filters what a statement can SEE; WITH CHECK constrains
-- what it can WRITE. A policy with only USING would let a transaction scoped to investor A
-- insert a row stamped investor B -- invisible to itself afterwards, and very much present.
--
-- `FORCE ROW LEVEL SECURITY` because a table's OWNER is exempt from its own policies by
-- default. Without it, the migration identity -- and anything that ever ran as it -- would read
-- and write across every tenant, which is precisely the hole RLS is here to close.
--
-- AND FORCE RLS DOES NOT BIND A SUPERUSER OR A ROLE WITH BYPASSRLS. **If the migration owner is
-- a superuser, nothing in this file isolates anything for it.** Measured, not assumed: with a
-- superuser owner, `SELECT count(*) FROM note` with no tenant selected returned every tenant's
-- rows; with a NOSUPERUSER NOBYPASSRLS owner, the same query returned zero.
-- scripts/provision_workspace_roles.sql now refuses to provision against such an owner, because
-- a tenant-isolation test that passes on the default single-role development container -- whose
-- owner IS the cluster superuser -- passes for the wrong reason and proves nothing.
-- The append-only triggers in 044 are unaffected: those are triggers, not privileges, and a
-- superuser owner is still refused an UPDATE.
--
-- NO SECURITY DEFINER FUNCTION, AND NO OWNERSHIP GRANT. Neither is used to paper over a
-- missing permission below. If one ever becomes genuinely necessary, it needs its narrow
-- authority, a fixed search_path and a named set of callable roles documented at the point of
-- creation, and tests that try to bypass it.
--
-- OPERATION-SPECIFIC INSERT GRANTS ARE THE POINT OF THE WHOLE ROLE SPLIT. Read the four GRANT
-- INSERT statements below as the capability model in one screen: the runner can submit attempts
-- and results and cannot touch a note, a thesis event or a review; the scheduler can queue work
-- and expire it and cannot propose output; the engine can record facts and evaluations and
-- cannot review them; the reader holds no INSERT at all. 044's triggers enforce the same
-- separations again from inside, so neither layer is the only thing standing between untrusted
-- output and the notebook.

-- ---------------------------------------------------------------------------------
-- 1. Per-table: RLS, PUBLIC revocation, universal SELECT, eraser DELETE
-- ---------------------------------------------------------------------------------
--
-- The same twenty-five names as 044, in the same order. A table missing from either list would
-- be a readable-across-tenants or editable corner of an otherwise closed schema.
DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'company','research_source','research_fact','note','note_link','ritual','question',
    'question_answer','thesis','trigger_evaluation','evaluation_fact','thesis_event',
    'skill','agent','task','task_context','task_attempt','task_result','result_citation',
    'task_review','chat_thread','chat_message','monitor_schedule','monitor_run','monitor_report'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tbl);
    EXECUTE format('CREATE POLICY ws_tenant_rows ON %I USING (investor_id=ws_tenant()) WITH CHECK (investor_id=ws_tenant())',tbl);
    -- Start from nothing. A future `GRANT ... TO PUBLIC` elsewhere, or a default privilege
    -- inherited from somewhere, must not be what decides who reads research notes.
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',tbl);
    -- SELECT for all six. Every capability has to READ to do its job -- and every read is
    -- already confined to the selected tenant by the policy above, so a broad SELECT grant is
    -- not a broad read. The guards in 044 also run SELECTs as the connected role, so a role
    -- without SELECT here would fail its own guard with a permission error rather than the
    -- intended message.
    EXECUTE format('GRANT SELECT ON %I TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser',tbl);
    -- DELETE for the eraser ALONE, and holding the grant is still not enough: 044's
    -- ws_append_only additionally requires the erasure flag, a matching selected tenant and no
    -- open legal hold. The privilege is the first of four conditions, not the decision.
    EXECUTE format('GRANT DELETE ON %I TO trackr_ws_eraser',tbl);
    -- No UPDATE is granted to anything, ever. 044 refuses it too. Two layers, because a future
    -- GRANT typed by a tired person should not be sufficient to make history editable.
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------------
-- 2. INSERT, by operation
-- ---------------------------------------------------------------------------------

-- The human capability writes everything a person can author. Note what is ABSENT: no
-- task_attempt, no task_result, no result_citation, no trigger_evaluation, no monitor_run,
-- no monitor_report. A person does not submit model output or forge an evaluation receipt.
GRANT INSERT ON company,research_source,research_fact,note,note_link,ritual,question,
  question_answer,thesis,thesis_event,skill,agent,task,task_context,task_review,
  chat_thread,chat_message,monitor_schedule TO trackr_ws_human;

-- The runner submits what came back from the browser, and nothing else. It CANNOT insert a
-- note, a thesis_event or a task_review -- so the component that produces untrusted output has
-- no privilege to accept it. This one line is the separation the whole role split exists for.
GRANT INSERT ON task_attempt,task_result,result_citation,chat_message TO trackr_ws_runner;

-- The engine records deterministic work: sourced facts, evaluation receipts and their sealed
-- inputs, and assembled reports. It cannot review anything or write a note.
GRANT INSERT ON research_source,research_fact,trigger_evaluation,evaluation_fact,
  monitor_report TO trackr_ws_engine;

-- The scheduler creates due occurrences and the MONITOR tasks they queue, and appends EXPIRED
-- results for dead leases. 044 additionally refuses it any task that is not a
-- system-attributed MONITOR task, and any result outcome other than EXPIRED -- so this grant
-- cannot become "the scheduler can propose research".
GRANT INSERT ON task,task_context,task_result,monitor_run TO trackr_ws_scheduler;

-- trackr_ws_reader receives NO INSERT anywhere. That absence is the grant: a corpus/tool
-- service that can only read cannot corrupt the corpus it reads.

-- Sequence USAGE for the seven bigserial `seq` columns. Easy to forget and confusing when
-- missing: the INSERT is granted, the policy passes, the trigger passes, and the statement
-- still fails on nextval.
GRANT USAGE ON SEQUENCE research_fact_seq_seq,note_seq_seq,question_answer_seq_seq,
  thesis_seq_seq,thesis_event_seq_seq,task_seq_seq,chat_message_seq_seq
  TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,trackr_ws_scheduler;

-- ---------------------------------------------------------------------------------
-- 3. The minimum legacy access the guards actually need
-- ---------------------------------------------------------------------------------
--
-- COLUMN-LEVEL, not table-level, wherever the need is column-shaped. 044's ws_insert_guard
-- reads `investor.owner_user_id` to verify human attribution; it has no business reading
-- display_name, family_group_id or anything else about the investor, and a table-level grant
-- would hand it all of them.
GRANT SELECT (id,owner_user_id) ON investor
  TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,trackr_ws_scheduler,
     trackr_ws_reader,trackr_ws_eraser;

-- ws_same_security() walks VERIFIED alias edges, so the identity gate needs both tables. These
-- are market reference data -- an ISIN, a symbol, a verified succession -- not client data.
GRANT SELECT ON security,security_alias TO trackr_ws_human,trackr_ws_runner,
  trackr_ws_engine,trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser;

-- Only the eraser needs the hold check, and only two columns of it. This narrow grant is
-- sufficient because 044's ws_append_only nests the hold query inside the eraser branch rather
-- than chaining it into one condition -- see the comment there; the flat version would have
-- forced this grant onto all six roles to avoid a spurious permission error.
GRANT SELECT (investor_id,released_at) ON legal_hold TO trackr_ws_eraser;

-- Every capability can APPEND to the audit log and none can read or amend it. app/db/audit.py's
-- rule is unchanged: the mutation and its audit row share one transaction, so an unauditable
-- mutation is impossible rather than merely discouraged.
GRANT INSERT ON audit_log TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,
  trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser;

-- ---------------------------------------------------------------------------------
-- 4. What is deliberately NOT granted
-- ---------------------------------------------------------------------------------
--
-- No grant of any kind on `transaction`, `account`, `import_batch`, `import_row`,
-- `portfolio_snapshot`, `price`, `live_price`, `corporate_action`, `dividend_event`,
-- `investor_secret` or any other financial or identity table. No workspace capability can read
-- the ledger, let alone write it. Existing engine reads continue to use the existing
-- repositories on the existing financial connection with their established permissions --
-- CLAUDE.md's rule 5 boundary, expressed as privileges.
--
-- No UPDATE and no TRUNCATE to anything. No CREATE, no ownership, no BYPASSRLS, no CREATEROLE:
-- those are role attributes, refused by scripts/provision_workspace_roles.sql, which also
-- refuses to let one login hold two capabilities.
--
-- `creation_xid` is not excluded from the INSERT grants, and does not need to be: 044's
-- ws_insert_guard overwrites whatever a caller supplies with txid_current(), so a forged
-- transaction marker cannot reopen a sealed child collection. Column-level INSERT grants on
-- twenty-five tables would be brittle and would break on every future column; the trigger is
-- the guarantee, and it is the one that holds even if a grant is widened later.
--
-- The dedicated erasure login ALSO needs the existing privacy workflow's approved privileges on
-- legacy tables to do its job end to end. Those are provisioned with that workflow (session 07,
-- row B04), never by adding a broad workspace membership and never by granting ownership to a
-- web process.
