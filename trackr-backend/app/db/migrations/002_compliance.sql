-- 002_compliance.sql -- DPDP + SEBI scaffolding. Source of truth: docs/COMPLIANCE.md.
--
-- This migration exists BEFORE any client data lands, on purpose. Retrofitting consent
-- and audit trails after the fact means backfilling records that cannot be backfilled
-- honestly - you would be attesting to a consent you never captured.

CREATE TABLE client_consent (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id      uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    notice_version   text NOT NULL,
    purpose          text NOT NULL,
    lawful_basis     text NOT NULL,
    consent_given_at timestamptz NOT NULL DEFAULT now(),
    withdrawn_at     timestamptz,
    evidence_ref     text NOT NULL
);
COMMENT ON TABLE client_consent IS
    'DPDP is consent-first, with withdrawal and erasure rights. withdrawn_at IS NULL '
    'means live consent. Consent is never edited - a withdrawal stamps withdrawn_at, and '
    'a fresh grant is a new row, so the history of what was agreed and when survives.';
COMMENT ON COLUMN client_consent.evidence_ref IS
    'Email message-id, signed PDF path - whatever proves the principal actually agreed.';

CREATE INDEX client_consent_investor_idx ON client_consent (investor_id, withdrawn_at);

CREATE TABLE audit_log (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_uid   uuid,
    action      text NOT NULL,
    entity_type text NOT NULL,
    entity_id   uuid,
    ts          timestamptz NOT NULL DEFAULT now(),
    diff_json   jsonb
);
COMMENT ON TABLE audit_log IS
    'Doubles as the DPDP access log and the SEBI advisory record. RETENTION: 5 years - '
    'SEBI Reg. 19 governs and exceeds DPDP''s 1-year log minimum, so the longer '
    'obligation wins. Written in the SAME transaction as the mutation it records, so an '
    'unaudited mutation is impossible rather than merely unlikely.';

CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id);
CREATE INDEX audit_log_ts_idx ON audit_log (ts);
CREATE INDEX audit_log_actor_idx ON audit_log (actor_uid);

CREATE TABLE investor_secret (
    investor_id uuid PRIMARY KEY REFERENCES investor(id) ON DELETE CASCADE,
    pan_hash    text,
    pan_enc     bytea,
    demat_enc   bytea,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE investor_secret IS
    'Separate table, separate access path. NEVER joined into a default investor read, so '
    'the ordinary query path cannot leak identifiers it does not need.';
COMMENT ON COLUMN investor_secret.pan_hash IS
    'HMAC-SHA256 with a secret pepper, NOT a bare digest. A PAN is ten characters with a '
    'known structure, so the whole space is enumerable in minutes and a plain hash is '
    'reversible by brute force. Exists to permit lookup without decryption; nothing else.';
COMMENT ON COLUMN investor_secret.pan_enc IS 'Fernet ciphertext. Key never in this database.';

-- There is NO broker credential table in this system, and there never will be: no broker
-- password, no API secret, no order-capable token of any kind. Ingestion is file- and
-- email-based by design, and that design IS the legal basis for operating without FIU
-- registration. `make lint` fails the build if such a column is ever added -- see the
-- ban list there, which tests/test_db_schema.py reads rather than restates.

CREATE TABLE retention_policy (
    entity_type  text PRIMARY KEY,
    retain_years integer NOT NULL DEFAULT 5 CHECK (retain_years >= 1),
    note         text
);
COMMENT ON TABLE retention_policy IS
    'Configurable, defaulting to 5 years. SEBI Reg. 19 requires 5 for advisory records '
    'and DPDP requires 1 for access logs; the longer obligation governs.';

INSERT INTO retention_policy (entity_type, retain_years, note) VALUES
    ('audit_log',   5, 'SEBI Reg. 19 governs; exceeds DPDP 1-year log minimum'),
    ('transaction', 5, 'Advisory record'),
    ('investor',    5, 'From first interaction until advisory services complete');

CREATE TABLE legal_hold (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    reason      text NOT NULL,
    placed_at   timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz
);
COMMENT ON TABLE legal_hold IS
    'An open hold (released_at IS NULL) suspends deletion for that investor, overriding '
    'any retention policy. Where a dispute has been raised, records are kept until it '
    'resolves or SEBI directs otherwise - a scheduled cleanup must never quietly destroy '
    'evidence in a live dispute.';

CREATE INDEX legal_hold_open_idx ON legal_hold (investor_id) WHERE released_at IS NULL;
