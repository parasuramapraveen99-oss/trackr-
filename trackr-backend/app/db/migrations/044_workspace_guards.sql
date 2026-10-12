-- 044_workspace_guards.sql -- append-only enforcement, server clocks, actor authority, seals.
--
-- FIFTH OF SIX (040-045). This is where the previous four files stop being a set of tables and
-- become a system with rules. Everything 040-043 documented as "044 enforces this" is here.
--
-- REQUIRES THE SIX CAPABILITY ROLES TO EXIST ALREADY. `ws_human()` and the guards below call
-- `pg_has_role(current_user, 'trackr_ws_human', 'member')`, which ERRORS on a role that does
-- not exist -- so without provisioning, every workspace INSERT would fail with an obscure
-- message. The explicit check at the top of this file turns that into an actionable one. Run
-- scripts/provision_workspace_roles.sql first; docs/DEPLOYMENT.md section 4d has the order.
--
-- WHAT THE DATABASE CAN AND CANNOT PROVE, stated here because the report must keep saying it:
--
--   CAN prove: nothing was edited or deleted; a child collection was not extended after its
--   parent committed; a version number is sequential; an actor matches the authenticated
--   owner; a reviewer held the human capability; receipt times came from the server clock.
--
--   CANNOT prove: that a person actually READ the output they accepted; that arbitrary prose
--   contains no pasted secret; that a browser really invoked a provider (`BROWSER_MODEL` is
--   browser-REPORTED, never attested); or that any natural-language claim is true. A CHECK
--   constraint cannot verify comprehension. These are boundary properties owned by the UI,
--   the endpoint policy and the human review flow, and no trigger below should ever be
--   described as covering them.

-- ---------------------------------------------------------------------------------
-- 0. Prerequisite check -- fail with a sentence, not a symbol
-- ---------------------------------------------------------------------------------
DO $$
DECLARE missing text[];
BEGIN
  SELECT array_agg(r ORDER BY r) INTO missing
  FROM unnest(ARRAY['trackr_ws_human','trackr_ws_runner','trackr_ws_engine',
                    'trackr_ws_scheduler','trackr_ws_reader','trackr_ws_eraser']) AS r
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r);
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION
      'workspace capability roles are not provisioned: % are missing. The guards in this '
      'migration call pg_has_role() on these names, which errors on a role that does not '
      'exist, so every workspace INSERT would fail. Run '
      'scripts/provision_workspace_roles.sql first (docs/DEPLOYMENT.md section 4d).',
      missing
      USING ERRCODE = 'invalid_authorization_specification';
  END IF;
END $$;

-- ---------------------------------------------------------------------------------
-- 1. Context readers
-- ---------------------------------------------------------------------------------
--
-- These read what `app/db/workspace_context.py` set transaction-locally. The `true` second
-- argument to current_setting is missing_ok: an unset setting is NULL, not an error, so a
-- missing context fails CLOSED at the tenant comparison below rather than aborting with a
-- configuration error that reads like a bug.

CREATE FUNCTION ws_actor() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.workspace_actor',true),'')::uuid
$$;

CREATE FUNCTION ws_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.workspace_investor',true),'')::uuid
$$;

-- IS THE CONNECTED ROLE A HUMAN CAPABILITY? Asked of PostgreSQL, not of the application, and
-- that is the entire point: an application variable claiming "this is a human request" is a
-- claim the request itself could make. `current_user` is decided by which credential opened
-- the socket, and a runner login is not a member of trackr_ws_human however it behaves.
CREATE FUNCTION ws_human() RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT pg_has_role(current_user,'trackr_ws_human','member')
$$;

COMMENT ON FUNCTION ws_human() IS
    'Whether the CONNECTED ROLE holds the human capability. Asked of PostgreSQL rather than '
    'of the application: a request cannot vouch for itself, but it cannot change which '
    'credential opened the socket either. This is why the six capabilities must never share '
    'a login -- Settings._validate_workspace_identities refuses that at startup.';

-- Do these two ISINs identify the same security, following only VERIFIED alias edges?
--
-- Same identity graph as app/db/security_alias.py, deliberately: a question asked about
-- Indosolar must still block a thesis published under WAAREEINDO, because it is the same
-- company and the ISIN never moved (CLAUDE.md rule 2). DRAFT aliases are excluded -- an
-- unverified identity claim must not silently merge two research histories.
--
-- UNION rather than UNION ALL, so the recursion terminates even on cyclic reference data.
-- app/db/security_alias.py rejects cycles at write; this is the belt to that braces, because
-- a guard that could hang on corrupt data is a guard that gets disabled.
CREATE FUNCTION ws_same_security(a text,b text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE family(isin) AS (
    SELECT a
    UNION
    SELECT CASE WHEN s.old_isin = f.isin THEN s.current_isin ELSE s.old_isin END
    FROM family f JOIN security_alias s
      ON (s.old_isin = f.isin OR s.current_isin = f.isin)
    WHERE s.verification = 'VERIFIED'
  ) SELECT EXISTS (SELECT 1 FROM family WHERE isin = b)
$$;

-- ---------------------------------------------------------------------------------
-- 2. Append-only, and the one authorised exception
-- ---------------------------------------------------------------------------------
--
-- EVERY UPDATE AND DELETE ON ALL TWENTY-FIVE TABLES IS REFUSED, except a narrowly authorised
-- erasure DELETE that must satisfy four independent conditions at once:
--
--   1. the connected role holds the eraser capability;
--   2. `app.erasure_authorised` is on for THIS transaction (set only by
--      app/db/workspace_context.py::authorise_erasure, never by ordinary context setup);
--   3. the row belongs to the tenant this transaction selected; and
--   4. no unreleased legal_hold covers that investor.
--
-- A normal role that sets the flag itself still fails on (1). The eraser without the flag
-- fails on (2). Either of them pointed at another tenant fails on (3). Both of them fail on
-- (4) while a dispute is open -- where a dispute has been raised, records are kept until it
-- resolves or SEBI directs otherwise.
--
-- NESTED IFs, NOT ONE `AND` CHAIN -- corrected from the blueprint draft, which wrote all four
-- as a single condition. PostgreSQL does not guarantee left-to-right short-circuit evaluation
-- of AND, and permission checks on a subquery's tables happen at executor startup for the
-- whole plan. A single chained condition therefore made the `legal_hold` read part of every
-- refused UPDATE by every role, so a runner attempting an UPDATE got "permission denied for
-- table legal_hold" instead of the intended append-only message -- and the fix would otherwise
-- have been to widen the legal_hold grant to all six roles, which is the wrong direction.
-- Nesting puts the hold query in its own statement, reached only by the eraser, so the narrow
-- grant in 045 is enough.
CREATE FUNCTION ws_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE'
     AND pg_has_role(current_user,'trackr_ws_eraser','member')
     AND current_setting('app.erasure_authorised',true) = 'on'
  THEN
    IF OLD.investor_id = ws_tenant() THEN
      IF NOT EXISTS (SELECT 1 FROM legal_hold
                     WHERE investor_id = OLD.investor_id AND released_at IS NULL)
      THEN RETURN OLD; END IF;
    END IF;
  END IF;
  RAISE EXCEPTION '% is append-only; % refused', TG_TABLE_NAME,TG_OP
    USING ERRCODE = 'restrict_violation';
END $$;

CREATE FUNCTION ws_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'workspace TRUNCATE refused' USING ERRCODE = 'restrict_violation';
END $$;

COMMENT ON FUNCTION ws_no_truncate() IS
    'TRUNCATE bypasses row triggers entirely, so append-only enforcement without a statement-'
    'level TRUNCATE trigger would be a DELETE guard with a one-word workaround.';

-- ---------------------------------------------------------------------------------
-- 3. The insert guard -- one function, twenty-five tables
-- ---------------------------------------------------------------------------------
--
-- ONE FUNCTION RATHER THAN TWENTY-FIVE, because the first third of it is identical for every
-- table (tenant match, owner attribution, server clock stamping) and that part must not be
-- possible to omit from one table by accident. The per-table branches then add what only that
-- table can check.
--
-- `creation_xid` IS SERVER-STAMPED AND IS NOT AN API FIELD. It carries the transaction id that
-- created the row, which is what makes a "sealed child collection" checkable: task context,
-- result citations and evaluation inputs may only be inserted while their parent's creating
-- transaction is still the current one. A caller-supplied value is overwritten here, so
-- repeating an old marker cannot reopen a seal.
--
-- RECEIPT TIMES ARE STAMPED, USER DATES ARE NOT. `recorded_at`, `created_at` and `reviewed_at`
-- are overwritten with the server clock; `written_on`, `answered_on`, `occurred_on`,
-- `publication_date`, `formed_on` and `version_on` are the person's own statements and are
-- left exactly as given. Backdating what you thought is legitimate; backdating when the server
-- heard it is not.
--
-- `clock_timestamp()` NOT `now()`. `now()` is the transaction start, so an acceptance that
-- inserts a note, links, a review and an audit row would stamp all four identically and the
-- order within the act would be unrecoverable.
CREATE FUNCTION ws_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_id uuid; attributed uuid; j jsonb; prior integer;
        n note%ROWTYPE; t task%ROWTYPE; a task_attempt%ROWTYPE;
        r task_result%ROWTYPE; ev trigger_evaluation%ROWTYPE;
        q question%ROWTYPE; s monitor_schedule%ROWTYPE; mr monitor_run%ROWTYPE;
BEGIN
  -- ONE ADVISORY LOCK PER TENANT, for the whole transaction. This is what serialises version
  -- allocation (thesis, skill, agent, schedule), attempt ordering, and the
  -- blocking-question-versus-thesis race -- without granting UPDATE on any existing row, which
  -- is the usual way people serialise and is unavailable here because nothing is updatable.
  --
  -- Repository reads take the SAME lock in a separate statement before reading, under READ
  -- COMMITTED, so a waiter sees the committed winner rather than its own stale snapshot: a
  -- lock acquired after a statement's snapshot was taken does not refresh that snapshot.
  --
  -- A hash collision between two tenants only over-serialises unrelated work. It cannot expose
  -- a row, because every check below independently compares investor_id.
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.investor_id::text,0));

  SELECT owner_user_id INTO owner_id FROM investor WHERE id = NEW.investor_id;
  IF NOT FOUND OR NEW.investor_id IS DISTINCT FROM ws_tenant() THEN
    RAISE EXCEPTION 'workspace tenant mismatch';
  END IF;

  -- A human write must be attributed to the authenticated owner of this investor. Background
  -- capabilities (runner, engine, scheduler) are not people and set no actor; ws_human() is
  -- false for them, so this does not apply -- and they cannot make it apply, because
  -- membership is decided by which credential opened the connection.
  IF ws_human() AND (ws_actor() IS NULL OR ws_actor() IS DISTINCT FROM owner_id) THEN
    RAISE EXCEPTION 'authenticated owner required';
  END IF;

  j := to_jsonb(NEW);
  attributed := COALESCE((j->>'actor_uid')::uuid,(j->>'reviewer_uid')::uuid,
                        (j->>'created_by')::uuid);
  -- THE CALLER DOES NOT GET TO SAY WHO DID IT. A request that could name its own actor could
  -- file a note in another user's name; the only accepted attribution is the one the
  -- authenticated session established.
  IF ws_human() AND attributed IS NOT NULL AND attributed IS DISTINCT FROM ws_actor() THEN
    RAISE EXCEPTION 'actor cannot be supplied by caller';
  END IF;

  -- Keys that do not exist on this table are ignored by jsonb_populate_record, so one
  -- statement serves all twenty-five rowtypes.
  NEW := jsonb_populate_record(NEW,jsonb_build_object(
    'creation_xid',txid_current(),'recorded_at',clock_timestamp(),
    'created_at',clock_timestamp(),'reviewed_at',clock_timestamp()));

  IF TG_TABLE_NAME = 'note' THEN
    -- A revision must be the same KIND as what it revises and must come after it. Otherwise a
    -- QUESTION could be "revised" into a DECISION, quietly rewriting what was committed to.
    IF NEW.previous_id IS NOT NULL THEN
      SELECT * INTO n FROM note WHERE investor_id = NEW.investor_id AND id = NEW.previous_id;
      IF NOT FOUND OR n.kind <> NEW.kind OR n.seq >= NEW.seq THEN
        RAISE EXCEPTION 'invalid note revision';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'question' THEN
    -- Only a human-authored QUESTION note can become a blocking question. A model does not
    -- get to decide what an investor must answer before forming a view.
    SELECT * INTO n FROM note WHERE investor_id = NEW.investor_id AND id = NEW.note_id;
    IF n.kind IS DISTINCT FROM 'QUESTION' OR n.authorship IS DISTINCT FROM 'HUMAN' THEN
      RAISE EXCEPTION 'question requires human QUESTION note';
    END IF;
    -- RITUAL QUESTIONS ARE SEALED AT CREATION. Adding a question to yesterday's ritual would
    -- rewrite what was asked before the view was formed, which is the one thing the ritual
    -- exists to fix in place. `creation_xid = txid_current()` is how "in the same transaction
    -- as its ritual" is checked, and the caller cannot forge it: it is stamped above.
    IF NEW.ritual_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM ritual WHERE investor_id = NEW.investor_id AND id = NEW.ritual_id
      AND creation_xid = txid_current()) THEN
      RAISE EXCEPTION 'ritual questions are sealed at creation';
    END IF;

  ELSIF TG_TABLE_NAME = 'question_answer' THEN
    -- A second FIRST answer would make the chain ambiguous and leave no defined earliest
    -- answer to return -- which is the answer this whole table exists to preserve.
    IF NEW.previous_id IS NULL AND EXISTS (
      SELECT 1 FROM question_answer WHERE investor_id = NEW.investor_id
      AND question_id = NEW.question_id) THEN
      RAISE EXCEPTION 'answer revision must name predecessor';
    END IF;

  ELSIF TG_TABLE_NAME = 'thesis' THEN
    -- Sequential versions, allocated under the tenant lock taken above. Two concurrent
    -- publishes cannot both become version 4: the second waits, re-reads and conflicts.
    SELECT COALESCE(max(version),0) INTO prior FROM thesis
      WHERE investor_id = NEW.investor_id AND isin = NEW.isin;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'thesis version conflict'; END IF;
    -- THE FALSIFIABILITY GATE. A thesis cannot be published while any blocking question for
    -- this company -- including one filed under a superseded ISIN -- has no answer. Applies to
    -- DRAFT too: saving an incomplete draft is fine, but not while the questions you told
    -- yourself to answer first are unanswered.
    IF EXISTS (SELECT 1 FROM question q0 WHERE q0.investor_id = NEW.investor_id
      AND ws_same_security(q0.isin,NEW.isin) AND q0.blocking
      AND NOT EXISTS (SELECT 1 FROM question_answer qa
        WHERE qa.investor_id = q0.investor_id AND qa.question_id = q0.id)) THEN
      RAISE EXCEPTION 'blocking question remains open';
    END IF;
    -- Formation date is immutable across versions: it is the one date an anniversary review is
    -- built on, and letting version 5 move it would rewrite when the commitment began.
    IF prior > 0 AND NEW.formed_on <> (SELECT formed_on FROM thesis
      WHERE investor_id = NEW.investor_id AND isin = NEW.isin ORDER BY version LIMIT 1) THEN
      RAISE EXCEPTION 'formation date is immutable';
    END IF;

  ELSIF TG_TABLE_NAME IN ('skill','agent','monitor_schedule') THEN
    -- Same sequential-version rule, keyed by (investor_id, id) rather than company.
    EXECUTE format('SELECT COALESCE(max(version),0) FROM %I WHERE investor_id=$1 AND id=$2',
      TG_TABLE_NAME) INTO prior USING NEW.investor_id,NEW.id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'configuration version conflict'; END IF;

  ELSIF TG_TABLE_NAME = 'thesis_event' THEN
    -- An override must name an evaluation that actually TRIGGERED. Overriding a condition that
    -- never fired is not a decision about anything, and recording one would make the decision
    -- journal unreadable.
    IF NEW.kind = 'OVERRIDE_EXIT_TRIGGER' THEN
      SELECT * INTO ev FROM trigger_evaluation
        WHERE investor_id = NEW.investor_id AND id = NEW.evaluation_id;
      IF ev.state IS DISTINCT FROM 'TRIGGERED' THEN
        RAISE EXCEPTION 'override must name triggered evaluation';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'task' THEN
    -- The scheduler may create ONLY system-attributed MONITOR tasks. It cannot open research
    -- work, and it cannot attribute anything to a person.
    IF pg_has_role(current_user,'trackr_ws_scheduler','member')
      AND (NEW.purpose<>'MONITOR' OR NEW.created_by IS NOT NULL) THEN
      RAISE EXCEPTION 'scheduler can create only system-attributed monitor tasks';
    END IF;
    -- A chat task and its thread must concern the same company; the FK alone only proves same
    -- tenant, and cross-company context release is exactly what the company gate prevents.
    IF NEW.chat_thread_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM chat_thread ct WHERE ct.investor_id=NEW.investor_id
      AND ct.id=NEW.chat_thread_id AND ct.isin=NEW.isin) THEN
      RAISE EXCEPTION 'chat task belongs to a different company';
    END IF;
    -- ANSWER_REVIEW reviews the investor's OWN answer, for THIS company (across verified
    -- aliases). Reviewing an answer you did not write, or one about another company, is a
    -- different operation wearing this one's name.
    IF NEW.answer_id IS NOT NULL THEN
      SELECT q0.* INTO q FROM question q0 JOIN question_answer qa
        ON qa.investor_id=q0.investor_id AND qa.question_id=q0.id
        WHERE qa.investor_id=NEW.investor_id AND qa.id=NEW.answer_id;
      IF NOT FOUND OR NOT ws_same_security(q.isin,NEW.isin) THEN
        RAISE EXCEPTION 'answer review requires own answer for this company';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'task_context' THEN
    -- SEAL. Context may only be added while the task's own creating transaction is current.
    IF (SELECT creation_xid FROM task WHERE investor_id=NEW.investor_id AND id=NEW.task_id)
       IS DISTINCT FROM txid_current() THEN
      RAISE EXCEPTION 'task context is sealed';
    END IF;

  ELSIF TG_TABLE_NAME = 'task_attempt' THEN
    -- THE LEASE IS THE SERVER'S. A client cannot grant itself an hour or backdate a start.
    NEW.started_at := clock_timestamp();
    NEW.lease_until := NEW.started_at + interval '10 minutes';
    -- No restart after a proposal or a review. A reviewed task is finished; a changed mind is
    -- a new task linked by parent_task_id, not a re-run that could overwrite the verdict.
    IF EXISTS (SELECT 1 FROM task_review WHERE investor_id=NEW.investor_id AND task_id=NEW.task_id)
      OR EXISTS (SELECT 1 FROM task_result WHERE investor_id=NEW.investor_id
        AND task_id=NEW.task_id AND outcome='PROPOSED') THEN
      RAISE EXCEPTION 'task already has a proposal or review';
    END IF;
    SELECT * INTO a FROM task_attempt WHERE investor_id=NEW.investor_id
      AND task_id=NEW.task_id ORDER BY attempt_no DESC LIMIT 1;
    -- One attempt in flight at a time: a previous attempt with no terminal result is still
    -- running, and two concurrent runs would race to be the single result.
    IF FOUND AND NOT EXISTS (SELECT 1 FROM task_result
      WHERE investor_id=NEW.investor_id AND attempt_id=a.id) THEN
      RAISE EXCEPTION 'previous attempt still running';
    END IF;
    IF NEW.attempt_no <> COALESCE(a.attempt_no,0)+1 THEN
      RAISE EXCEPTION 'attempt order conflict';
    END IF;

  ELSIF TG_TABLE_NAME = 'task_result' THEN
    NEW.finished_at := clock_timestamp();
    -- The scheduler's only authority over a result is to EXPIRE a dead lease. It cannot
    -- propose output, which is the whole reason it is a separate capability.
    IF pg_has_role(current_user,'trackr_ws_scheduler','member') AND NEW.outcome<>'EXPIRED' THEN
      RAISE EXCEPTION 'scheduler can only expire an attempt';
    END IF;
    SELECT * INTO a FROM task_attempt WHERE investor_id=NEW.investor_id AND id=NEW.attempt_id;
    -- Duration is COMPUTED from server clocks, never reported. A self-reported duration is a
    -- number with no source.
    NEW.duration_ms := trunc(extract(epoch FROM (NEW.finished_at-a.started_at))*1000)::bigint;
    IF NEW.finished_at < a.started_at THEN RAISE EXCEPTION 'result predates attempt'; END IF;
    -- A proposal arriving after its lease expired is refused: the service must append EXPIRED
    -- and retry, so a run that timed out cannot later resurrect itself as a success.
    IF NEW.outcome='PROPOSED' AND clock_timestamp() > a.lease_until THEN
      RAISE EXCEPTION 'late result refused; expire and retry';
    END IF;
    -- And the converse: an attempt cannot be declared expired while it is still inside its
    -- lease, which would let a sweep cancel work that is running fine.
    IF NEW.outcome='EXPIRED' AND clock_timestamp() < a.lease_until THEN
      RAISE EXCEPTION 'attempt has not expired';
    END IF;

  ELSIF TG_TABLE_NAME = 'result_citation' THEN
    -- SEAL. Citations cannot be added after the result committed -- otherwise an unsupported
    -- claim could be made to look supported by attaching evidence afterwards.
    IF (SELECT creation_xid FROM task_result WHERE investor_id=NEW.investor_id AND id=NEW.result_id)
       IS DISTINCT FROM txid_current() THEN
      RAISE EXCEPTION 'result citations are sealed';
    END IF;

  ELSIF TG_TABLE_NAME = 'evaluation_fact' THEN
    -- SEAL. The input set of a past evaluation is fixed, so a receipt cannot be justified
    -- retroactively by adding the facts that support its answer.
    IF (SELECT creation_xid FROM trigger_evaluation
        WHERE investor_id=NEW.investor_id AND id=NEW.evaluation_id)
       IS DISTINCT FROM txid_current() THEN
      RAISE EXCEPTION 'evaluation inputs are sealed';
    END IF;

  ELSIF TG_TABLE_NAME = 'task_review' THEN
    -- ONLY A HUMAN CAPABILITY REVIEWS, and only as itself. The runner -- which submitted the
    -- output -- cannot reach this table at all (no INSERT grant in 045) and would fail here
    -- too. That is the single most important separation in this schema: the thing that
    -- produced the answer must not be the thing that accepts it.
    IF NOT ws_human() OR NEW.reviewer_uid IS DISTINCT FROM ws_actor() THEN
      RAISE EXCEPTION 'only an authenticated human can review';
    END IF;
    SELECT * INTO r FROM task_result WHERE investor_id=NEW.investor_id AND id=NEW.result_id;
    IF r.outcome IS DISTINCT FROM 'PROPOSED' THEN RAISE EXCEPTION 'no reviewable proposal'; END IF;
    IF NEW.decision='ACCEPT' THEN
      -- A guardrail-blocked proposal is visible and reviewable, but can ONLY be rejected.
      IF r.guardrail_status <> 'PASS' THEN RAISE EXCEPTION 'blocked proposal'; END IF;
      SELECT * INTO t FROM task WHERE investor_id=NEW.investor_id AND id=NEW.task_id;
      SELECT * INTO n FROM note WHERE investor_id=NEW.investor_id AND id=NEW.accepted_note_id;
      -- ACCEPTANCE IS ATOMIC. The note must be new IN THIS TRANSACTION, for the same company,
      -- marked ACCEPTED_AGENT, attributed to this reviewer. Accepting cannot point at a note
      -- that already existed, and the note and the review commit together or neither does.
      IF NOT FOUND OR n.isin<>t.isin OR n.authorship<>'ACCEPTED_AGENT'
        OR n.actor_uid<>NEW.reviewer_uid OR n.creation_xid<>txid_current() THEN
        RAISE EXCEPTION 'acceptance needs a new attributed note in this transaction';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'chat_message' THEN
    -- The runner cannot author a human turn. Otherwise untrusted output could inject a
    -- "question the user asked" and then answer it.
    IF NEW.role='HUMAN' AND NOT ws_human() THEN
      RAISE EXCEPTION 'runner cannot author a human chat turn';
    END IF;
    -- An assistant turn must reference a PROPOSED result from a CHAT task bound to THIS thread
    -- and the thread's own company. Four separate ways of borrowing someone else's answer,
    -- closed together.
    IF NEW.role='ASSISTANT' THEN
      SELECT * INTO r FROM task_result WHERE investor_id=NEW.investor_id AND id=NEW.result_id;
      SELECT * INTO t FROM task WHERE investor_id=NEW.investor_id AND id=r.task_id;
      IF r.outcome IS DISTINCT FROM 'PROPOSED' OR t.purpose IS DISTINCT FROM 'CHAT'
        OR t.chat_thread_id IS DISTINCT FROM NEW.thread_id
        OR NOT EXISTS (SELECT 1 FROM chat_thread ct WHERE ct.investor_id=NEW.investor_id
          AND ct.id=NEW.thread_id AND ct.isin=t.isin) THEN
        RAISE EXCEPTION 'chat result mismatch';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'monitor_run' THEN
    SELECT * INTO s FROM monitor_schedule WHERE investor_id=NEW.investor_id
      AND id=NEW.schedule_id AND version=NEW.schedule_version;
    -- The run must be about what the schedule version actually committed to watching.
    IF s.isin IS DISTINCT FROM NEW.isin OR s.thesis_id IS DISTINCT FROM NEW.thesis_id THEN
      RAISE EXCEPTION 'schedule commitment mismatch';
    END IF;
    -- AN EVENT MONITOR NEEDS VERIFIED EVIDENCE THAT THE EVENT HAPPENED. A rumour, an
    -- UNVERIFIED extraction or a model's belief that results were announced does not queue
    -- work; that is what BLOCKED_EVENT is for.
    IF NEW.disposition='QUEUED' AND s.kind<>'SCHEDULED' AND NOT EXISTS (
      SELECT 1 FROM research_fact f WHERE f.investor_id=NEW.investor_id
      AND f.id=NEW.event_fact_id AND f.verification='VERIFIED' AND f.boolean_value=true) THEN
      RAISE EXCEPTION 'event has no verified evidence';
    END IF;
    IF NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task t0
      WHERE t0.investor_id=NEW.investor_id AND t0.id=NEW.task_id
      AND t0.purpose='MONITOR' AND t0.thesis_id=NEW.thesis_id) THEN
      RAISE EXCEPTION 'monitor task mismatch';
    END IF;

  ELSIF TG_TABLE_NAME = 'monitor_report' THEN
    SELECT * INTO mr FROM monitor_run WHERE investor_id=NEW.investor_id AND id=NEW.run_id;
    SELECT * INTO r FROM task_result WHERE investor_id=NEW.investor_id AND id=NEW.result_id;
    SELECT * INTO ev FROM trigger_evaluation
      WHERE investor_id=NEW.investor_id AND id=NEW.evaluation_id;
    -- A BLOCKED OCCURRENCE CANNOT YIELD A REPORT. Every link must agree: the run was queued,
    -- the result belongs to the run's task, it actually proposed something, it passed
    -- guardrails, and the evaluation is of the run's own thesis. Without the last one, a
    -- report could narrate one thesis while citing another's arithmetic.
    IF mr.disposition IS DISTINCT FROM 'QUEUED' OR mr.task_id IS DISTINCT FROM r.task_id
      OR r.outcome IS DISTINCT FROM 'PROPOSED' OR r.guardrail_status IS DISTINCT FROM 'PASS'
      OR ev.thesis_id IS DISTINCT FROM mr.thesis_id THEN
      RAISE EXCEPTION 'monitor report inputs do not match';
    END IF;
  END IF;

  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------------
-- 4. Deferred guards -- the two things only COMMIT can answer
-- ---------------------------------------------------------------------------------
--
-- Both of these are about a row whose validity depends on a SIBLING inserted afterwards, so
-- neither can be checked at INSERT time. A constraint trigger that is DEFERRABLE INITIALLY
-- DEFERRED fires at commit, which is the only moment the question is meaningful.
--
-- This is also why acceptance is genuinely atomic rather than nearly atomic: the note guard
-- and the review guard point at each other, so a transaction that writes one without the
-- other cannot commit in either order.
CREATE FUNCTION ws_deferred_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME='ritual' THEN
    -- A ritual with no questions is an empty ceremony. Completion means recorded questions.
    IF NOT EXISTS (SELECT 1 FROM question
      WHERE investor_id=NEW.investor_id AND ritual_id=NEW.id) THEN
      RAISE EXCEPTION 'ritual needs at least one saved human question';
    END IF;
  ELSIF TG_TABLE_NAME='note' THEN
    -- The inverse of the acceptance check in ws_insert_guard: an ACCEPTED_AGENT note with no
    -- accepting review is an orphan, i.e. model text that entered the notebook without anyone
    -- agreeing to it. Checking both directions is what makes that unreachable.
    IF NEW.authorship='ACCEPTED_AGENT' AND NOT EXISTS (
      SELECT 1 FROM task_review WHERE investor_id=NEW.investor_id
      AND accepted_note_id=NEW.id AND decision='ACCEPT') THEN
      RAISE EXCEPTION 'agent note needs explicit human acceptance';
    END IF;
  END IF;
  RETURN NULL;
END $$;

-- ---------------------------------------------------------------------------------
-- 5. Install on all twenty-five tables
-- ---------------------------------------------------------------------------------
--
-- THE LIST IS THE COMPLETE INVENTORY, not just root tables. A child table left off would be
-- an editable corner of an append-only schema, and `result_citation` or `evaluation_fact`
-- being editable is as bad as `note` being editable -- it would let the evidence behind a
-- claim be changed after the claim was accepted. The same twenty-five names appear in 045.
DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'company','research_source','research_fact','note','note_link','ritual','question',
    'question_answer','thesis','trigger_evaluation','evaluation_fact','thesis_event',
    'skill','agent','task','task_context','task_attempt','task_result','result_citation',
    'task_review','chat_thread','chat_message','monitor_schedule','monitor_run','monitor_report'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN creation_xid bigint NOT NULL DEFAULT txid_current()',tbl);
    EXECUTE format('CREATE TRIGGER ws_insert BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION ws_insert_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ws_append_only()',tbl);
    EXECUTE format('CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate()',tbl);
  END LOOP;
END $$;

CREATE CONSTRAINT TRIGGER ritual_complete AFTER INSERT ON ritual
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ws_deferred_guard();
CREATE CONSTRAINT TRIGGER note_acceptance AFTER INSERT ON note
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ws_deferred_guard();
