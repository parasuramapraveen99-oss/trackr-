-- 054_hosted_jobs.sql -- BT-P02's narrow platform-managed job runtime (RP 00/01, used by BT 14).
--
-- docs/BUTLER_REPORTS.md sections 3, 4 and 9. READ docs/ARCHITECTURE_INVARIANTS.md FIRST.
-- Nothing here is a financial record, and no job can write one: report and reflection workers
-- hold no grant on the ledger, on a thesis, on a note or on a decision.
--
-- EXACTLY TWO PURPOSES. COMPANY_REPORT (an explicit, owned request for an issuer-evidence report)
-- and NOTEBOOK_REFLECTION (the separately consented, bounded daily reflection over the investor's
-- own notebook episodes). There is no general "run a model" purpose, no scheduled stock monitor
-- (BE-Q01 still gates those) and no external delivery. A CHECK, not a convention.
--
-- WHAT A JOB SEALS AT ADMISSION: owner/investor, purpose, the validated request (strict JSON --
-- ws_string_numbers refuses a JSON number anywhere, so a budget is a decimal STRING), the
-- consent version (reflection), the provider/model/price version the operator declared, the
-- maximum billable budget in the operator's declared currency, token caps, retry bound and a
-- deadline. Nothing sealed is ever updated; a revised report is a NEW job naming `revision_of`.
--
-- THE CREDENTIAL IS NOT HERE. The operator's platform inference key lives in the worker's
-- runtime environment (app/research/platform_inference.py is the only reader) -- never in a
-- table, a request, a checkpoint, a charge row, the outbox or a log. `make lint` enforces it.
--
-- DURABILITY SHAPE, same as 051's outbox: a LEASE is a committed row; a stage CHECKPOINT is the
-- one accepted effect per (job, stage, stage_version) and is refused from a stale or superseded
-- lease, after a cancellation request, after the deadline and -- for reflection -- once the
-- sealed consent is no longer current. A crashed worker's lease expires and a later attempt
-- resumes from the last accepted checkpoint; a late worker's output cannot land.
--
-- BUDGET IS A LEDGER, CONSERVATIVELY. A reservation is written (and committed) BEFORE any model
-- call; the trigger refuses one that would take worst-case exposure over the sealed limit. An
-- ambiguous network failure settles as UNKNOWN and is charged at the FULL reservation -- the
-- provider may have billed it. Nothing here claims exact-once billing: provider calls are
-- at-least-once after an ambiguous failure; job EFFECTS are once, by the constraints below.
--
-- NUMBERED 054 AFTER THE ACTUAL LATEST (053).

CREATE TABLE hosted_job (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  purpose text NOT NULL CHECK (purpose IN ('COMPANY_REPORT','NOTEBOOK_REFLECTION')),
  trigger_kind text NOT NULL CHECK (trigger_kind IN ('USER_REQUEST','DAILY','USER_REFRESH')),
  isin text,
  request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object' AND ws_string_numbers(request)),
  request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
  contract_version text NOT NULL CHECK (contract_version ~ '^[a-z0-9._-]{3,40}$'),
  revision_of uuid,
  consent_version integer,
  provider text NOT NULL CHECK (provider ~ '^[a-z0-9_-]{2,32}$'),
  model text NOT NULL CHECK (model ~ '^[A-Za-z0-9._:-]{2,80}$'),
  price_version text NOT NULL CHECK (price_version ~ '^[A-Za-z0-9._-]{2,40}$'),
  currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  budget_limit numeric(20,6) NOT NULL CHECK (budget_limit > 0),
  max_input_tokens integer NOT NULL CHECK (max_input_tokens BETWEEN 1 AND 400000),
  max_output_tokens integer NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 64000),
  max_stage_attempts smallint NOT NULL CHECK (max_stage_attempts BETWEEN 1 AND 3),
  deadline_at timestamptz NOT NULL,
  local_day date,
  actor_uid uuid REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  dedupe_key ws_text NOT NULL,
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,revision_of) REFERENCES hosted_job,
  FOREIGN KEY (investor_id,consent_version) REFERENCES butler_learning_consent(investor_id,version),
  CHECK ((purpose = 'COMPANY_REPORT') = (isin IS NOT NULL)),
  CHECK ((purpose = 'NOTEBOOK_REFLECTION') = (consent_version IS NOT NULL)),
  CHECK ((purpose = 'NOTEBOOK_REFLECTION') = (local_day IS NOT NULL)),
  CHECK (purpose = 'COMPANY_REPORT' OR revision_of IS NULL),
  CHECK (purpose <> 'COMPANY_REPORT' OR trigger_kind = 'USER_REQUEST'),
  CHECK (purpose <> 'NOTEBOOK_REFLECTION' OR trigger_kind IN ('DAILY','USER_REFRESH')),
  CHECK (revision_of IS NULL OR revision_of <> id)
);

COMMENT ON TABLE hosted_job IS
    'One admitted hosted job: COMPANY_REPORT or NOTEBOOK_REFLECTION only. Everything it may do is '
    'sealed here at admission and never updated. No credential column exists. Report jobs never '
    'read notebook tables; reflection jobs never write a report.';

-- One scheduled reflection per investor per local day (section 4 of BUTLER_INVESTOR_MEMORY).
CREATE UNIQUE INDEX hosted_job_daily_reflection ON hosted_job(investor_id,local_day)
  WHERE purpose = 'NOTEBOOK_REFLECTION' AND trigger_kind = 'DAILY';

CREATE TABLE hosted_job_event (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  job_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  state text NOT NULL CHECK (state IN
    ('QUEUED','RUNNING','WAITING_FOR_SOURCE','BUDGET_PAUSED','NEEDS_REVIEW',
     'READY_WITH_LIMITATIONS','READY_CHECKED_SCOPE','FAILED','CANCEL_REQUESTED','CANCELLED',
     'EXPIRED')),
  stage text CHECK (stage ~ '^[A-Z][A-Z0-9_]{2,40}$'),
  reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'
                                           AND ws_string_numbers(detail)),
  actor_uid uuid REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,job_id,version),
  FOREIGN KEY (investor_id,job_id) REFERENCES hosted_job
);

COMMENT ON TABLE hosted_job_event IS
    'Job state history, versioned by append. The current state is the latest version. A person '
    'may append only CANCEL_REQUESTED; everything else is the worker''s. Terminal states are final.';

-- A LEASE IS A ROW. lease_until is the DATABASE clock plus a bounded lease length; the worker
-- cannot choose its own expiry. delegation_digest is the SHA-256 of the short-lived signed
-- delegation the worker was issued for this lease (never the signing key, never a credential).
CREATE TABLE hosted_job_lease (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  stage text NOT NULL CHECK (stage ~ '^[A-Z][A-Z0-9_]{2,40}$'),
  lease_seconds integer NOT NULL CHECK (lease_seconds BETWEEN 30 AND 900),
  delegation_digest text NOT NULL CHECK (delegation_digest ~ '^[0-9a-f]{64}$'),
  claimed_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,job_id,attempt_no),
  FOREIGN KEY (investor_id,job_id) REFERENCES hosted_job
);

-- EXACTLY ONE ACCEPTED EFFECT PER LOGICAL STAGE VERSION. `output` is typed, validated JSON --
-- identifiers, codes, counts and decimal strings -- never raw model text or chain-of-thought.
CREATE TABLE hosted_job_checkpoint (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  lease_id uuid NOT NULL,
  stage text NOT NULL CHECK (stage ~ '^[A-Z][A-Z0-9_]{2,40}$'),
  stage_version integer NOT NULL CHECK (stage_version > 0),
  outcome text NOT NULL CHECK (outcome IN ('ACCEPTED','ABSTAINED','REJECTED','BLOCKED')),
  input_digest text NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
  output_digest text NOT NULL CHECK (output_digest ~ '^[0-9a-f]{64}$'),
  output jsonb NOT NULL CHECK (jsonb_typeof(output) = 'object' AND ws_string_numbers(output)),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,job_id,stage,stage_version),
  FOREIGN KEY (investor_id,job_id) REFERENCES hosted_job,
  FOREIGN KEY (investor_id,lease_id) REFERENCES hosted_job_lease
);

-- The budget ledger. RESERVE before the call; exactly one of SETTLE / RELEASE / UNKNOWN per
-- reservation afterwards. RELEASE means the request was provably never sent. UNKNOWN is charged
-- at the full reservation. Token counts are what the provider reported, when it reported them.
CREATE TABLE hosted_job_charge (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  lease_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('RESERVE','SETTLE','RELEASE','UNKNOWN')),
  reserve_id uuid,
  amount numeric(20,6) NOT NULL CHECK (amount >= 0),
  input_tokens integer CHECK (input_tokens >= 0),
  output_tokens integer CHECK (output_tokens >= 0),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,reserve_id),
  FOREIGN KEY (investor_id,job_id) REFERENCES hosted_job,
  FOREIGN KEY (investor_id,lease_id) REFERENCES hosted_job_lease,
  FOREIGN KEY (investor_id,reserve_id) REFERENCES hosted_job_charge,
  CHECK ((kind = 'RESERVE') = (reserve_id IS NULL)),
  CHECK (kind <> 'RELEASE' OR amount = 0)
);

-- ---------------------------------------------------------------------------------
-- Guards
-- ---------------------------------------------------------------------------------

CREATE FUNCTION hosted_job_state(inv uuid, job uuid) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT state FROM hosted_job_event WHERE investor_id = inv AND job_id = job
   ORDER BY version DESC LIMIT 1
$$;

CREATE FUNCTION hosted_job_terminal(state text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT state IN ('READY_WITH_LIMITATIONS','READY_CHECKED_SCOPE','FAILED','CANCELLED','EXPIRED')
$$;

-- Worst-case exposure of a job: settled amounts, UNKNOWN at max(reservation, reported), open
-- reservations at their full amount, releases at zero.
CREATE FUNCTION hosted_job_exposure(inv uuid, job uuid) RETURNS numeric LANGUAGE sql STABLE AS $$
  SELECT COALESCE(sum(CASE
           WHEN s.kind IS NULL THEN r.amount
           WHEN s.kind = 'SETTLE' THEN s.amount
           WHEN s.kind = 'UNKNOWN' THEN GREATEST(r.amount, s.amount)
           ELSE 0 END), 0)
    FROM hosted_job_charge r
    LEFT JOIN hosted_job_charge s ON s.investor_id = r.investor_id AND s.reserve_id = r.id
   WHERE r.investor_id = inv AND r.job_id = job AND r.kind = 'RESERVE'
$$;

CREATE FUNCTION hosted_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE j hosted_job%ROWTYPE; prior integer; cur_state text; l hosted_job_lease%ROWTYPE;
        cn record; r hosted_job_charge%ROWTYPE; n integer; settlement boolean := false;
BEGIN
  IF TG_TABLE_NAME = 'hosted_job' THEN
    -- A person's request carries their id; a system-admitted daily reflection carries none.
    IF ws_human() <> (NEW.actor_uid IS NOT NULL) THEN
      RAISE EXCEPTION 'hosted job attribution mismatch';
    END IF;
    IF NEW.trigger_kind = 'DAILY' AND ws_human() THEN
      RAISE EXCEPTION 'hosted job attribution mismatch';
    END IF;
    IF NEW.deadline_at <= clock_timestamp() OR NEW.deadline_at > clock_timestamp() + interval '12 hours' THEN
      RAISE EXCEPTION 'hosted job deadline out of range';
    END IF;
    -- One running job per investor per purpose, initially (section 3).
    IF EXISTS (SELECT 1 FROM hosted_job o WHERE o.investor_id = NEW.investor_id
               AND o.purpose = NEW.purpose
               AND NOT hosted_job_terminal(hosted_job_state(o.investor_id, o.id))) THEN
      RAISE EXCEPTION 'hosted job already running';
    END IF;
    IF NEW.purpose = 'NOTEBOOK_REFLECTION' THEN
      SELECT * INTO cn FROM butler_consent_now(NEW.investor_id);
      IF cn.version IS NULL OR NOT cn.active OR NOT cn.reflection
         OR cn.version <> NEW.consent_version THEN
        RAISE EXCEPTION 'butler notebook consent not current';
      END IF;
      IF NEW.trigger_kind = 'USER_REFRESH' AND (SELECT count(*) FROM hosted_job o
           WHERE o.investor_id = NEW.investor_id AND o.purpose = 'NOTEBOOK_REFLECTION'
             AND o.trigger_kind = 'USER_REFRESH' AND o.local_day = NEW.local_day) >= 1 THEN
        RAISE EXCEPTION 'hosted job quota exhausted';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'hosted_job_event' THEN
    SELECT COALESCE(max(version),0) INTO prior FROM hosted_job_event
     WHERE investor_id = NEW.investor_id AND job_id = NEW.job_id;
    IF NEW.version <> prior + 1 THEN RAISE EXCEPTION 'hosted job version conflict'; END IF;
    cur_state := hosted_job_state(NEW.investor_id, NEW.job_id);
    IF (prior = 0) <> (NEW.state = 'QUEUED') THEN
      RAISE EXCEPTION 'hosted job invalid transition';
    END IF;
    IF hosted_job_terminal(cur_state) THEN
      RAISE EXCEPTION 'hosted job invalid transition';
    END IF;
    IF ws_human() THEN
      -- A person writes exactly two things: the QUEUED admission of their own request, and
      -- CANCEL_REQUESTED. Every other state is the worker's.
      IF NOT ((prior = 0 AND NEW.state = 'QUEUED') OR NEW.state = 'CANCEL_REQUESTED')
         OR NEW.actor_uid IS NULL THEN
        RAISE EXCEPTION 'hosted job invalid transition';
      END IF;
      IF cur_state = 'CANCEL_REQUESTED' THEN RAISE EXCEPTION 'hosted job invalid transition'; END IF;
    ELSE
      IF NEW.actor_uid IS NOT NULL OR (NEW.state = 'CANCEL_REQUESTED' AND prior > 0 AND
         NEW.reason_code NOT IN ('CONSENT_REVOKED','OWNER_ACCESS_REVOKED','INVESTOR_DELETED')) THEN
        RAISE EXCEPTION 'hosted job invalid transition';
      END IF;
      -- After a cancellation request, the worker may only finish it.
      IF cur_state = 'CANCEL_REQUESTED' AND NEW.state NOT IN ('CANCELLED','EXPIRED') THEN
        RAISE EXCEPTION 'hosted job cancellation pending';
      END IF;
    END IF;

  ELSIF TG_TABLE_NAME = 'hosted_job_lease' THEN
    NEW.claimed_at := clock_timestamp();
    NEW.lease_until := NEW.claimed_at + make_interval(secs => NEW.lease_seconds);
    SELECT * INTO j FROM hosted_job WHERE investor_id = NEW.investor_id AND id = NEW.job_id;
    cur_state := hosted_job_state(NEW.investor_id, NEW.job_id);
    IF cur_state IS NULL OR hosted_job_terminal(cur_state) OR cur_state = 'CANCEL_REQUESTED' THEN
      RAISE EXCEPTION 'hosted job not runnable';
    END IF;
    IF clock_timestamp() >= j.deadline_at THEN RAISE EXCEPTION 'hosted job deadline passed'; END IF;
    IF NEW.lease_until > j.deadline_at THEN NEW.lease_until := j.deadline_at; END IF;
    SELECT * INTO l FROM hosted_job_lease WHERE investor_id = NEW.investor_id
       AND job_id = NEW.job_id ORDER BY attempt_no DESC LIMIT 1;
    -- A live lease blocks a new one unless its stage already landed its checkpoint (the lease
    -- did its one job; the next stage may start without waiting for it to expire).
    IF FOUND AND l.lease_until > clock_timestamp() AND NOT EXISTS (
         SELECT 1 FROM hosted_job_checkpoint c WHERE c.investor_id = NEW.investor_id
            AND c.lease_id = l.id) THEN
      RAISE EXCEPTION 'hosted job lease still held';
    END IF;
    IF NEW.attempt_no <> COALESCE(l.attempt_no,0) + 1 THEN
      RAISE EXCEPTION 'hosted job version conflict';
    END IF;
    SELECT count(*) INTO n FROM hosted_job_lease WHERE investor_id = NEW.investor_id
       AND job_id = NEW.job_id AND stage = NEW.stage;
    IF n >= j.max_stage_attempts THEN RAISE EXCEPTION 'hosted job stage attempts exhausted'; END IF;

  ELSIF TG_TABLE_NAME IN ('hosted_job_checkpoint','hosted_job_charge') THEN
    SELECT * INTO j FROM hosted_job WHERE investor_id = NEW.investor_id AND id = NEW.job_id;
    SELECT * INTO l FROM hosted_job_lease WHERE investor_id = NEW.investor_id
       AND job_id = NEW.job_id ORDER BY attempt_no DESC LIMIT 1;
    -- THE LATE-OUTPUT FENCE. Only the latest, unexpired lease may land anything; and nothing
    -- lands after a cancellation request, a terminal state or the deadline. A charge
    -- SETTLE/UNKNOWN is the exception: the cost of a call that already happened must still be
    -- recordable after cancellation, or the ledger would understate spend.
    IF NOT FOUND OR l.id <> NEW.lease_id THEN RAISE EXCEPTION 'hosted job stale lease'; END IF;
    -- Nested, not AND-ed: plpgsql resolves NEW.<column> even when the other operand is false,
    -- and the two tables do not share these columns.
    IF TG_TABLE_NAME = 'hosted_job_charge' THEN
      settlement := NEW.kind IN ('SETTLE','UNKNOWN','RELEASE');
    END IF;
    IF NOT settlement THEN
      IF clock_timestamp() > l.lease_until THEN RAISE EXCEPTION 'hosted job stale lease'; END IF;
      cur_state := hosted_job_state(NEW.investor_id, NEW.job_id);
      IF hosted_job_terminal(cur_state) OR cur_state = 'CANCEL_REQUESTED' THEN
        RAISE EXCEPTION 'hosted job not runnable';
      END IF;
      IF clock_timestamp() >= j.deadline_at THEN RAISE EXCEPTION 'hosted job deadline passed'; END IF;
      IF j.purpose = 'NOTEBOOK_REFLECTION' THEN
        SELECT * INTO cn FROM butler_consent_now(NEW.investor_id);
        IF cn.version IS NULL OR NOT cn.active OR NOT cn.reflection
           OR cn.version <> j.consent_version THEN
          RAISE EXCEPTION 'butler notebook consent not current';
        END IF;
      END IF;
    END IF;
    IF TG_TABLE_NAME = 'hosted_job_checkpoint' THEN
      IF NEW.stage <> l.stage THEN RAISE EXCEPTION 'hosted job stale lease'; END IF;
    END IF;
    IF TG_TABLE_NAME = 'hosted_job_charge' THEN
      IF NEW.kind = 'RESERVE' THEN
        IF hosted_job_exposure(NEW.investor_id, NEW.job_id) + NEW.amount > j.budget_limit THEN
          RAISE EXCEPTION 'hosted job budget exhausted';
        END IF;
      ELSE
        SELECT * INTO r FROM hosted_job_charge WHERE investor_id = NEW.investor_id
           AND id = NEW.reserve_id;
        IF NOT FOUND OR r.kind <> 'RESERVE' OR r.job_id <> NEW.job_id THEN
          RAISE EXCEPTION 'hosted job charge must settle a reservation';
        END IF;
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- A reflection result persisted into the notebook is fenced by its job too: a cancelled,
-- expired or failed job's output cannot land, even if consent is still current.
CREATE FUNCTION hosted_job_output_fence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text; j hosted_job%ROWTYPE;
BEGIN
  IF NEW.reflection_job_id IS NULL THEN RETURN NEW; END IF;
  SELECT * INTO j FROM hosted_job WHERE investor_id = NEW.investor_id AND id = NEW.reflection_job_id;
  s := hosted_job_state(NEW.investor_id, NEW.reflection_job_id);
  IF NOT FOUND OR j.purpose <> 'NOTEBOOK_REFLECTION' OR j.consent_version <> NEW.consent_version
     OR s IS NULL OR hosted_job_terminal(s) OR s = 'CANCEL_REQUESTED'
     OR clock_timestamp() >= j.deadline_at THEN
    RAISE EXCEPTION 'hosted job not runnable';
  END IF;
  RETURN NEW;
END $$;

ALTER TABLE butler_pattern_revision
  ADD CONSTRAINT butler_pattern_revision_job_fk
  FOREIGN KEY (investor_id,reflection_job_id) REFERENCES hosted_job;

CREATE TRIGGER ws_zz_job_fence BEFORE INSERT ON butler_pattern_revision
  FOR EACH ROW EXECUTE FUNCTION hosted_job_output_fence();

-- A job with no QUEUED event is a request nobody recorded; refuse it at COMMIT.
CREATE FUNCTION hosted_job_deferred_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM hosted_job_event WHERE investor_id = NEW.investor_id
                 AND job_id = NEW.id AND version = 1 AND state = 'QUEUED') THEN
    RAISE EXCEPTION 'hosted job needs a QUEUED event';
  END IF;
  RETURN NULL;
END $$;

DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['hosted_job','hosted_job_event','hosted_job_lease',
                             'hosted_job_checkpoint','hosted_job_charge'] LOOP
    EXECUTE format('CREATE TRIGGER ws_insert BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION ws_insert_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_z_hosted BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION hosted_job_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ws_append_only()',tbl);
    EXECUTE format('CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate()',tbl);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tbl);
    EXECUTE format('CREATE POLICY ws_tenant_rows ON %I USING (investor_id=ws_tenant()) WITH CHECK (investor_id=ws_tenant())',tbl);
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',tbl);
    -- No runner: browser model output never sees or touches a hosted job.
    EXECUTE format('GRANT SELECT ON %I TO trackr_ws_human,trackr_ws_engine,trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser',tbl);
    EXECUTE format('GRANT DELETE ON %I TO trackr_ws_eraser',tbl);
  END LOOP;
END $$;

CREATE CONSTRAINT TRIGGER hosted_job_queued AFTER INSERT ON hosted_job
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hosted_job_deferred_guard();

-- A person requests and cancels; the system admits the daily reflection; only the worker
-- (engine) leases, checkpoints and charges.
GRANT INSERT ON hosted_job,hosted_job_event TO trackr_ws_human,trackr_ws_engine;
GRANT INSERT ON hosted_job_lease,hosted_job_checkpoint,hosted_job_charge TO trackr_ws_engine;
GRANT USAGE ON SEQUENCE hosted_job_seq_seq,hosted_job_event_seq_seq
  TO trackr_ws_human,trackr_ws_engine;

CREATE INDEX hosted_job_by_purpose ON hosted_job(investor_id,purpose,seq DESC);
CREATE INDEX hosted_job_events ON hosted_job_event(investor_id,job_id,version DESC);
CREATE INDEX hosted_job_leases ON hosted_job_lease(investor_id,job_id,attempt_no DESC);
CREATE INDEX hosted_job_charges ON hosted_job_charge(investor_id,job_id);
