-- 052_research_execution.sql -- what BE 16-25 and BT 08-10 need that 040-051 do not provide.
--
-- READ docs/ARCHITECTURE_INVARIANTS.md FIRST. Nothing here is a financial record. No holding,
-- price, transaction or valuation is stored, and nothing below can become an input to one.
--
-- THE EXECUTION TABLES ALREADY EXIST. skill, agent, task, task_context, task_attempt,
-- task_result, result_citation, task_review, chat_thread and chat_message were created by 042
-- and 043 and are guarded by 044/045. Sessions 16-25 are repository, service and route work
-- over those tables; this file adds only the two things they cannot do without:
--
--   1. THE RUNNER NEEDS AN IDEMPOTENCY RECEIPT. Attempt starts and result/failure/cancellation
--      submissions are runner-capability writes (045 gives the runner INSERT on task_attempt and
--      task_result and nothing that could accept output). Each takes an Idempotency-Key, and a
--      retry after a dropped connection must replay the ORIGINAL response even after a process
--      restart -- which is exactly what 047's ws_receipt is for. 047 granted it to the human and
--      engine capabilities only, because those were the only writers then. Granting INSERT on
--      ws_receipt to the runner widens nothing it can SEE (RLS still confines it to the
--      selected tenant) and lets it write nothing but identifiers (047's CHECK).
--
--   2. BT 09'S EXPLICIT TEACHING PREFERENCES AND ITEM FEEDBACK. Concise/detailed view, lesson
--      cadence and follow-up preference, and "useful / not useful" on a card, question or
--      lesson. Explicit choices, versioned by append, never inferred -- this is NOT a profile,
--      and silence is not agreement. Neither table feeds validation: the factual and
--      contradiction gates are identical whatever these rows say.
--
-- NUMBERED 052 AFTER THE ACTUAL LATEST (051).

-- ---------------------------------------------------------------------------------
-- 1. Runner receipts
-- ---------------------------------------------------------------------------------

GRANT INSERT ON ws_receipt TO trackr_ws_runner;

-- ---------------------------------------------------------------------------------
-- 2. Teaching preferences -- versioned, explicit, investor-level
-- ---------------------------------------------------------------------------------
--
-- Presentation only. `detail` chooses how much reviewed lesson text is shown; `lesson_cadence`
-- how often an applicable lesson is offered; `follow_ups` whether a reviewed follow-up question
-- is asked after an answer. None of them can relax a check, hide a required review or change
-- which evidence is shown -- they change how much reviewed template text surrounds it.
CREATE TABLE butler_teaching_preference (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version integer NOT NULL CHECK (version > 0),
  detail text NOT NULL CHECK (detail IN ('CONCISE','DETAILED')),
  lesson_cadence text NOT NULL CHECK (lesson_cadence IN ('EVERY_REVIEW','WEEKLY','OFF')),
  follow_ups text NOT NULL CHECK (follow_ups IN ('ASK','SKIP')),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,version),
  UNIQUE (investor_id,dedupe_key)
);

COMMENT ON TABLE butler_teaching_preference IS
    'Explicit Butler teaching presentation choices, versioned by append. Never inferred, never '
    'a profile, never an input to validation: factual and contradiction gates are identical '
    'whatever these rows say.';

-- ---------------------------------------------------------------------------------
-- 3. Item feedback -- "useful / not useful", per item, append-only
-- ---------------------------------------------------------------------------------
--
-- The subject is a CATALOGUE identity (a reviewed question, lesson or card template id and its
-- version), optionally with the inbox item it was shown on. Free text is deliberately absent:
-- a feedback comment would be an unreviewed channel into material that is later shown back,
-- and "not useful because <prose>" would become a place model-shaped text could land. A
-- revised opinion is a new row; the latest per subject is the current one.
CREATE TABLE butler_feedback (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  subject_kind text NOT NULL CHECK (subject_kind IN ('QUESTION','LESSON','CARD')),
  subject_id text NOT NULL CHECK (subject_id ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  subject_version text NOT NULL CHECK (subject_version ~ '^[A-Za-z0-9._-]{1,32}$'),
  inbox_item_id uuid,
  rating text NOT NULL CHECK (rating IN ('USEFUL','NOT_USEFUL')),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,inbox_item_id) REFERENCES butler_inbox_item
);

COMMENT ON TABLE butler_feedback IS
    'Explicit useful/not-useful on a reviewed catalogue item. No free text by design. Not '
    'model training, not engagement optimisation, and never read by validation.';

CREATE INDEX butler_feedback_subject
  ON butler_feedback(investor_id,subject_kind,subject_id,seq DESC);

-- ---------------------------------------------------------------------------------
-- 4. Guards, RLS and grants -- the same shape as 044/045/051
-- ---------------------------------------------------------------------------------

-- Sequential versions for the preference table, under the tenant lock ws_insert_guard already
-- took. Two concurrent saves cannot both become version 3.
CREATE FUNCTION butler_teaching_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior integer;
BEGIN
  IF TG_TABLE_NAME = 'butler_teaching_preference' THEN
    SELECT COALESCE(max(version),0) INTO prior FROM butler_teaching_preference
      WHERE investor_id = NEW.investor_id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'butler version conflict'; END IF;
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['butler_teaching_preference','butler_feedback'] LOOP
    EXECUTE format('CREATE TRIGGER ws_insert BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION ws_insert_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_z_teaching BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION butler_teaching_guard()',tbl);
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

-- A person's explicit choice: the human capability only.
GRANT INSERT ON butler_teaching_preference,butler_feedback TO trackr_ws_human;
GRANT USAGE ON SEQUENCE butler_feedback_seq_seq TO trackr_ws_human;
