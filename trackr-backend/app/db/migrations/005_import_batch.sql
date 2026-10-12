-- 005_import_batch.sql -- staged imports. NOTHING AUTO-COMMITS.
--
-- BUILD_PLAN section 6: adapters stage rows with confidence scores and never write to the
-- ledger. A second, explicit, attributed action promotes them. The point is that no
-- automated path can put a row into a client's financial record without a human having
-- looked at the batch and said so.

CREATE TYPE import_batch_status AS ENUM ('STAGED', 'PROMOTED', 'DISCARDED');
CREATE TYPE import_row_status  AS ENUM ('STAGED', 'PROMOTED', 'DUPLICATE', 'REJECTED');

CREATE TABLE import_batch (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id    uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    account_id     uuid REFERENCES account(id) ON DELETE SET NULL,
    adapter        text NOT NULL,
    filename       text,
    sha256         text NOT NULL UNIQUE,
    document_id    uuid REFERENCES document(id) ON DELETE SET NULL,
    status         import_batch_status NOT NULL DEFAULT 'STAGED',
    uploaded_at    timestamptz NOT NULL DEFAULT now(),
    promoted_at    timestamptz,
    promoted_by    uuid,
    discarded_at   timestamptz,
    discard_reason text,
    row_count      integer NOT NULL DEFAULT 0,
    rejected_count integer NOT NULL DEFAULT 0,
    notes          jsonb,

    -- A promotion nobody is attached to is a record that fails exactly when it is needed:
    -- the moment somebody asks who put these numbers into a client's portfolio.
    CONSTRAINT promotion_is_attributed CHECK (
        status <> 'PROMOTED' OR (promoted_at IS NOT NULL AND promoted_by IS NOT NULL)
    ),
    CONSTRAINT discard_has_a_reason CHECK (
        status <> 'DISCARDED'
        OR (discarded_at IS NOT NULL AND discard_reason IS NOT NULL
            AND length(trim(discard_reason)) > 0)
    )
);
COMMENT ON TABLE import_batch IS
    'One uploaded file. sha256 UNIQUE, so re-uploading the identical file is caught before '
    'it is parsed - clients send overlapping Console exports by necessity, since each '
    'download is capped at 365 days.';

CREATE INDEX import_batch_investor_idx ON import_batch (investor_id, uploaded_at DESC);
CREATE INDEX import_batch_staged_idx ON import_batch (status) WHERE status = 'STAGED';

CREATE TABLE import_row (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id   uuid NOT NULL REFERENCES import_batch(id) ON DELETE CASCADE,
    row_number integer NOT NULL,
    status     import_row_status NOT NULL DEFAULT 'STAGED',
    confidence numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    dedupe_key text,
    payload    jsonb NOT NULL,
    reason     text,
    UNIQUE (batch_id, row_number)
);
COMMENT ON COLUMN import_row.confidence IS
    'How much of this row was read rather than inferred. 1.0 for a structured CSV field. '
    'Exists so the contract-note parser uses this same staging flow instead of a second '
    'one, and so a reviewer reads one consistent signal across every source.';
COMMENT ON COLUMN import_row.payload IS
    'The transaction as parsed, plus the original source row. Kept so a promotion can be '
    'explained afterwards without re-reading the file, which may be gone.';

CREATE INDEX import_row_batch_idx ON import_row (batch_id, status);
