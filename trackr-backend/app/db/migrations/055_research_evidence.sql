-- 055_research_evidence.sql -- external evidence provenance, revised filings, dated events.
--
-- SESSIONS 29 AND 34 (BE 29 external evidence, BE 34 catalyst calendar). Three additions, all
-- append-only, none touching the ledger or any financial table:
--
-- 1. ADAPTER PROVENANCE ON research_source. `adapter_id` + `origin_item_id` name exactly which
--    approved adapter retrieved which origin item; together with `content_sha256` they make a
--    repeated refresh a no-op. `revises_source_id` records that a later retrieval of the SAME
--    origin item is a revised document. Nothing is updated: the revision is a new row.
--
-- 2. SUPERSESSION ACROSS A REVISED FILING, AND ONLY THERE. 049 allows a fact to supersede
--    only a fact from the SAME source row. A revised annual report is a new source row, so a
--    changed value in it could not supersede the original -- it would sit beside it as a
--    permanent CONFLICT, which misstates a correction as a disagreement. This file widens the
--    rule by exactly one case: the new fact's source must REVISE the old fact's source (same
--    adapter, same origin item, recorded later). Unrelated sources still cannot supersede each
--    other (BE-V13): two filings that disagree remain two readable rows and a CONFLICT.
--
-- 3. research_event. A dated corporate event with an EXACT identity (event type, company ISIN,
--    period where relevant, source) -- BE-V06: a generic VERIFIED flag on some fact is not an
--    event. SCHEDULED and OBSERVED are different states stored in a column, not a wording.
--    A CLAIMED event (rumour, press expectation, analyst date) can never carry a date, and a
--    date that is not exact is stored as no date. A moved or cancelled event is a NEW row that
--    names the one it reschedules; the original keeps its own date forever.
--
-- NO SOURCE IS APPROVED BY THIS FILE (BE-Q04 open). The live adapter registry is empty
-- (app/research/sources.py); these tables are filled only by synthetic fixtures in tests.

ALTER TABLE research_source ADD COLUMN adapter_id text
  CHECK (adapter_id IS NULL OR adapter_id ~ '^[a-z][a-z0-9_.-]{2,63}$');
ALTER TABLE research_source ADD COLUMN origin_item_id text
  CHECK (origin_item_id IS NULL OR length(origin_item_id) BETWEEN 1 AND 200);
ALTER TABLE research_source ADD COLUMN revises_source_id uuid;
ALTER TABLE research_source ADD CONSTRAINT research_source_adapter_pair
  CHECK ((adapter_id IS NULL) = (origin_item_id IS NULL));
ALTER TABLE research_source ADD CONSTRAINT research_source_revision_needs_adapter
  CHECK (revises_source_id IS NULL OR adapter_id IS NOT NULL);
ALTER TABLE research_source ADD CONSTRAINT research_source_revision_not_self
  CHECK (revises_source_id IS NULL OR revises_source_id <> id);
ALTER TABLE research_source ADD CONSTRAINT research_source_revises
  FOREIGN KEY (investor_id,isin,revises_source_id) REFERENCES research_source(investor_id,isin,id);

-- One revision per source: the history is a chain, not a tree nobody can read.
CREATE UNIQUE INDEX research_source_one_revision
  ON research_source(investor_id,revises_source_id) WHERE revises_source_id IS NOT NULL;
CREATE INDEX research_source_by_origin
  ON research_source(investor_id,adapter_id,origin_item_id) WHERE adapter_id IS NOT NULL;

COMMENT ON COLUMN research_source.revises_source_id IS
    'The earlier retrieval of the SAME origin item (same adapter_id and origin_item_id) that '
    'this row revises. A correction appends; the revised row is never updated or deleted.';

CREATE FUNCTION ws_source_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p research_source%ROWTYPE;
BEGIN
  IF NEW.revises_source_id IS NOT NULL THEN
    SELECT * INTO p FROM research_source
      WHERE investor_id = NEW.investor_id AND id = NEW.revises_source_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'revised source not found'; END IF;
    IF p.adapter_id IS DISTINCT FROM NEW.adapter_id
       OR p.origin_item_id IS DISTINCT FROM NEW.origin_item_id THEN
      RAISE EXCEPTION 'a revision must come from the same origin item';
    END IF;
    IF p.content_sha256 IS NOT DISTINCT FROM NEW.content_sha256 THEN
      RAISE EXCEPTION 'identical bytes are not a revision';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ws_z_source_revision BEFORE INSERT ON research_source
  FOR EACH ROW EXECUTE FUNCTION ws_source_revision_guard();

-- The 049 guard, with exactly one widening: supersession may cross from a source to the
-- source that REVISES it. Everything else is unchanged and re-stated so this file is the
-- single current definition.
CREATE OR REPLACE FUNCTION ws_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p research_fact%ROWTYPE;
BEGIN
  IF NEW.verification = 'VERIFIED' THEN
    IF ws_human() THEN
      IF NEW.verified_by IS NULL OR NEW.verified_by IS DISTINCT FROM ws_actor() THEN
        RAISE EXCEPTION 'verification must be attributed to the authenticated human';
      END IF;
      IF NEW.supersedes_id IS NULL THEN
        RAISE EXCEPTION 'human verification must recheck an existing fact';
      END IF;
    ELSIF pg_has_role(current_user,'trackr_ws_engine','member') THEN
      IF NEW.verified_by IS NOT NULL THEN
        RAISE EXCEPTION 'engine verification cannot be attributed to a person';
      END IF;
    ELSE
      RAISE EXCEPTION 'this capability cannot verify a fact';
    END IF;
  ELSIF NEW.verified_by IS NOT NULL THEN
    RAISE EXCEPTION 'an unverified fact cannot name a verifier';
  END IF;

  IF NEW.supersedes_id IS NOT NULL THEN
    SELECT * INTO p FROM research_fact
      WHERE investor_id = NEW.investor_id AND id = NEW.supersedes_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'superseded fact not found'; END IF;
    IF p.isin <> NEW.isin OR p.metric <> NEW.metric OR p.basis <> NEW.basis
       OR p.unit <> NEW.unit OR p.cadence <> NEW.cadence
       OR p.period_start <> NEW.period_start OR p.period_end <> NEW.period_end THEN
      RAISE EXCEPTION 'supersession must stay within one series';
    END IF;
    IF p.source_id <> NEW.source_id THEN
      -- The one widening (055): the new source revises the old one. A revised document's
      -- extraction is unverified like any other; it cannot arrive VERIFIED.
      IF NEW.verification = 'VERIFIED' OR NOT EXISTS (
           SELECT 1 FROM research_source s
            WHERE s.investor_id = NEW.investor_id AND s.id = NEW.source_id
              AND s.revises_source_id = p.source_id) THEN
        RAISE EXCEPTION 'only a same-source revision may supersede a fact';
      END IF;
    END IF;
    IF NEW.verification = 'VERIFIED' AND (
         p.numeric_value IS DISTINCT FROM NEW.numeric_value
      OR p.boolean_value IS DISTINCT FROM NEW.boolean_value
      OR p.text_value IS DISTINCT FROM NEW.text_value) THEN
      RAISE EXCEPTION 'verification must copy the checked value unchanged';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------------
-- research_event -- one dated corporate event with an exact identity
-- ---------------------------------------------------------------------------------
CREATE TABLE research_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text NOT NULL,
  source_id uuid NOT NULL,
  event_type text NOT NULL CHECK (event_type IN
    ('BOARD_MEETING','FINANCIAL_RESULTS','AGM','EGM','DIVIDEND','BONUS','SPLIT','BUYBACK',
     'RIGHTS_ISSUE','MERGER_OR_DEMERGER','AUDITOR_CHANGE','CREDIT_RATING',
     'REGULATORY_ORDER','MANAGEMENT_CHANGE')),
  -- SCHEDULED: a published future date. OBSERVED: it happened and has a filed record.
  -- CLAIMED: somebody said it would happen (rumour, press, analyst) -- never a date.
  -- CANCELLED: a later announcement withdrew an earlier SCHEDULED event.
  status text NOT NULL CHECK (status IN ('SCHEDULED','OBSERVED','CLAIMED','CANCELLED')),
  event_date date,
  date_precision text NOT NULL CHECK (date_precision IN ('EXACT','IMPRECISE','UNKNOWN')),
  period_start date,
  period_end date,
  -- What the source says, attributed; for CLAIMED this is the claim, quoted.
  description ws_text NOT NULL,
  -- Where in the source (page, section, announcement id). "The filing" is not a citation.
  locator ws_text NOT NULL,
  verification text NOT NULL CHECK (verification IN ('UNVERIFIED','VERIFIED')),
  verified_by uuid REFERENCES app_user(id),
  verification_reference ws_text,
  -- A verification copy of an earlier row (same identity, same source).
  supersedes_id uuid,
  -- A later announcement that moves or cancels an earlier event. The earlier row stays.
  reschedules_id uuid,
  dedupe_key ws_text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,isin,source_id) REFERENCES research_source(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,supersedes_id) REFERENCES research_event(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,reschedules_id) REFERENCES research_event(investor_id,isin,id),
  -- A date field holds an exact published date or nothing. An imprecise ("second week of")
  -- or unknown date is NULL; the wording stays in `description`.
  CHECK ((event_date IS NOT NULL) = (date_precision = 'EXACT')),
  CHECK (status <> 'CLAIMED' OR (event_date IS NULL AND verification = 'UNVERIFIED')),
  CHECK (status <> 'CANCELLED' OR reschedules_id IS NOT NULL),
  CHECK ((period_start IS NULL) = (period_end IS NULL)),
  CHECK (period_start IS NULL OR period_end >= period_start),
  CHECK (event_type <> 'FINANCIAL_RESULTS' OR period_start IS NOT NULL),
  CHECK (verification <> 'VERIFIED' OR verification_reference IS NOT NULL),
  CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  CHECK (reschedules_id IS NULL OR reschedules_id <> id),
  CHECK (supersedes_id IS NULL OR reschedules_id IS NULL)
);

COMMENT ON TABLE research_event IS
    'A dated corporate event with exact identity (type, ISIN, period, source). SCHEDULED vs '
    'OBSERVED is data, not wording; CLAIMED events never carry a date; a moved or cancelled '
    'event is a new row naming the one it reschedules. Extraction is UNVERIFIED; only a '
    'person''s recheck produces VERIFIED. Nothing here is a trading signal.';

CREATE UNIQUE INDEX research_event_one_successor
  ON research_event(investor_id,supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX research_event_by_company ON research_event(investor_id,isin,seq DESC);

CREATE FUNCTION ws_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p research_event%ROWTYPE;
BEGIN
  IF NEW.verification = 'VERIFIED' THEN
    IF NOT ws_human() OR NEW.verified_by IS NULL OR NEW.verified_by IS DISTINCT FROM ws_actor()
    THEN
      RAISE EXCEPTION 'event verification must be attributed to the authenticated human';
    END IF;
    IF NEW.supersedes_id IS NULL THEN
      RAISE EXCEPTION 'human verification must recheck an existing event';
    END IF;
  ELSIF NEW.verified_by IS NOT NULL THEN
    RAISE EXCEPTION 'an unverified event cannot name a verifier';
  END IF;
  IF NEW.supersedes_id IS NOT NULL THEN
    SELECT * INTO p FROM research_event
      WHERE investor_id = NEW.investor_id AND id = NEW.supersedes_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'superseded event not found'; END IF;
    IF p.source_id <> NEW.source_id OR p.event_type <> NEW.event_type
       OR p.status <> NEW.status OR p.event_date IS DISTINCT FROM NEW.event_date
       OR p.date_precision <> NEW.date_precision
       OR p.period_start IS DISTINCT FROM NEW.period_start
       OR p.period_end IS DISTINCT FROM NEW.period_end
       OR p.reschedules_id IS DISTINCT FROM NEW.reschedules_id THEN
      RAISE EXCEPTION 'an event verification must copy the checked event unchanged';
    END IF;
  END IF;
  IF NEW.reschedules_id IS NOT NULL THEN
    SELECT * INTO p FROM research_event
      WHERE investor_id = NEW.investor_id AND id = NEW.reschedules_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'rescheduled event not found'; END IF;
    IF p.event_type <> NEW.event_type OR p.status NOT IN ('SCHEDULED') THEN
      RAISE EXCEPTION 'only a scheduled event of the same type can be moved or cancelled';
    END IF;
    IF NEW.status NOT IN ('SCHEDULED','CANCELLED') THEN
      RAISE EXCEPTION 'a reschedule is a new scheduled date or a cancellation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ws_insert BEFORE INSERT ON research_event
  FOR EACH ROW EXECUTE FUNCTION ws_insert_guard();
CREATE TRIGGER ws_z_event_guard BEFORE INSERT ON research_event
  FOR EACH ROW EXECUTE FUNCTION ws_event_guard();
CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON research_event
  FOR EACH ROW EXECUTE FUNCTION ws_append_only();
CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON research_event
  FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate();

ALTER TABLE research_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_event FORCE ROW LEVEL SECURITY;
CREATE POLICY ws_tenant_rows ON research_event
  USING (investor_id = ws_tenant()) WITH CHECK (investor_id = ws_tenant());
REVOKE ALL ON research_event FROM PUBLIC;
GRANT SELECT ON research_event TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,
  trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser;
GRANT DELETE ON research_event TO trackr_ws_eraser;
GRANT INSERT ON research_event TO trackr_ws_human;
GRANT USAGE ON SEQUENCE research_event_seq_seq TO trackr_ws_human;
