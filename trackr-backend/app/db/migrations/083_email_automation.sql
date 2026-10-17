-- Mailgun admission is a single durable DB write, never inline financial parsing.
ALTER TABLE document ADD CONSTRAINT document_email_scope UNIQUE(id, investor_id);
ALTER TABLE import_batch ADD CONSTRAINT import_batch_email_scope UNIQUE(id, investor_id);

CREATE TABLE email_inbox (
    investor_id uuid PRIMARY KEY REFERENCES investor(id) ON DELETE CASCADE,
    token uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE email_import_mandate (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    policy_version text NOT NULL CHECK (policy_version = 'email-deterministic-v1'),
    authorized_by uuid NOT NULL REFERENCES app_user(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE(id, investor_id)
);
CREATE UNIQUE INDEX email_import_mandate_active ON email_import_mandate(investor_id)
    WHERE revoked_at IS NULL;

CREATE TABLE email_service_actor (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE
);
INSERT INTO email_service_actor VALUES
    ('9f35fb4b-7526-51a8-84db-0c02a518304e', 'email-deterministic-v1');

CREATE TABLE email_delivery (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    sha256 text NOT NULL,
    recipient text NOT NULL,
    payload_enc bytea,
    source_document_id uuid REFERENCES document(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'ARCHIVED', 'FAILED')),
    last_error text,
    received_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    extraction_attempts integer NOT NULL DEFAULT 0,
    UNIQUE(investor_id, sha256),
    UNIQUE(id, investor_id),
    FOREIGN KEY(source_document_id, investor_id) REFERENCES document(id, investor_id)
);
COMMENT ON COLUMN email_delivery.payload_enc IS
    'Temporary Fernet-encrypted complete provider payload. Cleared only after the worker '
    'archives a complete source document and queues all attachment work in one transaction. '
    'Re-encrypt pending payloads when rotating PORTFOLIO_ENCRYPTION_KEY.';
CREATE INDEX email_delivery_received ON email_delivery(investor_id, received_at DESC);

CREATE TABLE email_attachment (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    delivery_id uuid NOT NULL REFERENCES email_delivery(id) ON DELETE CASCADE,
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    extraction_attempt integer NOT NULL DEFAULT 1,
    document_id uuid REFERENCES document(id) ON DELETE SET NULL,
    batch_id uuid REFERENCES import_batch(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'PENDING',
    detail text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(delivery_id, extraction_attempt, ordinal),
    UNIQUE(id, investor_id),
    FOREIGN KEY(delivery_id, investor_id) REFERENCES email_delivery(id, investor_id),
    FOREIGN KEY(document_id, investor_id) REFERENCES document(id, investor_id),
    FOREIGN KEY(batch_id, investor_id) REFERENCES import_batch(id, investor_id)
);
CREATE INDEX email_attachment_investor ON email_attachment(investor_id, updated_at DESC);

-- An in-app notification is committed with the import, not sent from a DB transaction.
CREATE TABLE email_import_event (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    attachment_id uuid NOT NULL REFERENCES email_attachment(id) ON DELETE CASCADE,
    outcome text NOT NULL,
    detail text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(attachment_id, outcome),
    FOREIGN KEY(attachment_id, investor_id) REFERENCES email_attachment(id, investor_id)
);

CREATE TABLE email_refresh_state (
    investor_id uuid PRIMARY KEY REFERENCES investor(id) ON DELETE CASCADE,
    requested_revision bigint NOT NULL DEFAULT 1,
    completed_revision bigint NOT NULL DEFAULT 0
);
CREATE INDEX email_import_event_recent ON email_import_event(investor_id, created_at DESC);

ALTER TABLE import_batch ADD COLUMN email_mandate_id uuid REFERENCES email_import_mandate(id);
ALTER TABLE import_batch ADD COLUMN promotion_origin text NOT NULL DEFAULT 'USER'
    CHECK (promotion_origin IN ('USER', 'EMAIL_AUTOMATION'));
ALTER TABLE import_batch ADD CONSTRAINT email_mandate_scope
    FOREIGN KEY(email_mandate_id, investor_id) REFERENCES email_import_mandate(id, investor_id);
ALTER TABLE import_batch ADD CONSTRAINT email_promotion_attributed CHECK (
    promotion_origin <> 'EMAIL_AUTOMATION' OR
    (email_mandate_id IS NOT NULL AND
     promoted_by = '9f35fb4b-7526-51a8-84db-0c02a518304e'::uuid)
);

ALTER TABLE job ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE job ADD COLUMN claim_token uuid;
ALTER TABLE job ADD COLUMN work_key text;
CREATE UNIQUE INDEX job_active_work_key ON job(work_key)
    WHERE work_key IS NOT NULL AND status IN ('PENDING', 'RUNNING');
