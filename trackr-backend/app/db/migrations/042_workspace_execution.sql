-- 042_workspace_execution.sql -- methods, agent configurations, tasks, attempts, reviewed output.
--
-- THIRD OF SIX (040-045). Still no triggers, no RLS, no grants -- see 040's header.
--
-- WHAT THIS FILE MODELS: a piece of work handed to a model, and what came back. The single
-- idea it is built around is that **model output is a PROPOSAL until a person accepts it.**
-- Nothing a provider returns is a fact, a note, a decision or a number this system will
-- stand behind. `task_result` holds untrusted text; `task_review` is the human act that
-- turns one into a note, and it is the only thing that can.
--
-- THERE IS NO CREDENTIAL COLUMN IN THIS FILE, AND THERE WILL NOT BE ONE. No api_key, no
-- encrypted_key, no auth headers, no generic "config" jsonb that could hold one, no request
-- envelope. The provider key lives in the user's browser and is sent from the browser to the
-- provider the user chose. The server never holds it, never proxies it and never validates
-- it. `make lint` fails the build on a credential column, and `agent.base_url` is
-- additionally constrained so a URL cannot smuggle authentication in its userinfo.
--
-- THERE IS ALSO NO `status` COLUMN ON `task`. The five states -- assigned, running, review,
-- done, rejected -- are DERIVED from which attempt/result/review rows exist, in the order
-- documented below. A status column would be a second answer to that question and would be
-- the one that lied: a task marked `done` whose review row was rolled back is precisely the
-- forged acceptance this schema spends two migrations preventing.
--
-- BE-Q01 REMAINS OPEN. Nothing here settles how scheduled research runs when the key is only
-- in a browser. The schema can represent a blocked occurrence honestly (043's
-- `monitor_run.disposition`); it cannot represent a completed run that did not happen.

-- ---------------------------------------------------------------------------------
-- skill -- a research method, versioned, with its scope written down
-- ---------------------------------------------------------------------------------
--
-- A SKILL IS A CONTRACT, NOT A PROMPT. It declares what the method reads, which tools it may
-- use, how it must cite, what it is FORBIDDEN to conclude, and the exact shape of its output.
-- `forbidden_conclusions` is mandatory and non-empty because that is the part a prompt always
-- omits: "never recommend a trade", "never assert a number you did not read from a cited
-- source". A method with no stated limits has no scope at all.
--
-- VERSIONED, AND PINNED BY EVERY TASK. Changing a method creates version n+1; the tasks
-- already run keep pointing at the version that actually ran. Otherwise "why did it conclude
-- that" becomes unanswerable the moment a method is edited. 044 allocates the versions
-- sequentially.
--
-- `permitted_tools` MAY be empty -- a purely interpretive method that reads only what was
-- pinned into its context is legitimate. `reads` and `citation_rules` may not be.
CREATE TABLE skill (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version integer NOT NULL CHECK (version > 0),
  name ws_text NOT NULL,
  summary ws_text NOT NULL,
  method ws_text NOT NULL,
  reads text[] NOT NULL CHECK (cardinality(reads) > 0),
  citation_rules text[] NOT NULL CHECK (cardinality(citation_rules) > 0),
  permitted_tools text[] NOT NULL,
  forbidden_conclusions text[] NOT NULL CHECK (cardinality(forbidden_conclusions) > 0),
  output_contract jsonb NOT NULL CHECK (
    jsonb_typeof(output_contract) = 'object' AND ws_string_numbers(output_contract)),
  house_rules_version ws_text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id,version),
  UNIQUE (investor_id,dedupe_key)
);

COMMENT ON TABLE skill IS
    'A research method as a versioned contract: what it reads, which tools it may use, how '
    'it must cite, what it is forbidden to conclude, and its exact output shape. '
    'forbidden_conclusions is mandatory and non-empty because that is the part a prompt '
    'always omits. Every task pins a version, so "why did it conclude that" stays answerable '
    'after the method is edited.';

-- ---------------------------------------------------------------------------------
-- agent -- which model, at which endpoint, running which method version
-- ---------------------------------------------------------------------------------
--
-- CONFIGURATION ONLY. Provider, model name, base URL, a role description, and the pinned
-- skill version. Nothing that could authenticate: no key, no token, no header map, no
-- arbitrary JSON. This row describes WHERE the browser should send a request, never HOW to
-- prove it may.
--
-- THE base_url REGEX IS PART OF THE SECURITY BOUNDARY, not formatting. It admits
-- `https://host[:port][/path]` and nothing else, which rules out:
--   * `https://user:pass@host/` -- userinfo, i.e. a credential in a config column, the exact
--     thing this file forbids by having no credential column;
--   * `?key=...` and `#...` -- a query or fragment, the other two places a secret hides in
--     a URL;
--   * `http://` -- plaintext to a model provider.
-- Percent-encoding is permitted in the path (`%` is in the class) because legitimate paths
-- contain it; a percent-DECODED path can still smuggle a separator, so the endpoint policy at
-- the API boundary validates the decoded form too. This constraint is the floor, not the
-- whole check.
--
-- ENABLED IS NOT READINESS. A row here says a user configured an agent. It does not say a key
-- exists, the endpoint is reachable, or the model will answer. The server cannot know any of
-- that -- it never talks to the provider.
CREATE TABLE agent (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version integer NOT NULL CHECK (version > 0),
  name ws_text NOT NULL,
  provider text NOT NULL CHECK (provider IN ('ANTHROPIC','OPENAI_COMPATIBLE')),
  model ws_text NOT NULL,
  base_url text NOT NULL CHECK (
    base_url ~ '^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/%-]*)?$'),
  role_description ws_text NOT NULL,
  skill_id uuid NOT NULL,
  skill_version integer NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id,version),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,skill_id,skill_version) REFERENCES skill
);

COMMENT ON COLUMN agent.base_url IS
    'HTTPS host/port/path only. The regex is a security boundary, not formatting: it rules '
    'out userinfo, query strings and fragments -- the three places a credential hides in a '
    'URL -- in a file that deliberately has no credential column at all. Percent-encoding is '
    'allowed in the path, so the decoded form is additionally validated at the API boundary; '
    'this constraint is the floor, not the whole check.';

-- ---------------------------------------------------------------------------------
-- task -- one unit of work, against context frozen at creation
-- ---------------------------------------------------------------------------------
--
-- CONTEXT IS SEALED. `context_cutoff`, `context_digest` and the `task_context` rows record
-- exactly what evidence this work was given, and 044 closes that collection when the creating
-- transaction commits. New evidence does not extend an old task -- it means a NEW task, linked
-- by `parent_task_id`. Otherwise "what did it actually look at" has no answer, and a result
-- could be justified after the fact by adding the sources that support it.
--
-- PURPOSE DECIDES WHICH REFERENCE IS REQUIRED, biconditionally where that is the honest
-- shape. An ANSWER_REVIEW with no answer to review is meaningless; so is a non-ANSWER_REVIEW
-- carrying one. Hence `(purpose = 'ANSWER_REVIEW') = (answer_id IS NOT NULL)` rather than a
-- one-way implication.
--
-- `created_by` IS NULL ONLY FOR MONITOR. A scheduled occurrence is genuinely not a person's
-- act, and attributing it to the account owner would be a small lie that makes the audit
-- trail useless. Every other purpose requires a human. 044 additionally refuses to let the
-- scheduler role create anything but a system-attributed MONITOR task.
CREATE TABLE task (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN
    ('RESEARCH','ANSWER_REVIEW','CHAT','INLINE','MONITOR')),
  title ws_text NOT NULL,
  brief ws_text NOT NULL,
  skill_id uuid NOT NULL,
  skill_version integer NOT NULL,
  agent_id uuid NOT NULL,
  agent_version integer NOT NULL,
  thesis_id uuid,
  answer_id uuid,
  -- No FK yet: chat_thread arrives in 043, which adds task_chat_thread_fk.
  chat_thread_id uuid,
  parent_task_id uuid,
  created_by uuid REFERENCES app_user(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  context_cutoff timestamptz NOT NULL DEFAULT now(),
  context_digest text NOT NULL CHECK (context_digest ~ '^[a-f0-9]{64}$'),
  context_manifest jsonb NOT NULL DEFAULT '{}' CHECK (
    jsonb_typeof(context_manifest) = 'object' AND ws_string_numbers(context_manifest)),
  house_rules_version ws_text NOT NULL,
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  -- Version-pinned: a task names the exact skill and agent version that ran, so editing
  -- either afterwards cannot rewrite the history of work already done.
  FOREIGN KEY (investor_id,skill_id,skill_version) REFERENCES skill,
  FOREIGN KEY (investor_id,agent_id,agent_version) REFERENCES agent,
  FOREIGN KEY (investor_id,isin,thesis_id) REFERENCES thesis(investor_id,isin,id),
  FOREIGN KEY (investor_id,answer_id) REFERENCES question_answer,
  FOREIGN KEY (investor_id,parent_task_id) REFERENCES task,
  CHECK ((purpose = 'ANSWER_REVIEW') = (answer_id IS NOT NULL)),
  CHECK ((purpose = 'CHAT') = (chat_thread_id IS NOT NULL)),
  CHECK (purpose <> 'MONITOR' OR thesis_id IS NOT NULL),
  CHECK (purpose = 'MONITOR' OR created_by IS NOT NULL),
  CHECK (parent_task_id IS NULL OR parent_task_id <> id),
  -- Context cannot be newer than the task. A cutoff in the future would claim the work saw
  -- evidence that did not exist when it was created.
  CHECK (context_cutoff <= created_at)
);

COMMENT ON TABLE task IS
    'One unit of model work against context frozen at creation. Deliberately no status '
    'column: the five states (assigned/running/review/done/rejected) are derived from which '
    'attempt, result and review rows exist. A status column would be a second answer to that '
    'question and would be the one that lied -- a task marked done whose review was rolled '
    'back is exactly the forged acceptance this schema prevents. New evidence means a new '
    'task linked by parent_task_id, never an extended old one.';

-- Which records were pinned into one task's context, in order. Exactly one typed target per
-- row: a context entry that pointed at both a note and a fact would be two entries sharing
-- one ordinal, and citations reference entries by identity.
CREATE TABLE task_context (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  note_id uuid,
  thesis_id uuid,
  answer_id uuid,
  source_id uuid,
  fact_id uuid,
  evaluation_id uuid,
  PRIMARY KEY (investor_id,id),
  -- The task-scoped identity result_citation references, so a citation cannot point at a
  -- context entry belonging to a different task.
  UNIQUE (investor_id,task_id,id),
  UNIQUE (investor_id,task_id,ordinal),
  FOREIGN KEY (investor_id,task_id) REFERENCES task,
  FOREIGN KEY (investor_id,note_id) REFERENCES note,
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis,
  FOREIGN KEY (investor_id,answer_id) REFERENCES question_answer,
  FOREIGN KEY (investor_id,source_id) REFERENCES research_source,
  FOREIGN KEY (investor_id,fact_id) REFERENCES research_fact,
  FOREIGN KEY (investor_id,evaluation_id) REFERENCES trigger_evaluation,
  CHECK (num_nonnulls(note_id,thesis_id,answer_id,source_id,fact_id,evaluation_id) = 1)
);

-- ---------------------------------------------------------------------------------
-- task_attempt -- one try, with a bounded lease
-- ---------------------------------------------------------------------------------
--
-- SAMPLE PROVENANCE IS EXPLICIT AND PERMANENT. `mode` is BROWSER_MODEL or STORED_SAMPLE, and
-- the biconditional with `sample_ref` means a sample run can never be mistaken for a real one
-- or vice versa. This exists because the tempting failure is precise: a user with no provider
-- key configured, and a server that quietly serves a fixture so the screen looks alive. That
-- is a fabricated research result. **A missing key is an error, never a sample fallback.**
--
-- BROWSER_MODEL MEANS BROWSER-REPORTED, NOT ATTESTED. The server did not make the provider
-- call and cannot prove one happened. The mode records what the browser claims; it is not
-- independent evidence, and the UI must not present it as such.
--
-- THE LEASE IS THE SERVER'S, NOT THE CALLER'S. 044 overwrites `started_at` and `lease_until`
-- with server clock values, so a client cannot grant itself an hour or backdate a start. Here
-- the schema only guarantees the ordering.
CREATE TABLE task_attempt (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  mode text NOT NULL CHECK (mode IN ('BROWSER_MODEL','STORED_SAMPLE')),
  sample_ref ws_text,
  started_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz NOT NULL,
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,task_id,id),
  UNIQUE (investor_id,task_id,attempt_no),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,task_id) REFERENCES task,
  CHECK (lease_until > started_at),
  CHECK ((mode = 'STORED_SAMPLE') = (sample_ref IS NOT NULL))
);

COMMENT ON COLUMN task_attempt.mode IS
    'BROWSER_MODEL | STORED_SAMPLE, paired biconditionally with sample_ref so the two can '
    'never be confused. BROWSER_MODEL means browser-REPORTED invocation, never independent '
    'attestation -- the server does not make the provider call and cannot prove one happened. '
    'A missing provider key is an error, never a silent sample fallback: serving a fixture so '
    'the screen looks alive would be a fabricated research result.';

-- ---------------------------------------------------------------------------------
-- task_result -- exactly one terminal outcome per attempt
-- ---------------------------------------------------------------------------------
--
-- THE THREE BICONDITIONALS ARE THE CONTRACT, and each closes a specific way of lying:
--   * `(outcome = 'PROPOSED') = (output non-blank)` -- a failure cannot carry output, and a
--     proposal cannot be empty. Failure, cancellation and expiry do not invent a result.
--   * `(outcome = 'PROPOSED') = (error_code IS NULL)` -- and its converse: a failure must say
--     which failure, from a closed list of REDACTED codes. Never a provider traceback, never
--     response headers, never an exception body that might quote a key.
--   * `outcome = 'PROPOSED' OR guardrail_status = 'NOT_APPLICABLE'` -- there is nothing to
--     screen when nothing was produced, so a failed attempt cannot claim to have PASSED.
--
-- `UNIQUE (investor_id, attempt_id)` IS THE ANTI-OVERWRITE RULE. One attempt, one terminal
-- result, forever. A retry is a new attempt with its own result; the previous one stays
-- readable. Nothing in this schema can overwrite a result, which is what makes a rejection
-- durable evidence rather than a thing that disappeared.
--
-- A BLOCKED PROPOSAL IS STILL REVIEWABLE -- and can only be REJECTED. 044 refuses an ACCEPT
-- whose result did not pass guardrails. It is kept visible rather than hidden, because a
-- person should be able to see what was proposed and why it was stopped.
--
-- `structured_output` HOLDS NO AUTHORITATIVE NUMBER. Purpose-specific closed schemas go here
-- -- cited claims, answer-review ok/gap arrays, monitor sections -- and every scalar in them
-- is a string (ws_string_numbers). No computed holding, valuation or trigger state produced
-- by a model is ever read from this column as truth.
CREATE TABLE task_result (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('PROPOSED','FAILED','CANCELLED','EXPIRED')),
  output text NOT NULL DEFAULT '',
  structured_output jsonb NOT NULL DEFAULT '{}' CHECK (
    jsonb_typeof(structured_output) = 'object' AND ws_string_numbers(structured_output)),
  guardrail_status text NOT NULL CHECK (guardrail_status IN ('PASS','BLOCKED','NOT_APPLICABLE')),
  error_code text CHECK (error_code IN
    ('PROVIDER_UNREACHABLE','AUTH_FAILED','RATE_LIMITED','CLIENT_CLOSED',
     'LEASE_EXPIRED','INVALID_OUTPUT','USER_CANCELLED')),
  finished_at timestamptz NOT NULL DEFAULT now(),
  duration_ms bigint NOT NULL CHECK (duration_ms >= 0),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,task_id,id),
  UNIQUE (investor_id,attempt_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,task_id,attempt_id)
    REFERENCES task_attempt(investor_id,task_id,id),
  CHECK ((outcome = 'PROPOSED') = (length(btrim(output)) > 0)),
  CHECK ((outcome = 'PROPOSED') = (error_code IS NULL)),
  CHECK (outcome = 'PROPOSED' OR guardrail_status = 'NOT_APPLICABLE')
);

COMMENT ON TABLE task_result IS
    'Exactly one terminal outcome per attempt, immutable. UNIQUE (investor_id, attempt_id) '
    'is the anti-overwrite rule: a retry is a new attempt, and the previous result stays '
    'readable, which is what makes a rejection durable evidence rather than something that '
    'disappeared. Failure/cancellation/expiry cannot carry output; a proposal cannot be '
    'empty; error codes are a closed redacted list, never a provider traceback or headers.';
COMMENT ON COLUMN task_result.output IS
    'The submitted proposal, and untrusted. Not a raw provider HTTP response, not a '
    'traceback. Nothing reads this as fact: only an explicit human task_review can turn it '
    'into a note.';

-- Which pinned context entry supports which claim in a result. Both FKs carry task_id, so a
-- citation cannot reach across tasks or tenants to borrow evidence it was not given. 044
-- seals this collection to the result's creating transaction, so citations cannot be added
-- afterwards to make an unsupported claim look supported.
CREATE TABLE result_citation (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  task_id uuid NOT NULL,
  result_id uuid NOT NULL,
  context_id uuid NOT NULL,
  claim_ref ws_text NOT NULL,
  PRIMARY KEY (investor_id,result_id,context_id,claim_ref),
  FOREIGN KEY (investor_id,task_id,result_id) REFERENCES task_result(investor_id,task_id,id),
  FOREIGN KEY (investor_id,task_id,context_id) REFERENCES task_context(investor_id,task_id,id)
);

-- ---------------------------------------------------------------------------------
-- task_review -- the human act, and the only one that files a note
-- ---------------------------------------------------------------------------------
--
-- ONE REVIEW PER TASK, ONE PER RESULT, FOREVER. `UNIQUE (investor_id, task_id)` and
-- `UNIQUE (investor_id, result_id)` mean a decision cannot be revisited by inserting a second
-- row; a changed mind is new work, not a rewritten verdict.
--
-- ACCEPT IS BICONDITIONAL WITH A NOTE. `(decision = 'ACCEPT') = (accepted_note_id IS NOT
-- NULL)`: accepting means a note now exists, and a REJECT cannot quietly file one. 044 goes
-- further and requires that note to be created in THIS transaction, attributed to THIS
-- reviewer, for the same company, with authorship ACCEPTED_AGENT -- and adds the inverse
-- deferred guard so an ACCEPTED_AGENT note with no acceptance is impossible. The two
-- constraints together are what make acceptance atomic: note, links, review and audit commit
-- as one act or not at all.
--
-- `reason` IS MANDATORY FOR BOTH DECISIONS. A rejection without a reason teaches nobody
-- anything six months later, and an acceptance without one is indistinguishable from a
-- reflex.
--
-- WHAT THIS CANNOT PROVE, and the report must keep saying so: that a person actually READ the
-- output. It proves an authenticated human session submitted an explicit accept for this
-- result. A CHECK constraint cannot verify comprehension, and a reviewer_uid or a `decision`
-- appearing inside model output is never consumed from there.
CREATE TABLE task_review (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  result_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('ACCEPT','REJECT')),
  reason ws_text NOT NULL,
  accepted_note_id uuid,
  reviewer_uid uuid NOT NULL REFERENCES app_user(id),
  reviewed_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,task_id),
  UNIQUE (investor_id,result_id),
  -- One note can be the product of at most one acceptance.
  UNIQUE (investor_id,accepted_note_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,task_id,result_id) REFERENCES task_result(investor_id,task_id,id),
  FOREIGN KEY (investor_id,accepted_note_id) REFERENCES note,
  CHECK ((decision = 'ACCEPT') = (accepted_note_id IS NOT NULL))
);

COMMENT ON TABLE task_review IS
    'The human act that turns untrusted model output into a note, and the only thing that '
    'can. One review per task and per result, forever: a changed mind is new work, not a '
    'rewritten verdict. ACCEPT is biconditional with a note; migration 044 additionally '
    'requires that note to be created in the same transaction and attributed to this '
    'reviewer. Cannot prove a person READ the output -- only that an authenticated human '
    'session submitted an explicit accept.';

-- ---------------------------------------------------------------------------------
-- The five-state projection, computed in THIS ORDER. Documented here because the order is
-- the definition, and getting it wrong is how a reviewed task shows as runnable.
--
--   done      -- an ACCEPT review exists, with its immutable accepted note.
--   rejected  -- a REJECT review exists, with its reason. The result stays readable.
--   review    -- a PROPOSED result exists, INCLUDING a guardrail-blocked one, which can
--                only be rejected.
--   running   -- the latest attempt has no terminal result. At lease expiry the service
--                appends an EXPIRED result before any retry is permitted.
--   assigned  -- no attempt, or the latest result is FAILED/CANCELLED/EXPIRED. The failure
--                reason is displayed separately; it is not a result.
--
-- A retry is only legal after a failed terminal result. A task that has been proposed or
-- reviewed cannot restart -- create a new task linked by parent_task_id. 044 enforces that.
-- ---------------------------------------------------------------------------------

CREATE INDEX task_company ON task(investor_id,isin,seq DESC);
CREATE INDEX attempt_latest ON task_attempt(investor_id,task_id,attempt_no DESC);
-- Lease first, so the expiry sweep is a range scan across all tenants rather than a scan per
-- investor. This is the one index here deliberately not tenant-first.
CREATE INDEX attempt_expiry ON task_attempt(lease_until,investor_id);
-- "Which tasks pinned this note", for showing a note's downstream use.
CREATE INDEX context_notes ON task_context(investor_id,note_id);
