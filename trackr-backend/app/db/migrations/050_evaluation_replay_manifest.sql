-- 050_evaluation_replay_manifest.sql -- make a trigger receipt actually replayable.
--
-- CLOSES BE-V12 (BLUEPRINT_BACKEND.md section 6.1, "Receipt replay completion"). 041's
-- `trigger_evaluation` records engine_version, evidence_cutoff and an input digest, and 044
-- seals its `evaluation_fact` membership. Neither says WHICH REQUIRED WINDOWS were evaluated,
-- under WHICH metric-registry and calendar definitions, over WHICH identity family. Replaying
-- a receipt from those fields alone would have to consult today's registry, today's calendar
-- and today's aliases -- and a replay that silently uses current definitions is a fresh
-- evaluation wearing an old receipt's date.
--
-- TWO ADDITIONS.
--
-- 1. `research_definition` -- the registry, calendar and identity-scope definitions an
--    evaluation used, stored ONCE by content identity (SHA-256 of canonical JSON). Global,
--    not tenant data: these are code-owned definitions and public reference data, never an
--    investor's writing. Append-only; a changed definition is a new digest.
--
-- 2. `trigger_evaluation.replay_manifest` -- a closed, string-only JSON object naming the
--    definition digests, the required windows per rule, the ordered fact ids, the cutoff and
--    the canonical-input schema version. Required on every receipt written from now on
--    (NOT VALID: the constraint binds new rows without re-validating history -- there is none
--    in any deployed database, but the honest form is the one that would be right if there
--    were). An UNKNOWN/CALENDAR_UNKNOWN receipt pins the ABSENCE too: its manifest names the
--    calendar definition digest that had no entry, so "no calendar was known" is itself
--    reproducible.
--
-- NO HOLDINGS, NO REQUEST ENVELOPE. Nothing in the manifest is a portfolio figure.

CREATE TABLE research_definition (
  digest text PRIMARY KEY CHECK (digest ~ '^[a-f0-9]{64}$'),
  kind text NOT NULL CHECK (kind IN ('METRIC_REGISTRY','CALENDAR','IDENTITY_SCOPE')),
  version ws_text NOT NULL,
  body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object' AND ws_string_numbers(body)),
  recorded_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE research_definition IS
    'Content-addressed, append-only copies of the metric-registry, reporting-calendar and '
    'identity-scope definitions an evaluation receipt used. Global reference material, not '
    'tenant data. Replay reads the pinned body by digest, never the current code default.';

CREATE FUNCTION ws_definition_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'research_definition is append-only; % refused', TG_OP
    USING ERRCODE = 'restrict_violation';
END $$;

CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON research_definition
  FOR EACH ROW EXECUTE FUNCTION ws_definition_immutable();
CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON research_definition
  FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate();

REVOKE ALL ON research_definition FROM PUBLIC;
GRANT SELECT ON research_definition TO trackr_ws_human,trackr_ws_runner,trackr_ws_engine,
  trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser;
-- Only the engine records definitions, alongside the receipt that used them.
GRANT INSERT ON research_definition TO trackr_ws_engine;

ALTER TABLE trigger_evaluation ADD COLUMN replay_manifest jsonb;

ALTER TABLE trigger_evaluation ADD CONSTRAINT trigger_evaluation_replay_manifest_required
  CHECK (replay_manifest IS NOT NULL
         AND jsonb_typeof(replay_manifest) = 'object'
         AND ws_string_numbers(replay_manifest)
         AND replay_manifest ?& ARRAY['schema_version','registry_digest','calendar_digest',
                                      'identity_digest','windows','fact_ids','evidence_cutoff'])
  NOT VALID;

COMMENT ON COLUMN trigger_evaluation.replay_manifest IS
    'Closed replay manifest (BE-V12): schema_version, registry/calendar/identity definition '
    'digests (research_definition), required windows per rule, ordered fact ids and the '
    'evidence cutoff. Every value a string. Required on every receipt written after 050.';

-- A receipt's manifest may only cite definitions that exist. Checked in the inserting
-- transaction, where the engine records definitions just before the receipt.
CREATE FUNCTION ws_manifest_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.replay_manifest IS NOT NULL AND EXISTS (
    SELECT 1 FROM unnest(ARRAY[NEW.replay_manifest->>'registry_digest',
                               NEW.replay_manifest->>'calendar_digest',
                               NEW.replay_manifest->>'identity_digest']) d
    WHERE NOT EXISTS (SELECT 1 FROM research_definition r WHERE r.digest = d)) THEN
    RAISE EXCEPTION 'replay manifest cites an unrecorded definition';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ws_z_manifest BEFORE INSERT ON trigger_evaluation
  FOR EACH ROW EXECUTE FUNCTION ws_manifest_guard();
