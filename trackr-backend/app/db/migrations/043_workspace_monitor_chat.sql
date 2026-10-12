-- 043_workspace_monitor_chat.sql -- chat history, monitor schedules, due occurrences, reports.
--
-- FOURTH OF SIX (040-045). This file completes the twenty-five-table inventory. It is still
-- the LAST file with no enforcement: 044 installs the triggers, 045 installs RLS and grants.
-- A deployment stopping here would hold the whole research schema writable by anything that
-- can reach it, which is why `app/db/migrate.py` refuses a partial bundle.
--
-- TWO SUBJECTS, ONE FILE, BECAUSE THEY SHARE A DEPENDENCY. Chat needs `task_result` (042) and
-- closes 042's forward reference to `chat_thread`; monitors need theses (041), skills/agents
-- (042) and tasks (042). Splitting them would add a fifth intermediate state with no benefit.
--
-- BE-Q01 IS STILL OPEN AND THIS FILE DOES NOT CLOSE IT. The honest position the schema takes:
-- a due occurrence can be recorded as BLOCKED_EXECUTION_POLICY, with no task and a stated
-- reason. It cannot be recorded as a completed report that nothing produced. Catch-up
-- semantics, closed-browser execution, provider spending limits and notification channels all
-- remain undecided, and nothing here quietly picks a default for them.

-- ---------------------------------------------------------------------------------
-- chat_thread / chat_message -- the conversation, with no second copy of the answer
-- ---------------------------------------------------------------------------------
--
-- AN ASSISTANT TURN STORES NO BODY. `CHECK (role <> 'ASSISTANT' OR body = '')` with
-- `(role = 'ASSISTANT') = (result_id IS NOT NULL)`: the text is READ THROUGH the linked
-- `task_result`. That is not normalisation for its own sake. A copy of the answer in the chat
-- row would be an authoritative-looking string that had drifted from the result it came from
-- -- and from the result's citations, its guardrail status and its review. There is exactly
-- one place the model's words live, and everything that judges them judges that place.
--
-- `UNIQUE (investor_id, result_id)` means one result can appear as at most one assistant turn:
-- the same proposal cannot be replayed into a thread twice to look like two answers.
--
-- REFUSAL IS ITS OWN ROLE, and it carries a body while the assistant does not. A refusal is
-- the system's own words -- "you have not answered your own question about this company yet"
-- -- not a provider's, so there is no result to reference and the text belongs here.
--
-- NO STORED CHAIN OF THOUGHT, anywhere in this file. A short evidence-use summary shown in
-- the UI is derived from the result's citations, not from hidden reasoning text.
CREATE TABLE chat_thread (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  title ws_text NOT NULL,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company
);

CREATE TABLE chat_message (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  thread_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('HUMAN','ASSISTANT','REFUSAL')),
  body text NOT NULL DEFAULT '',
  actor_uid uuid REFERENCES app_user(id),
  result_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  UNIQUE (investor_id,result_id),
  FOREIGN KEY (investor_id,thread_id) REFERENCES chat_thread,
  FOREIGN KEY (investor_id,result_id) REFERENCES task_result,
  CHECK ((role = 'ASSISTANT') = (result_id IS NOT NULL)),
  CHECK (role <> 'HUMAN' OR (actor_uid IS NOT NULL AND length(btrim(body)) > 0)),
  CHECK (role <> 'ASSISTANT' OR body = ''),
  CHECK (role <> 'REFUSAL' OR length(btrim(body)) > 0)
);

COMMENT ON TABLE chat_message IS
    'One conversation turn. An ASSISTANT turn stores NO body -- the text is read through its '
    'linked task_result, so there is exactly one place the model''s words live and everything '
    'that judges them (citations, guardrail status, review) judges that place. A copy here '
    'would be an authoritative-looking string that had drifted from all of it. REFUSAL '
    'carries a body because those are the system''s own words, not a provider''s.';

-- Close 042's forward reference. A CHAT task must name a thread, and 044 additionally checks
-- the thread belongs to the same company as the task.
ALTER TABLE task ADD CONSTRAINT task_chat_thread_fk
  FOREIGN KEY (investor_id,chat_thread_id) REFERENCES chat_thread;

-- ---------------------------------------------------------------------------------
-- monitor_schedule -- a commitment to keep checking a specific thesis version
-- ---------------------------------------------------------------------------------
--
-- A SCHEDULE IS BOUND TO A THESIS, AND REBINDING IS AN EXPLICIT NEW VERSION. If a person
-- publishes thesis version 4, the schedule watching version 3 does NOT silently follow: a
-- worker must never quietly swap the object being monitored, because "your exit condition has
-- not fired" would then be a statement about a condition the person never committed to. The
-- stale case is recorded honestly as `SUPERSEDED_THESIS` on the run, not papered over.
--
-- `(kind = 'SCHEDULED') = (interval_days IS NOT NULL)` -- a calendar schedule needs a period
-- and an event-driven one must not pretend to have one. POST_RESULTS and POST_CONCALL fire
-- when verified evidence of the event exists, which 044 checks; they are not timers.
--
-- `timezone` DEFAULTS TO Asia/Kolkata and is validated as an IANA zone at the service
-- boundary, not here -- a CHECK against the tzdata set would bake a moving list into DDL.
-- Intervals mean local calendar days at the original local wall time; fold and gap policy is
-- a deterministic service contract, and this column exists so that contract has an input
-- rather than a guess.
--
-- ENABLED IS A COLUMN ON A VERSION, NOT A MUTABLE SWITCH. Pausing is a new version with
-- enabled=false, so the history says when it was paused and by whom. 044 allocates the
-- versions sequentially.
CREATE TABLE monitor_schedule (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version integer NOT NULL CHECK (version > 0),
  isin text NOT NULL,
  thesis_id uuid NOT NULL,
  skill_id uuid NOT NULL,
  skill_version integer NOT NULL,
  agent_id uuid NOT NULL,
  agent_version integer NOT NULL,
  kind text NOT NULL CHECK (kind IN ('SCHEDULED','POST_RESULTS','POST_CONCALL')),
  timezone ws_text NOT NULL DEFAULT 'Asia/Kolkata',
  starts_at timestamptz NOT NULL,
  interval_days integer CHECK (interval_days > 0),
  enabled boolean NOT NULL DEFAULT true,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id,version),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin,thesis_id) REFERENCES thesis(investor_id,isin,id),
  FOREIGN KEY (investor_id,skill_id,skill_version) REFERENCES skill,
  FOREIGN KEY (investor_id,agent_id,agent_version) REFERENCES agent,
  CHECK ((kind = 'SCHEDULED') = (interval_days IS NOT NULL))
);

COMMENT ON TABLE monitor_schedule IS
    'A versioned commitment to keep checking one specific thesis version. Rebinding to a new '
    'thesis requires an explicit new schedule version: a worker must never silently swap the '
    'object being monitored, or "your exit condition has not fired" becomes a statement about '
    'a condition nobody committed to. Pausing is a new version with enabled=false, so the '
    'history records when and by whom.';

-- ---------------------------------------------------------------------------------
-- monitor_run -- one due occurrence, and the truth about what happened to it
-- ---------------------------------------------------------------------------------
--
-- A DUE OCCURRENCE IS NOT A REPORT. This row says "this schedule came due at this moment".
-- Whether anything ran is `disposition`, and three of its four values mean nothing ran:
--   QUEUED                     -- a task exists (biconditional with task_id)
--   BLOCKED_EXECUTION_POLICY   -- BE-Q01: no supported way to execute with a browser-only key
--   BLOCKED_EVENT              -- the event this schedule waits for has no verified evidence
--   SUPERSEDED_THESIS          -- the thesis this schedule was bound to is no longer in force
--
-- `CHECK (disposition = 'QUEUED' OR length(btrim(reason)) > 0)` -- every non-run states why,
-- in words a person reads. A blocked monitor that showed as simply "no alerts" would be the
-- most dangerous screen in this product: silence that looks like reassurance.
--
-- `UNIQUE (investor_id, schedule_id, due_at)` is the catch-up guard: one occurrence per
-- schedule per due moment, across schedule versions, so a duplicate tick or a replayed
-- worker cannot manufacture a second run of the same moment.
--
-- `UNIQUE (investor_id, task_id)` -- a task belongs to at most one run, so two occurrences
-- cannot claim the same piece of work.
CREATE TABLE monitor_run (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  schedule_id uuid NOT NULL,
  schedule_version integer NOT NULL,
  thesis_id uuid NOT NULL,
  -- When it came due. The actual execution time, if anything executed, is on the attempt --
  -- 044 keeps the two separate rather than letting a late run look punctual.
  due_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  event_fact_id uuid,
  task_id uuid,
  disposition text NOT NULL CHECK (disposition IN
    ('QUEUED','BLOCKED_EXECUTION_POLICY','BLOCKED_EVENT','SUPERSEDED_THESIS')),
  reason text NOT NULL DEFAULT '',
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,task_id),
  UNIQUE (investor_id,schedule_id,due_at),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,schedule_id,schedule_version) REFERENCES monitor_schedule,
  FOREIGN KEY (investor_id,isin,thesis_id) REFERENCES thesis(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,event_fact_id) REFERENCES research_fact(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,task_id) REFERENCES task(investor_id,isin,id),
  CHECK ((disposition = 'QUEUED') = (task_id IS NOT NULL)),
  CHECK (disposition = 'QUEUED' OR length(btrim(reason)) > 0)
);

COMMENT ON TABLE monitor_run IS
    'One due occurrence and the truth about what happened to it. A due occurrence is NOT a '
    'report: three of the four dispositions mean nothing ran, and each of those requires a '
    'stated reason, because a blocked monitor showing as "no alerts" would be silence that '
    'looks like reassurance. UNIQUE (investor_id, schedule_id, due_at) is the catch-up guard '
    'across schedule versions. While BE-Q01 is open, scheduled occurrences record '
    'BLOCKED_EXECUTION_POLICY rather than queueing a pretend model execution.';

-- ---------------------------------------------------------------------------------
-- monitor_report -- one immutable report per run
-- ---------------------------------------------------------------------------------
--
-- THE BODY IS NOT HERE. Only a title and a lede; the ordered sections live in the linked
-- result's closed schema (material_change, why_it_matters, original_commitment,
-- supported_or_challenged, deterministic_conditions, uncertainties, next_questions). Same
-- reason as chat_message: one place for the model's words, and everything that validates them
-- validates that place.
--
-- `evaluation_id` IS MANDATORY. A report always names the deterministic evaluation of the
-- investor's own conditions, so prose and arithmetic sit side by side and a reader can tell
-- which is which. 044 verifies the evaluation belongs to the run's thesis; without that, a
-- report could narrate one thesis while citing another's receipt.
--
-- INSERTING A REPORT IS NOT A DECISION. It writes no task_review and no thesis_event. Being
-- told something changed is not the same as deciding what to do, and the schema does not let
-- the first become the second.
CREATE TABLE monitor_report (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL,
  result_id uuid NOT NULL,
  evaluation_id uuid NOT NULL,
  title ws_text NOT NULL,
  lede ws_text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,run_id),
  UNIQUE (investor_id,result_id),
  FOREIGN KEY (investor_id,run_id) REFERENCES monitor_run,
  FOREIGN KEY (investor_id,result_id) REFERENCES task_result,
  FOREIGN KEY (investor_id,evaluation_id) REFERENCES trigger_evaluation
);

COMMENT ON TABLE monitor_report IS
    'One immutable report per run. Title and lede only -- the ordered sections live in the '
    'linked result''s closed schema, so the model''s words have one home. evaluation_id is '
    'mandatory so prose and deterministic arithmetic sit side by side and a reader can tell '
    'which is which. Inserting a report writes no review and no decision event: being told '
    'something changed is not deciding what to do.';

CREATE INDEX chat_history ON chat_message(investor_id,thread_id,seq);
CREATE INDEX monitor_history ON monitor_run(investor_id,isin,due_at DESC);
CREATE INDEX schedule_versions ON monitor_schedule(investor_id,id,version DESC);
