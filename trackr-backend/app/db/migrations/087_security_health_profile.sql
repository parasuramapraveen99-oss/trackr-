-- Which business-quality rubric applies to a listed company (portfolio-health-v1).
--
-- Reference data keyed by ISIN; not per investor, never a holding. Filled by the
-- `security_health_profiles` job from BSE's per-scrip header, which carries the
-- exchange's own classification (Sector -> Industry group -> Industry). Never inferred
-- from a company name or a free-text sector string.
--
--   non_financial          Sector is not "Financial Services"
--   bank                   Sector "Financial Services" and industry group "Banks"
--   unsupported_financial  Any other financial (NBFC, insurer, broker, AMC ...). The v1
--                          rubric has no tests for these, so their quality score is
--                          withheld and named rather than scored with the wrong tests.
--
-- An unsupported row is still stored: "checked, unsupported" is different from
-- "never checked", and the page names each separately.
CREATE TABLE security_health_profile (
    isin               text PRIMARY KEY REFERENCES security (isin),
    profile            text NOT NULL
        CHECK (profile IN ('non_financial', 'bank', 'unsupported_financial')),
    exchange_sector    text NOT NULL,
    industry_group     text,
    industry           text,
    source             text NOT NULL CHECK (source IN ('BSE_SCRIP_HEADER')),
    source_ref         text NOT NULL,
    retrieved_at       timestamptz NOT NULL
);

COMMENT ON TABLE security_health_profile IS
    'Exchange-sourced rubric profile per ISIN for portfolio health v1. Refreshed by job; '
    'retrieved_at is the observation time, never backdated.';

CREATE INDEX security_health_profile_retrieved_idx ON security_health_profile (retrieved_at);
