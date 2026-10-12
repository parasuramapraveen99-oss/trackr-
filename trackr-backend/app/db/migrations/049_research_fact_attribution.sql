-- 049_research_fact_attribution.sql -- who verified a fact, and what may supersede it.
--
-- SESSION 13 (BE 13). Two holes in the bundle's fact enforcement, both found by reading 044
-- against the fact contract rather than assumed:
--
-- 1. SPOOFABLE VERIFIER. 044's generic attribution check reads COALESCE(actor_uid,
--    reviewer_uid, created_by) -- research_fact has none of those, so `verified_by` was never
--    compared with the authenticated actor. A human-capability connection could insert
--    `verification='VERIFIED', verified_by=<someone else>`; any capability with INSERT could
--    insert VERIFIED rows with no human action at all. Verification is a claim about WHO
--    checked, so the claim has to be checked.
--
-- 2. FORKABLE AND CROSS-SERIES SUPERSESSION. Nothing stopped two facts from both superseding
--    the same row (two "current" successors), or a revision from superseding a fact in a
--    different series -- a standalone figure replacing a consolidated one, or another
--    quarter's value. Either would let a selector pick a winner the evidence never chose.
--
-- AND ONE ADDITION: `registry_version`, the code-owned metric registry version a fact was
-- validated against (app/research/registry.py). Recorded so later evaluation can know which
-- semantics a row was accepted under without re-reading a registry file that may since have
-- changed. Nullable only because rows written before this migration have none; the API
-- always sets it.
--
-- NOTHING HERE IS A FINANCIAL FORMULA. No metric is approved by this file (BE-Q03 open).

ALTER TABLE research_fact ADD COLUMN registry_version text
  CHECK (registry_version IS NULL OR registry_version ~ '^[A-Za-z0-9._:-]{1,64}$');

COMMENT ON COLUMN research_fact.registry_version IS
    'The metric-registry version this fact was validated against at write time '
    '(app/research/registry.py). A later registry file is a new version, never an edit to '
    'the semantics an existing row was accepted under.';

-- One successor per fact. A verification copy and a revision are both successors; two of
-- either would fork the series. Serialised further by 044's tenant lock.
CREATE UNIQUE INDEX research_fact_one_successor
  ON research_fact(investor_id,supersedes_id) WHERE supersedes_id IS NOT NULL;

CREATE FUNCTION ws_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p research_fact%ROWTYPE;
BEGIN
  -- WHO MAY SAY "VERIFIED".
  IF NEW.verification = 'VERIFIED' THEN
    IF ws_human() THEN
      -- A person verifies as themselves, and only by rechecking an existing stored fact:
      -- a fresh VERIFIED row with no predecessor would be FactCreate claiming verification.
      IF NEW.verified_by IS NULL OR NEW.verified_by IS DISTINCT FROM ws_actor() THEN
        RAISE EXCEPTION 'verification must be attributed to the authenticated human';
      END IF;
      IF NEW.supersedes_id IS NULL THEN
        RAISE EXCEPTION 'human verification must recheck an existing fact';
      END IF;
    ELSIF pg_has_role(current_user,'trackr_ws_engine','member') THEN
      -- An approved deterministic adapter names its reference and never a person.
      IF NEW.verified_by IS NOT NULL THEN
        RAISE EXCEPTION 'engine verification cannot be attributed to a person';
      END IF;
    ELSE
      RAISE EXCEPTION 'this capability cannot verify a fact';
    END IF;
  ELSIF NEW.verified_by IS NOT NULL THEN
    RAISE EXCEPTION 'an unverified fact cannot name a verifier';
  END IF;

  -- WHAT MAY SUPERSEDE WHAT.
  IF NEW.supersedes_id IS NOT NULL THEN
    SELECT * INTO p FROM research_fact
      WHERE investor_id = NEW.investor_id AND id = NEW.supersedes_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'superseded fact not found'; END IF;
    -- Same series, exactly. A different basis, unit, cadence or period is different evidence.
    IF p.isin <> NEW.isin OR p.metric <> NEW.metric OR p.basis <> NEW.basis
       OR p.unit <> NEW.unit OR p.cadence <> NEW.cadence
       OR p.period_start <> NEW.period_start OR p.period_end <> NEW.period_end THEN
      RAISE EXCEPTION 'supersession must stay within one series';
    END IF;
    -- Same source. A different filing correcting this one needs a recorded, verifiable
    -- correction relationship that does not exist yet; until it does, the other filing is
    -- independent evidence, and the evaluator reports the disagreement as CONFLICT.
    IF p.source_id <> NEW.source_id THEN
      RAISE EXCEPTION 'only a same-source revision may supersede a fact';
    END IF;
    -- A verification is an exact copy of what was checked: the value cannot change on the
    -- way through. A changed value is a revision (UNVERIFIED) followed by a verification.
    IF NEW.verification = 'VERIFIED' AND (
         p.numeric_value IS DISTINCT FROM NEW.numeric_value
      OR p.boolean_value IS DISTINCT FROM NEW.boolean_value
      OR p.text_value IS DISTINCT FROM NEW.text_value) THEN
      RAISE EXCEPTION 'verification must copy the checked value unchanged';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- After `ws_insert` alphabetically, so the tenant lock is already held when the
-- successor/series checks read.
CREATE TRIGGER ws_z_fact_guard BEFORE INSERT ON research_fact
  FOR EACH ROW EXECUTE FUNCTION ws_fact_guard();
