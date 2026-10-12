-- A vendor record whose company name does not match ours is a QUESTION, not a silent drop.
--
-- vendor_identity_review keeps what the vendor said -- its company name and the ratios it
-- would have supplied -- for every ISIN the identity check could not confirm on its own.
-- vendor_identity_decision records each investor's answer. A "SAME" answer makes that
-- investor's analytics use the candidate ratios; it never changes another investor's
-- numbers, and it never writes security_fundamentals (shared reference data). "DIFFERENT"
-- keeps the ratios withheld. Answers can be changed; the latest one applies.
CREATE TABLE vendor_identity_review (
    isin         text NOT NULL REFERENCES security (isin),
    provider     text NOT NULL,
    ticker       text NOT NULL,
    vendor_name  text NOT NULL,
    reason       text NOT NULL,
    candidate    jsonb NOT NULL,
    observed_on  date NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, provider, ticker, vendor_name)
);

CREATE TABLE vendor_identity_decision (
    investor_id  uuid NOT NULL REFERENCES investor (id) ON DELETE CASCADE,
    isin         text NOT NULL,
    provider     text NOT NULL,
    ticker       text NOT NULL,
    vendor_name  text NOT NULL,
    decision     text NOT NULL CHECK (decision IN ('SAME', 'DIFFERENT')),
    decided_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, isin, provider, ticker, vendor_name)
);
