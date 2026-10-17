-- Per-login daily message allowance. Store an opaque HMAC, never questions or portfolios.
CREATE TABLE IF NOT EXISTS market_assistant_message_usage (
    subject_hash text NOT NULL CHECK (subject_hash ~ '^[a-f0-9]{64}$'),
    day date NOT NULL,
    used integer NOT NULL CHECK (used >= 0),
    PRIMARY KEY (subject_hash, day)
);
