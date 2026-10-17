# Mailgun setup for ingest.meicapital.in

Updated 17 October 2026. Trackr already has a signed inbound-email endpoint, durable
PostgreSQL queue, document processing worker, Gmail confirmation-code handling, and
per-investor forwarding settings. Mailgun still needs account/domain/DNS configuration.
The saved production environment currently has forwarding disabled and no signing key.
Frontend: `https://trackrplus.vercel.app`. Backend: `https://trackr-ebon.vercel.app`.

The connection is:

```text
Broker → your Gmail attachment filter → mail-<inbox-token>@ingest.meicapital.in
       → Mailgun MX → Mailgun receiving route → production backend /ingest/email/mime
       → encrypted Neon queue → worker → import results in Trackr
```

## 1. Add the receiving domain

In Mailgun, add the custom domain `ingest.meicapital.in`. Choose a region and use that
region consistently for the domain, receiving routes, and MX records. Confirm that your
Mailgun account/plan enables receiving routes. Use the custom domain, not a sandbox.

At the DNS provider for `meicapital.in`, add the two MX records Mailgun shows for this
subdomain. For a US-region Mailgun domain the usual records are:

| Type | Name | Priority | Target |
| --- | --- | --- | --- |
| MX | `ingest` | 10 | `mxa.mailgun.org` |
| MX | `ingest` | 10 | `mxb.mailgun.org` |

EU-region domains use `mxa.eu.mailgun.org` and `mxb.eu.mailgun.org`. The Mailgun dashboard
is the authority for the exact records. Leave the apex domain's existing MX records in
place. Mailgun recommends using a separate domain/subdomain when existing mail already
uses another provider. This receiving domain does not require an A record pointing at
Vercel, and the website domain does not need to move.

Add the verification/SPF/DKIM records Mailgun supplies with their exact hostnames and
values, then use its DNS verification action. Do not create two SPF TXT records at the
same hostname; combine mechanisms if one already exists. Keep any Mailgun CNAME record
DNS-only if your DNS provider offers HTTP proxying.

Sources: [Mailgun routing prerequisites](https://help.mailgun.com/hc/en-us/articles/360011335773-Troubleshooting-issues-with-a-route),
[regional endpoints](https://help.mailgun.com/hc/en-us/articles/360007512013-Can-I-transfer-my-domain-to-another-region-US-to-EU-EU-to-US).

## 2. Configure the production backend

In Vercel, open the backend project **trackr**, whose stable production domain is
`https://trackr-ebon.vercel.app`. This is separate from the **trackrplus** frontend
project. The Mailgun URL must remain reachable without a Vercel login.

In the backend project's production environment variables set:

```dotenv
SELF_BASE_URL=https://trackr-ebon.vercel.app
EMAIL_FORWARDING_ENABLED=true
INGEST_DOMAIN=ingest.meicapital.in
INGEST_WEBHOOK_PROVIDER=mailgun
INGEST_MAILGUN_SIGNING_KEY=<your HTTP webhook signing key>
EMAIL_AUTO_PROMOTE_ENABLED=true
```

Find the key in Mailgun Settings → API Security → **HTTP webhook signing key**. This is
distinct from an API key or domain sending key. Keep it in backend secrets only. No
frontend environment variable or Mailgun sending API key is needed for inbound mail.
Redeploy the backend after changing environment variables.

Keep the existing Neon connection, `PORTFOLIO_ENCRYPTION_KEY`, private S3/R2 document
storage, and AWS credentials. These must also be available to the processing worker.
Do not generate a new encryption key for an existing database. Migrations through 085
are required for this release; migration 083 adds the email inbox/queue schema.

Source: [Mailgun signing key and signatures](https://documentation.mailgun.com/docs/mailgun/user-manual/webhooks/securing-webhooks).

## 3. Add the Mailgun receiving route

In Mailgun Receiving → Routes, create a route in the same region as your domain:

```text
Priority: 0
Description: Trackr portfolio imports
Expression: match_recipient("^.*@ingest[.]meicapital[.]in$")
Action 1: forward("https://trackr-ebon.vercel.app/ingest/email/mime")
Action 2: stop()
```

The `/mime` suffix makes Mailgun include the original MIME message, including nested
forwarded messages.
The backend also accepts parsed multipart delivery at `/ingest/email`. Do not select a
JSON endpoint: this receiver authenticates Mailgun's form signature fields.

Use a Receiving route, rather than an event webhook such as delivered/opened. The
backend checks the signature, recipient domain, inbox and processing consent. A valid
delivery receives HTTP 200 after its complete encrypted payload is committed to Neon;
parsing happens later. Mailgun treats 406 as a permanent rejection and retries other
failed delivery responses for a limited period.

Sources: [route filters](https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/route-filters),
[route actions and HTTP delivery](https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http),
[route ordering](https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/routes).

## 4. Run the processing worker

The existing Vercel `jobs-drain` cron runs daily. For prompt imports, run the same worker
continuously on an available machine/server, or invoke the authenticated drain endpoint
every minute with an external scheduler. Queue receipt alone does not run parsing.

For a persistent worker, create a private `.env.worker` in `trackr-backend/` containing
the production backend's database, encryption, storage and feature settings. Then:

```bash
cd /path/to/Trackr-plus/trackr-backend
../.venv/bin/python -m dotenv -f .env.worker run -- \
  ../.venv/bin/python -m app.jobs.drain \
  --watch --poll-seconds 10 --budget-seconds 180 --claim-batch 1
```

Use a process manager to restart this worker after crashes or machine restarts. A laptop
worker stops when the laptop sleeps. Alternatively, schedule an authenticated request:

```bash
curl --fail --request POST \
  "https://trackr-ebon.vercel.app/ops/jobs/drain?budget_seconds=45&limit=100" \
  --header "X-Ops-Key: ${OPS_API_KEY}"
```

Store `OPS_API_KEY` in the scheduler's secret storage. It is the backend's existing ops
key, not a Mailgun key. Do not configure this cron inside a browser or frontend bundle.

## 5. Finish Gmail setup in Trackr

1. Sign in and select the correct portfolio. Set its PAN and each broker's exact client
   code/UCC, accept statement-processing consent, and save necessary file passwords.
2. Open Trackr's Gmail forwarding setup. Copy the backend-generated address
   `mail-<32-character-token>@ingest.meicapital.in`; do not invent an inbox token.
3. In Gmail Settings → See all settings → Forwarding and POP/IMAP, add that address.
   Gmail sends a confirmation message. Retrieve its code from Trackr's setup panel and
   verify the address in Gmail.
4. Leave global forwarding disabled. Create filters using actual broker/depository
   sending addresses, with **Has attachment** selected, and choose **Forward it to** the
   verified Trackr address. Keep Gmail's original copy.
5. Enable automatic trade imports in Trackr if desired. Validated supported contract
   notes can be promoted automatically; other files may need review. CSV/XLSX trade files
   are not automatically approved merely because they arrived by email.

## 6. Verify delivery and processing

Send one small supported statement to the generated inbox. In Mailgun logs verify that
the route matched and the backend responded with 200. In Trackr verify the delivery,
processing result and import history, then verify the resulting portfolio refresh.
Gmail's confirmation message is a separate setup exception and needs no attachment.

Useful backend checks are `GET /health/storage`, owner-authenticated
`GET /investors/{id}/email/settings` and `/email/activity`, and ops-authenticated
`GET /ops/jobs/pending`. Settings should show `available: true` and a generated address.
An accepted delivery that stays pending usually means the worker is not running or lacks
the production storage/encryption configuration.

**Current hosting size limit:** Vercel Functions accept at most 4.5 MB request bodies.
MIME encoding and form encoding count toward that limit, so a smaller attachment can
still exceed it. Trackr's configurable 25 MiB application limit cannot override Vercel.
Use a small file for the first test. Reliable larger-message support requires a receiver
host with a sufficient request limit, or a separately implemented stored-message retrieval
flow; setting Mailgun `store()` alone is not supported by this receiver.

Source: [Vercel Function request limits](https://vercel.com/docs/functions/limitations).

## Other environment variables missing from the supplied list

For the **trackrplus frontend** project, verify this production setting:

```dotenv
TRACKR_API_BASE_URL=https://trackr-ebon.vercel.app
```

The same-origin API proxy is already healthy, so an existing compatible setting may
already provide this upstream. Set the explicit server-side variable for this release.

For the **trackr backend**, the enabled public market assistant also needs:

```dotenv
MARKET_ASSISTANT_OPENROUTER_API_KEY=<your assistant OpenRouter key>
MARKET_ASSISTANT_MODEL=<the pinned free model ID configured locally>
MARKET_ASSISTANT_EXA_API_KEY=<your assistant Exa key>
```

Use the existing local keys in the backend's private environment; do not paste them into
Git or frontend variables. `MARKET_ASSISTANT_MODEL` must identify one specific model
ending in `:free`; `openrouter/free`, `openrouter/auto`, and fallback lists are rejected.
The assistant model key is separate from `OPENROUTER_API_KEY`, which serves optional
document parsing/narration. Keep the existing parsing/narration flags unchanged unless
you intend to configure those separate features. `EXA_API_KEY` is another optional key
used by corporate-action research; the assistant's Exa setting does not configure it.

These quota settings have working defaults; add them only to make your intended limits
explicit:

```dotenv
MARKET_ASSISTANT_MESSAGE_DAILY_LIMIT=20
MARKET_ASSISTANT_SEARCH_DAILY_LIMIT=50
MARKET_ASSISTANT_MODEL_DAILY_LIMIT=50
MARKET_ASSISTANT_RETRIEVAL=lexical
```

Keep the existing mutual-fund flags enabled if those features are intended:
`MF_REFERENCE_ENABLED`, `MF_VALUATION_ENABLED`, `MF_TAX_ENABLED`. No additional vendor
key is required for the public AMFI reference source. Production should keep `DEBUG=false`
and `CORS_ORIGINS=https://trackrplus.vercel.app` (plus any other frontend origins you
actually use). Keep Workspace/hosted-job flags at their existing intended values.

The dashboard's masked “Needs Attention” labels alone do not prove a value is missing.
Preserve the existing database credentials, encryption key, PAN pepper, JWT secret, ops
key and cron secret. Renaming/rotating those is not needed for this release.
