-- 040_workspace_evidence.sql -- Workspace research evidence: companies, sources, facts, notes.
--
-- FIRST OF SIX. This file and 041-043 define twenty-five tables with NO triggers, NO
-- row-level security and NO grants. 044 makes them append-only and 045 makes them
-- tenant-scoped. A database sitting between here and 045 therefore holds the entire
-- research schema completely unguarded, which is why `app/db/migrate.py` refuses to apply
-- a partial 040-045 bundle through any normal deployment path: intermediate files are
-- applied only through `apply_through`, against a disposable database that holds no
-- investor rows.
--
-- READ `docs/ARCHITECTURE_INVARIANTS.md` BEFORE CHANGING ANYTHING HERE. Nothing in this
-- file is a financial record. No table below is a holding, a price, a transaction or a
-- valuation, and nothing here may become an input to one: the ledger remains the only
-- source of financial truth, and `research_fact` is a record of WHAT A SOURCE SAID, not of
-- what is true. Migration 042's `task_result` is even further from truth -- it is untrusted
-- model output that a human has not yet looked at.
--
-- NUMBERED 040, NOT 031. The blueprint proposed 031-036 against a tree ending at 030; nine
-- migrations landed first (031_isin_change_circulars .. 039_user_capability). The whole
-- unapplied bundle was renumbered together -- see app/db/migrate.py::WORKSPACE_BUNDLE.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO: no catalogue seed, no repository, no route, no
-- runtime grant, and no change to any financial table beyond adding one UNIQUE constraint
-- to `document` as an FK target. The transaction ledger is not touched.

-- ---------------------------------------------------------------------------------
-- Shared domains and validators
-- ---------------------------------------------------------------------------------

-- Nonblank text. Used everywhere a column means "somebody wrote something here", so that
-- '' and '   ' are impossible rather than merely discouraged. A source with an empty URL
-- is not provenance; a note with an empty title is not a note.
CREATE DOMAIN ws_text AS text CHECK (length(btrim(VALUE)) > 0);

-- A decimal as TEXT, in exactly the precision NUMERIC(20,6) stores: up to 14 integer
-- digits and up to 6 fractional ones.
--
-- WHY A TEXT DOMAIN EXISTS AT ALL, given rule 1 of CLAUDE.md. Typed numeric research
-- inputs use real NUMERIC(20,6) columns -- see research_fact.numeric_value. This domain is
-- for the OTHER case: a number that has to live inside a JSON structure, such as a thesis
-- exit-rule threshold. A JSON number is an IEEE double by the time anything parses it, so
-- `{"threshold": 10.5}` would be a float in the middle of a falsifiability condition. The
-- grammar below is the same one `ws_rules_valid` (041) applies to those JSON scalars --
-- repeated there rather than referenced because a DOMAIN cannot be attached to a key
-- inside a jsonb value, only to a column.
CREATE DOMAIN ws_decimal AS text CHECK (
  VALUE ~ '^-?[0-9]{1,14}(\.[0-9]{1,6})?$'
);

-- True unless the value contains a JSON number anywhere, at any depth.
--
-- THE POINT IS THE WORD "ANYWHERE". A check that only looked at top-level keys would pass
-- `{"rules":[{"threshold":10.5}]}` -- which is the exact shape that matters, since rules
-- are nested. Recursion is what makes the guarantee worth stating.
--
-- IMMUTABLE so it can be used in a CHECK constraint; STRICT so a NULL input is NULL rather
-- than a false negative (every column using it is NOT NULL, so that case cannot arise, but
-- a later nullable column must not silently pass).
CREATE FUNCTION ws_string_numbers(j jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE v jsonb;
BEGIN
  IF jsonb_typeof(j) = 'number' THEN RETURN false; END IF;
  IF jsonb_typeof(j) = 'array' THEN
    FOR v IN SELECT value FROM jsonb_array_elements(j) LOOP
      IF NOT ws_string_numbers(v) THEN RETURN false; END IF;
    END LOOP;
  ELSIF jsonb_typeof(j) = 'object' THEN
    FOR v IN SELECT value FROM jsonb_each(j) LOOP
      IF NOT ws_string_numbers(v) THEN RETURN false; END IF;
    END LOOP;
  END IF;
  RETURN true;
END $$;

COMMENT ON FUNCTION ws_string_numbers(jsonb) IS
    'False if the value contains a JSON number at any depth. Recursive on purpose: a '
    'top-level-only check would pass a threshold nested inside a rules array, which is '
    'where every number in this schema actually lives. A JSON number is an IEEE double '
    'once parsed, and a float has no business inside a falsifiability condition.';

-- ---------------------------------------------------------------------------------
-- company -- which securities this investor has opened a research file on
-- ---------------------------------------------------------------------------------
--
-- A JOIN ROW AND NOTHING ELSE. No symbol, no name, no last price, no change, no holding
-- quantity, no ritual-complete boolean. Every one of those is READ from somewhere that
-- already owns it: symbol/name from `security`, price from the existing price engine,
-- holdings from the ledger on read (CLAUDE.md's one architectural rule), and
-- ritual-completion from an EXISTS over 041's `question` rows. Copying any of them here
-- would create a second answer to a question that already has one, and the copy would be
-- the one that went stale.
--
-- Keyed by ISIN, never symbol -- CLAUDE.md rule 2. Indosolar's ISIN now resolves to the
-- symbol WAAREEINDO; the ISIN never moved.
CREATE TABLE company (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  isin text NOT NULL REFERENCES security(isin),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (investor_id, isin)
);

COMMENT ON TABLE company IS
    'Which securities an investor has opened a research file on. A join row: symbol, '
    'name, price, holdings and ritual state are all READ from the tables that own them, '
    'never copied here. The composite primary key is the FK target every company-scoped '
    'research table uses, which is what makes "same investor AND same company" a database '
    'fact rather than an application convention.';

-- ---------------------------------------------------------------------------------
-- document -- one added constraint, no behaviour change
-- ---------------------------------------------------------------------------------
--
-- `research_source.document_id` must reference a document belonging to the SAME investor,
-- which needs (investor_id, id) to be a unique key. `document.id` is already the primary
-- key, so this constraint adds no new restriction whatsoever -- it adds an FK target.
--
-- It composes with migration 020's per-investor content dedupe rather than competing with
-- it: 020 made `UNIQUE (investor_id, sha256)` replace a global `sha256 UNIQUE`, so two
-- investors may hold identical bytes as separate rows. That is exactly what the two-tenant
-- privacy case requires, and nothing here changes it.
--
-- NO PARSE-STATUS, DOCTYPE OR STORAGE CHANGE. Reusing a same-investor financial document
-- as a research citation does NOT license relabelling its parse state (BE-V04, session 12).
ALTER TABLE document ADD CONSTRAINT document_investor_id_id_uniq UNIQUE (investor_id, id);

-- ---------------------------------------------------------------------------------
-- research_source -- where a claim came from
-- ---------------------------------------------------------------------------------
--
-- PUBLICATION IS NOT RETRIEVAL, and conflating them is the defining mistake here. When a
-- company said something and when we fetched it are different facts with different uses:
-- publication_date orders evidence and decides which quarter a number belongs to,
-- retrieved_at is a server receipt. A retrieval timestamp standing in for a publication
-- date would silently date a 2019 annual report to today.
--
-- If the publication date is genuinely unknown, the material does NOT belong in this table.
-- It stays unverified intake and can never become a sourced observation or a trigger input.
-- An undated source is not provenance.
CREATE TABLE research_source (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  isin text NOT NULL,
  source ws_text NOT NULL,
  source_type text NOT NULL CHECK (source_type IN
    ('EXCHANGE_FILING','ANNUAL_REPORT','RESULTS','PRESENTATION','TRANSCRIPT',
     'SHAREHOLDING','NEWS','MANAGEMENT_COMMENTARY','USER_DOCUMENT','BOOK')),
  -- A public canonical URL, a book identifier, or an internal document URI. NEVER a signed
  -- download URL: those expire, and one stored here would be both useless later and a live
  -- credential now.
  source_url ws_text NOT NULL,
  publication_date date NOT NULL,
  period ws_text NOT NULL,
  retrieved_at timestamptz NOT NULL,
  -- Which page, section or note inside the source. "The annual report" is not a citation.
  document_reference ws_text NOT NULL,
  document_id uuid,
  content_sha256 text CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
  dedupe_key ws_text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (investor_id,id),
  -- The company-scoped FK target for research_fact and note. Those tables reference
  -- (investor_id, isin, id), which is how the database refuses a fact that cites a source
  -- filed under a DIFFERENT COMPANY in the same tenant -- a same-investor FK alone would
  -- happily allow it.
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,document_id) REFERENCES document(investor_id,id),
  -- Retained bytes are identified by BOTH their row and their content hash. A document_id
  -- without a hash would say "the text was read" while leaving no way to prove which bytes
  -- were read; metadata-only citations (no document_id, no hash) are permitted and are
  -- honest about not having the text.
  CHECK (document_id IS NULL OR content_sha256 IS NOT NULL),
  -- A source cannot have been published after it was fetched. Compared in IST because
  -- publication dates on Indian exchange filings are IST calendar dates, and a UTC
  -- comparison would reject anything filed after 05:30 IST on the day it was retrieved.
  --
  -- CHANGED FROM THE BLUEPRINT DRAFT, which wrote `AT TIME ZONE 'Asia/Kolkata'`.
  --
  -- To be accurate about why, because the obvious reason is wrong and was checked: both
  -- forms are ACCEPTED by PostgreSQL 16. Despite `timezone(text, timestamptz)` being STABLE
  -- rather than IMMUTABLE, PostgreSQL does not enforce immutability in CHECK constraints the
  -- way it does in index expressions -- the draft creates fine.
  --
  -- The interval form is used anyway because a CHECK whose meaning depends on the session's
  -- timezone database is a CHECK whose meaning can change underneath stored rows: a tzdata
  -- update, or a dump and restore that revalidates the constraint, could evaluate it
  -- differently from the day the row was written, and a constraint that is true at INSERT
  -- and false at restore is the worst kind. The fixed offset is exact for this zone
  -- specifically -- India has observed UTC+05:30 with no daylight saving since 1945, so
  -- there is no fold or gap for an offset to get wrong -- which makes it a free win here and
  -- would NOT be safe for a zone that observes DST.
  CHECK (publication_date <= (retrieved_at AT TIME ZONE INTERVAL '+05:30')::date)
);

COMMENT ON TABLE research_source IS
    'Where a research claim came from. publication_date (when the source said it) and '
    'retrieved_at (when we fetched it) are separate facts and neither substitutes for the '
    'other. Material with an unknown publication date does not belong here at all: it '
    'stays unverified intake and can never become a sourced observation or a trigger input.';
COMMENT ON COLUMN research_source.source_url IS
    'Public canonical URL, book identifier, or internal document URI. Never a signed '
    'download URL -- one stored here would be a live credential today and a dead link '
    'tomorrow.';

-- ---------------------------------------------------------------------------------
-- research_fact -- one sourced value, with its provenance and its verification state
-- ---------------------------------------------------------------------------------
--
-- EXACTLY ONE VALUE KIND PER ROW, and `false` and `0` are values. num_nonnulls = 1 is what
-- makes "no value" and "the value is zero" different rows rather than the same row read two
-- ways -- the same distinction CLAUDE.md rule 4 makes about a missing price.
--
-- INDEPENDENT SOURCES STAY INDEPENDENT. There is deliberately no unique key on
-- (metric, basis, period): two filings that disagree about one number are two rows, both
-- readable, and the evaluator reports CONFLICT. A last-write-wins key would resolve a
-- disagreement between sources by whichever was inserted second, which is inventing
-- financial history.
--
-- VERIFICATION IS A CLAIM ABOUT WHO CHECKED, NOT ABOUT PLAUSIBILITY. An LLM extraction is
-- always UNVERIFIED, whatever it says about itself; only a person or an approved
-- deterministic adapter produces VERIFIED, and either way a reference to the check is
-- required. 044 tightens the attribution chain further.
CREATE TABLE research_fact (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text NOT NULL,
  source_id uuid NOT NULL,
  metric ws_text NOT NULL,
  basis ws_text NOT NULL,
  unit ws_text NOT NULL,
  period_start date NOT NULL,
  period_end date NOT NULL,
  cadence text NOT NULL CHECK (cadence IN ('QUARTER','YEAR','POINT')),
  -- NUMERIC(20,6), matching every money column in this schema. CLAUDE.md rule 1.
  numeric_value NUMERIC(20,6),
  boolean_value boolean,
  text_value ws_text,
  verification text NOT NULL CHECK (verification IN ('UNVERIFIED','VERIFIED')),
  -- NULL is permitted ONLY for an approved deterministic adapter, which still has to name
  -- its verification_reference. The pairing is enforced by the CHECK below; which adapters
  -- are approved is a service decision, not a schema one.
  verified_by uuid REFERENCES app_user(id),
  verification_reference ws_text,
  supersedes_id uuid,
  dedupe_key ws_text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,dedupe_key),
  -- Three-column FKs, so a fact cannot cite a source belonging to another company.
  FOREIGN KEY (investor_id,isin,source_id)
    REFERENCES research_source(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,supersedes_id)
    REFERENCES research_fact(investor_id,isin,id),
  CHECK (num_nonnulls(numeric_value,boolean_value,text_value) = 1),
  -- NUMERIC accepts 'NaN' and (since PostgreSQL 14) '±Infinity'. A non-finite quantity in
  -- a financial series propagates into every aggregate that touches it and is never a
  -- measurement; refuse it at the column.
  CHECK (numeric_value IS NULL OR numeric_value::text NOT IN ('NaN','Infinity','-Infinity')),
  CHECK (period_end >= period_start),
  CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  CHECK (verification <> 'VERIFIED' OR verification_reference IS NOT NULL)
);

COMMENT ON TABLE research_fact IS
    'One sourced value with its provenance and verification state. NOT a financial record: '
    'this says what a source said, never what is true, and nothing here feeds the ledger. '
    'Deliberately no unique key on (metric, basis, period) -- two filings that disagree '
    'are two readable rows and the evaluator reports CONFLICT, because resolving a '
    'disagreement by insert order would be inventing financial history.';
COMMENT ON COLUMN research_fact.verification IS
    'UNVERIFIED | VERIFIED. A claim about who checked, not about plausibility. LLM '
    'extraction is always UNVERIFIED whatever the model asserts; VERIFIED requires a '
    'person or an approved deterministic adapter and always a verification_reference.';

-- ---------------------------------------------------------------------------------
-- note -- what the investor actually thinks, and its revision history
-- ---------------------------------------------------------------------------------
--
-- APPEND-ONLY, LIKE THE LEDGER. A revision is a NEW ROW naming its predecessor, so the old
-- text survives exactly as written. `UNIQUE (investor_id, previous_id)` gives each note at
-- most one successor, which is what makes the history a chain rather than a tree nobody can
-- read. Many notes have no predecessor, and NULLs do not collide in a unique index.
--
-- AN OBSERVATION WITHOUT A SOURCE IS NOT AN OBSERVATION. That is the one kind whose
-- primary_source_id is mandatory: an INTERPRETATION is explicitly the investor's own
-- reasoning and needs none, but "the company said X" needs to say where.
--
-- ACCEPTED_AGENT IS NARROW. Model-derived text can only ever be an OBSERVATION or an
-- INTERPRETATION -- never a QUESTION, a DECISION, a THESIS_NOTE or LITERATURE. A model does
-- not get to author a commitment or a decision, and 044 additionally requires an explicit
-- human acceptance row to exist in the same transaction as any ACCEPTED_AGENT note.
CREATE TABLE note (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  seq bigserial NOT NULL UNIQUE,
  isin text NOT NULL,
  kind text NOT NULL CHECK (kind IN
    ('QUESTION','OBSERVATION','INTERPRETATION','THESIS_NOTE','LITERATURE','DECISION')),
  title ws_text NOT NULL,
  -- Plain text, and '' is allowed: a titled note with an empty body is a real state while
  -- someone is still writing. The title is what must not be blank.
  body text NOT NULL DEFAULT '',
  -- When the person says they thought it. Separate from recorded_at, which is when the
  -- server received it, and 044 stamps the latter rather than trusting a caller.
  written_on date NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  authorship text NOT NULL CHECK (authorship IN ('HUMAN','ACCEPTED_AGENT')),
  primary_source_id uuid,
  previous_id uuid,
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,isin,id),
  UNIQUE (investor_id,previous_id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,isin) REFERENCES company,
  FOREIGN KEY (investor_id,isin,primary_source_id)
    REFERENCES research_source(investor_id,isin,id),
  FOREIGN KEY (investor_id,isin,previous_id) REFERENCES note(investor_id,isin,id),
  CHECK (previous_id IS NULL OR previous_id <> id),
  CHECK (kind <> 'OBSERVATION' OR primary_source_id IS NOT NULL),
  CHECK (authorship <> 'ACCEPTED_AGENT' OR kind IN ('OBSERVATION','INTERPRETATION'))
);

COMMENT ON TABLE note IS
    'What the investor thinks, append-only. A revision is a new row naming its '
    'predecessor; the old text survives exactly as written. UNIQUE (investor_id, '
    'previous_id) keeps the history a chain rather than a tree. An OBSERVATION requires a '
    'primary source -- "the company said X" has to say where -- while an INTERPRETATION is '
    'explicitly the investor''s own reasoning and needs none.';
COMMENT ON COLUMN note.authorship IS
    'HUMAN | ACCEPTED_AGENT. Model-derived text can only ever be an OBSERVATION or an '
    'INTERPRETATION: a model does not author a commitment, a decision or a question. '
    'Migration 044 additionally requires a human acceptance row in the same transaction.';

-- ---------------------------------------------------------------------------------
-- note_link -- a dated assertion that two things relate
-- ---------------------------------------------------------------------------------
--
-- A LINK NEVER REWRITES WHAT IT LINKS. Recording "this note contradicts that one" adds a
-- row; it does not annotate, amend or supersede either note. That is what keeps a
-- contradiction pair readable years later as two things believed at two times.
--
-- Exactly one target per row. A link to a note, a source or a thesis -- never two at once,
-- because "supports" pointing at both a source and a thesis is two different claims sharing
-- one relation word.
--
-- thesis_id has NO foreign key yet: `thesis` does not exist until 041, which adds it. The
-- dangling reference is closed in the same bundle, before anything can write to this table.
CREATE TABLE note_link (
  investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  from_note_id uuid NOT NULL,
  to_note_id uuid,
  source_id uuid,
  thesis_id uuid,
  relation text NOT NULL CHECK (relation IN ('READS','SUPPORTS','CONTRADICTS','CITES')),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  actor_uid uuid NOT NULL REFERENCES app_user(id),
  dedupe_key ws_text NOT NULL,
  PRIMARY KEY (investor_id,id),
  UNIQUE (investor_id,dedupe_key),
  FOREIGN KEY (investor_id,from_note_id) REFERENCES note,
  FOREIGN KEY (investor_id,to_note_id) REFERENCES note,
  FOREIGN KEY (investor_id,source_id) REFERENCES research_source,
  CHECK (num_nonnulls(to_note_id,source_id,thesis_id) = 1),
  CHECK (to_note_id IS NULL OR to_note_id <> from_note_id),
  -- A contradiction is between two pieces of thinking. A note cannot "contradict" a source:
  -- disagreeing with what a filing says is an INTERPRETATION of it, which is a different
  -- and more honest claim.
  CHECK (relation <> 'CONTRADICTS' OR to_note_id IS NOT NULL)
);

COMMENT ON TABLE note_link IS
    'A dated assertion that two things relate. Adding a link never annotates, amends or '
    'supersedes what it links, which is what keeps a contradiction pair readable years '
    'later as two things believed at two times. Exactly one target per row.';

-- ---------------------------------------------------------------------------------
-- Indexes -- the four reads this schema actually performs
-- ---------------------------------------------------------------------------------

-- A company's notes, newest first. seq breaks ties within a day, because written_on is a
-- date and several notes a day is normal.
CREATE INDEX note_company_order ON note(investor_id,isin,written_on DESC,seq DESC);
-- Backlinks: "what points at this note". The forward direction is already covered by the
-- primary key.
CREATE INDEX note_links_back ON note_link(investor_id,to_note_id);
-- One metric's history on one basis, newest period first. The basis column is in the key
-- because comparing standalone to consolidated figures is the mistake this ordering exists
-- to make awkward.
CREATE INDEX fact_series ON research_fact
  (investor_id,isin,metric,basis,period_end DESC,seq DESC);
-- A company's sources by publication date, for "what is the latest filing".
CREATE INDEX source_company_date ON research_source(investor_id,isin,publication_date DESC);
