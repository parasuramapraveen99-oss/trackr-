-- 057_monitor_event_identity.sql -- event monitors fire on an identified event, never a flag.
--
-- SESSIONS 36-37 (BE 36 schedules and due history, BE 37 verified event occurrences). BE-Q01
-- IS STILL OPEN and this file does not close it: nothing here queues a model task. It makes
-- the due history that already exists (043) able to say WHICH event an event-driven occurrence
-- is about, and refuses one that is about the wrong thing.
--
-- WHY 044's EVENT CHECK IS NOT ENOUGH (BE-V06). 044 lets a QUEUED POST_RESULTS run cite any
-- research_fact that is VERIFIED with boolean_value = true. That is "some verified flag", not
-- "this company's results for this period, from this source". A true boolean about an
-- auditor resignation would have satisfied it. The event contract now lives in 055's
-- research_event (exact type, ISIN, period, source), and this file binds monitor_run to it.
--
-- THREE CHANGES, all additive:
--
-- 1. research_event gains EARNINGS_CALL. POST_CONCALL had no event type to match, so it could
--    never have fired truthfully; a concall is a distinct event from the results filing it
--    discusses, and matching it to FINANCIAL_RESULTS would be the "adjacent disclosure" BE 37
--    forbids. Like FINANCIAL_RESULTS, it must name the period it discusses.
--
-- 2. monitor_run gains event_id + event_identity. event_identity is the event's IDENTITY
--    (type and exact period) rather than a row id: a results filing republished, mirrored,
--    corrected or delivered twice is several rows and ONE event, and
--    UNIQUE (investor_id, schedule_id, event_identity) makes it one occurrence. The ISIN is
--    not in the key because the key is already per schedule and the schedule's company is
--    fixed; family membership is checked below instead. `event_fact_id` (044) is retired for
--    new rows: no new occurrence may cite a bare fact as its event.
--
-- 3. A guard that ties each event occurrence to its event. Every non-BLOCKED_EVENT disposition
--    (SUPERSEDED_THESIS, BLOCKED_EXECUTION_POLICY, and a future QUEUED) is a MATCHED event and
--    must cite a VERIFIED, OBSERVED, exactly-dated event of the mapped type in the schedule's
--    verified ISIN family, whose identity equals event_identity. BLOCKED_EVENT must still cite
--    an event of the right type in the family -- it records a verified event that failed a
--    definitive check (source use not permitted, ambiguous period, inconsistent dates).
--
-- WHAT THIS LEAVES FOR BE 38. 044's branch still demands a verified boolean fact for a QUEUED
-- event run, and this guard refuses any event_fact_id -- so a QUEUED event run is impossible
-- in this schema. That is deliberate while BE-Q01 is open: BE 38, once the decision exists,
-- replaces 044's monitor_run branch in its own forward migration. Nothing is loosened here.

ALTER TABLE research_event DROP CONSTRAINT research_event_event_type_check;
ALTER TABLE research_event ADD CONSTRAINT research_event_event_type_check CHECK (event_type IN
  ('BOARD_MEETING','FINANCIAL_RESULTS','AGM','EGM','DIVIDEND','BONUS','SPLIT','BUYBACK',
   'RIGHTS_ISSUE','MERGER_OR_DEMERGER','AUDITOR_CHANGE','CREDIT_RATING',
   'REGULATORY_ORDER','MANAGEMENT_CHANGE','EARNINGS_CALL'));
ALTER TABLE research_event ADD CONSTRAINT research_event_call_needs_period
  CHECK (event_type <> 'EARNINGS_CALL' OR period_start IS NOT NULL);

ALTER TABLE monitor_run ADD COLUMN event_id uuid;
ALTER TABLE monitor_run ADD COLUMN event_identity text
  CHECK (event_identity IS NULL OR event_identity ~ '^[A-Z_]+\|[0-9]{4}-[0-9]{2}-[0-9]{2}\|[0-9]{4}-[0-9]{2}-[0-9]{2}$');
ALTER TABLE monitor_run ADD CONSTRAINT monitor_run_event_pair
  CHECK ((event_id IS NULL) = (event_identity IS NULL));
ALTER TABLE monitor_run ADD CONSTRAINT monitor_run_event_fk
  FOREIGN KEY (investor_id,event_id) REFERENCES research_event(investor_id,id);
-- One occurrence per schedule per EVENT, across every schedule version and every copy of the
-- filing. (043's UNIQUE (investor_id, schedule_id, due_at) still holds as well.)
CREATE UNIQUE INDEX monitor_run_one_per_event
  ON monitor_run(investor_id,schedule_id,event_identity) WHERE event_identity IS NOT NULL;

COMMENT ON COLUMN monitor_run.event_identity IS
    'TYPE|period_start|period_end of the event this occurrence is about. Several rows of one '
    'filing (republished, mirrored, corrected, delivered twice) share it, so the unique index '
    'makes them one occurrence. NULL exactly for SCHEDULED occurrences.';
COMMENT ON COLUMN monitor_run.event_fact_id IS
    'Retired by 057: no new occurrence may cite a bare fact as its event (BE-V06). Kept for '
    'history; event occurrences cite research_event through event_id.';

CREATE FUNCTION ws_monitor_run_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s monitor_schedule%ROWTYPE; e research_event%ROWTYPE; wanted text;
BEGIN
  SELECT * INTO s FROM monitor_schedule WHERE investor_id=NEW.investor_id
    AND id=NEW.schedule_id AND version=NEW.schedule_version;
  IF NOT FOUND THEN RAISE EXCEPTION 'monitor schedule version not found'; END IF;
  IF NEW.event_fact_id IS NOT NULL THEN
    RAISE EXCEPTION 'monitor event must cite a research_event, not a fact';
  END IF;
  IF s.kind = 'SCHEDULED' THEN
    IF NEW.event_id IS NOT NULL THEN
      RAISE EXCEPTION 'a scheduled occurrence has no event';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.event_id IS NULL THEN
    RAISE EXCEPTION 'an event occurrence must name its event';
  END IF;
  wanted := CASE s.kind WHEN 'POST_RESULTS' THEN 'FINANCIAL_RESULTS'
                        WHEN 'POST_CONCALL' THEN 'EARNINGS_CALL' END;
  SELECT * INTO e FROM research_event WHERE investor_id=NEW.investor_id AND id=NEW.event_id;
  IF NOT FOUND OR e.event_type IS DISTINCT FROM wanted
     OR NOT ws_same_security(s.isin, e.isin)
     OR e.period_start IS NULL
     OR NEW.event_identity IS DISTINCT FROM
        (e.event_type || '|' || to_char(e.period_start,'YYYY-MM-DD') || '|'
         || to_char(e.period_end,'YYYY-MM-DD')) THEN
    RAISE EXCEPTION 'monitor event identity mismatch';
  END IF;
  -- A matched occurrence (anything but BLOCKED_EVENT) needs an event a person verified, that
  -- actually happened, on an exact date. A claim, a scheduled date or an unverified extraction
  -- is not an event that occurred.
  IF NEW.disposition <> 'BLOCKED_EVENT' AND (e.verification <> 'VERIFIED'
     OR e.status <> 'OBSERVED' OR e.date_precision <> 'EXACT') THEN
    RAISE EXCEPTION 'monitor event is not a verified observed event';
  END IF;
  RETURN NEW;
END $$;

-- Runs after 044's ws_insert (alphabetical order of BEFORE triggers), so the tenant lock and
-- tenant check have already happened.
CREATE TRIGGER ws_z_monitor_event BEFORE INSERT ON monitor_run
  FOR EACH ROW EXECUTE FUNCTION ws_monitor_run_event_guard();
