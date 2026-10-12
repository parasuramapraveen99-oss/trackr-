-- 053_butler_notebook.sql -- Butler's consented, user-visible reasoning notebook (BT 12-15).
--
-- docs/BUTLER_INVESTOR_MEMORY.md sections 2-7. READ docs/ARCHITECTURE_INVARIANTS.md FIRST.
-- Nothing here is a financial record: no holding, price, transaction or valuation is stored,
-- and nothing below can become an input to one. Nothing here is a fact about a company either:
-- a report-generation packet may never read these tables (app/research/report_contract.py
-- refuses notebook keys, and `make lint` refuses a report module importing notebook code).
--
-- WHAT THE NOTEBOOK IS. A private record of OBSERVABLE RESEARCH PROCESS the investor opted in to
-- having Butler keep: what they said about themselves, bounded episodes built by code from their
-- own saved reviews/theses, and tentative hypotheses over those episodes that always carry their
-- counterexamples. It is not a personality type, a diagnosis, a sincerity judgement, a
-- suitability score or a hidden dossier: every row is readable by the person it describes,
-- through Settings -> Butler -> Understanding you.
--
-- FOUR LAYERS, FOUR TABLES, NEVER MERGED (section 3):
--   butler_self_description   what the person said, verbatim, human-only
--   butler_episode            what code observed, engine-only, with its denominator/limits
--   butler_pattern_revision   a tentative interpretation, engine-only, origin CODE or MODEL
--   butler_coaching_use       which question was offered because of which revision
-- A model conclusion is never stored as an episode, so it can never be cited to support itself.
--
-- CONSENT IS CHECKED IN THE DATABASE AT PERSISTENCE, not only in Python. Every engine write
-- below takes the tenant advisory lock (ws_insert_guard) and then refuses unless the LATEST
-- consent row is active and names the same consent version the writer sealed. Consent rows are
-- written under the same lock, so a revocation racing a worker is serialised: whichever commits
-- second sees the other, and a result sealed under a revoked version cannot land.
--
-- APPEND-ONLY AND EVENT-SOURCED like 051/052. A dispute, a disabled pattern, a deleted pattern,
-- an excluded episode and a paused learning state are appended rows; current state is derived on
-- read. Physical erasure is the eraser capability's (the existing privacy path, plus
-- app/db/butler_notebook.py::purge_notebook) and leaves butler_notebook_exclusion behind so a
-- reindex cannot silently rebuild what the person deleted.
--
-- RETENTION IS NOT DECIDED HERE. These are new SENSITIVE categories (section 7): BE-Q05 must map
-- them before live writes. workspace_privacy.py lists them UNMAPPED with category
-- INVESTOR_NOTEBOOK. The 90-day relevance rule is a staleness rule, not a retention period.
--
-- NUMBERED 053 AFTER THE ACTUAL LATEST (052).

-- ---------------------------------------------------------------------------------
-- Codes -- reviewed catalogue identifiers only, never prose
-- ---------------------------------------------------------------------------------
CREATE FUNCTION butler_codes_ok(codes text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT codes IS NOT NULL AND cardinality(codes) <= 20 AND NOT EXISTS (
    SELECT 1 FROM unnest(codes) c WHERE c IS NULL OR c !~ '^[A-Z][A-Z0-9_]{2,63}$')
$$;

-- ---------------------------------------------------------------------------------
-- 1. Consent -- versioned, off by default, separate from signup and from follows
-- ---------------------------------------------------------------------------------
--
-- No row == never opted in. Nothing in account creation, portfolio import, a company follow or
-- a report request writes here: only POST /butler/notebook/consent, as the human capability.
--
-- `sources` is the person's explicit category choice. PORTFOLIO_LINKED_DECISIONS is its own
-- category because portfolio trades/performance are excluded unless separately chosen.
-- `include_from` is the earliest date whose saved records may be read. NULL means "from the
-- moment of opt-in" -- a retrospective backfill requires an explicit earlier date, and the
-- actual earliest included date is always displayed. `reflection_enabled` is the SEPARATE
-- opt-in for the bounded hosted daily reflection job (BT-P02); deterministic episodes need no
-- model and do not depend on it.
CREATE TABLE butler_learning_consent (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version integer NOT NULL CHECK (version > 0),
  action text NOT NULL CHECK (action IN ('OPT_IN','UPDATE_SCOPE','PAUSE','RESUME','REVOKE')),
  disclosure_version text NOT NULL CHECK (disclosure_version ~ '^[a-z0-9.-]{3,40}$'),
  sources text[] NOT NULL DEFAULT '{}',
  include_from date,
  reflection_enabled boolean NOT NULL DEFAULT false,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,version),
  UNIQUE (investor_id,dedupe_key),
  CHECK (sources <@ ARRAY['RESEARCH_NOTES','REVIEW_RESPONSES','BUTLER_CONVERSATION',
                          'DECISION_RECEIPTS','PORTFOLIO_LINKED_DECISIONS']::text[]),
  CHECK (action NOT IN ('OPT_IN','UPDATE_SCOPE') OR cardinality(sources) > 0),
  CHECK (action NOT IN ('PAUSE','REVOKE') OR NOT reflection_enabled),
  CHECK (include_from IS NULL OR include_from >= DATE '2000-01-01')
);

COMMENT ON TABLE butler_learning_consent IS
    'Versioned notebook consent. Absent == off. Written only by the person through the notebook '
    'consent endpoint; signup, imports, follows and reports never write it. Checked at capture, '
    'packet assembly, invocation, persistence (triggers below) and read.';

-- ---------------------------------------------------------------------------------
-- 2. Explicit self-description -- the person's own words, versioned by chain
-- ---------------------------------------------------------------------------------
CREATE TABLE butler_self_description (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text,
  statement ws_text NOT NULL,
  previous_id uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,previous_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,previous_id) REFERENCES butler_self_description,
  CHECK (length(btrim(statement)) BETWEEN 1 AND 1000),
  CHECK (previous_id IS NULL OR previous_id <> id)
);

COMMENT ON TABLE butler_self_description IS
    'What the investor said about their own process, verbatim. A claim, not verified behaviour.';

-- ---------------------------------------------------------------------------------
-- 3. Observable episodes -- built by code from the investor's own saved records
-- ---------------------------------------------------------------------------------
--
-- ONE EPISODE PER (catalogue entry, anchor record), ever. The anchor is the saved record the
-- episode is about (a review, a thesis version, a response). A rebuild with a newer builder
-- version supersedes by `supersedes_id`, it never adds a second countable copy -- which is what
-- stops "the same review seen three times" from satisfying a three-episode threshold.
--
-- `polarity` is relative to the catalogue entry: SUPPORTS, COUNTERS (a counterexample -- tracked
-- as carefully as a lapse) or UNDETERMINED (the opportunity existed but code could not decide,
-- e.g. a free-text horizon it cannot read). `knowable_at` is the evidence cutoff: what the
-- record showed at the time, not what is known now. `limitations` are reviewed codes, and
-- OUTSIDE_WORK_NOT_RECORDED is on every episode because it is always true.
CREATE TABLE butler_episode (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  catalogue_id text NOT NULL CHECK (catalogue_id ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  polarity text NOT NULL CHECK (polarity IN ('SUPPORTS','COUNTERS','UNDETERMINED')),
  observation_code text NOT NULL CHECK (observation_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  source_category text NOT NULL CHECK (source_category IN
    ('RESEARCH_NOTES','REVIEW_RESPONSES','BUTLER_CONVERSATION','DECISION_RECEIPTS',
     'PORTFOLIO_LINKED_DECISIONS')),
  comparability_key text NOT NULL CHECK (comparability_key ~ '^[A-Z0-9_:.-]{3,120}$'),
  isin text NOT NULL,
  occurred_on date NOT NULL,
  knowable_at timestamptz NOT NULL,
  anchor_kind text NOT NULL CHECK (anchor_kind IN ('REVIEW','THESIS','RESPONSE')),
  anchor_id uuid NOT NULL,
  review_id uuid,
  thesis_id uuid,
  prior_thesis_id uuid,
  response_id uuid,
  evaluation_id uuid,
  limitations text[] NOT NULL DEFAULT '{}' CHECK (butler_codes_ok(limitations)),
  builder_version text NOT NULL CHECK (builder_version ~ '^[a-z0-9.-]{3,40}$'),
  consent_version integer NOT NULL,
  supersedes_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  UNIQUE (investor_id,supersedes_id),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,review_id) REFERENCES butler_review,
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis,
  FOREIGN KEY (investor_id,prior_thesis_id) REFERENCES thesis,
  FOREIGN KEY (investor_id,response_id) REFERENCES butler_review_response,
  FOREIGN KEY (investor_id,evaluation_id) REFERENCES trigger_evaluation,
  FOREIGN KEY (investor_id,supersedes_id) REFERENCES butler_episode,
  FOREIGN KEY (investor_id,consent_version) REFERENCES butler_learning_consent(investor_id,version),
  CHECK (anchor_kind <> 'REVIEW' OR anchor_id = review_id),
  CHECK (anchor_kind <> 'THESIS' OR anchor_id = thesis_id),
  CHECK (anchor_kind <> 'RESPONSE' OR anchor_id = response_id),
  CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);

COMMENT ON TABLE butler_episode IS
    'One bounded, attributable observation built by deterministic code from the investor''s own '
    'saved records, relative to one reviewed catalogue entry. Carries its counter-polarity and '
    'limitations. Never written from model output. One countable episode per anchor record.';

-- ---------------------------------------------------------------------------------
-- 4. Patterns -- a hypothesis identity, and immutable revisions of it
-- ---------------------------------------------------------------------------------
--
-- ONE PATTERN PER (catalogue entry, scope). scope_key is 'INVESTOR' or the ISIN. A deleted or
-- disabled pattern is therefore found again by any rebuild and stays deleted/disabled: it
-- cannot be "rediscovered" under a new id.
CREATE TABLE butler_pattern (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  catalogue_id text NOT NULL CHECK (catalogue_id ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  scope_key text NOT NULL CHECK (scope_key = 'INVESTOR' OR scope_key ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,catalogue_id,scope_key),
  UNIQUE (investor_id,dedupe_key)
);

-- The server computes counts and status from the cited episodes; the trigger re-derives the
-- minimum-threshold facts from the rows themselves rather than trusting the numbers supplied.
-- `origin` MODEL means a hosted reflection proposal chose these ids and code accepted them;
-- the counts are still code's. SUSPENDED_PENDING_RECHECK is written when a source correction or
-- user correction touches a cited episode (section 4: "immediately suspends").
CREATE TABLE butler_pattern_revision (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  pattern_id uuid NOT NULL,
  revision integer NOT NULL CHECK (revision > 0),
  status text NOT NULL CHECK (status IN
    ('TENTATIVE','REPEATED_IN_RECORDS','DISPUTED_BY_USER','SUSPENDED_PENDING_RECHECK','STALE',
     'RETIRED')),
  origin text NOT NULL CHECK (origin IN ('CODE','MODEL')),
  supporting_episode_ids uuid[] NOT NULL DEFAULT '{}',
  counter_episode_ids uuid[] NOT NULL DEFAULT '{}',
  eligible_count integer NOT NULL CHECK (eligible_count >= 0),
  supporting_count integer NOT NULL CHECK (supporting_count >= 0),
  counter_count integer NOT NULL CHECK (counter_count >= 0),
  undetermined_count integer NOT NULL CHECK (undetermined_count >= 0),
  distinct_dates integer NOT NULL CHECK (distinct_dates >= 0),
  denominator_known boolean NOT NULL,
  window_start date NOT NULL,
  window_end date NOT NULL,
  alternative_ids text[] NOT NULL DEFAULT '{}' CHECK (butler_codes_ok(alternative_ids)),
  question_ids text[] NOT NULL DEFAULT '{}' CHECK (butler_codes_ok(question_ids)),
  limitations text[] NOT NULL DEFAULT '{}' CHECK (butler_codes_ok(limitations)),
  reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  consent_version integer NOT NULL,
  reflection_job_id uuid,
  rule_version text NOT NULL CHECK (rule_version ~ '^[a-z0-9.-]{3,40}$'),
  evaluated_at timestamptz NOT NULL DEFAULT now(),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,pattern_id,revision),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,pattern_id) REFERENCES butler_pattern,
  FOREIGN KEY (investor_id,consent_version) REFERENCES butler_learning_consent(investor_id,version),
  CHECK (window_end >= window_start),
  CHECK (eligible_count >= supporting_count + counter_count + undetermined_count),
  CHECK (cardinality(supporting_episode_ids) <= 50 AND cardinality(counter_episode_ids) <= 50),
  CHECK (NOT (supporting_episode_ids && counter_episode_ids)),
  CHECK (origin = 'CODE' OR reflection_job_id IS NOT NULL),
  CHECK (status <> 'REPEATED_IN_RECORDS' OR (supporting_count >= 3 AND distinct_dates >= 2))
);

COMMENT ON TABLE butler_pattern_revision IS
    'An immutable revision of a tentative process hypothesis: status, the exact supporting and '
    'counter episodes, alternatives and question ids, all reviewed catalogue codes. Provenance '
    'states, not personality confidence. Never a user note, never a verified fact about a person.';

-- ---------------------------------------------------------------------------------
-- 5. The person's controls -- dispute, correct, disable, exclude, delete, pause scope
-- ---------------------------------------------------------------------------------
--
-- Disagreement is recorded as disagreement. Nothing reads a DISPUTE as evidence about the
-- person; the only effect is that the pattern becomes DISPUTED_BY_USER and stops being used
-- for personality-directed prompts. `note` is the person's own words and optional.
CREATE TABLE butler_notebook_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  action text NOT NULL CHECK (action IN
    ('DISPUTE','WITHDRAW_DISPUTE','DISABLE','ENABLE','DELETE','CORRECT_EPISODE',
     'EXCLUDE_EPISODE','RETRACT_SELF_DESCRIPTION')),
  pattern_id uuid,
  episode_id uuid,
  self_description_id uuid,
  correction_code text CHECK (correction_code IN
    ('WRONG_RECORD','NOT_COMPARABLE','CIRCUMSTANCES_CHANGED','OUTSIDE_WORK_NOT_RECORDED',
     'LONG_HORIZON_INTENDED','OTHER')),
  note text CHECK (note IS NULL OR length(btrim(note)) BETWEEN 1 AND 1000),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,pattern_id) REFERENCES butler_pattern,
  FOREIGN KEY (investor_id,episode_id) REFERENCES butler_episode,
  FOREIGN KEY (investor_id,self_description_id) REFERENCES butler_self_description,
  CHECK (num_nonnulls(pattern_id,episode_id,self_description_id) = 1),
  CHECK ((action IN ('DISPUTE','WITHDRAW_DISPUTE','DISABLE','ENABLE','DELETE'))
         = (pattern_id IS NOT NULL)),
  CHECK ((action IN ('CORRECT_EPISODE','EXCLUDE_EPISODE')) = (episode_id IS NOT NULL)),
  CHECK ((action = 'RETRACT_SELF_DESCRIPTION') = (self_description_id IS NOT NULL)),
  CHECK ((action = 'CORRECT_EPISODE') = (correction_code IS NOT NULL))
);

COMMENT ON TABLE butler_notebook_event IS
    'The investor''s notebook controls. A dispute is disagreement, never evidence of '
    'defensiveness. State is derived from the latest relevant event.';

-- THE REPLAY FENCE. Minimal by design -- identifiers and a reason code, no content -- so it can
-- outlive a purge of everything it protects. A rebuild that meets an excluded anchor, catalogue
-- entry or scope skips it. Written by the person (delete/exclude); survives purge_notebook.
CREATE TABLE butler_notebook_exclusion (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  target_kind text NOT NULL CHECK (target_kind IN ('ANCHOR','PATTERN_SCOPE','ALL_BEFORE')),
  anchor_id uuid,
  catalogue_id text CHECK (catalogue_id ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  scope_key text,
  before_at timestamptz,
  reason_code text NOT NULL CHECK (reason_code IN
    ('USER_DELETED_PATTERN','USER_EXCLUDED_EPISODE','USER_CORRECTED_RECORD',
     'USER_DELETED_NOTEBOOK')),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  CHECK ((target_kind = 'ANCHOR') = (anchor_id IS NOT NULL)),
  CHECK ((target_kind = 'PATTERN_SCOPE') = (catalogue_id IS NOT NULL AND scope_key IS NOT NULL)),
  CHECK ((target_kind = 'ALL_BEFORE') = (before_at IS NOT NULL))
);

COMMENT ON TABLE butler_notebook_exclusion IS
    'Replay fence: identifiers only. Deleted/excluded notebook material cannot be rebuilt from '
    'the same records. Survives notebook purge; erased with the investor.';

-- ---------------------------------------------------------------------------------
-- 6. Influence history -- which personalised question was offered, and why
-- ---------------------------------------------------------------------------------
--
-- The "Why this question?" link reads this row: the question id, the review it was offered in
-- and the exact pattern revision (and so the exact episodes) that made it applicable.
CREATE TABLE butler_coaching_use (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  question_id text NOT NULL CHECK (question_id ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  pattern_revision_id uuid NOT NULL,
  review_id uuid NOT NULL,
  consent_version integer NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,review_id,question_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,pattern_revision_id) REFERENCES butler_pattern_revision,
  FOREIGN KEY (investor_id,review_id) REFERENCES butler_review,
  FOREIGN KEY (investor_id,consent_version) REFERENCES butler_learning_consent(investor_id,version)
);

-- ---------------------------------------------------------------------------------
-- Guards
-- ---------------------------------------------------------------------------------

-- The consent in force for a tenant: (version, active?). Active means the latest action is
-- OPT_IN, UPDATE_SCOPE or RESUME. PAUSE and REVOKE both stop new capture and analysis.
CREATE FUNCTION butler_consent_now(inv uuid, OUT version integer, OUT active boolean,
                                   OUT sources text[], OUT include_from date,
                                   OUT reflection boolean, OUT opted_in_at timestamptz)
LANGUAGE sql STABLE AS $$
  SELECT c.version, c.action IN ('OPT_IN','UPDATE_SCOPE','RESUME'), c.sources,
         COALESCE(c.include_from, (SELECT (o.recorded_at AT TIME ZONE 'UTC')::date
                                     FROM butler_learning_consent o
                                    WHERE o.investor_id = inv AND o.action = 'OPT_IN'
                                      AND o.version <= c.version
                                    ORDER BY o.version DESC LIMIT 1)),
         c.reflection_enabled,
         (SELECT o.recorded_at FROM butler_learning_consent o
           WHERE o.investor_id = inv AND o.action = 'OPT_IN' AND o.version <= c.version
           ORDER BY o.version DESC LIMIT 1)
    FROM butler_learning_consent c WHERE c.investor_id = inv
   ORDER BY c.version DESC LIMIT 1
$$;

CREATE FUNCTION butler_notebook_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior integer; last_action text; cn record; e butler_episode%ROWTYPE;
        ids uuid[]; n_eligible integer; n_support integer; n_dates integer; pat butler_pattern%ROWTYPE;
BEGIN
  IF TG_TABLE_NAME IN ('butler_learning_consent','butler_self_description',
      'butler_notebook_event','butler_notebook_exclusion') AND NOT ws_human() THEN
    RAISE EXCEPTION 'butler human write required';
  END IF;
  IF TG_TABLE_NAME IN ('butler_episode','butler_pattern','butler_pattern_revision')
     AND ws_human() THEN
    -- A person does not author a hypothesis about themselves through this path; their words go
    -- to butler_self_description and their corrections to butler_notebook_event.
    RAISE EXCEPTION 'butler notebook system write required';
  END IF;

  IF TG_TABLE_NAME = 'butler_learning_consent' THEN
    SELECT COALESCE(max(version),0) INTO prior FROM butler_learning_consent
     WHERE investor_id = NEW.investor_id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'butler version conflict'; END IF;
    SELECT action INTO last_action FROM butler_learning_consent
     WHERE investor_id = NEW.investor_id ORDER BY version DESC LIMIT 1;
    IF (last_action IS NULL OR last_action = 'REVOKE') <> (NEW.action = 'OPT_IN') THEN
      RAISE EXCEPTION 'butler invalid consent transition';
    END IF;
    IF (NEW.action = 'PAUSE' AND last_action = 'PAUSE')
       OR (NEW.action = 'RESUME' AND last_action IS DISTINCT FROM 'PAUSE')
       OR (NEW.action = 'UPDATE_SCOPE' AND last_action = 'PAUSE') THEN
      RAISE EXCEPTION 'butler invalid consent transition';
    END IF;
    IF NEW.action IN ('PAUSE','RESUME','REVOKE') THEN
      -- Scope is carried forward unchanged; only OPT_IN/UPDATE_SCOPE choose it.
      SELECT sources, include_from INTO NEW.sources, NEW.include_from
        FROM butler_learning_consent WHERE investor_id = NEW.investor_id
       ORDER BY version DESC LIMIT 1;
      IF NEW.action = 'REVOKE' THEN NEW.sources := '{}'; END IF;
    END IF;
    IF NEW.include_from IS NOT NULL AND NEW.include_from > current_date THEN
      RAISE EXCEPTION 'butler consent date out of range';
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_self_description' THEN
    IF NEW.previous_id IS NULL THEN
      NULL;
    ELSIF NOT EXISTS (SELECT 1 FROM butler_self_description WHERE investor_id = NEW.investor_id
                      AND id = NEW.previous_id AND isin IS NOT DISTINCT FROM NEW.isin) THEN
      RAISE EXCEPTION 'butler response revision must name predecessor';
    END IF;

  ELSIF TG_TABLE_NAME IN ('butler_episode','butler_pattern_revision','butler_coaching_use') THEN
    SELECT * INTO cn FROM butler_consent_now(NEW.investor_id);
    IF cn.version IS NULL OR NOT cn.active OR cn.version <> NEW.consent_version THEN
      RAISE EXCEPTION 'butler notebook consent not current';
    END IF;

    IF TG_TABLE_NAME = 'butler_episode' THEN
      IF NOT (NEW.source_category = ANY(cn.sources)) THEN
        RAISE EXCEPTION 'butler notebook source not consented';
      END IF;
      IF NEW.occurred_on < cn.include_from THEN
        RAISE EXCEPTION 'butler notebook source not consented';
      END IF;
      IF EXISTS (SELECT 1 FROM butler_notebook_exclusion x WHERE x.investor_id = NEW.investor_id
                 AND ((x.target_kind = 'ANCHOR' AND x.anchor_id = NEW.anchor_id)
                   OR (x.target_kind = 'ALL_BEFORE' AND NEW.knowable_at <= x.before_at))) THEN
        RAISE EXCEPTION 'butler notebook record excluded';
      END IF;
      IF NEW.supersedes_id IS NULL THEN
        IF EXISTS (SELECT 1 FROM butler_episode WHERE investor_id = NEW.investor_id
                   AND catalogue_id = NEW.catalogue_id AND anchor_id = NEW.anchor_id) THEN
          RAISE EXCEPTION 'butler episode already recorded';
        END IF;
      ELSE
        SELECT * INTO e FROM butler_episode WHERE investor_id = NEW.investor_id
           AND id = NEW.supersedes_id;
        IF NOT FOUND OR e.catalogue_id <> NEW.catalogue_id OR e.anchor_id <> NEW.anchor_id THEN
          RAISE EXCEPTION 'butler episode supersession must keep its anchor';
        END IF;
      END IF;

    ELSIF TG_TABLE_NAME = 'butler_pattern_revision' THEN
      SELECT * INTO pat FROM butler_pattern WHERE investor_id = NEW.investor_id
         AND id = NEW.pattern_id;
      SELECT COALESCE(max(revision),0) INTO prior FROM butler_pattern_revision
       WHERE investor_id = NEW.investor_id AND pattern_id = NEW.pattern_id;
      IF NEW.revision <> prior + 1 THEN RAISE EXCEPTION 'butler version conflict'; END IF;
      -- A deleted pattern, or one whose scope was excluded, is never revised again.
      IF EXISTS (SELECT 1 FROM butler_notebook_event WHERE investor_id = NEW.investor_id
                 AND pattern_id = NEW.pattern_id AND action = 'DELETE')
         OR EXISTS (SELECT 1 FROM butler_notebook_exclusion x WHERE x.investor_id = NEW.investor_id
                 AND x.target_kind = 'PATTERN_SCOPE' AND x.catalogue_id = pat.catalogue_id
                 AND x.scope_key = pat.scope_key) THEN
        RAISE EXCEPTION 'butler notebook record excluded';
      END IF;
      ids := NEW.supporting_episode_ids || NEW.counter_episode_ids;
      -- Every cited episode exists, belongs to this catalogue entry and scope, is the CURRENT
      -- (unsuperseded) copy, carries the cited polarity and has not been excluded or corrected.
      IF EXISTS (
        SELECT 1 FROM unnest(ids) i WHERE NOT EXISTS (
          SELECT 1 FROM butler_episode ep WHERE ep.investor_id = NEW.investor_id AND ep.id = i
             AND ep.catalogue_id = pat.catalogue_id
             AND (pat.scope_key = 'INVESTOR' OR ep.isin = pat.scope_key)
             AND NOT EXISTS (SELECT 1 FROM butler_episode s WHERE s.investor_id = ep.investor_id
                             AND s.supersedes_id = ep.id)
             AND NOT EXISTS (SELECT 1 FROM butler_notebook_event ne
                             WHERE ne.investor_id = ep.investor_id AND ne.episode_id = ep.id)
             AND NOT EXISTS (SELECT 1 FROM butler_notebook_exclusion x
                             WHERE x.investor_id = ep.investor_id AND x.target_kind = 'ANCHOR'
                               AND x.anchor_id = ep.anchor_id)))
      THEN
        RAISE EXCEPTION 'butler pattern cites an ineligible episode';
      END IF;
      IF EXISTS (SELECT 1 FROM butler_episode ep WHERE ep.investor_id = NEW.investor_id
                 AND ((ep.id = ANY(NEW.supporting_episode_ids) AND ep.polarity <> 'SUPPORTS')
                   OR (ep.id = ANY(NEW.counter_episode_ids) AND ep.polarity <> 'COUNTERS'))) THEN
        RAISE EXCEPTION 'butler pattern cites an ineligible episode';
      END IF;
      -- Re-derive the threshold facts from the rows: DISTINCT anchors and dates, never the
      -- supplied integers. Copies of one episode cannot reach three.
      SELECT count(DISTINCT ep.anchor_id), count(DISTINCT ep.occurred_on)
        INTO n_support, n_dates
        FROM butler_episode ep WHERE ep.investor_id = NEW.investor_id
         AND ep.id = ANY(NEW.supporting_episode_ids);
      IF n_support <> NEW.supporting_count OR cardinality(NEW.supporting_episode_ids) <> n_support
         OR cardinality(NEW.counter_episode_ids) <> NEW.counter_count THEN
        RAISE EXCEPTION 'butler pattern counts do not match cited episodes';
      END IF;
      IF NEW.status = 'REPEATED_IN_RECORDS' AND (n_support < 3 OR n_dates < 2) THEN
        RAISE EXCEPTION 'butler pattern counts do not match cited episodes';
      END IF;
      SELECT count(DISTINCT ep.occurred_on) INTO n_eligible FROM butler_episode ep
       WHERE ep.investor_id = NEW.investor_id AND ep.id = ANY(ids);
      IF n_eligible <> NEW.distinct_dates THEN
        RAISE EXCEPTION 'butler pattern counts do not match cited episodes';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'butler_notebook_event' THEN
    IF NEW.action IN ('DISPUTE','DISABLE','ENABLE','WITHDRAW_DISPUTE') AND EXISTS (
       SELECT 1 FROM butler_notebook_event WHERE investor_id = NEW.investor_id
          AND pattern_id = NEW.pattern_id AND action = 'DELETE') THEN
      RAISE EXCEPTION 'butler notebook record excluded';
    END IF;
  END IF;

  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------------
-- Install: generic guards, notebook guard, RLS and grants -- same shape as 051/052
-- ---------------------------------------------------------------------------------
DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'butler_learning_consent','butler_self_description','butler_episode','butler_pattern',
    'butler_pattern_revision','butler_notebook_event','butler_notebook_exclusion',
    'butler_coaching_use'
  ] LOOP
    EXECUTE format('CREATE TRIGGER ws_insert BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION ws_insert_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_z_notebook BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION butler_notebook_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ws_append_only()',tbl);
    EXECUTE format('CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate()',tbl);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tbl);
    EXECUTE format('CREATE POLICY ws_tenant_rows ON %I USING (investor_id=ws_tenant()) WITH CHECK (investor_id=ws_tenant())',tbl);
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',tbl);
    -- PURPOSE-BOUND READ. The runner (browser model output) and the scheduler have no reason
    -- to read a person's notebook and get no SELECT: a browser task packet cannot be assembled
    -- from it. Human (the person's own reads/writes), engine (the deterministic builder and
    -- the hosted reflection worker), reader (the person's GET routes) and eraser only.
    EXECUTE format('GRANT SELECT ON %I TO trackr_ws_human,trackr_ws_engine,trackr_ws_reader,trackr_ws_eraser',tbl);
    EXECUTE format('GRANT DELETE ON %I TO trackr_ws_eraser',tbl);
  END LOOP;
END $$;

-- The person's own records and controls.
GRANT INSERT ON butler_learning_consent,butler_self_description,butler_notebook_event,
  butler_notebook_exclusion,butler_coaching_use TO trackr_ws_human;
-- The deterministic episode builder and the reflection worker: system notes only.
GRANT INSERT ON butler_episode,butler_pattern,butler_pattern_revision,butler_coaching_use
  TO trackr_ws_engine;
GRANT USAGE ON SEQUENCE butler_self_description_seq_seq,butler_notebook_event_seq_seq,
  butler_coaching_use_seq_seq TO trackr_ws_human;
GRANT USAGE ON SEQUENCE butler_episode_seq_seq,butler_pattern_revision_seq_seq,
  butler_coaching_use_seq_seq TO trackr_ws_engine;
GRANT EXECUTE ON FUNCTION butler_consent_now(uuid) TO trackr_ws_human,trackr_ws_engine,
  trackr_ws_reader,trackr_ws_eraser;

CREATE INDEX butler_consent_latest ON butler_learning_consent(investor_id,version DESC);
CREATE INDEX butler_episode_catalogue ON butler_episode(investor_id,catalogue_id,occurred_on);
CREATE INDEX butler_episode_anchor ON butler_episode(investor_id,anchor_id);
CREATE INDEX butler_pattern_revisions ON butler_pattern_revision(investor_id,pattern_id,revision DESC);
CREATE INDEX butler_notebook_events ON butler_notebook_event(investor_id,seq DESC);
CREATE INDEX butler_coaching_review ON butler_coaching_use(investor_id,review_id);
