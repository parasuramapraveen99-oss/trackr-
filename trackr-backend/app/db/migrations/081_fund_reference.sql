-- 081_fund_reference.sql -- mutual funds and ETFs: AMFI reference data, NAVs, the fund
-- return series and its repair markers, and the fund-tax inputs. docs/MF_ETF_PLAN.md
-- sections 3, 4, 5 and 7.
--
-- ADDITIVE ONLY. No existing table changes shape and no ledger row is touched. The
-- `price` table stays exchange-only: a NAV is a different observation from an exchange
-- close, and `price` has one row per (isin, date) whose upsert overwrites close AND
-- source -- so a NAV there would silently replace an ETF's official close.

-- One fetched AMFI file and what became of it. A refused file is kept, with its reason,
-- so a broken refresh is visible rather than merely absent.
CREATE TABLE amfi_batch (
    id              bigserial PRIMARY KEY,
    layout          text NOT NULL CHECK (layout IN ('DAILY', 'HISTORY')),
    source_url      text NOT NULL,
    payload_sha256  text,
    bytes           integer,
    parser_version  text NOT NULL,
    amc_id          integer,
    window_start    date,
    window_end      date,
    rows_parsed     integer NOT NULL DEFAULT 0,
    rows_rejected   integer NOT NULL DEFAULT 0,
    rows_unavailable integer NOT NULL DEFAULT 0,
    conflicts       integer NOT NULL DEFAULT 0,
    schemes_seen    integer NOT NULL DEFAULT 0,
    navs_published  integer NOT NULL DEFAULT 0,
    navs_revised    integer NOT NULL DEFAULT 0,
    status          text NOT NULL
                    CHECK (status IN ('PUBLISHED', 'QUARANTINED', 'FAILED')),
    reason          text,
    elapsed_ms      integer,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz
);
CREATE INDEX amfi_batch_layout_status_idx ON amfi_batch (layout, status, fetched_at DESC);

-- AMFI's scheme/plan/option record. Exact text as printed; `category` is the reviewed
-- display label from app.prices.fund_category and `heading` the heading it came from.
-- A scheme is never deleted because one file omitted it.
CREATE TABLE amfi_scheme (
    scheme_code     integer PRIMARY KEY,
    amc_name        text NOT NULL,
    scheme_name     text NOT NULL,
    plan            text,
    option          text,
    heading         text NOT NULL,
    category        text NOT NULL,
    instrument_kind text NOT NULL CHECK (instrument_kind IN ('ETF', 'MUTUAL_FUND')),
    exposure        text NOT NULL CHECK (exposure IN
                    ('DOMESTIC_EQUITY','DEBT','COMMODITY','INTERNATIONAL','MIXED','UNKNOWN')),
    first_seen_on   date NOT NULL,
    last_seen_on    date NOT NULL,
    first_batch_id  bigint NOT NULL REFERENCES amfi_batch(id),
    last_batch_id   bigint NOT NULL REFERENCES amfi_batch(id)
);
COMMENT ON COLUMN amfi_scheme.exposure IS
    'Display candidate from the AMFI category heading. NOT a tax qualification: see '
    'fund_tax_profile.';

-- Every category heading AMFI has printed a scheme under, over the NAV dates it was seen
-- there. A dated fact: fund-tax facts derived from a SEBI category apply only over the
-- interval the scheme was actually observed in that category (plan section 5.1 --
-- today's category must not rewrite history).
CREATE TABLE amfi_scheme_heading (
    scheme_code     integer NOT NULL REFERENCES amfi_scheme(scheme_code),
    heading_key     text NOT NULL,
    structure       text NOT NULL,
    first_seen_on   date NOT NULL,
    last_seen_on    date NOT NULL,
    PRIMARY KEY (scheme_code, heading_key)
);

-- Which ISINs AMFI printed against a scheme. A separate table, not two columns, so a
-- dated change or a conflict (one ISIN on two scheme codes -- six such ISINs in
-- NAVAll.txt on 13-Oct-2026) is preserved rather than overwritten.
CREATE TABLE amfi_scheme_isin (
    scheme_code     integer NOT NULL REFERENCES amfi_scheme(scheme_code),
    isin            text NOT NULL CHECK (isin ~ '^IN[A-Z0-9]{10}$'),
    role            text NOT NULL CHECK (role IN ('GROWTH_OR_PAYOUT', 'REINVESTMENT')),
    first_seen_on   date NOT NULL,
    last_seen_on    date NOT NULL,
    first_batch_id  bigint NOT NULL REFERENCES amfi_batch(id),
    last_batch_id   bigint NOT NULL REFERENCES amfi_batch(id),
    PRIMARY KEY (scheme_code, isin, role)
);
CREATE INDEX amfi_scheme_isin_isin_idx ON amfi_scheme_isin (isin);

-- AMFI's numeric AMC id for the history report, learned from AMFI's own responses: a
-- valid id answers with a file naming exactly one AMC; an invalid one answers with an
-- HTML page (observed 13-Oct-2026). Never inferred from a name.
CREATE TABLE amfi_amc (
    amc_id          integer PRIMARY KEY,
    amc_name        text NOT NULL UNIQUE,
    observed_url    text NOT NULL,
    observed_at     timestamptz NOT NULL DEFAULT now()
);

-- The NAV of record per scheme and date. One current observation; a different NAV for
-- the same scheme/date arriving later is a correction, audited in fund_nav_revision.
-- NUMERIC without scale so the printed precision survives.
CREATE TABLE fund_nav (
    scheme_code     integer NOT NULL REFERENCES amfi_scheme(scheme_code),
    nav_date        date NOT NULL,
    nav             numeric NOT NULL CHECK (nav > 0),
    batch_id        bigint NOT NULL REFERENCES amfi_batch(id),
    revision        integer NOT NULL DEFAULT 1,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, nav_date)
);
CREATE INDEX fund_nav_scheme_date_desc_idx ON fund_nav (scheme_code, nav_date DESC);

CREATE TABLE fund_nav_revision (
    scheme_code     integer NOT NULL,
    nav_date        date NOT NULL,
    revision        integer NOT NULL,
    nav             numeric NOT NULL,
    batch_id        bigint NOT NULL REFERENCES amfi_batch(id),
    superseded_by   bigint NOT NULL REFERENCES amfi_batch(id),
    superseded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, nav_date, revision)
);

-- Quarantined observations, by reason. Held schemes affected stay visible through this.
CREATE TABLE amfi_reject (
    batch_id        bigint NOT NULL REFERENCES amfi_batch(id) ON DELETE CASCADE,
    line_no         integer NOT NULL,
    scheme_code     integer,
    reason          text NOT NULL,
    raw             text NOT NULL,
    PRIMARY KEY (batch_id, line_no, reason)
);
CREATE INDEX amfi_reject_scheme_idx ON amfi_reject (scheme_code);

-- History coverage PER SCHEME and window. A window fetched for other schemes never vouches
-- for a scheme added later: that scheme has no row here, so its windows are fetched again
-- (plan section 7.2). A window whose valid, filter-honouring file printed no NAV for a
-- requested scheme is still covered -- AMFI published nothing for it then -- and stays
-- unpriceable, rather than being re-fetched forever.
CREATE TABLE amfi_history_coverage (
    scheme_code     integer NOT NULL,
    window_start    date NOT NULL,
    window_end      date NOT NULL,
    batch_id        bigint NOT NULL REFERENCES amfi_batch(id),
    navs_found      integer NOT NULL,
    covered_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scheme_code, window_start, window_end)
);

-- The mutual-fund return series. A SEPARATE TABLE from portfolio_snapshot, deliberately:
-- the listed-securities series (stocks and ETFs) keeps its identity, readers and values
-- exactly as they were, and fund rows can never collide with or leak into it. A cache,
-- like portfolio_snapshot: reproducible from the ledger and fund_nav as they stood.
CREATE TABLE fund_snapshot (
    investor_id      uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    as_of            date NOT NULL,
    advised_only     boolean NOT NULL DEFAULT false,
    invested         numeric(20,6) NOT NULL,
    market_value     numeric(20,6) NOT NULL,
    unrealised       numeric(20,6) NOT NULL,
    realised_to_date numeric(20,6) NOT NULL,
    -- False while any held scheme's NAV for as_of is still awaited (carried within the
    -- 7-day policy). A provisional row is rebuilt when the NAV lands.
    final            boolean NOT NULL,
    pending_isins    text[] NOT NULL DEFAULT '{}',
    excluded_isins   text[] NOT NULL DEFAULT '{}',
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, as_of, advised_only)
);

-- Durable "this date must be recomputed" markers for the fund series. `version` lets a
-- rebuild clear only the marker it read: a correction that lands mid-rebuild bumps the
-- version and survives.
CREATE TABLE fund_snapshot_dirty (
    investor_id     uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    as_of           date NOT NULL,
    reason          text NOT NULL,
    version         bigint NOT NULL,
    marked_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, as_of)
);
CREATE SEQUENCE fund_snapshot_dirty_version_seq;

-- What the stored fund series was computed from. A different fingerprint (a fund
-- transaction imported, reversed or reclassified) rebuilds that investor's whole fund
-- series: fund ledgers are small, and a partial rebuild that misses an anchor is worse.
CREATE TABLE fund_snapshot_ledger (
    investor_id     uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    advised_only    boolean NOT NULL,
    fingerprint     text NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investor_id, advised_only)
);

-- The valuation route last seen for each ledger fund unit. When it changes (AMFI first
-- lists an ISIN, a bhavcopy first prices one), the cached series computed under the old
-- route are invalidated (app.db.fund_reference.sync_route_observations).
CREATE TABLE security_route_observation (
    isin            text PRIMARY KEY,
    route           text NOT NULL CHECK (route IN ('EXCHANGE', 'AMFI', 'UNRESOLVED')),
    instrument_kind text NOT NULL,
    reason          text NOT NULL,
    observed_at     timestamptz NOT NULL DEFAULT now()
);

-- price_absent rows removed because the ISIN is a NAV-valued fund: a bhavcopy not
-- printing a fund that never trades on an exchange is not evidence of anything. Kept
-- here so the repair is auditable.
CREATE TABLE price_absent_fund_repair (
    isin            text NOT NULL,
    price_date      date NOT NULL,
    recorded_at     timestamptz,
    removed_at      timestamptz NOT NULL DEFAULT now(),
    reason          text NOT NULL,
    PRIMARY KEY (isin, price_date)
);

-- Evidenced fund-tax facts for a period (plan section 5). One row per ISIN and validity
-- interval; NULL in a fact means NOT KNOWN, never false. A lot whose disposal falls in no
-- interval, or in one with an unknown fact the branch needs, is unresolved.
CREATE TABLE fund_tax_profile (
    isin              text NOT NULL CHECK (isin ~ '^IN[A-Z0-9]{10}$'),
    valid_from        date NOT NULL,
    valid_to          date NOT NULL,
    equity_oriented   boolean,
    -- 50AA / 2025 Act s.76 as applicable to the period (the definition changed for
    -- FY 2025-26 disposals); the evidence must be for THAT definition.
    specified_fund    boolean,
    listed            boolean,
    domiciled_in_india boolean NOT NULL DEFAULT true,
    evidence_url      text NOT NULL,
    evidence_sha256   text,
    evidence_note     text NOT NULL,
    profile_version   integer NOT NULL DEFAULT 1,
    -- OPERATOR: recorded with evidence through /ops/funds/tax-profile; wins over
    -- SEBI_CATEGORY: derived from the scheme's observed SEBI category, whose mandated
    -- asset allocation proves the fact (app.engine.fund_tax_category). Derived rows are
    -- regenerated after each AMFI publication; operator rows never are.
    source            text NOT NULL DEFAULT 'OPERATOR'
                      CHECK (source IN ('OPERATOR', 'SEBI_CATEGORY')),
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_from <= valid_to),
    PRIMARY KEY (isin, valid_from, profile_version, source)
);

-- Who the gains belong to, for tax purposes. The first release supports resident
-- individuals and HUFs; anything else is reported as unsupported, never rated as one.
CREATE TABLE investor_tax_profile (
    investor_id     uuid PRIMARY KEY REFERENCES investor(id) ON DELETE CASCADE,
    taxpayer_type   text NOT NULL CHECK (taxpayer_type IN
                    ('INDIVIDUAL', 'HUF', 'COMPANY', 'FIRM', 'TRUST', 'OTHER')),
    residency       text NOT NULL CHECK (residency IN ('RESIDENT', 'NON_RESIDENT')),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- AMFI work runs through the shared reference queue (plan section 7.2).
ALTER TABLE reference_sync_task DROP CONSTRAINT IF EXISTS reference_sync_task_kind_check;
ALTER TABLE reference_sync_task ADD CONSTRAINT reference_sync_task_kind_check
    CHECK (kind IN ('NSDL', 'NSE', 'IDENTITY', 'ACTION', 'REPLAY', 'SNAPSHOT', 'GAP',
                    'AMFI_NAV_WINDOW', 'AMFI_AMC_DISCOVERY'));

-- CBDT Cost Inflation Index, 2001-02 = 100 (Income-tax Act, 1961 s.48 Explanation (v);
-- latest by Notification 70/2025, 1-Jul-2025). Needed to index pre-1-Apr-2023 debt-fund
-- lots sold before 23-Jul-2024. Values cross-checked 14-Oct-2026 against two independent
-- reproductions of the CBDT table; 2001-02, 2017-18, 2022-23, 2023-24, 2024-25 and
-- 2025-26 also against the incometaxindia.gov.in table. Existing rows are left as they are.
INSERT INTO cost_inflation_index (financial_year, cii_value, source) VALUES
    ('2001-02', 100, 'CBDT'), ('2002-03', 105, 'CBDT'), ('2003-04', 109, 'CBDT'),
    ('2004-05', 113, 'CBDT'), ('2005-06', 117, 'CBDT'), ('2006-07', 122, 'CBDT'),
    ('2007-08', 129, 'CBDT'), ('2008-09', 137, 'CBDT'), ('2009-10', 148, 'CBDT'),
    ('2010-11', 167, 'CBDT'), ('2011-12', 184, 'CBDT'), ('2012-13', 200, 'CBDT'),
    ('2013-14', 220, 'CBDT'), ('2014-15', 240, 'CBDT'), ('2015-16', 254, 'CBDT'),
    ('2016-17', 264, 'CBDT'), ('2017-18', 272, 'CBDT'), ('2018-19', 280, 'CBDT'),
    ('2019-20', 289, 'CBDT'), ('2020-21', 301, 'CBDT'), ('2021-22', 317, 'CBDT'),
    ('2022-23', 331, 'CBDT'), ('2023-24', 348, 'CBDT'), ('2024-25', 363, 'CBDT'),
    ('2025-26', 376, 'CBDT Notification 70/2025')
ON CONFLICT (financial_year) DO NOTHING;
