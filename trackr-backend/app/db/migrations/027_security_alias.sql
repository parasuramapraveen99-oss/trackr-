-- 027_security_alias.sql -- one security, many ISINs over time.
--
-- WHY THIS EXISTS. A company's ISIN is retired and replaced (a face-value change, a
-- reorganisation) and the broker reports trades under whichever identifier was current
-- at the time. The ledger, keyed by ISIN, then sees two unrelated securities: the old
-- one holds a phantom position nothing will ever price again, and the new one shows
-- disposals with no matching acquisition. Real case this was built for: GUJTHEM, bought
-- as INE942C01029 and sold as INE942C01045, which read as "sold 487 shares you never
-- bought".
--
-- IDENTITY ONLY, NEVER A RATIO. An identifier change and a face-value split usually
-- arrive together but are two different facts. Fusing them into one record would mean
-- inventing a quantity ratio wherever one is not independently sourced -- the exact
-- thing CLAUDE.md's "never invent financial logic" forbids. Any quantity effect is a
-- separate, price-corroborated corporate_action row. Using an identity mapping to
-- smuggle in a quantity or cost transformation is the specific mistake this separation
-- prevents.
--
-- NOT APPLIED BY REWRITING THE LEDGER. `transaction` rows keep the ISIN the broker
-- actually reported, forever -- the append-only rule, and what makes a wrong alias
-- revocable rather than a migration to undo. The mapping happens at the read boundary
-- (app.db.repository.load_transactions), which is also what preserves FIFO acquisition
-- dates: a synthetic TRANSFER_OUT/TRANSFER_IN pair would date the new lot at the change
-- and silently turn a long-term holding short-term (app.engine.lots opens a lot at
-- txn_date and derives holding_days from it). An ISIN change is not a taxable event.
--
-- THE GOVERNING RULE, restated here because this table is where it is enforced: an
-- alias is an IDENTITY ASSERTION, NOT AN INFERENCE. The structural checks in
-- app.db.security_alias can REJECT an unsafe alias; they can never create one. Passing
-- them proves nothing -- one issuer legitimately has many securities, distinguished by
-- the security code at characters 8-9. Only verified external evidence creates an alias.
CREATE TABLE security_alias (
    -- The retired identifier. PRIMARY KEY, not just unique: an ISIN can be superseded
    -- once. Two rows for the same old_isin would be two contradictory claims about the
    -- same fact, and there is no sensible way to pick between them at read time.
    old_isin       text PRIMARY KEY REFERENCES security(isin),
    -- The identifier that replaced it. Chains are resolved to their terminal ISIN
    -- (A -> B -> C resolves to C) and cycles are rejected at write -- see
    -- app.db.security_alias.verified_map/_assert_acyclic.
    current_isin   text NOT NULL REFERENCES security(isin),
    effective_date date NOT NULL,

    -- Same trust model as corporate_action, deliberately identical so there is one
    -- standard of evidence in this system rather than two. DRAFT is inert: it changes
    -- no number anywhere until a human or an automated check promotes it.
    source         text NOT NULL,
    source_url     text NOT NULL,
    verification   corp_action_verification NOT NULL DEFAULT 'DRAFT',
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now(),

    -- An ISIN cannot alias to itself: that is a no-op claim that would also be the
    -- shortest possible cycle.
    CONSTRAINT security_alias_not_self CHECK (old_isin <> current_isin),
    -- Country+type (chars 1-3, INE equity vs INF fund) and issuer (chars 4-7, per
    -- NSDL's structure) must match. Enforced in SQL as well as in Python so the
    -- invariant survives anything that writes here without going through that module.
    -- REJECTION ONLY -- see the note above on why passing this proves nothing.
    CONSTRAINT security_alias_same_issuer CHECK (
        substring(old_isin from 1 for 7) = substring(current_isin from 1 for 7)
    )
);

CREATE INDEX security_alias_current_idx ON security_alias (current_isin);

COMMENT ON TABLE security_alias IS
    'Retired ISIN -> its replacement, for the SAME security. Identity only: any quantity '
    'change belongs in corporate_action, price-corroborated like any other. Applied at '
    'the read boundary (load_transactions), never by rewriting the ledger, which is what '
    'preserves FIFO acquisition dates across an identifier change.';

COMMENT ON COLUMN security_alias.verification IS
    'DRAFT is inert -- only VERIFIED rows affect any number. Structural checks can '
    'reject an alias but never create one: only verified external evidence does that.';
