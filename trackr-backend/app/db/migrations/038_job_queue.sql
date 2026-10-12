-- 038_job_queue.sql -- the asynchronous split, and the two columns that make a bulk
-- forward legible.
--
-- WHY A TABLE AND NOT A BROKER. Mailgun's webhook timeout is 15 seconds and it retries
-- any non-200 six times over eight hours, so a delivery carrying more than a handful of
-- PDFs cannot be parsed inside the request. The work therefore has to be recorded and
-- done later. It is recorded HERE rather than in Redis for one reason that matters more
-- than convenience: this row and the import_batch it produces commit in the SAME
-- transaction, in the same database. A separate broker can ack work whose database write
-- rolled back, which in an append-only ledger is a silently missing trade -- nothing
-- raises, and it surfaces months later as a reconciliation delta.
--
-- See docs/BACKFILL_PIPELINE.md section 4.

CREATE TABLE job (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL,
    status       text NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')),
    payload      jsonb NOT NULL,

    -- Scoping, so a drain can be run for one investor and so erasure reaches queued work.
    -- Nullable: a job kind that is not investor-scoped is allowed to exist later.
    investor_id  uuid REFERENCES investor(id) ON DELETE CASCADE,

    attempts     smallint NOT NULL DEFAULT 0,
    max_attempts smallint NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    last_error   text,

    -- THE LEASE. A worker that dies mid-document -- a function timeout, a lost laptop
    -- lid -- would otherwise leave its row in RUNNING forever, which is the quietest
    -- possible data loss: nothing errors, and one document is simply never parsed.
    -- claim() reclaims a RUNNING row whose lease has expired instead of stranding it.
    claimed_at   timestamptz,

    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

COMMENT ON TABLE job IS
    'Deferred work, claimed with SELECT ... FOR UPDATE SKIP LOCKED. Drained by '
    'app/jobs/drain.py, which runs as `python -m app.jobs.drain` OR behind '
    'POST /ops/jobs/drain -- the module is the implementation and the route is a thin '
    'wrapper, so where the worker runs stays a deployment choice.';
COMMENT ON COLUMN job.payload IS
    'IDS ONLY. Never document bytes, never a PAN, never a password. A job table is '
    'queried, dumped into backups and printed into error logs; the worker re-reads the '
    'bytes from object storage and re-derives the PAN from investor_secret at parse '
    'time. See docs/BACKFILL_PIPELINE.md section 6.';
COMMENT ON COLUMN job.claimed_at IS
    'When the current RUNNING holder claimed it. NULL whenever status is not RUNNING. '
    'claim() treats a lease older than its lease_seconds as abandoned and re-claims it.';
COMMENT ON COLUMN job.attempts IS
    'Incremented on every claim, not on every failure -- a worker killed without ever '
    'calling fail() still burns an attempt, so a document that reliably crashes the '
    'process cannot be retried forever.';

-- The claim path, and the only index it needs: oldest claimable row first. Partial,
-- because DONE rows accumulate and must not widen the index the hot path walks.
CREATE INDEX job_claimable_idx ON job (created_at)
    WHERE status IN ('PENDING', 'RUNNING');

CREATE INDEX job_investor_idx ON job (investor_id, created_at DESC);


-- One inbound message may carry hundreds of attachments. Without this, promoting a bulk
-- forward means a human clicking Promote once per document -- five hundred separate
-- decisions to import one mailbox. The staging rule is untouched: a person still makes
-- one explicit, attributed decision. It just is not five hundred of them.
ALTER TABLE import_batch ADD COLUMN delivery_id uuid;

COMMENT ON COLUMN import_batch.delivery_id IS
    'Groups every batch staged from ONE inbound delivery, so "promote all 487 documents '
    'from this forward" is one attributed decision. NULL for a single-file upload, which '
    'is its own delivery of one.';

CREATE INDEX import_batch_delivery_idx ON import_batch (delivery_id)
    WHERE delivery_id IS NOT NULL;


-- The archive index. Gmail's bulk "Forward as attachment" nests each original message as
-- a message/rfc822 part, and unwrapping it yields the ORIGINAL email's Date, From,
-- Subject and Message-ID -- not the forwarding wrapper's. Those four fields are how
-- reconciliation later finds "the contract note from around 12 April 2023" without
-- parsing all five hundred of them first.
--
-- One jsonb rather than four columns: these are provenance for a human and a date-window
-- query, not a schema anyone joins on, and a provider that supplies three of the four
-- should not need a migration.
ALTER TABLE document ADD COLUMN email_meta jsonb;

COMMENT ON COLUMN document.email_meta IS
    'Headers of the ORIGINAL message when this document arrived by email: '
    '{subject, from, date, message_id}. NULL for an uploaded file. The date here is the '
    'email date and is NOT evidence of a trade date -- only the parsed document says '
    'that.';
