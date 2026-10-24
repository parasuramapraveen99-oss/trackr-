-- Public source evidence, separate from account ledgers. A missing record is never
-- a zero price, an empty successful feed or proof that an event did not occur.
CREATE TABLE exchange_reference_request (
    request_key text PRIMARY KEY,
    url text NOT NULL,
    method text NOT NULL CHECK (method IN ('GET','POST')),
    request_body bytea NOT NULL DEFAULT ''::bytea,
    request_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
    immutable boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','collected','failed')),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_attempt_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text
);
CREATE TABLE exchange_reference_response (
    request_key text PRIMARY KEY REFERENCES exchange_reference_request(request_key),
    status integer NOT NULL CHECK (status BETWEEN 200 AND 599),
    response_body bytea NOT NULL,
    response_sha256 text NOT NULL,
    response_headers jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE exchange_reference_dependency (
    kind text NOT NULL,
    task_key text NOT NULL,
    request_key text NOT NULL REFERENCES exchange_reference_request(request_key),
    PRIMARY KEY (kind,task_key,request_key),
    FOREIGN KEY (kind,task_key) REFERENCES reference_sync_task(kind,task_key) ON DELETE CASCADE
);
CREATE TABLE exchange_reference_revision (
    request_key text NOT NULL REFERENCES exchange_reference_request(request_key),
    status integer NOT NULL,
    response_sha256 text NOT NULL,
    response_body bytea NOT NULL,
    response_headers jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (request_key,status,response_sha256)
);
CREATE INDEX exchange_reference_pending ON exchange_reference_request(requested_at)
    WHERE state IN ('pending','failed');
COMMENT ON TABLE exchange_reference_response IS
    'Actual public responses, not verified financial facts. Existing parsers and evidence guards still apply.';
