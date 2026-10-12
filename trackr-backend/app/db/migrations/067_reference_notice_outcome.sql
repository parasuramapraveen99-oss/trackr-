-- 067_reference_notice_outcome.sql -- the investor's answer to a corporate-action notice.
--
-- reference_notice (036) only ever grows: a buyback or rights notice for a security the
-- investor held stays "unresolved" forever, because nothing can know whether they
-- tendered or applied. This table records what the investor SAYS they did, so the notice
-- can leave the reference-status list.
--
-- Mutable on purpose, unlike the ledger: an answer can be corrected. It never writes a
-- transaction. "PARTICIPATED" means the investor will record the trade through the normal
-- entry path, which needs a price the notice does not carry (rule 4: a missing price
-- raises, never defaults).
--
-- Keyed by the same identity app.db.reference_sync.status uses for a notice: ISIN, ex-date
-- text and subject. notice_hash is not used because the same notice is observed more than
-- once, under different hashes.

CREATE TABLE reference_notice_outcome (
    investor_id uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    isin        text NOT NULL,
    ex_date     text NOT NULL,
    subject     text NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN ('NOT_PARTICIPATED', 'PARTICIPATED', 'UNSURE')),
    note        text,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, isin, ex_date, subject)
);
