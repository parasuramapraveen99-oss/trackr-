-- 041_workspace_commitments.sql -- rituals, questions, own answers, theses, trigger receipts.
--
-- SECOND OF SIX (040-045). Still no triggers, no RLS and no grants: see 040's header and
-- app/db/migrate.py::WORKSPACE_BUNDLE for why a partial bundle cannot be deployed.
--
-- WHAT THIS FILE IS ABOUT. 040 recorded what a source said and what the investor thinks.
-- This records what they COMMITTED to: the questions they made themselves answer before
-- forming a view, the answers in their own words, the thesis as a dated snapshot, and the
-- deterministic receipts that say whether their own stated exit conditions have been met.
--
-- THE ONE IDEA BEHIND ALL OF IT: a commitment you can quietly edit is not a commitment.
-- Every table here is append-only in shape before 044 makes it append-only in fact. A
-- thesis is not updated, it is superseded by version n+1 with the old text intact. An answer
-- is not corrected, it is succeeded. "NEEDS REVIEW" is never a column someone can set -- it
-- is derived from an evaluation receipt, so the only way to make a thesis look healthy is
-- for the evidence to say it is.
--
-- NO FINANCIAL LOGIC IS INVENTED HERE. The rule DSL below validates the SHAPE of an exit
-- condition -- that it names a metric, a basis, an operator and a decimal threshold. It says
-- nothing about which metrics or formulas are approved; that is BE-Q03 and remains open. A
-- well-formed rule is not an approved rule.

-- ---------------------------------------------------------------------------------
-- ritual -- the questions this investor made themselves answer first
-- ---------------------------------------------------------------------------------
--
-- A ritual is a CONTAINER, and its meaning is entirely in the questions attached to it.
-- Completion means the questions were recorded, NOT that they were answered: the point of
-- the gate is to force the questions to be written down before a view is formed, not to
-- pretend that writing an answer makes the answer good.
--
-- An empty ritual is refused, but not here -- it cannot be, since the questions are inserted
-- after the ritual row inside the same transaction. 044 adds a DEFERRED constraint trigger
-- that checks at COMMIT, which is the only moment the question can be asked truthfully.
CREATE TABLE ritual (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company
);

COMMENT ON TABLE ritual IS
    'A container for the questions an investor made themselves record before forming a '
    'view. Completion means the questions were RECORDED, not answered -- the gate exists '
    'to force the questions to be written down, not to pretend an answer makes them good. '
    'Migration 044 adds a deferred trigger refusing a ritual with no questions at commit.';

-- ---------------------------------------------------------------------------------
-- question -- a QUESTION note promoted to something that can block
-- ---------------------------------------------------------------------------------
--
-- NO MUTABLE OPEN/ANSWERED COLUMN, deliberately. OPEN means no answer row exists; ANSWERED
-- means at least one does. A status column would be a second answer to a question the
-- `question_answer` table already answers, and it would be the one that drifted -- a row
-- marked ANSWERED with no answer is exactly the bug it would create.
--
-- The text lives in the linked note, not here, so a question cannot be reworded after the
-- fact. A different question is a different note and a different row. `blocking` is
-- likewise fixed at creation: 044 refuses to let a thesis be published while any blocking
-- question for that company (across verified ISIN aliases) has no answer, and a flag that
-- could be cleared afterwards would make that gate optional.
CREATE TABLE question (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  note_id uuid NOT NULL,
  ritual_id uuid,
  blocking boolean NOT NULL DEFAULT false,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  -- One question per note. Promoting the same note twice would give one piece of text two
  -- independent answer chains.
  UNIQUE (investor_id,note_id),
  FOREIGN KEY (investor_id,isin,note_id) REFERENCES note(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,ritual_id) REFERENCES ritual(investor_id,isin,id)
);

COMMENT ON TABLE question IS
    'A QUESTION note promoted to something that can block a thesis. Deliberately no '
    'OPEN/ANSWERED column: OPEN means no question_answer row exists. A status column would '
    'be a second answer to a question the answer table already answers, and the one that '
    'drifts. The text lives in the note, so a question cannot be reworded after the fact.';

-- ---------------------------------------------------------------------------------
-- question_answer -- the investor's own words, in order, forever
-- ---------------------------------------------------------------------------------
--
-- THE WHOLE POINT IS THAT THE FIRST ANSWER SURVIVES. Reading what you believed two years
-- ago, in your own words, before the outcome was known, is the single most useful artifact
-- in this product -- and it only exists if a revision cannot overwrite it. A revision names
-- its predecessor; `UNIQUE (investor_id, previous_id)` gives each answer at most one
-- successor, so the chain is linear and readable.
--
-- 044 additionally refuses a second FIRST answer: if any answer exists for a question, a new
-- one must name a predecessor. Without that, two "originals" would make the chain ambiguous
-- and there would be no defined earliest answer to return.
CREATE TABLE question_answer (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  question_id uuid NOT NULL,
  seq bigserial NOT NULL UNIQUE,
  answer ws_text NOT NULL,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  -- The date the person says they answered. recorded_at is the server receipt; 044 stamps
  -- that one rather than trusting a caller, so a backdated answer cannot claim an earlier
  -- receipt than it has.
  answered_on date NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  previous_id uuid,
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,question_id,id),
  UNIQUE (investor_id,previous_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,question_id) REFERENCES question,
  -- Three columns, so a revision cannot claim a predecessor belonging to a DIFFERENT
  -- question. Chains stay inside one question.
  FOREIGN KEY (investor_id,question_id,previous_id)
    REFERENCES question_answer(investor_id,question_id,id),
  CHECK (previous_id IS NULL OR previous_id <> id)
);

COMMENT ON TABLE question_answer IS
    'The investor''s own answers, append-only and in a linear chain. Reading what you '
    'believed two years ago in your own words, before the outcome was known, is the point '
    'of this table -- and it only exists because a revision cannot overwrite the original. '
    'ANSWER_REVIEW tasks pin a specific answer id, so revising an answer cannot silently '
    'reuse an obsolete review of the previous wording.';

-- ---------------------------------------------------------------------------------
-- ws_rules_valid -- the closed grammar for an exit or variable rule
-- ---------------------------------------------------------------------------------
--
-- WHAT THIS DOES AND DOES NOT PROVE. It proves a rule is a well-formed, executable,
-- deterministic condition: an array of objects with exactly nine known keys, every scalar a
-- string, an operator from a closed set, a threshold matching the NUMERIC(20,6) grammar, a
-- cadence, a bounded period count, and no duplicate rule ids. It proves NOTHING about
-- whether `jewellery_ebit_margin` is a metric this system can compute or whether 10.5 is a
-- sensible level. Metric registries, formulas and fiscal calendars are BE-Q03 and remain
-- open; a well-formed rule is not an approved rule.
--
-- EVERY SCALAR IS A STRING, INCLUDING THE THRESHOLD. `{"threshold": 10.5}` would be an IEEE
-- double by the time anything parsed it -- a float in the middle of a falsifiability
-- condition, which is CLAUDE.md rule 1 with extra consequences: the condition that decides
-- whether someone sells is not a place for a representation error.
--
-- EXTRA KEYS ARE REJECTED, not ignored. `r - ARRAY[...] <> '{}'` is the closed-schema half:
-- an unknown key is a rule the evaluator would silently not honour, and a rule that is
-- silently not honoured is worse than one that fails loudly.
CREATE FUNCTION ws_rules_valid(j jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE r jsonb; seen text[] := '{}';
BEGIN
  IF jsonb_typeof(j) <> 'array' OR NOT ws_string_numbers(j) THEN RETURN false; END IF;
  FOR r IN SELECT value FROM jsonb_array_elements(j) LOOP
    IF jsonb_typeof(r) <> 'object'
      -- All nine keys present, and no tenth.
      OR NOT (r ?& ARRAY['id','label','metric','basis','unit','op','threshold','cadence','periods'])
      OR r - ARRAY['id','label','metric','basis','unit','op','threshold','cadence','periods'] <> '{}'
      OR EXISTS (SELECT 1 FROM jsonb_each(r) e WHERE jsonb_typeof(e.value) <> 'string')
      OR EXISTS (SELECT 1 FROM jsonb_each_text(r) e WHERE btrim(e.value) = '')
      OR r->>'op' NOT IN ('LT','LE','EQ','GE','GT','IS_TRUE','IS_FALSE')
      OR r->>'cadence' NOT IN ('QUARTER','YEAR','POINT')
      -- 1..99 consecutive periods. Bounded because "below this level for 400 quarters" is
      -- not a condition anyone is committing to, and an unbounded count is an unbounded
      -- evidence scan.
      OR r->>'periods' !~ '^[1-9][0-9]?$'
      -- A POINT observation is a single moment; "two consecutive points" has no meaning.
      OR (r->>'cadence' = 'POINT' AND r->>'periods' <> '1')
      -- Duplicate ids would make rule results unattributable to the rule that produced them.
      OR r->>'id' = ANY(seen)
    THEN RETURN false; END IF;
    IF r->>'op' IN ('IS_TRUE','IS_FALSE') THEN
      -- A boolean rule has no numeric threshold. The literal 'boolean' is required rather
      -- than an empty string so the intent is explicit in the stored rule.
      IF r->>'threshold' <> 'boolean' THEN RETURN false; END IF;
    ELSIF r->>'threshold' !~ '^-?[0-9]{1,14}(\.[0-9]{1,6})?$' THEN
      -- Same grammar as the ws_decimal domain (040). Repeated rather than referenced
      -- because a DOMAIN cannot be attached to a key inside a jsonb value.
      RETURN false;
    END IF;
    seen := array_append(seen,r->>'id');
  END LOOP;
  RETURN true;
END $$;

COMMENT ON FUNCTION ws_rules_valid(jsonb) IS
    'The closed grammar for a thesis variable or exit rule: an array of objects with '
    'exactly nine known keys, every scalar a string, a closed operator set, a '
    'NUMERIC(20,6)-shaped decimal-string threshold, and bounded consecutive periods. '
    'Proves a rule is well formed and executable; proves nothing about whether its metric '
    'or formula is approved (BE-Q03, open). Extra keys are rejected, not ignored: a rule '
    'the evaluator would silently not honour is worse than one that fails loudly.';

-- ---------------------------------------------------------------------------------
-- thesis -- a dated, complete, immutable snapshot of a view
-- ---------------------------------------------------------------------------------
--
-- VERSIONS, NOT EDITS. Each row is the whole thesis as it stood, so reading version 3 does
-- not require replaying diffs, and version 1 still says what was actually believed on the
-- day money was committed. 044 allocates versions sequentially under a tenant advisory lock
-- so two concurrent publishes cannot both become version 4.
--
-- formed_on IS IMMUTABLE ACROSS VERSIONS. 044 enforces that every later version carries the
-- same formation date as version 1. Letting it move would rewrite when a commitment began,
-- which is the one date an anniversary review is built on.
--
-- ACTIVE MEANS COMPLETE. Argument, anti-thesis, pre-mortem, intended holding period, at
-- least one variable and at least one exit, and no outstanding draft reasons. DRAFT may hold
-- incomplete prose, but must say WHY it is incomplete -- an unexplained draft is
-- indistinguishable from an abandoned one.
--
-- WELL-FORMED RULES EVEN IN A DRAFT. variables/exits are validated whatever the status,
-- because half-written rule text belongs in the human prose, not in the executable DSL. A
-- malformed rule in an ACTIVE thesis would mean a condition that never evaluates -- an exit
-- that silently never fires.
--
-- STATUS IS NOT HEALTH. There is no NEEDS_REVIEW value: that is derived from a
-- trigger_evaluation receipt. The only way to make a thesis look healthy is for the
-- evidence to say so.
CREATE TABLE thesis (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  status text NOT NULL CHECK (status IN ('DRAFT','ACTIVE','RETIRED')),
  formed_on date NOT NULL,
  version_on date NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  argument text NOT NULL DEFAULT '',
  anti_thesis text NOT NULL DEFAULT '',
  pre_mortem text NOT NULL DEFAULT '',
  intended_holding_period text NOT NULL DEFAULT '',
  variables jsonb NOT NULL DEFAULT '[]' CHECK (ws_rules_valid(variables)),
  exits jsonb NOT NULL DEFAULT '[]' CHECK (ws_rules_valid(exits)),
  draft_reasons text[] NOT NULL DEFAULT '{}',
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,isin,version),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  CHECK (version_on >= formed_on),
  CHECK (status <> 'ACTIVE' OR (
    length(btrim(argument)) > 0 AND length(btrim(anti_thesis)) > 0
    AND length(btrim(pre_mortem)) > 0 AND length(btrim(intended_holding_period)) > 0
    AND jsonb_array_length(variables) > 0 AND jsonb_array_length(exits) > 0
    AND cardinality(draft_reasons) = 0)),
  CHECK (status <> 'DRAFT' OR cardinality(draft_reasons) > 0)
);

COMMENT ON TABLE thesis IS
    'A dated, complete, immutable snapshot of a view. Versions, not edits: version 1 still '
    'says what was believed on the day money was committed. ACTIVE requires argument, '
    'anti-thesis, pre-mortem, holding period and at least one variable and one exit; DRAFT '
    'may be incomplete but must say why. There is no NEEDS_REVIEW status -- that is derived '
    'from an evaluation receipt, so the only way to look healthy is for the evidence to say '
    'so. Migration 044 allocates versions sequentially and pins formed_on across them.';

-- Close 040's forward reference. note_link.thesis_id was declared without an FK because
-- `thesis` did not exist yet; the bundle deploys atomically, so no writable window with a
-- dangling target is ever reachable.
ALTER TABLE note_link ADD CONSTRAINT note_link_thesis_fk
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis;

-- ---------------------------------------------------------------------------------
-- trigger_evaluation -- a receipt, not a cache
-- ---------------------------------------------------------------------------------
--
-- THIS IS A RECORD OF A COMPUTATION, REPRODUCIBLE FROM ITS INPUTS. engine_version,
-- evidence_cutoff and input_digest exist so that "your margin exit condition fired on 3
-- March" can be re-derived and checked rather than trusted. A receipt that cannot be
-- replayed is a number with a date on it.
--
-- FOUR STATES, AND TWO OF THEM ARE HONEST IGNORANCE. UNKNOWN means the evidence needed is
-- missing (a quarter not filed, a different basis); CONFLICT means sources disagree. Neither
-- is filled in, averaged or resolved -- a rule with no data has not passed.
--
-- NO MODEL PRODUCES THIS. State is computed deterministically from verified facts; a
-- model-asserted trigger state is refused before it reaches this table. Which facts fed the
-- computation is recorded explicitly in `evaluation_fact` and sealed by 044 to the creating
-- transaction, so the input set cannot be extended afterwards to justify the answer.
CREATE TABLE trigger_evaluation (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  thesis_id uuid NOT NULL,
  evaluated_at timestamptz NOT NULL DEFAULT now(),
  -- Evidence known as of this moment. Separate from evaluated_at so a replay can reproduce
  -- the same answer: "what did the conditions say given only what we knew then".
  evidence_cutoff timestamptz NOT NULL,
  engine_version ws_text NOT NULL,
  input_digest text NOT NULL CHECK (input_digest ~ '^[a-f0-9]{64}$'),
  state text NOT NULL CHECK (state IN ('TRIGGERED','NOT_TRIGGERED','UNKNOWN','CONFLICT')),
  rule_results jsonb NOT NULL CHECK (
    jsonb_typeof(rule_results) = 'array' AND ws_string_numbers(rule_results)),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  -- The thesis-scoped target thesis_event's three-column FK needs, so an override cannot
  -- name an evaluation belonging to a different thesis.
  UNIQUE (investor_id,thesis_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis,
  -- Cannot know the future. An evidence cutoff after the evaluation would mean the receipt
  -- claims to have used evidence that did not exist when it ran.
  CHECK (evidence_cutoff <= evaluated_at)
);

COMMENT ON TABLE trigger_evaluation IS
    'A receipt for a deterministic evaluation of a thesis''s own stated conditions, '
    'replayable from engine_version + evidence_cutoff + input_digest. UNKNOWN (evidence '
    'missing) and CONFLICT (sources disagree) are honest outcomes and are never filled in '
    'or averaged. No model produces a state here. Not a cache: nothing derives a portfolio '
    'figure from this table.';

-- Which facts fed one evaluation. Membership is sealed to the creating transaction by 044,
-- so the input set of a past evaluation cannot be extended later to justify its answer.
CREATE TABLE evaluation_fact (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  evaluation_id uuid NOT NULL,
  fact_id uuid NOT NULL,
  PRIMARY KEY (investor_id,evaluation_id,fact_id),
  FOREIGN KEY (investor_id,evaluation_id) REFERENCES trigger_evaluation,
  FOREIGN KEY (investor_id,fact_id) REFERENCES research_fact
);

-- ---------------------------------------------------------------------------------
-- thesis_event -- what the human decided, and why
-- ---------------------------------------------------------------------------------
--
-- CONVICTION IS STATED, NEVER GENERATED. Nothing computes a conviction level; a person says
-- it and gives a reason, or there is no row. `reason` is ws_text, so "why" cannot be blank.
--
-- AN OVERRIDE DOES NOT CLEAR THE TRIGGER. Deciding to hold through your own stated exit
-- condition is a legitimate act, and this records it -- permanently, against the specific
-- TRIGGERED evaluation it overrides (044 verifies that evaluation really is TRIGGERED and
-- belongs to this thesis). The evaluation stays TRIGGERED afterwards. That is the entire
-- value of the record: at the next anniversary you can read that you overrode this exact
-- condition on this exact date for this exact reason, instead of discovering that the
-- warning quietly disappeared.
--
-- NO TRADE ACTION IS IMPLIED BY ANY ROW HERE. This system is not an execution venue.
CREATE TABLE thesis_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  thesis_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('CONVICTION','OVERRIDE_EXIT_TRIGGER','REVIEWED')),
  conviction text CHECK (conviction IN ('HIGH','MEDIUM','LOW','UNDER_REVIEW')),
  reason ws_text NOT NULL,
  evaluation_id uuid,
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  occurred_on date NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,thesis_id) REFERENCES thesis,
  FOREIGN KEY (investor_id,thesis_id,evaluation_id)
    REFERENCES trigger_evaluation(investor_id,thesis_id,id),
  CHECK (kind <> 'CONVICTION' OR conviction IS NOT NULL),
  CHECK (kind <> 'OVERRIDE_EXIT_TRIGGER' OR evaluation_id IS NOT NULL)
);

COMMENT ON TABLE thesis_event IS
    'What the human decided about a thesis, with a mandatory reason. Conviction is stated, '
    'never generated. An OVERRIDE_EXIT_TRIGGER names the specific TRIGGERED evaluation it '
    'overrides and does NOT clear it -- the value of the record is being able to read, at '
    'the next review, that you overrode this exact condition on this date for this reason, '
    'rather than finding the warning had quietly disappeared. No trade action is implied.';

-- ---------------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------------

-- "Which questions for this company block a thesis." blocking is in the key because the
-- gate query filters on it.
CREATE INDEX question_company ON question(investor_id,isin,blocking);
-- Latest answer for a question. seq DESC because the chain is ordered by receipt, and
-- answered_on is a user-stated date that may repeat or go backwards.
CREATE INDEX answer_latest ON question_answer(investor_id,question_id,seq DESC);
CREATE INDEX thesis_company_latest ON thesis(investor_id,isin,version DESC);
CREATE INDEX evaluation_latest ON trigger_evaluation(investor_id,thesis_id,evaluated_at DESC);
-- Conviction history in ASCENDING order: this one is read as a timeline, front to back.
CREATE INDEX conviction_history ON thesis_event(investor_id,thesis_id,seq);
