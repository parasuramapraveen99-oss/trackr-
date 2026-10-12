-- 021_terms_acceptance.sql -- the contract with the PERSON, which had no record at all.
--
-- There are two separate permissions in this product and only one of them was stored.
--
--   * DPDP PROCESSING CONSENT belongs to an INVESTOR, is withdrawable, and is enforced
--     on every ingestion path (client_consent, 002_compliance.sql). Built, and working.
--   * TERMS OF SERVICE + PRIVACY POLICY is a contract with the LOGIN. One per user,
--     forever, not withdrawable. Until this migration it lived nowhere: a user could
--     register, import, and be billed without the system holding any evidence they had
--     ever been shown terms, let alone which version.
--
-- Two columns on app_user rather than a table: unlike consent, this is not a series of
-- grants and withdrawals to replay -- it is one fact about the account, and the only
-- thing that ever changes is the version, when the terms themselves are reissued.
--
-- NULLABLE, deliberately. Every user who registered before this migration accepted
-- nothing, and back-filling a version they were never shown would manufacture evidence.
-- NULL is the honest reading: "we do not hold an acceptance for this login."

ALTER TABLE app_user
    ADD COLUMN tos_version     text,
    ADD COLUMN tos_accepted_at timestamptz;

COMMENT ON COLUMN app_user.tos_version IS
    'The version string of the Terms of Service + Privacy Policy this login accepted, '
    'as shown to them at registration (app.terms.CURRENT_TOS_VERSION). NULL means no '
    'acceptance is on file - true for every account created before this column existed. '
    'Never back-filled: an acceptance nobody was shown is not an acceptance.';

COMMENT ON COLUMN app_user.tos_accepted_at IS
    'When that acceptance was given. Set together with tos_version and never separately.';
