-- 003_documents.sql -- raw client documents, and the key-rotation groundwork.
--
-- Two things that must exist before real client data arrives, for the same reason:
-- both are cheap now and unrecoverable later.

CREATE TABLE document (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id       uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    account_id        uuid REFERENCES account(id) ON DELETE SET NULL,
    doctype           text NOT NULL,
    storage_key       text NOT NULL UNIQUE,
    sha256            text NOT NULL UNIQUE,
    byte_size         bigint NOT NULL CHECK (byte_size >= 0),
    content_type      text NOT NULL DEFAULT 'application/pdf',
    filename          text,
    source_message_id text,
    received_at       date NOT NULL,
    stored_at         timestamptz NOT NULL DEFAULT now(),
    parsed_at         timestamptz,
    parse_status      text NOT NULL DEFAULT 'UNPARSED'
);
COMMENT ON TABLE document IS
    'Raw statements as received. Stored BEFORE parsing, always: a parser bug then costs a '
    'reparse rather than the document itself. The bytes live in private object storage; '
    'this table holds only the pointer and the provenance.';
COMMENT ON COLUMN document.sha256 IS
    'UNIQUE. Content addressing is what makes re-storing a forwarded duplicate a no-op, '
    'enforced here rather than by convention - the same argument as transaction.dedupe_key.';
COMMENT ON COLUMN document.storage_key IS
    'Object key. Contains the investor CODE, never a name or PAN: keys appear in bucket '
    'listings, access logs and support tickets.';

CREATE INDEX document_investor_idx ON document (investor_id, received_at DESC);
CREATE INDEX document_unparsed_idx ON document (parse_status) WHERE parse_status = 'UNPARSED';

-- Key and pepper rotation groundwork. See docs/KEY_ROTATION.md section 4.
ALTER TABLE investor_secret
    ADD COLUMN key_version    smallint NOT NULL DEFAULT 1,
    ADD COLUMN pepper_version smallint NOT NULL DEFAULT 1,
    ADD COLUMN pan_hash_next  text;

COMMENT ON COLUMN investor_secret.pan_hash_next IS
    'Populated during a pepper rotation so lookups can match either the old or the new '
    'hash for the duration. Without it, every lookup by PAN fails for as long as the '
    'migration runs.';

-- THE LINE THAT MATTERS. A pan_hash cannot be re-peppered from itself; the only way to
-- recompute it is from the PAN, which is recoverable only from pan_enc. A hash stored
-- without its ciphertext is therefore permanently orphaned - that investor could never be
-- found by PAN again after any rotation, and the only remedy would be asking a human for
-- their PAN a second time.
ALTER TABLE investor_secret
    ADD CONSTRAINT pan_hash_requires_ciphertext
    CHECK (pan_hash IS NULL OR pan_enc IS NOT NULL);

CREATE INDEX investor_secret_pan_hash_idx ON investor_secret (pan_hash);
CREATE INDEX investor_secret_pan_hash_next_idx ON investor_secret (pan_hash_next)
    WHERE pan_hash_next IS NOT NULL;
