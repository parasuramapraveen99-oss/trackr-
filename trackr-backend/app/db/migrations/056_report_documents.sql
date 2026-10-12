-- 056_report_documents.sql -- published company-report documents, artifacts, retractions.
--
-- RP 05-06 (docs/BUTLER_REPORTS.md sections 7-8). A report job (054) ends in ONE immutable,
-- validated document; HTML and PDF artifacts are rendered FROM THAT DOCUMENT and pinned to it
-- by digest. Nothing is updated: a correction is a new report version (a revision job), and a
-- withdrawal is an appended retraction row that downloads check.
--
-- WHAT IS STORED WHERE:
--   report_document.document_json  the published typed document (report.document.v1), exactly
--                                  the bytes whose SHA-256 is document_sha256.
--   report_document.ledger_json    the full attributed draft: every claim, including those
--                                  withheld at publication, with the check findings. Retained
--                                  so a reviewer can see what was withheld and why.
--   report_artifact                one row per rendered format; the bytes live in the private
--                                  document store (document_id, parse_status RESEARCH_ONLY).
--   report_retraction              rights loss, source retraction, correction or owner request.
--
-- Stored as TEXT, not jsonb, on purpose: the typed document carries integers (report_version,
-- fiscal-period months) and the workspace rule forbids JSON numbers in jsonb columns
-- (ws_string_numbers). The text is canonical JSON and its digest is recorded beside it.
--
-- LATE-OUTPUT FENCE (as 054): a document or artifact cannot be inserted for a job that is
-- cancelled, cancel-requested, expired or failed, nor by anything but the engine capability.
-- The investor notebook is not referenced by anything here, and never may be.

CREATE TABLE report_document (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  job_id uuid NOT NULL,
  isin text NOT NULL,
  report_version integer NOT NULL CHECK (report_version > 0),
  status text NOT NULL CHECK (status IN ('READY_WITH_LIMITATIONS','READY_CHECKED_SCOPE')),
  contract_version text NOT NULL CHECK (contract_version ~ '^[a-z0-9._-]{3,40}$'),
  document_json text NOT NULL,
  document_sha256 text NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
  ledger_json text NOT NULL,
  ledger_sha256 text NOT NULL CHECK (ledger_sha256 ~ '^[0-9a-f]{64}$'),
  claims_published integer NOT NULL CHECK (claims_published >= 0),
  claims_withheld integer NOT NULL CHECK (claims_withheld >= 0),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,job_id),
  FOREIGN KEY (investor_id,job_id) REFERENCES hosted_job
);

COMMENT ON TABLE report_document IS
    'One published company-report document per report job: the typed, validated model every '
    'artifact is rendered from, plus the full attributed draft ledger (withheld claims '
    'included). Immutable; corrections are new report versions via revision jobs.';

CREATE TABLE report_artifact (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  report_document_id uuid NOT NULL,
  format text NOT NULL CHECK (format IN ('HTML','PDF')),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  byte_size integer NOT NULL CHECK (byte_size > 0),
  page_count integer CHECK (page_count IS NULL OR page_count > 0),
  document_id uuid NOT NULL,
  renderer text NOT NULL CHECK (renderer ~ '^[A-Za-z0-9._:-]{3,80}$'),
  manifest_json text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,report_document_id,format),
  FOREIGN KEY (investor_id,report_document_id) REFERENCES report_document(investor_id,id),
  FOREIGN KEY (investor_id,document_id) REFERENCES document(investor_id,id),
  CHECK ((format = 'PDF') = (page_count IS NOT NULL))
);

CREATE TABLE report_retraction (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  report_document_id uuid NOT NULL,
  reason text NOT NULL CHECK (reason IN
    ('RIGHTS_LOST','SOURCE_RETRACTED','CORRECTED','OWNER_REQUEST')),
  detail text CHECK (detail IS NULL OR length(detail) BETWEEN 1 AND 500),
  actor_uid uuid REFERENCES app_user(id),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  FOREIGN KEY (investor_id,report_document_id) REFERENCES report_document(investor_id,id)
);

COMMENT ON TABLE report_retraction IS
    'An appended withdrawal of a report document and all its artifacts. Downloads check it. '
    'It cannot recall a file already downloaded, and says so in the UI.';

CREATE FUNCTION report_output_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE j hosted_job%ROWTYPE; st text; d report_document%ROWTYPE;
BEGIN
  IF TG_TABLE_NAME = 'report_document' THEN
    IF ws_human() THEN RAISE EXCEPTION 'report output is written by the worker only'; END IF;
    SELECT * INTO j FROM hosted_job WHERE investor_id = NEW.investor_id AND id = NEW.job_id;
    IF NOT FOUND OR j.purpose <> 'COMPANY_REPORT' OR j.isin IS DISTINCT FROM NEW.isin THEN
      RAISE EXCEPTION 'report document must belong to its company report job';
    END IF;
    st := hosted_job_state(NEW.investor_id, NEW.job_id);
    IF st IN ('CANCEL_REQUESTED','CANCELLED','EXPIRED','FAILED') THEN
      RAISE EXCEPTION 'hosted job output refused: job is %', st;
    END IF;
  ELSIF TG_TABLE_NAME = 'report_artifact' THEN
    IF ws_human() THEN RAISE EXCEPTION 'report output is written by the worker only'; END IF;
    SELECT * INTO d FROM report_document
      WHERE investor_id = NEW.investor_id AND id = NEW.report_document_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'report document not found'; END IF;
    st := hosted_job_state(NEW.investor_id, d.job_id);
    IF st IN ('CANCEL_REQUESTED','CANCELLED','EXPIRED','FAILED') THEN
      RAISE EXCEPTION 'hosted job output refused: job is %', st;
    END IF;
    IF EXISTS (SELECT 1 FROM report_retraction r WHERE r.investor_id = NEW.investor_id
               AND r.report_document_id = NEW.report_document_id) THEN
      RAISE EXCEPTION 'report document is retracted';
    END IF;
  ELSIF TG_TABLE_NAME = 'report_retraction' THEN
    IF ws_human() <> (NEW.actor_uid IS NOT NULL) THEN
      RAISE EXCEPTION 'report retraction attribution mismatch';
    END IF;
    IF ws_human() AND NEW.reason <> 'OWNER_REQUEST' THEN
      RAISE EXCEPTION 'a person retracts only by owner request';
    END IF;
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['report_document','report_artifact','report_retraction'] LOOP
    EXECUTE format('CREATE TRIGGER ws_insert BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION ws_insert_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_z_report BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION report_output_guard()',tbl);
    EXECUTE format('CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ws_append_only()',tbl);
    EXECUTE format('CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate()',tbl);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tbl);
    EXECUTE format('CREATE POLICY ws_tenant_rows ON %I USING (investor_id=ws_tenant()) WITH CHECK (investor_id=ws_tenant())',tbl);
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',tbl);
    EXECUTE format('GRANT SELECT ON %I TO trackr_ws_human,trackr_ws_engine,trackr_ws_reader,trackr_ws_eraser',tbl);
    EXECUTE format('GRANT DELETE ON %I TO trackr_ws_eraser',tbl);
  END LOOP;
END $$;

GRANT INSERT ON report_document,report_artifact TO trackr_ws_engine;
GRANT INSERT ON report_retraction TO trackr_ws_human,trackr_ws_engine;
GRANT USAGE ON SEQUENCE report_document_seq_seq,report_artifact_seq_seq,report_retraction_seq_seq
  TO trackr_ws_engine;
GRANT USAGE ON SEQUENCE report_retraction_seq_seq TO trackr_ws_human;

CREATE INDEX report_artifact_by_document ON report_artifact(investor_id,report_document_id);
CREATE INDEX report_retraction_by_document ON report_retraction(investor_id,report_document_id);
