-- 051_butler.sql -- Butler: explicit follows, memory references, outbox, inbox and reviews.
--
-- BT 01 (prompts/bt_01_contracts_and_schema.md), docs/BUTLER_ARCHITECTURE.md sections 3, 6, 7
-- and 9. READ docs/ARCHITECTURE_INVARIANTS.md FIRST. Nothing in this file is a financial record:
-- no table here is a holding, a price, a transaction or a valuation, and nothing may become an
-- input to one. Butler records WHAT THE INVESTOR ASKED TO BE REMINDED OF and WHAT A
-- DETERMINISTIC CHECK SHOWED THEM -- never what is true about a company.
--
-- ONLY WHAT THE RESEARCH SCHEMA DOES NOT ALREADY HAVE. Notes, questions, answers, theses,
-- sources, facts, evaluations and tasks are REUSED by reference. What is new is exactly the
-- list in architecture section 6: scoped subscription, explicit memory selection, inbox/review
-- and a transactional outbox, plus two small records the prompts require and nothing else
-- provides -- a source retraction (BT 04's correction state) and a worker tick receipt (BT 05's
-- truthful last-success/lag). Research ACTIVITY has no table: the existing rows (a note's
-- recorded_at, a note_link, a question_answer) already ARE the server receipts, and a second
-- copy would be the one that drifted.
--
-- STATE IS DERIVED FROM APPENDED EVENTS, NEVER UPDATED. Every table below gets the same three
-- triggers and the same RLS policy as the 040-045 bundle (ws_insert_guard, ws_append_only,
-- ws_no_truncate; ws_tenant_rows). A pause is a new subscription event, a snooze is a new
-- inbox event, a forget is a memory tombstone, a lease is a new claim row. Whether something
-- is "paused" or "snoozed" is read from the latest event, exactly as a task's status is read
-- from which attempt/result/review rows exist (042).
--
-- NUMBERED 051 AFTER THE ACTUAL LATEST (050), not the blueprint's reserved 031-036.

-- ---------------------------------------------------------------------------------
-- Preferences -- versioned, investor-level, explicit
-- ---------------------------------------------------------------------------------
--
-- The timezone is REQUIRED before anything is scheduled and is never inferred from an exchange
-- or a browser (architecture section 6). Quiet hours and the digest limit are product defaults
-- the person can change; they suppress interruption, never event capture.
CREATE TABLE butler_preference (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version integer NOT NULL CHECK (version > 0),
  timezone ws_text NOT NULL,
  quiet_start time NOT NULL,
  quiet_end time NOT NULL,
  digest_limit smallint NOT NULL CHECK (digest_limit BETWEEN 1 AND 10),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,version),
  UNIQUE (investor_id,dedupe_key)
);

COMMENT ON TABLE butler_preference IS
    'Explicit Butler presentation preferences, versioned. timezone is required before any '
    'checkpoint is scheduled and is never inferred. Quiet hours and digest limit suppress '
    'interruption only; events are still captured. No hidden personality scoring.';

-- ---------------------------------------------------------------------------------
-- Subscriptions -- what the investor explicitly asked Butler to follow
-- ---------------------------------------------------------------------------------
--
-- EXPLICIT ONLY. A holding, a casual chat mention or a note does not create one; only the
-- follow endpoint does (BT 02). A company subscription references the investor's own linked
-- company, so "follow a company you have not opened" is a foreign-key failure, not a policy.
-- Topics carry their user-confirmed aliases; a topic never becomes an ISIN by name matching.
CREATE TABLE butler_subscription (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subject_kind text NOT NULL CHECK (subject_kind IN ('COMPANY','TOPIC')),
  isin text,
  topic ws_text,
  topic_aliases text[] NOT NULL DEFAULT '{}',
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  CHECK ((subject_kind = 'COMPANY') = (isin IS NOT NULL)),
  CHECK ((subject_kind = 'TOPIC') = (topic IS NOT NULL)),
  CHECK (subject_kind = 'TOPIC' OR cardinality(topic_aliases) = 0),
  CHECK (cardinality(topic_aliases) <= 20)
);

-- Version 1 is always FOLLOW; the latest version is the current state. Nothing follows
-- UNFOLLOW -- following again is a new subscription, so the history of the first one stays
-- readable as it was. The checkpoint is set explicitly (FOLLOW or SET_CHECKPOINT) and is
-- NULL until someone chooses one: the optional 30-day default is offered, never activated.
CREATE TABLE butler_subscription_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscription_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  action text NOT NULL CHECK (action IN
    ('FOLLOW','PAUSE','RESUME','UNFOLLOW','SET_CHECKPOINT')),
  checkpoint_days smallint CHECK (checkpoint_days BETWEEN 1 AND 366),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,subscription_id,version),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,subscription_id) REFERENCES butler_subscription,
  CHECK (action IN ('FOLLOW','SET_CHECKPOINT') OR checkpoint_days IS NULL)
);

COMMENT ON TABLE butler_subscription_event IS
    'Follow/pause/resume/unfollow/checkpoint history. Version 1 is FOLLOW; nothing follows '
    'UNFOLLOW. The current state is the latest version, derived on read. A checkpoint is '
    'NULL until the investor explicitly sets one.';

-- ---------------------------------------------------------------------------------
-- Memory -- references to SAVED, ATTRIBUTED records; never free text
-- ---------------------------------------------------------------------------------
--
-- A MEMORY HAS NO TEXT COLUMN. It points at one exact immutable version of a note, answer,
-- thesis or saved chat turn, and the words are read from there. That is what keeps a user's
-- belief, an accepted assistant suggestion and an assistant turn distinguishable forever:
-- attribution is read from the referenced row (note.authorship, chat_message.role), so it
-- cannot be asserted here and cannot drift.
CREATE TABLE butler_memory (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  ref_kind text NOT NULL CHECK (ref_kind IN ('NOTE','ANSWER','THESIS','CHAT_MESSAGE')),
  note_id uuid,
  answer_id uuid,
  thesis_id uuid,
  chat_message_id uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,note_id) REFERENCES note,
  FOREIGN KEY (investor_id,answer_id) REFERENCES question_answer,
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis,
  FOREIGN KEY (investor_id,chat_message_id) REFERENCES chat_message,
  CHECK (num_nonnulls(note_id,answer_id,thesis_id,chat_message_id) = 1),
  CHECK ((ref_kind = 'NOTE') = (note_id IS NOT NULL)),
  CHECK ((ref_kind = 'ANSWER') = (answer_id IS NOT NULL)),
  CHECK ((ref_kind = 'THESIS') = (thesis_id IS NOT NULL)),
  CHECK ((ref_kind = 'CHAT_MESSAGE') = (chat_message_id IS NOT NULL))
);

-- One terminal event per memory. FORGET is the tombstone architecture section 3 requires: it
-- excludes the referenced record from Butler retrieval and invalidates cards that cite it,
-- and because it is a row, a background reindex or a late event cannot silently remember the
-- record again. It does NOT delete the note -- that is the editor's own erasure path.
CREATE TABLE butler_memory_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  memory_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('FORGET','SUPERSEDE')),
  superseded_by uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,memory_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,memory_id) REFERENCES butler_memory,
  FOREIGN KEY (investor_id,superseded_by) REFERENCES butler_memory,
  CHECK ((action = 'SUPERSEDE') = (superseded_by IS NOT NULL)),
  CHECK (superseded_by IS NULL OR superseded_by <> memory_id)
);

-- ---------------------------------------------------------------------------------
-- Source retraction -- the correction state BT 04 needs
-- ---------------------------------------------------------------------------------
--
-- A research_source row is immutable (044), so "this filing was withdrawn" cannot be an edit.
-- It is an attributed row naming the source and, optionally, its replacement. Cards and
-- review evidence that cite the source are invalidated on read, and a correction card is
-- added; the old cards are never rewritten.
CREATE TABLE butler_source_retraction (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  source_id uuid NOT NULL,
  reason_code text NOT NULL CHECK (reason_code IN
    ('RETRACTED_BY_ISSUER','CORRECTED_BY_ISSUER','WRONG_DOCUMENT','DUPLICATE_COPY',
     'RIGHTS_WITHDRAWN')),
  replacement_source_id uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,source_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin,source_id) REFERENCES research_source(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,replacement_source_id)
    REFERENCES research_source(investor_id,isin,id),
  CHECK (replacement_source_id IS NULL OR replacement_source_id <> source_id)
);

-- ---------------------------------------------------------------------------------
-- Outbox -- written in the SAME transaction as the record it announces
-- ---------------------------------------------------------------------------------
--
-- Filled only by the butler_emit() trigger below, so a saved note, source, thesis version,
-- answer, evaluation, retraction or review cannot commit without its event and an event cannot
-- exist without its record. One row per (kind, record): the record id is a version id, since
-- every one of those tables is append-only.
CREATE TABLE butler_outbox (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  event_kind text NOT NULL CHECK (event_kind IN
    ('SOURCE_VERSION_ADDED','USER_NOTE_SAVED','THESIS_VERSION_ADDED','QUESTION_ANSWERED',
     'EVALUATION_RECORDED','SOURCE_RETRACTED','USER_REVIEW_REQUESTED')),
  isin text NOT NULL,
  record_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,event_kind,record_id)
);

-- A LEASE IS A ROW, like task_attempt. Committed before the work, so a crash mid-work leaves a
-- claim whose lease expires and a new attempt can be made; attempt numbers are sequential and
-- a new claim is refused while the previous lease is live. The lease length is the server's.
CREATE TABLE butler_outbox_claim (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  outbox_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  claimed_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,outbox_id,attempt_no),
  FOREIGN KEY (investor_id,outbox_id) REFERENCES butler_outbox
);

-- EXACTLY ONE LOGICAL EFFECT PER EVENT. UNIQUE (investor_id, outbox_id) is the at-least-once
-- worker's exactly-once boundary: a duplicate tick, a retry after a crash or a second worker
-- can all try, and only one effect commits. The guard also refuses an effect from a claim
-- that is not the latest or whose lease expired, so a stalled worker cannot land late.
CREATE TABLE butler_outbox_effect (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  outbox_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('CARD_CREATED','SUPPRESSED','FAILED')),
  reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z_]{2,63}$'),
  inbox_item_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,outbox_id),
  FOREIGN KEY (investor_id,outbox_id) REFERENCES butler_outbox,
  FOREIGN KEY (investor_id,claim_id) REFERENCES butler_outbox_claim,
  CHECK ((outcome = 'CARD_CREATED') = (inbox_item_id IS NOT NULL))
);

-- ---------------------------------------------------------------------------------
-- Inbox -- validated template items, and what the investor did with them
-- ---------------------------------------------------------------------------------
--
-- NO PROSE COLUMN. An item stores a template id/version and typed ARGUMENTS (identifiers,
-- enums, dates, decimal strings -- ws_string_numbers refuses a JSON number anywhere). The text
-- is rendered at read time from the reviewed catalogue, after the referenced records are
-- re-read under current permissions. A card whose evidence became inaccessible renders as
-- invalidated; it is never served from a cached sentence.
--
-- dedupe_key encodes investor + subscription + kind + record version + rule version
-- (architecture section 6), which is what makes duplicate delivery produce one item.
CREATE TABLE butler_inbox_item (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  card_kind text NOT NULL CHECK (card_kind IN
    ('NEW_MATERIAL','REVIEW_DUE','RULE_STATE_CHANGED','COVERAGE_WARNING','SOURCE_CORRECTION',
     'THESIS_CHANGED')),
  priority text NOT NULL CHECK (priority IN ('INFORMATIONAL','REQUIRED')),
  isin text NOT NULL,
  subscription_id uuid,
  template_id ws_text NOT NULL,
  template_version ws_text NOT NULL,
  rule_version ws_text NOT NULL,
  issue_key ws_text NOT NULL,
  evidence_version ws_text NOT NULL,
  args jsonb NOT NULL CHECK (jsonb_typeof(args) = 'object' AND ws_string_numbers(args)),
  source_id uuid,
  thesis_id uuid,
  evaluation_id uuid,
  supersedes_item_id uuid,
  deliver_after timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  UNIQUE (investor_id,supersedes_item_id),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,subscription_id) REFERENCES butler_subscription,
  FOREIGN KEY (investor_id,source_id) REFERENCES research_source,
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis,
  FOREIGN KEY (investor_id,evaluation_id) REFERENCES trigger_evaluation,
  FOREIGN KEY (investor_id,supersedes_item_id) REFERENCES butler_inbox_item,
  CHECK (supersedes_item_id IS NULL OR supersedes_item_id <> id)
);

COMMENT ON TABLE butler_inbox_item IS
    'One deterministic, template-rendered inbox card: template id/version plus typed '
    'arguments, never prose. Rendered at read after re-checking the referenced records. '
    'A revised issue is a NEW item naming the one it supersedes; the old one is not edited.';

CREATE TABLE butler_inbox_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  item_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  action text NOT NULL CHECK (action IN ('SEEN','SNOOZE','DISMISS','RESOLVE','RESTORE')),
  snooze_until timestamptz,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,item_id,version),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,item_id) REFERENCES butler_inbox_item,
  CHECK ((action = 'SNOOZE') = (snooze_until IS NOT NULL))
);

COMMENT ON TABLE butler_inbox_event IS
    'Seen/snooze/dismiss/resolve/restore on an inbox item, version-checked. Resolving a card '
    'is a statement about the reminder, never a research conclusion.';

-- ---------------------------------------------------------------------------------
-- Decision reviews -- the investor's own answers, field by field
-- ---------------------------------------------------------------------------------
--
-- PINNED AT START. The thesis version, checklist version and question ids are fixed when the
-- review opens; a changed thesis or a retracted source produces a SUPERSEDING review, never an
-- edited one. There is no status column and no AI_APPROVED anywhere: DRAFT / AWAITING_INPUT /
-- DOCUMENTED / SUPERSEDED are derived from which rows exist.
CREATE TABLE butler_review (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text NOT NULL,
  decision_kind text NOT NULL CHECK (decision_kind IN
    ('RESEARCH_CHECKPOINT','THESIS_CHANGE','POSITION_DECISION')),
  thesis_id uuid,
  checklist_id ws_text NOT NULL,
  checklist_version ws_text NOT NULL,
  question_ids text[] NOT NULL CHECK (cardinality(question_ids) BETWEEN 1 AND 7),
  trigger_item_id uuid,
  supersedes_review_id uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  UNIQUE (investor_id,supersedes_review_id),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,isin,thesis_id) REFERENCES thesis(investor_id,isin,id),
  FOREIGN KEY (investor_id,trigger_item_id) REFERENCES butler_inbox_item,
  FOREIGN KEY (investor_id,supersedes_review_id) REFERENCES butler_review,
  CHECK (supersedes_review_id IS NULL OR supersedes_review_id <> id)
);

-- A human response, in a linear revision chain exactly like question_answer. The answer text is
-- the person's, verbatim; the model has no INSERT grant and the guard requires the human
-- capability. `attested` and `not_applicable_reason` are the person's explicit statements, never
-- inferred from how long or confident the answer sounds.
CREATE TABLE butler_review_response (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  review_id uuid NOT NULL,
  question_id text NOT NULL CHECK (question_id ~ '^[A-Z][A-Z_]{2,63}$'),
  answer text NOT NULL DEFAULT '',
  attested boolean NOT NULL DEFAULT false,
  not_applicable_reason text,
  milestone_on date,
  previous_id uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,review_id,id),
  UNIQUE (investor_id,previous_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,review_id) REFERENCES butler_review,
  FOREIGN KEY (investor_id,review_id,previous_id)
    REFERENCES butler_review_response(investor_id,review_id,id),
  CHECK (previous_id IS NULL OR previous_id <> id),
  CHECK (not_applicable_reason IS NULL OR length(btrim(not_applicable_reason)) > 0),
  CHECK (length(btrim(answer)) > 0 OR not_applicable_reason IS NOT NULL)
);

-- Evidence the person attached to one response. Sealed to the response's own transaction, so
-- a weak answer cannot be made to look supported afterwards. Attaching is not sufficiency.
CREATE TABLE butler_review_evidence (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  response_id uuid NOT NULL,
  ordinal smallint NOT NULL CHECK (ordinal BETWEEN 0 AND 19),
  source_id uuid,
  note_id uuid,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,response_id,ordinal),
  FOREIGN KEY (investor_id,response_id) REFERENCES butler_review_response(investor_id,id),
  FOREIGN KEY (investor_id,source_id) REFERENCES research_source,
  FOREIGN KEY (investor_id,note_id) REFERENCES note,
  CHECK (num_nonnulls(source_id,note_id) = 1)
);

-- The person's explicit process choice. `unresolved` lists the checks they acknowledged as
-- still open -- the record says "documented with these unresolved", never "approved".
CREATE TABLE butler_review_completion (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  review_id uuid NOT NULL,
  choice text NOT NULL CHECK (choice IN ('CONTINUE','PAUSE','REVISE','MORE_INFORMATION')),
  reason text NOT NULL DEFAULT '',
  unresolved text[] NOT NULL DEFAULT '{}',
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,review_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,review_id) REFERENCES butler_review,
  CHECK (cardinality(unresolved) = 0 OR length(btrim(reason)) > 0)
);

COMMENT ON TABLE butler_review_completion IS
    'The investor''s explicit continue/pause/revise/more-information choice, with every '
    'unresolved check they acknowledged. "Review documented", never an AI approval; it '
    'creates no trade, changes no thesis and waives no ritual gate.';

-- ---------------------------------------------------------------------------------
-- Worker receipts -- truthful last success and lag
-- ---------------------------------------------------------------------------------
CREATE TABLE butler_tick (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  started_at timestamptz NOT NULL,
  finished_at timestamptz NOT NULL DEFAULT now(),
  outcome text NOT NULL CHECK (outcome IN ('OK','PARTIAL','FAILED','SKIPPED_REVOKED')),
  processed integer NOT NULL CHECK (processed >= 0),
  cards integer NOT NULL CHECK (cards >= 0),
  suppressed integer NOT NULL CHECK (suppressed >= 0),
  failed integer NOT NULL CHECK (failed >= 0),
  oldest_pending_at timestamptz,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id)
);

COMMENT ON TABLE butler_tick IS
    'One Butler worker pass for one investor. Read for last-success and lag; a missing or '
    'failed tick is shown as such, never as "nothing happened".';

-- ---------------------------------------------------------------------------------
-- Butler-specific insert rules
-- ---------------------------------------------------------------------------------
--
-- Runs AFTER ws_insert_guard (trigger names fire in alphabetical order: ws_insert < ws_z_butler),
-- so tenant, owner attribution and server clocks are already settled here.
CREATE FUNCTION butler_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior integer; last_action text; pause_state text; target_isin text;
        c butler_outbox_claim%ROWTYPE; r butler_review%ROWTYPE;
        p butler_review_response%ROWTYPE;
BEGIN
  -- HUMAN-ONLY RECORDS. Grants already keep other capabilities out; this makes the refusal a
  -- sentence rather than a permission error, and keeps it true if a grant is ever widened.
  IF TG_TABLE_NAME IN ('butler_preference','butler_subscription','butler_subscription_event',
      'butler_memory','butler_memory_event','butler_source_retraction','butler_inbox_event',
      'butler_review','butler_review_response','butler_review_evidence',
      'butler_review_completion') AND NOT ws_human() THEN
    RAISE EXCEPTION 'butler human write required';
  END IF;

  IF TG_TABLE_NAME = 'butler_preference' THEN
    SELECT COALESCE(max(version),0) INTO prior FROM butler_preference
     WHERE investor_id = NEW.investor_id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'butler version conflict'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = NEW.timezone) THEN
      RAISE EXCEPTION 'butler unknown timezone';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_subscription_event' THEN
    SELECT COALESCE(max(version),0) INTO prior FROM butler_subscription_event
     WHERE investor_id = NEW.investor_id AND subscription_id = NEW.subscription_id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'butler version conflict'; END IF;
    IF (prior = 0) <> (NEW.action = 'FOLLOW') THEN
      RAISE EXCEPTION 'butler invalid subscription transition';
    END IF;
    SELECT action INTO last_action FROM butler_subscription_event
     WHERE investor_id = NEW.investor_id AND subscription_id = NEW.subscription_id
     ORDER BY version DESC LIMIT 1;
    IF last_action = 'UNFOLLOW' THEN RAISE EXCEPTION 'butler invalid subscription transition'; END IF;
    SELECT action INTO pause_state FROM butler_subscription_event
     WHERE investor_id = NEW.investor_id AND subscription_id = NEW.subscription_id
       AND action IN ('PAUSE','RESUME') ORDER BY version DESC LIMIT 1;
    IF (NEW.action = 'PAUSE' AND pause_state = 'PAUSE')
       OR (NEW.action = 'RESUME' AND pause_state IS DISTINCT FROM 'PAUSE') THEN
      RAISE EXCEPTION 'butler invalid subscription transition';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_memory' THEN
    -- The remembered record must concern this company (across VERIFIED aliases). A chat turn
    -- the system refused is not memory; a refusal is the system's words, not the investor's.
    IF NEW.note_id IS NOT NULL THEN
      SELECT isin INTO target_isin FROM note WHERE investor_id = NEW.investor_id AND id = NEW.note_id;
    ELSIF NEW.answer_id IS NOT NULL THEN
      SELECT q.isin INTO target_isin FROM question_answer a JOIN question q
        ON q.investor_id = a.investor_id AND q.id = a.question_id
       WHERE a.investor_id = NEW.investor_id AND a.id = NEW.answer_id;
    ELSIF NEW.thesis_id IS NOT NULL THEN
      SELECT isin INTO target_isin FROM thesis WHERE investor_id = NEW.investor_id AND id = NEW.thesis_id;
    ELSE
      SELECT t.isin INTO target_isin FROM chat_message m JOIN chat_thread t
        ON t.investor_id = m.investor_id AND t.id = m.thread_id
       WHERE m.investor_id = NEW.investor_id AND m.id = NEW.chat_message_id AND m.role <> 'REFUSAL';
    END IF;
    IF target_isin IS NULL OR NOT ws_same_security(target_isin, NEW.isin) THEN
      RAISE EXCEPTION 'butler memory reference mismatch';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_outbox_claim' THEN
    NEW.claimed_at := clock_timestamp();
    NEW.lease_until := NEW.claimed_at + interval '2 minutes';
    IF EXISTS (SELECT 1 FROM butler_outbox_effect WHERE investor_id = NEW.investor_id
               AND outbox_id = NEW.outbox_id) THEN
      RAISE EXCEPTION 'butler event already has an effect';
    END IF;
    SELECT * INTO c FROM butler_outbox_claim WHERE investor_id = NEW.investor_id
       AND outbox_id = NEW.outbox_id ORDER BY attempt_no DESC LIMIT 1;
    IF FOUND AND c.lease_until > clock_timestamp() THEN
      RAISE EXCEPTION 'butler lease still held';
    END IF;
    IF NEW.attempt_no <> COALESCE(c.attempt_no,0) + 1 THEN
      RAISE EXCEPTION 'butler version conflict';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_outbox_effect' THEN
    SELECT * INTO c FROM butler_outbox_claim WHERE investor_id = NEW.investor_id
       AND outbox_id = NEW.outbox_id ORDER BY attempt_no DESC LIMIT 1;
    IF NOT FOUND OR c.id <> NEW.claim_id OR clock_timestamp() > c.lease_until THEN
      RAISE EXCEPTION 'butler stale lease';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_inbox_event' THEN
    SELECT COALESCE(max(version),0) INTO prior FROM butler_inbox_event
     WHERE investor_id = NEW.investor_id AND item_id = NEW.item_id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'butler version conflict'; END IF;
    IF NEW.snooze_until IS NOT NULL AND (NEW.snooze_until <= clock_timestamp()
       OR NEW.snooze_until > clock_timestamp() + interval '90 days') THEN
      RAISE EXCEPTION 'butler snooze out of range';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_review_response' THEN
    SELECT * INTO r FROM butler_review WHERE investor_id = NEW.investor_id AND id = NEW.review_id;
    IF NOT (NEW.question_id = ANY(r.question_ids)) THEN
      RAISE EXCEPTION 'butler question not in review';
    END IF;
    IF EXISTS (SELECT 1 FROM butler_review_completion WHERE investor_id = NEW.investor_id
               AND review_id = NEW.review_id)
       OR EXISTS (SELECT 1 FROM butler_review WHERE investor_id = NEW.investor_id
               AND supersedes_review_id = NEW.review_id) THEN
      RAISE EXCEPTION 'butler review is closed';
    END IF;
    IF NEW.previous_id IS NULL THEN
      IF EXISTS (SELECT 1 FROM butler_review_response WHERE investor_id = NEW.investor_id
                 AND review_id = NEW.review_id AND question_id = NEW.question_id) THEN
        RAISE EXCEPTION 'butler response revision must name predecessor';
      END IF;
    ELSE
      SELECT * INTO p FROM butler_review_response WHERE investor_id = NEW.investor_id
         AND id = NEW.previous_id;
      IF p.question_id IS DISTINCT FROM NEW.question_id THEN
        RAISE EXCEPTION 'butler response revision must name predecessor';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_review_evidence' THEN
    IF (SELECT creation_xid FROM butler_review_response WHERE investor_id = NEW.investor_id
        AND id = NEW.response_id) IS DISTINCT FROM txid_current() THEN
      RAISE EXCEPTION 'butler response evidence is sealed';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_review_completion' THEN
    IF EXISTS (SELECT 1 FROM butler_review WHERE investor_id = NEW.investor_id
               AND supersedes_review_id = NEW.review_id) THEN
      RAISE EXCEPTION 'butler review is closed';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_tick' THEN
    NEW.finished_at := clock_timestamp();
    IF NEW.started_at > NEW.finished_at THEN RAISE EXCEPTION 'butler tick predates start'; END IF;
  END IF;

  RETURN NEW;
END $$;

-- A subscription with no FOLLOW event is a follow nobody recorded; refuse it at COMMIT, the
-- only moment the sibling row can be checked (same pattern as 044's ritual_complete).
CREATE FUNCTION butler_deferred_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM butler_subscription_event WHERE investor_id = NEW.investor_id
                 AND subscription_id = NEW.id AND version = 1 AND action = 'FOLLOW') THEN
    RAISE EXCEPTION 'butler subscription needs a FOLLOW event';
  END IF;
  RETURN NULL;
END $$;

-- ---------------------------------------------------------------------------------
-- The outbox emitter -- same transaction as the record, by construction
-- ---------------------------------------------------------------------------------
CREATE FUNCTION butler_emit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text; subject text;
BEGIN
  IF TG_TABLE_NAME = 'research_source' THEN
    kind := 'SOURCE_VERSION_ADDED'; subject := NEW.isin;
  ELSIF TG_TABLE_NAME = 'note' THEN
    kind := 'USER_NOTE_SAVED'; subject := NEW.isin;
  ELSIF TG_TABLE_NAME = 'thesis' THEN
    kind := 'THESIS_VERSION_ADDED'; subject := NEW.isin;
  ELSIF TG_TABLE_NAME = 'question_answer' THEN
    kind := 'QUESTION_ANSWERED';
    SELECT isin INTO subject FROM question WHERE investor_id = NEW.investor_id AND id = NEW.question_id;
  ELSIF TG_TABLE_NAME = 'trigger_evaluation' THEN
    kind := 'EVALUATION_RECORDED';
    SELECT isin INTO subject FROM thesis WHERE investor_id = NEW.investor_id AND id = NEW.thesis_id;
  ELSIF TG_TABLE_NAME = 'butler_source_retraction' THEN
    kind := 'SOURCE_RETRACTED'; subject := NEW.isin;
  ELSIF TG_TABLE_NAME = 'butler_review' THEN
    kind := 'USER_REVIEW_REQUESTED'; subject := NEW.isin;
  END IF;
  INSERT INTO butler_outbox (investor_id, event_kind, isin, record_id)
  VALUES (NEW.investor_id, kind, subject, NEW.id);
  RETURN NULL;
END $$;

COMMENT ON FUNCTION butler_emit() IS
    'Appends the Butler outbox row for a saved research record in the same transaction as '
    'the record. Emitting is not delivering: the worker decides, under current subscription, '
    'capability and rights, whether any card results.';

-- ---------------------------------------------------------------------------------
-- Install: the generic workspace guards, the Butler guard, RLS and grants
-- ---------------------------------------------------------------------------------
DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'butler_preference','butler_subscription','butler_subscription_event','butler_memory',
    'butler_memory_event','butler_source_retraction','butler_outbox','butler_outbox_claim',
    'butler_outbox_effect','butler_inbox_item','butler_inbox_event','butler_review',
    'butler_review_response','butler_review_evidence','butler_review_completion','butler_tick'
  ] LOOP
    EXECUTE format('CREATE TRIGGER ws_insert BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION ws_insert_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_z_butler BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION butler_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ws_append_only()',tbl);
    EXECUTE format('CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate()',tbl);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tbl);
    EXECUTE format('CREATE POLICY ws_tenant_rows ON %I USING (investor_id=ws_tenant()) WITH CHECK (investor_id=ws_tenant())',tbl);
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',tbl);
    EXECUTE format('GRANT SELECT ON %I TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser',tbl);
    EXECUTE format('GRANT DELETE ON %I TO trackr_ws_eraser',tbl);
  END LOOP;
END $$;

CREATE CONSTRAINT TRIGGER butler_subscription_followed AFTER INSERT ON butler_subscription
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION butler_deferred_guard();

-- AFTER INSERT so the record's own guards (044/048/049/050) have already accepted it.
CREATE TRIGGER butler_emit AFTER INSERT ON research_source FOR EACH ROW EXECUTE FUNCTION butler_emit();
CREATE TRIGGER butler_emit AFTER INSERT ON note FOR EACH ROW EXECUTE FUNCTION butler_emit();
CREATE TRIGGER butler_emit AFTER INSERT ON thesis FOR EACH ROW EXECUTE FUNCTION butler_emit();
CREATE TRIGGER butler_emit AFTER INSERT ON question_answer FOR EACH ROW EXECUTE FUNCTION butler_emit();
CREATE TRIGGER butler_emit AFTER INSERT ON trigger_evaluation FOR EACH ROW EXECUTE FUNCTION butler_emit();
CREATE TRIGGER butler_emit AFTER INSERT ON butler_source_retraction FOR EACH ROW EXECUTE FUNCTION butler_emit();
CREATE TRIGGER butler_emit AFTER INSERT ON butler_review FOR EACH ROW EXECUTE FUNCTION butler_emit();

-- A person's explicit Butler records.
GRANT INSERT ON butler_preference,butler_subscription,butler_subscription_event,butler_memory,
  butler_memory_event,butler_source_retraction,butler_inbox_event,butler_review,
  butler_review_response,butler_review_evidence,butler_review_completion TO trackr_ws_human;

-- The outbox is written by the emitter trigger, which runs as whichever capability inserted
-- the record: human (notes, theses, answers, sources, retractions, reviews) or engine
-- (sources, evaluations). Neither can write an arbitrary outbox row without the record.
GRANT INSERT ON butler_outbox TO trackr_ws_human,trackr_ws_engine;

-- The deterministic worker: claims, effects, template items and tick receipts. It holds no
-- grant on any human table above, so it cannot answer a review, follow a company or forget a
-- memory on anyone's behalf. The runner (browser model output) has no Butler grant at all.
GRANT INSERT ON butler_outbox_claim,butler_outbox_effect,butler_inbox_item,butler_tick
  TO trackr_ws_engine;

GRANT USAGE ON SEQUENCE butler_outbox_seq_seq,butler_inbox_item_seq_seq,butler_review_seq_seq,
  butler_review_response_seq_seq TO trackr_ws_human,trackr_ws_engine;

-- ---------------------------------------------------------------------------------
-- Indexes -- the reads Butler performs
-- ---------------------------------------------------------------------------------
CREATE INDEX butler_outbox_pending ON butler_outbox(investor_id,seq);
CREATE INDEX butler_claim_latest ON butler_outbox_claim(investor_id,outbox_id,attempt_no DESC);
CREATE INDEX butler_inbox_order ON butler_inbox_item(investor_id,seq DESC);
CREATE INDEX butler_inbox_issue ON butler_inbox_item(investor_id,issue_key,recorded_at DESC);
CREATE INDEX butler_inbox_events ON butler_inbox_event(investor_id,item_id,version DESC);
CREATE INDEX butler_sub_events ON butler_subscription_event(investor_id,subscription_id,version DESC);
CREATE INDEX butler_memory_company ON butler_memory(investor_id,isin);
CREATE INDEX butler_review_responses ON butler_review_response(investor_id,review_id,seq);
CREATE INDEX butler_tick_latest ON butler_tick(investor_id,finished_at DESC);
