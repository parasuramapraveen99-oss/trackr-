-- 037_forwarding_confirmation.sql -- Gmail's forwarding verification codes.
--
-- WHY THIS TABLE EXISTS. Gmail will not forward to an address until it has verified that
-- the address is willing to receive: it mails a numeric code and waits for that code to
-- be pasted back into Gmail's own settings. That message has NO ATTACHMENT and carries
-- the code in its BODY, so before this existed it reached the webhook, matched nothing
-- the attachment loop understood, and was dropped. The consequence was not cosmetic --
-- no client could complete Gmail's setup, so the entire forwarding feature was
-- unreachable however correctly the rest of it was built (docs/ONBOARDING_FLOW.md,
-- defect 2).
--
-- WHY A TABLE AND NOT A COLUMN. The code belongs to an (investor, broker, doctype)
-- address, not to an investor: an adviser with three brokers verifies three addresses
-- separately, each with its own code. And Gmail re-sends on request, so the same address
-- can produce several codes. An append-only log with "newest wins" on read handles both
-- without an UPDATE, and matches how the rest of this schema treats evidence.
--
-- WHY IT IS NOT A CREDENTIAL. This is a one-time proof that a mailbox owner consented to
-- forward, not a secret that grants access to anything: it is worthless once pasted,
-- belongs to the client's Gmail rather than to a broker, and is deliberately SHOWN to the
-- user rather than used by us. CLAUDE.md's "no credentials table" rule bars broker
-- passwords, API secrets and OAuth tokens -- none of which this is. The column is named
-- `code` rather than anything token-shaped so that stays legible to a reader and to
-- `make lint`.
--
-- RETENTION. Rows are transient by nature and are read with an age cut-off
-- (app/db/forwarding_confirmation.py), so a stale code is never presented as current.
-- They fall with their investor: DPDP erasure cascades through the FK rather than
-- needing app/routers/privacy.py to learn about this table.

CREATE TABLE forwarding_confirmation (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_id  uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    -- Lowercase, exactly as app.ingest.webhook.parse_to_address splits them out of the
    -- address, so a lookup never has to guess at casing.
    broker       text NOT NULL,
    doctype      text NOT NULL,
    code         text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now()
);

-- The only read this table has: newest code for one investor, filtered by age.
CREATE INDEX forwarding_confirmation_investor_idx
    ON forwarding_confirmation (investor_id, received_at DESC);

COMMENT ON TABLE forwarding_confirmation IS
    'Gmail forwarding verification codes, per (investor, broker, doctype). Transient '
    'evidence that a mailbox owner consented to forward; shown to the user, never used '
    'as a credential. See migration 037 for why this is a table and not a column.';
