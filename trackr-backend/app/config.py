"""12-factor configuration -- the single ``Settings`` object every new module reads from.

``db/connection.py`` and ``db/crypto.py`` read their env vars directly and keep doing so
unchanged (see their own modules) -- ``Settings`` sources from the same names, so both
paths agree without one having to call the other. New code added on top of them (auth,
the routers, the webhook) takes config via dependency injection from here instead.

No field below that guards a secret has a default. A ``DEBUG=True`` fallback secret is
exactly the kind of dummy value CLAUDE.md's Rule 1 forbids: local dev sets a real value
in ``.env``, same as production.
"""

from __future__ import annotations

import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: provider name -> the Settings field names its verifier requires. Keys here are the
#: only values INGEST_WEBHOOK_PROVIDER accepts -- see app/ingest/webhook_providers.py,
#: which is registered by the same three names.
_WEBHOOK_PROVIDER_REQUIRES: dict[str, tuple[str, ...]] = {
    "mailgun": ("ingest_mailgun_signing_key",),
    "postmark": ("ingest_postmark_basic_auth_user", "ingest_postmark_basic_auth_password"),
    "sendgrid": ("ingest_sendgrid_shared_secret",),
}

#: Dev-only placeholder domain -- RFC 2606 reserves example.com so this can never
#: resolve and never collides with a real client-facing address. Matches the address
#: already used throughout tests/test_webhook.py and tests/test_db_webhook.py.
_INGEST_DOMAIN_DEV_DEFAULT = "ingest.example.com"


#: What a skipped NSE job reports where ``nse_api_enabled`` is false.
NSE_API_DISABLED_NOTE = (
    "NSE API disabled on this deployment (NSE_API_ENABLED=false); run app.nse_worker"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore", hide_input_in_errors=True
    )

    # --- database -----------------------------------------------------------------
    # Exact existing name: db/connection.py's DATABASE_URL_ENV. No default -- a silent
    # fallback to localhost is how a test run ends up writing to something real.
    database_url: str
    # Neon supplies this alongside its pooled DATABASE_URL. Use it for migrations and
    # backups; it is the same login, not a separate Workspace migration identity.
    database_url_unpooled: str | None = Field(default=None, repr=False)

    # --- migration identity ---------------------------------------------------------
    # The connection migrations are applied through, which is NOT the connection requests
    # use. The identity that runs DDL owns the tables, and a table owner can drop the
    # append-only triggers migration 044 installs; handing that authority to a request
    # handler would make those guards decorative.
    #
    # Optional, and the fallback is deliberate but narrow: unset, the runner uses
    # `database_url`, which is what every existing deployment and dev box does today and
    # what keeps `make install` working on a single-role docker Postgres. What is NOT
    # allowed is that fallback surviving into a Workspace deployment -- see
    # `_validate_workspace_identities` below, which requires this to be set and distinct
    # the moment `workspace_enabled` is true. The runner also announces which identity it
    # used (`app/db/migrate.py::migration_url`) so the fallback is never silent.
    migration_database_url: str | None = None

    # --- workspace database identities ----------------------------------------------
    # Six capability logins, provisioned by scripts/provision_workspace_roles.sql and
    # selected BY THE SERVER OPERATION, never by a request. `app/db/connection.py` maps a
    # finite `WorkspaceCapability` enum member to one of these; a DTO, header or query
    # parameter can no more ask for the eraser connection than it can ask for a DSN.
    #
    # All six default to None and FAIL CLOSED when used. There is no fallback to the
    # migration owner or to `database_url`: a missing eraser credential must refuse to
    # erase, not quietly erase as the schema owner with every trigger bypassed.
    workspace_human_database_url: str | None = None
    workspace_runner_database_url: str | None = None
    workspace_engine_database_url: str | None = None
    workspace_scheduler_database_url: str | None = None
    workspace_reader_database_url: str | None = None
    workspace_eraser_database_url: str | None = None

    # OFF. Not a beta flag -- research routes do not exist yet. Even once they do, this
    # flag alone cannot turn them on: the complete 040-045 migration bundle has to be
    # recorded as applied as well (`app/db/migrate.py::assert_workspace_ready`), because a
    # flag that could expose an unguarded half-built schema is worse than no flag.
    workspace_enabled: bool = False

    # --- research model execution (BE 21-25, BT 08, BT 10) ---------------------------
    # KILL SWITCHES, not readiness flags. The server never calls a model; these decide
    # whether it will ACCEPT a browser-reported attempt at all. Turning one off refuses new
    # attempt starts with 503 and leaves every stored task, result, review, note and Butler
    # memory untouched and readable -- portfolio routes are unaffected either way.
    research_model_execution_enabled: bool = True
    butler_model_selection_enabled: bool = True
    # Butler's source-derived cards (new saved documents). Off -> those events are recorded as
    # SUPPRESSED(SOURCE_INGESTION_DISABLED); nothing is deleted and portfolio is unaffected.
    butler_source_events_enabled: bool = True
    # Investors allowed to run STORED_SAMPLE attempts (comma-separated UUIDs). Empty by
    # default: a sample is a fixture, permanently labelled, and is confined to explicitly
    # listed demo/test investors. A missing key is never a reason to serve one.
    workspace_sample_investors: str = ""

    # --- BT-P02 hosted jobs: company reports and notebook reflection (RP 01) -----------
    # OFF, and inert until an OPERATOR declares every value below. A job is refused at
    # admission (503 PLATFORM_NOT_CONFIGURED) while any is missing: no invented price, no
    # unlimited budget, no default model. This is the ONLY place the platform credential is
    # named; app/research/platform_inference.py is the ONLY module that reads it, and only
    # inside the worker (`make lint` enforces both). It is never a user key, never stored in
    # a table, never placed in a packet, queue payload, checkpoint or log. Personal BYOK keys
    # stay in the browser (BE 23) and cannot reach this path.
    hosted_jobs_enabled: bool = False
    # HMAC key for short-lived, purpose-bound worker delegations. Not a provider credential.
    hosted_delegation_signing_key: str | None = None
    platform_inference_provider: str = ""  # only "anthropic" is allowlisted
    platform_inference_model: str = ""
    platform_inference_key: str | None = Field(default=None, repr=False)
    platform_inference_price_version: str = ""
    platform_inference_currency: str = ""  # ISO 4217, e.g. "USD"
    # Decimal strings: price per million input / output tokens in that currency.
    platform_inference_input_price_per_mtok: str = ""
    platform_inference_output_price_per_mtok: str = ""
    # Operator bounds a request must fit inside (decimal strings in the currency above).
    report_max_budget: str = ""
    report_max_deadline_minutes: int = 240
    report_stage_max_input_tokens: int = 60000
    report_stage_max_output_tokens: int = 4000
    reflection_daily_budget: str = ""
    reflection_max_input_tokens: int = 20000
    reflection_max_output_tokens: int = 1024
    reflection_deadline_minutes: int = 30

    @property
    def sample_investor_ids(self) -> frozenset[str]:
        return frozenset(
            i.strip().lower() for i in self.workspace_sample_investors.split(",") if i.strip()
        )

    # --- field-level encryption -----------------------------------------------------
    # Exact existing names: db/crypto.py's ENCRYPTION_KEY_ENV and PAN_PEPPER_ENV.
    portfolio_encryption_key: str
    portfolio_pan_pepper: str

    # --- auth -------------------------------------------------------------------
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expiry_seconds: int = 3600
    # The server-side session's horizon, deliberately far longer than the token's:
    # the short JWT is what travels on every request; app_session is what a refresh
    # exchange actually renews. Without this gap there is nothing to refresh -- a
    # session that dies with its token makes "invalid or expired token" permanent
    # until the next full login.
    session_ttl_seconds: int = 30 * 24 * 3600

    # --- ops endpoints (app.auth.ops) --------------------------------------------
    # Shared secret for POST /ops/prices/refresh and /ops/snapshots/run -- unset means
    # those two endpoints refuse every request, same "no safe default" posture as
    # jwt_secret above. Not a per-user credential: these mutate every investor's stored
    # data at once and are meant for a cron job or you, curling the box directly, not a
    # signed-up alpha user.
    ops_api_key: str | None = None

    # Vercel's own cron-authentication convention (docs/DEPLOYMENT.md): set a
    # CRON_SECRET project env var and Vercel automatically sends it as
    # `Authorization: Bearer <value>` on every cron-triggered request -- no other
    # signing or IP-allowlisting mechanism exists for Vercel Cron. Deliberately
    # separate from ops_api_key rather than reused for it: Vercel Cron can only send
    # GET with this one header shape, so it hits its own GET /ops/cron/* routes
    # (app.routers.analytical), while ops_api_key keeps gating the POST routes a human
    # or a non-Vercel scheduler would call directly.
    cron_secret: str | None = None
    # Where a catch-up cycle calls to start the next one (app.continuation). Vercel sets
    # VERCEL_PROJECT_PRODUCTION_URL (host only, no scheme) on every deployment; SELF_BASE_URL
    # (a full https URL) overrides it, e.g. behind a custom domain. Neither set -> no chain;
    # the daily crons and the import screen's /sync still advance the work.
    self_base_url: str | None = None
    vercel_project_production_url: str | None = None
    # Select whether NSE discovery tasks are claimed. Hosted GAP/replay remain enabled
    # and use stored evidence. This flag alone NEVER grants exchange network access:
    # app.exchange_reference grants it only inside the explicit local collector context.
    nse_api_enabled: bool = True
    # Oldest bhavcopy day /ops/cron/market-history keeps in the full-market store
    # (`market_close`, docs/INSTANT_IMPORT_PLAN.md). Each year is about 130 MB.
    market_history_start: date = date(2016, 1, 1)

    # --- mutual funds / ETFs (docs/MF_ETF_PLAN.md section 10) -------------------------
    # Independent rollout flags, OFF by default; each is turned on only after its gate
    # passes. With all off, fund units still route away from the bhavcopy's absence
    # evidence and out of the listed series (that protection is not a flag), and a fund
    # holding is shown unpriced with its reason.
    #   MF_REFERENCE_ENABLED  AMFI daily/history fetch, catalogue and NAV writes
    #   MF_VALUATION_ENABLED  value fund holdings at their AMFI NAV; the fund series
    #   MF_TAX_ENABLED        fund capital-gains classification in /realised
    mf_reference_enabled: bool = False
    mf_valuation_enabled: bool = False
    mf_tax_enabled: bool = False
    # ADVISOR_HEALTH_ENABLED: operational kill switch for the portfolio-health-v1 grade,
    # capital state and health evidence on the advisor analytics page. Off shows a named
    # "disabled" state; it never hides the tile or falls back to another grade.
    advisor_health_enabled: bool = True
    # Whether THIS host may call AMFI (portal.amfiindia.com). A capability, like
    # NSE_API_ENABLED: where false, AMFI queue tasks are left for a host that can.
    amfi_fetch_enabled: bool = True
    amfi_http_timeout_seconds: int = Field(default=60, ge=5, le=120)
    # Plan section 7.1: an initial 180 s working budget per AMFI job invocation, well
    # inside a 300 s serverless limit; stop claiming work with insufficient headroom.
    amfi_job_budget_seconds: int = Field(default=180, ge=10, le=280)

    # --- web --------------------------------------------------------------------
    # Comma-separated in the environment; split into a list for CORSMiddleware.
    cors_origins: str = "http://localhost:3000"
    debug: bool = False

    # Free public reference sources. DEBUG never changes the source or supplies data.
    reference_sync_enabled: bool = True
    reference_http_timeout_seconds: int = Field(default=20, ge=1, le=30)
    reference_worker_poll_seconds: int = Field(default=30, ge=5, le=3600)
    reference_worker_batch_size: int = Field(default=5, ge=1, le=100)
    reference_nsdl_max_pages: int = Field(default=100, ge=1, le=1000)

    # Corporate actions. Operator-imported actions are created with this
    # automatic_application value; verification is still required either way.
    corporate_action_auto_apply: bool = False
    # Optional provenance only, never a gate: when set, copied into
    # corporate_action_observation.rights_reference for that source's rows, else NULL.
    nse_automated_access_rights_reference: str | None = None
    yahoo_data_rights_reference: str | None = None
    # A provisional bonus tax lot is flagged classification-sensitive when moving its
    # acquisition date forward by up to this many days would change STCG/LTCG or the
    # grandfathering cut-off. A day count, not money.
    bonus_allotment_sensitivity_days: int = Field(default=15, ge=0, le=365)
    # BSE corporate-actions export import (POST /ops/corporate-actions/import/bse).
    # Exchange data, verified from exchange closes like the NSE feed's actions, so it
    # applies automatically once VERIFIED unless this is turned off.
    bse_corporate_action_auto_apply: bool = True
    # How far back to look for a published BSE bhavcopy to map scrip codes to ISINs.
    bse_scrip_map_lookback_days: int = Field(default=10, ge=1, le=31)
    bse_export_max_bytes: int = Field(default=5_000_000, ge=1_000, le=50_000_000)

    # --- document storage ---------------------------------------------------------
    # "local" or "s3". Local is the development default; production selects s3 (R2).
    storage_backend: str = "local"
    local_storage_root: str = "./data/documents"
    # Explicit self-hosted deployment choice; never use on ephemeral serverless disks.
    allow_local_storage_in_production: bool = False
    local_storage_base_url: str = "http://localhost:8000/documents"
    # Exact existing names, read directly by storage/s3.py and storage/local.py -- kept
    # here too so app/storage/factory.py can choose and construct a backend at startup
    # without duplicating storage/s3.py's own env parsing.
    portfolio_storage_bucket: str | None = None
    portfolio_storage_endpoint: str | None = None
    portfolio_storage_signing_secret: str | None = None

    # --- price-pipe failure alerts (prices/alerts.py) ------------------------------
    # Required only to send real alerts -- app.prices.alerts.get_notifier() falls back
    # to log-only when any of the three below is unset, same "required only when
    # selected" convention as the storage backend fields above. No default host/from/
    # recipient: a fallback that looked configured but pointed nowhere would be worse
    # than the honest log-only fallback it would be hiding.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str | None = None
    smtp_alert_recipient: str | None = None

    # --- inbound-email forwarding addresses (app/ingest/addresses.py) ---------------
    # Not a secret -- this is mailed to clients, so unlike jwt_secret et al. above it
    # may carry a placeholder dev default, the same way cors_origins does. Production
    # must still set a real value: _validate_ingest_domain below fails at startup
    # rather than mailing addresses at a domain that will bounce.
    ingest_domain: str = _INGEST_DOMAIN_DEV_DEFAULT
    # THE ON/OFF SWITCH for email forwarding (EMAIL_FORWARDING_ENABLED=true|false). Off by
    # default while its architecture is reworked. Off means: no forwarding address is
    # shown or generated, POST /ingest/email refuses every delivery, the Gmail
    # verification-code lookup answers 404, and INGEST_DOMAIN is not required. Read
    # through ``forwarding_domain`` below, never ingest_domain directly.
    email_forwarding_enabled: bool = False
    # Deployment kill switch; promotion still requires a live investor mandate and
    # strict document/account validation. Turning this off keeps receipt/staging alive.
    email_auto_promote_enabled: bool = True
    # Pending encrypted delivery bytes per investor. Backpressure is retryable (503),
    # never an acknowledgement of work we could not retain. Archived payloads leave DB.
    ingest_max_spooled_bytes_per_investor: int = Field(default=67_108_864, gt=0)

    # --- inbound-email webhook (app/ingest/webhook_providers.py) --------------------
    # POST /ingest/email is internet-facing, so unlike storage_backend/SMTP above --
    # which degrade gracefully to a safe fallback when unset -- an unset or
    # inconsistent choice here must fail loudly rather than accept a request nothing
    # can verify. ingest_webhook_provider itself keeps the same "no safe default"
    # shape as storage_backend (None means the feature is off; the router refuses
    # every request rather than guessing a provider), but the field it selects is
    # validated below, eagerly, at Settings construction -- not lazily at first
    # request -- so a provider chosen without its matching secret fails at startup.
    ingest_webhook_provider: str | None = None
    ingest_mailgun_signing_key: str | None = None
    ingest_postmark_basic_auth_user: str | None = None
    ingest_postmark_basic_auth_password: str | None = None
    ingest_sendgrid_shared_secret: str | None = None
    # Not secret-guarding, so unlike the fields above this may default: 25 MiB matches
    # Mailgun's own combined-attachment cap.
    ingest_max_attachment_bytes: int = 26_214_400
    # How many attachments one inbound message may carry. Gmail's bulk "Forward as
    # attachment" is bounded by its own page size of 100 conversations, so this is that
    # plus headroom: high enough never to refuse a legitimate forward, low enough that
    # someone forwarding an entire mailbox cannot enqueue ten thousand jobs in one POST.
    # Exceeding it answers 406, which is Mailgun's "rejected, do not retry".
    ingest_max_attachments_per_delivery: int = 150

    # --- LLM-assisted document extraction (app/ai/llm.py, app/ingest/llm_*.py) -----
    # TWO FACTS, DELIBERATELY SEPARATE. ``openrouter_api_key`` is "we hold credentials";
    # ``llm_parsing_enabled`` is "we are permitted to transmit the contents of client
    # financial documents to a fourth Data Processor" -- which docs/COMPLIANCE.md gates
    # on a signed DPA and an MITC AI-usage clause, both currently OPEN gates.
    #
    # Those are not the same fact and must not share a switch: turning the feature off
    # would otherwise mean deleting the key, which is how keys get pasted back in by
    # whoever needs it next. A key present with the flag false is the NORMAL state.
    openrouter_api_key: str | None = None
    # Not secret-guarding, so unlike the key this may default. Swappable by env alone --
    # OpenRouter's endpoint is OpenAI-compatible and takes any model it serves, so trying a
    # cheaper or newer one is a config change, not a code change. Must support
    # response_format={"type": "json_object"}.
    openrouter_model: str = "dots-studio/dots-3-note-preview:free"
    llm_parsing_enabled: bool = False

    # --- grounded analytics narration (app/narrative/, app/routers/narrative.py) -----
    # A THIRD, SEPARATE PERMISSION, and it must not be folded into either flag above.
    # ``llm_parsing_enabled`` grants transmitting the contents of client DOCUMENTS for
    # extraction. This grants transmitting an investor's DERIVED POSITIONS -- names,
    # ISINs, quantities and rupee amounts -- so a model can write the analytics page's
    # explanations. Same vendor, same key, different processing purpose, and DPDP
    # consent is per purpose rather than per vendor.
    #
    # It is also the case app/ai/llm.py's docstring reserved: "If a future change makes
    # any value from this module trusted on its own, that reason is gone." Nothing here
    # is trusted on its own either -- app/narrative/verify.py re-decides every claim and
    # the model cannot emit a number at all -- which is why the boundary moved rather
    # than broke. See docs/COMPLIANCE.md for the processor gate this waits on.
    insight_generation_enabled: bool = False

    # --- Ask about markets (app/market_assistant/, app/routers/market_assistant.py) ---
    # The public-only assistant (docs/AI_ASSISTANT_PRIVACY_PLAN.md). It has no portfolio
    # access; definitions come from a local concept catalogue, and only "latest" topics
    # with the user's opt-in make external calls (Phases 2-3). Separate from
    # INSIGHT_GENERATION_ENABLED on purpose: that flag permits sending an investor's
    # positions; this one never does. Off by default, so a deployment that has not
    # chosen it gets a clear 503, not a half-working chat.
    public_research_enabled: bool = False
    # Phase 2: "latest from RBI / SEBI / NSE / BSE" searches those bodies' own websites
    # and nothing else. Its OWN key, separate from EXA_API_KEY (corporate-action research),
    # so the assistant's spend and quota are separate from the background jobs'. Unset =
    # no searching; the assistant still answers from the concept library and says so.
    market_assistant_exa_api_key: str | None = Field(default=None, repr=False)
    # Hard ceiling on searches per UTC day, shared through Postgres (migration 078).
    # 0 disables searching.
    market_assistant_search_daily_limit: int = Field(default=50, ge=0)
    # Phase 3: a free model picks which official passages answer the question and quotes
    # them; every quote is checked word for word before it is shown, and the model's own
    # words are never shown. Its own key, separate from OPENROUTER_API_KEY. The model must
    # be ONE pinned free model ID ending ":free" -- no router, no fallback list -- checked
    # at startup below. Unset key or model = no model step; ranked excerpts are shown.
    market_assistant_openrouter_api_key: str | None = Field(default=None, repr=False)
    market_assistant_model: str | None = None
    market_assistant_model_daily_limit: int = Field(default=50, ge=0)
    # Per-login allowance, persisted separately from the shared provider-call ceilings.
    market_assistant_message_daily_limit: int = Field(default=20, ge=0)
    # Phase 5: how admitted official passages are ranked before the selector sees them.
    # "lexical" (default) is the Phase 2 word-overlap baseline. "hybrid" fuses BM25 with a
    # LOCAL embedding model by reciprocal rank fusion; it needs the optional extra
    # `assistant-embeddings` and weights already in the cache dir (fetched once by
    # scripts/fetch_market_assistant_embedding_model.py; never downloaded at request
    # time). Missing model = BM25 alone, reported as "bm25" in the response.
    market_assistant_retrieval: Literal["lexical", "bm25", "hybrid"] = "lexical"
    market_assistant_embedding_model: str = "BAAI/bge-small-en-v1.5"
    market_assistant_embedding_cache_dir: str | None = None

    # --- fast semantic classification (app/ai/jev.py, app/ingest/corporate_action_feed.py) --
    # PUBLIC-DATA-ONLY, unlike openrouter_api_key above. The one caller sends an NSE
    # corporate-action subject line, ISIN and symbol to disambiguate a dividend notice --
    # a company-wide announcement, never an investor's own data -- so this does not carry
    # openrouter_api_key/llm_parsing_enabled's DPA gate. Degrades gracefully to "attribution
    # unresolved, notice refused" when unset, same convention as gemini_api_key above: a
    # classification feature going dark must never take the rest of the app down with it.
    jev_api_key: str | None = None

    # Rule-20 research (docs/AUTO_GAP_RESOLUTION.md, owner decision 2026-10-08): Exa search
    # restricted to nseindia.com/bseindia.com, read by the OpenRouter model above, verified
    # deterministically. PUBLIC DATA ONLY -- company, ISIN, dates, exchange text; no
    # investor data egresses -- so it is gated on these two keys alone, not on
    # llm_parsing_enabled's DPA gate. Unset = BSE-only gaps stay open, excluded and named.
    exa_api_key: str | None = Field(default=None, repr=False)
    jev_model: str = "jev-latest"

    # --- demerger/amalgamation notification watch (app/ingest/demerger_watch.py) ----
    # ON BY DEFAULT since 2026-09-22 AND DELIBERATELY LIGHTWEIGHT. Reuses the SAME NSE
    # circulars source app.ingest.isin_change_feed already relies on (interim,
    # unofficial, confirmed live -- see that module's docstring) rather than adding a
    # second, less-vetted third-party dependency; this feature adds no new ToS exposure
    # of its own. It writes NOTHING to the ledger, NOTHING to security_alias, and never
    # touches app.engine.demerger -- it only shortens the gap between a company filing
    # an apportionment letter and a human knowing to go read it. A log line, not a queue.
    #
    # WHAT DEFAULTING IT TO TRUE COMMITS TO, since a default is the value most
    # deployments will actually run. One GET to a feed this codebase already polls
    # daily, and warnings on stderr. Measured before flipping, over a real 90-day NSE
    # window (2026-06-24..2026-09-22): 8 notices matched company-restructuring terms
    # market-wide -- roughly one every eleven days -- and 0 of them named a symbol this
    # project's own database tracks. The steady state of this feature is therefore
    # silence, which is why it is a log line and not a queue, and why enabling it cannot
    # bury anything. If that ratio ever inverts, the thing to revisit is the notification
    # channel, not this flag.
    demerger_watch_enabled: bool = True

    @property
    def forwarding_domain(self) -> str:
        """The mail-receiving domain, or "" when email forwarding is switched off.

        Every address builder treats "" as "this deployment receives no mail"
        (app.ingest.addresses.forwarding_enabled), so one switch turns off the addresses,
        the broker registry's ``forwarding`` flag and the onboarding step together."""
        return self.ingest_domain if self.email_forwarding_enabled else ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _validate_ingest_domain(self) -> Settings:
        """Outside DEBUG mode, the dev placeholder must have been overridden -- a
        forgotten INGEST_DOMAIN in production would mail every client an address at
        a domain nothing receives, which fails silently until someone asks why their
        forwarded statement never showed up. Fail at startup instead.

        Checks falsiness too, not just equality to the placeholder: an ``INGEST_DOMAIN=``
        line left blank in a copied ``.env`` parses as ``""``, not the placeholder
        string, and ``""`` would be an even worse address to hand out.

        AND IT MUST BE A BARE HOSTNAME, checked in every mode including DEBUG.

        This setting is interpolated straight into an address by
        ``app.ingest.addresses.forwarding_addresses``, so whatever survives this
        validator is what a client is told to paste into Gmail. Before the format check
        existed, ``INGEST_DOMAIN=https://meicapital.in/`` -- the natural thing to paste
        when someone asks you for "your domain" -- was accepted, passed
        ``forwarding_enabled`` (it has a dot and no reserved suffix), and produced
        ``c001-zerodha-contractnote@https://meicapital.in/`` on a real screen.

        ``forwarding_enabled`` already normalises case and a trailing dot for its own
        check, but ``forwarding_addresses`` interpolated the RAW value, so
        ``"Ingest.MeiCapital.IN "`` yielded an address with the trailing space intact.
        Normalising HERE, once, and assigning back is what keeps those two readings of
        the same setting from disagreeing.

        Rejected in DEBUG too, unlike the placeholder rule above: a placeholder is a
        deployment that has not got there yet, which is a normal state to develop in,
        but a malformed hostname is wrong everywhere and is cheapest to learn about on
        the machine where it was typed.
        """
        normalised = self.ingest_domain.strip().lower().rstrip(".")
        if normalised != self.ingest_domain:
            # Assign through __dict__: this is a mode="after" validator, so ordinary
            # attribute assignment would re-enter validation on a validate_assignment
            # model and recurse.
            self.__dict__["ingest_domain"] = normalised

        if normalised:
            bad = next((c for c in ' \t/@\\?#"' if c in normalised), None)
            if bad is not None:
                raise ValueError(
                    "INGEST_DOMAIN must be a bare hostname such as "
                    f"'ingest.example.org' -- no scheme, path, port or credentials "
                    f"(got {self.ingest_domain!r}, which contains {bad!r})"
                )
            if ":" in normalised:
                raise ValueError(
                    "INGEST_DOMAIN must not carry a port: mail is routed by MX record, "
                    f"not by port (got {self.ingest_domain!r})"
                )
            if normalised.startswith("-") or normalised.startswith("."):
                raise ValueError(
                    f"INGEST_DOMAIN is not a valid hostname (got {self.ingest_domain!r})"
                )

        placeholder = not normalised or normalised == _INGEST_DOMAIN_DEV_DEFAULT
        # Only a live feature needs a real domain: with forwarding switched off no
        # address is ever handed out, so nothing can bounce.
        if not self.debug and placeholder and self.email_forwarding_enabled:
            raise ValueError(
                "INGEST_DOMAIN must be set to a real, mail-receiving domain outside "
                f"of DEBUG mode (got {self.ingest_domain!r})"
            )
        return self

    @model_validator(mode="after")
    def _validate_webhook_provider(self) -> Settings:
        provider = self.ingest_webhook_provider
        if provider is None:
            return self
        required = _WEBHOOK_PROVIDER_REQUIRES.get(provider)
        if required is None:
            raise ValueError(
                f"INGEST_WEBHOOK_PROVIDER={provider!r} is not a known provider "
                f"(expected one of {sorted(_WEBHOOK_PROVIDER_REQUIRES)})"
            )
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            raise ValueError(
                f"INGEST_WEBHOOK_PROVIDER={provider!r} requires "
                f"{', '.join(name.upper() for name in missing)}"
            )
        return self

    @model_validator(mode="after")
    def _validate_cors_origins(self) -> Settings:
        """Outside DEBUG, CORS_ORIGINS must name the real frontend. The localhost default
        left in production blocks every browser request, and the only symptom is a bare
        "Failed to fetch" on each screen -- so fail at startup and name the setting."""
        if self.debug:
            return self
        origins = self.cors_origin_list
        local = [o for o in origins if re.search(r"//(localhost|127\.0\.0\.1)(:|/|$)", o)]
        if not origins or local:
            raise ValueError(
                "CORS_ORIGINS must list the deployed frontend origin(s) outside of DEBUG "
                f"mode (got {self.cors_origins!r})"
            )
        for origin in origins:
            try:
                parsed = urlsplit(origin)
                port = parsed.port
                valid = (parsed.scheme == "https" and bool(parsed.hostname)
                         and not parsed.username and not parsed.password
                         and not parsed.path and not parsed.query and not parsed.fragment
                         and port != 0)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("CORS_ORIGINS must contain exact HTTPS origins in production")
        return self

    @model_validator(mode="after")
    def _validate_storage_backend(self) -> Settings:
        """Production local storage requires explicit persistent self-hosting opt-in.

        ``LocalDocumentStore`` writes to a real directory on disk (``mkdir`` then
        ``write_bytes``) -- true on a VPS, but on Vercel (docs/DEPLOYMENT.md) the
        deployed function directory is read-only and only ``/tmp`` is writable, and
        even that is wiped between invocations. Left at the ``local`` default outside
        DEBUG, every document upload would fail with a raw filesystem error the first
        time someone actually tried one -- possibly days after deploying, whenever
        someone first uploads a statement. Fail at startup instead, the same posture
        ``_validate_ingest_domain`` takes for a forgotten ``INGEST_DOMAIN``.
        """
        if not self.debug and self.storage_backend == "local":
            if self.allow_local_storage_in_production:
                if not Path(self.local_storage_root).is_absolute():
                    raise ValueError(
                        "Production LOCAL_STORAGE_ROOT must be an absolute persistent path"
                    )
                if not self.local_storage_base_url.startswith("https://"):
                    raise ValueError("Production LOCAL_STORAGE_BASE_URL must use HTTPS")
                return self
            raise ValueError(
                "STORAGE_BACKEND=local is a development-only default and will not "
                "work outside of DEBUG mode -- set STORAGE_BACKEND=s3 and the "
                "PORTFOLIO_STORAGE_* variables instead (see docs/DEPLOYMENT.md)"
            )

        # THE OTHER HALF OF THE SAME ARGUMENT, WHICH WAS MISSING. The branch above
        # refuses `local` outside DEBUG so an upload cannot fail with a raw filesystem
        # error days after deploying. But when the backend IS `s3`, nothing was checked at
        # all: bucket, endpoint and credentials were unvalidated, and S3DocumentStore
        # builds its boto3 client LAZILY (app/storage/s3.py), so a missing bucket surfaced
        # on the first request and a missing or rotated key surfaced on the first PUT --
        # the exact late, confusing failure this validator exists to prevent. It closed the
        # hole for local storage and left it open for the backend production actually uses.
        #
        # Credentials themselves are deliberately NOT asserted here: boto3 resolves them
        # from its own chain (env vars, instance role, shared config), so this code cannot
        # know whether they will resolve without attempting a call -- which a config
        # validator must not do. The reachability check belongs to GET /health, which does
        # one HEAD against the bucket. Bucket and endpoint, though, are ours to require.
        if self.storage_backend == "s3":
            missing = [
                name
                for name, value in (
                    ("PORTFOLIO_STORAGE_BUCKET", self.portfolio_storage_bucket),
                    ("PORTFOLIO_STORAGE_ENDPOINT", self.portfolio_storage_endpoint),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"STORAGE_BACKEND=s3 requires {', '.join(missing)} "
                    f"(see docs/DEPLOYMENT.md). boto3 additionally needs AWS_ACCESS_KEY_ID "
                    f"and AWS_SECRET_ACCESS_KEY in the environment; those are checked by "
                    f"GET /health, not here, because only a real call can prove them."
                )
        return self

    @model_validator(mode="after")
    def _validate_llm_parsing(self) -> Settings:
        """Enabling LLM parsing without the credentials it needs fails at startup, in
        the shape ``_validate_webhook_provider`` uses -- a feature that sends personal
        data to a third party must not discover its own misconfiguration mid-request.

        Deliberately NOT gated on DEBUG, unlike ``_validate_storage_backend``. Code
        cannot know whether a Data Processing Agreement has been signed, and a check
        that pretended to would be dishonest about what it verified. The DPA is tracked
        as a go-live gate in docs/COMPLIANCE.md, where a human reads it.
        """
        if not self.llm_parsing_enabled:
            return self
        missing = [
            name
            for name in ("openrouter_api_key", "openrouter_model")
            if not (getattr(self, name) or "").strip()
        ]
        if missing:
            raise ValueError(
                "LLM_PARSING_ENABLED=true requires " + ", ".join(n.upper() for n in missing)
            )
        return self

    @model_validator(mode="after")
    def _validate_workspace_identities(self) -> Settings:
        """WORKSPACE_ENABLED=true requires all six capability logins and a separate
        migration identity, and requires them to be six DIFFERENT connections.

        Same shape as ``_validate_webhook_provider``: a feature that is off needs no
        configuration, and a feature that is on must not discover its own
        misconfiguration mid-request. The specific failure this prevents is worse than a
        missing setting, though, which is why the distinctness check is here too.

        POINTING TWO CAPABILITIES AT ONE LOGIN SILENTLY DELETES A GUARD. Every check in
        migration 044 asks the database who is connected -- ``ws_human()`` is
        ``pg_has_role(current_user, 'trackr_ws_human', 'member')``. If the runner DSN and
        the human DSN carry the same login, then the untrusted browser-result submission
        path IS a human reviewer as far as PostgreSQL is concerned: it can insert a
        ``task_review`` and accept its own output, and no test that only reads Python
        would notice. The connection factory cannot detect this -- both DSNs work -- so
        startup is the last place it can be caught.

        Equality is compared on the raw string, which catches the copy-paste that
        actually happens (the same line duplicated across six env vars) and does not
        pretend to catch a determined operator writing the same login two different ways.
        A cluster-side proof of separation is the role matrix printed by
        scripts/provision_workspace_roles.sql, not a string comparison here.
        """
        capability_fields = (
            "workspace_human_database_url",
            "workspace_runner_database_url",
            "workspace_engine_database_url",
            "workspace_scheduler_database_url",
            "workspace_reader_database_url",
            "workspace_eraser_database_url",
        )
        if not self.workspace_enabled:
            return self

        missing = [
            name
            for name in ("migration_database_url", *capability_fields)
            if not (getattr(self, name) or "").strip()
        ]
        if missing:
            raise ValueError(
                "WORKSPACE_ENABLED=true requires "
                + ", ".join(name.upper() for name in missing)
                + " (see docs/DEPLOYMENT.md, 'Workspace database identities'). Provision "
                "them with scripts/provision_workspace_roles.sql first."
            )

        seen: dict[str, str] = {}
        for name in capability_fields:
            url = (getattr(self, name) or "").strip()
            if url == (self.migration_database_url or "").strip():
                raise ValueError(
                    f"{name.upper()} is the same connection as MIGRATION_DATABASE_URL. A "
                    "runtime identity must not own the workspace tables: an owner can drop "
                    "the append-only triggers that make them append-only."
                )
            if url == self.database_url.strip():
                raise ValueError(
                    f"{name.upper()} is the same connection as DATABASE_URL. The financial "
                    "connection has no workspace capability and must not acquire one."
                )
            if url in seen:
                raise ValueError(
                    f"{name.upper()} and {seen[url].upper()} are the same connection. Each "
                    "workspace capability needs its own login, or the database cannot tell "
                    "the two apart and every actor check in migration 044 collapses."
                )
            seen[url] = name
        return self

    @model_validator(mode="after")
    def _validate_market_assistant_model(self) -> Settings:
        """A paid model or an automatic router must fail at startup, not at the first
        question. Mirrors ``app.market_assistant.model.is_pinned_free_model``; config is
        read before that package, so the rule is restated here rather than imported."""
        model = (self.market_assistant_model or "").strip()
        if not model:
            return self
        if (
            not model.endswith(":free")
            or "/" not in model
            or "," in model
            or model.startswith("openrouter/")
        ):
            raise ValueError("MARKET_ASSISTANT_MODEL must be one pinned model ID ending ':free'")
        return self

    @model_validator(mode="after")
    def _validate_insight_generation(self) -> Settings:
        """Same shape and same reasoning as ``_validate_llm_parsing`` above, for the
        separate permission to narrate an investor's own positions.

        Kept as its own validator rather than a branch inside that one so the error
        message names the flag the operator actually set, and so removing either
        feature cannot silently weaken the other's check.
        """
        if not self.insight_generation_enabled:
            return self
        missing = [
            name
            for name in ("openrouter_api_key", "openrouter_model")
            if not (getattr(self, name) or "").strip()
        ]
        if missing:
            raise ValueError(
                "INSIGHT_GENERATION_ENABLED=true requires " + ", ".join(n.upper() for n in missing)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached: ``Settings()`` re-reads the environment on every call otherwise, and a
    request-scoped dependency calling this on every request should not re-parse .env
    each time."""
    return Settings(_env_file=os.environ.get("TRACKR_ENV_FILE", ".env"))
