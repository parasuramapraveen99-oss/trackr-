-- 048_research_source_extraction.sql -- pin how a research source's text was extracted.
--
-- CLOSES BE-V11 (BLUEPRINT_BACKEND.md section 6.4, "Source content replay"). 040's
-- `research_source` pins the BYTES (`document_id` + `content_sha256`) but records nothing
-- about how text was obtained from them. A later citation, task context or evaluation that
-- says "the filing said X" therefore could not prove which text it read: upgrading the PDF
-- library would silently change what every old source "said".
--
-- WHAT THIS RECORDS. One immutable row per extracted source: the extractor's identity and
-- version, the normalisation policy, the digest of the bytes it read, the digest of the text
-- it produced, and a reference to the retained text artifact. A content read (session 12's
-- `GET /sources/{id}/content`) rehashes both and serves the RETAINED text -- it never
-- re-extracts under whatever library happens to be installed and never updates this row.
--
-- THREE STATES, KEPT DISTINCT:
--   * no row           -- a metadata-only citation. Nothing was read; content reads say so.
--   * status EXTRACTED -- text exists, is retained, and is pinned to the extractor.
--   * status UNAVAILABLE -- bytes are retained but no text could honestly be produced
--     (encrypted, malformed, image-only). The reason is recorded; no text is invented.
--
-- RE-EXTRACTION IS A NEW SOURCE. `UNIQUE (investor_id, source_id)` means one pin per source
-- forever; a changed policy produces a new `research_source` row with its own provenance, so
-- references to the old source keep meaning what they meant.
--
-- THE TEXT ARTIFACT IS A `document` ROW (doctype RESEARCH_TEXT, parse_status RESEARCH_ONLY),
-- not a new storage path. That reuses private storage, per-investor content dedupe and -- the
-- reason that decided it -- the existing erasure path, which deletes every `document` object
-- for the investor. A parallel key space would have been one more thing erasure had to know.

CREATE TABLE research_source_extraction (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL,
  isin text NOT NULL,
  status text NOT NULL CHECK (status IN ('EXTRACTED','UNAVAILABLE')),
  unavailable_reason text CHECK (unavailable_reason IN
    ('ENCRYPTED','MALFORMED','NO_TEXT','PAGE_LIMIT','DECODE_ERROR','OUTPUT_LIMIT')),
  -- e.g. 'pdfminer.six' / 'utf8-text'. Identity of the implementation, not of this app.
  extractor_id ws_text NOT NULL,
  -- The installed library version at extraction time, e.g. '20260107'.
  extractor_version ws_text NOT NULL,
  -- The deterministic post-processing applied to the library's output, versioned in code
  -- (app/research/documents.py::NORMALIZATION).
  normalization ws_text NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
  text_sha256 text CHECK (text_sha256 ~ '^[a-f0-9]{64}$'),
  text_document_id uuid,
  page_count integer CHECK (page_count >= 0),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  creation_xid bigint NOT NULL DEFAULT txid_current(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,source_id),
  FOREIGN KEY (investor_id,isin,source_id) REFERENCES research_source(investor_id,isin,id),
  FOREIGN KEY (investor_id,text_document_id) REFERENCES document(investor_id,id),
  CHECK ((status = 'EXTRACTED') = (text_sha256 IS NOT NULL AND text_document_id IS NOT NULL
                                   AND page_count IS NOT NULL)),
  CHECK ((status = 'UNAVAILABLE') = (unavailable_reason IS NOT NULL))
);

COMMENT ON TABLE research_source_extraction IS
    'Immutable pin of how a research source''s text was extracted: extractor identity, '
    'version and normalisation, the digest of the bytes read and of the text produced, and '
    'the retained text artifact (a RESEARCH_TEXT document row). No row means a metadata-only '
    'citation; UNAVAILABLE records why no text exists. Content reads verify against this and '
    'never re-extract. Re-extraction under a new policy is a new source (BE-V11).';

-- SEALED TO THE SOURCE'S CREATING TRANSACTION, and the bytes must be the source's bytes.
-- Without the seal, a pin could be attached to an old metadata-only source later and make it
-- look as though its text had been read all along.
CREATE FUNCTION ws_extraction_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s research_source%ROWTYPE;
BEGIN
  SELECT * INTO s FROM research_source
    WHERE investor_id = NEW.investor_id AND id = NEW.source_id;
  IF NOT FOUND OR s.creation_xid IS DISTINCT FROM txid_current() THEN
    RAISE EXCEPTION 'source extraction is sealed to its source';
  END IF;
  IF s.content_sha256 IS DISTINCT FROM NEW.content_sha256 THEN
    RAISE EXCEPTION 'extraction must pin the source''s own bytes';
  END IF;
  RETURN NEW;
END $$;

-- Named to sort after `ws_insert`, so the generic guard (tenant lock, tenant match, server
-- clocks, creation_xid stamp) runs first and this one sees the stamped row.
CREATE TRIGGER ws_insert BEFORE INSERT ON research_source_extraction
  FOR EACH ROW EXECUTE FUNCTION ws_insert_guard();
CREATE TRIGGER ws_seal BEFORE INSERT ON research_source_extraction
  FOR EACH ROW EXECUTE FUNCTION ws_extraction_guard();
CREATE TRIGGER ws_immutable BEFORE UPDATE OR DELETE ON research_source_extraction
  FOR EACH ROW EXECUTE FUNCTION ws_append_only();
CREATE TRIGGER ws_truncate BEFORE TRUNCATE ON research_source_extraction
  FOR EACH STATEMENT EXECUTE FUNCTION ws_no_truncate();

ALTER TABLE research_source_extraction ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_source_extraction FORCE ROW LEVEL SECURITY;
CREATE POLICY ws_tenant_rows ON research_source_extraction
  USING (investor_id = ws_tenant()) WITH CHECK (investor_id = ws_tenant());
REVOKE ALL ON research_source_extraction FROM PUBLIC;
GRANT SELECT ON research_source_extraction TO trackr_ws_human,trackr_ws_runner,
  trackr_ws_engine,trackr_ws_scheduler,trackr_ws_reader,trackr_ws_eraser;
GRANT DELETE ON research_source_extraction TO trackr_ws_eraser;
GRANT INSERT ON research_source_extraction TO trackr_ws_human,trackr_ws_engine;

CREATE INDEX research_source_extraction_source
  ON research_source_extraction(investor_id,source_id);
