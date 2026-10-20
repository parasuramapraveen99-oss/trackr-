# Snapshot batch diagnosis and validation — 19 October 2026

## Follow-up: removing the remaining per-portfolio round trips

The first change still made separate reads and claims per investor/scope. The next
change loads a bounded claim across scopes together, reads the raw ledger once,
shares price/reference reads, and sends independent price reads and atomic publication
writes using Psycopg pipeline mode. Scope separation and financial rules are retained.
Catch-up also prefetches its missing scopes together.

| Same 32 actual pending jobs | Deployed | First change | Current change |
| --- | ---: | ---: | ---: |
| Worker time | 148.972 s | 53.927 s | 8.288 s |
| Client database-call time, including pipeline synchronization | 148.552 s | 53.552 s | 8.185 s |
| SQL statements, excluding commit/rollback/synchronization | 355 | 117 | 18 |
| Ledger reads | 32 | 8 | 1 |
| Bulk/single snapshot insert statements | 32 | 8 | 1 |

All three 32-job runs produced exactly the same stored snapshot values. The current
500-job run took **25.799 s**, down from the first change's **61.376 s**, with **53 SQL
statements**, **one ledger read**, **0.150 s** of instrumented engine computation and
zero selected jobs left pending. Its complete stored values also matched the first
500-job run. There was no deployed-code 500-job Neon run.

These remain development-host-to-Neon measurements. They are not a measured Vercel
import-to-chart duration. No UI components, public API contracts or financial rules
were changed by this follow-up. It does not establish full browser-level production
equivalence or eliminate the time needed for genuinely missing source data.

The expanded regression selection passed **311 tests**, with **5 skipped** because
private real-document fixtures were unavailable and **1 deselected**. It includes
imports, source precedence, holdings, gap resolution, aliases/actions, snapshots,
fund routing, analytical routes and benchmark-series routes. New failure-path tests
verify investor/advised separation, isolation of malformed tasks, and atomic rollback
of both snapshots and queue completion when a pipeline statement fails.
After applying the shared reads to the catch-up path, its final focused selection
also passed all **52 tests**. Ruff and whitespace checks passed.

The sections below retain the original diagnosis and first-change measurements.

## What causes the long batch

The 210-second cycle budget is a ceiling, not a sleep or the time required by one
calculation. The deployed snapshot implementation repeats database reads, ledger
replays, individual writes, queue updates and commits for every historical date.
Network/database latency accumulates inside that loop even when exchange downloads
and the financial calculation itself are fast.

The session-lock defect is separate: it can stop the next cycle altogether. Fixing
that defect alone does not reduce the work inside a successful cycle.

Production logs recorded a 204.23-second shared cycle with 253 reference tasks and
256 snapshot writes. Those logs do not contain sufficient phase instrumentation to
assign an exact duration to each original phase retrospectively. The measurements
below isolate the snapshot worker body rather than claiming to reproduce that whole
production cycle.

## Measured against Neon

An expiring, isolated production-data branch in the same Neon `aws-us-east-2` region
was used, with the same 0.25–2 CU autoscaling limits. Production tables and environment
credentials were not changed. Each run restored identical derived snapshots and queue
state on the test branch. The original code was loaded from `trackr-deploy`; the
changed code was loaded from this checkout, in separate processes.

| Same 32 actual pending snapshot jobs | Deployed code | Changed code |
| --- | ---: | ---: |
| Total worker time | 148.972 s | 53.927 s |
| SQL and commit/rollback calls, elapsed client time | 148.552 s | 53.552 s |
| Instrumented financial engine calls | 0.250 s | 0.027 s |
| SQL statements, excluding commit/rollback | 355 | 117 |
| Commits | 67 | 19 |
| Ledger reads | 32 | 8 |
| Snapshot insert statements | 32 | 8 |
| Completed jobs | 32 | 32 |

The complete stored snapshot values matched exactly, excluding computation timestamps.
The 32 jobs span eight investor/scope combinations, so this is more demanding than
32 consecutive dates for one scope. About 99.7% of the original runtime was inside
client database calls; this includes network transfer and server execution, not just
Postgres CPU. Simple warmed `SELECT 1` calls commonly took 0.25–0.35 seconds on this
connection.

The changed worker also completed all 500 selected real pending jobs in **61.376 s**,
with **117 SQL statements**, **19 commits**, **8 ledger reads** and **8 bulk snapshot
inserts**. Instrumented engine computation took **0.180 s**; database calls took
**60.537 s**. No selected snapshot jobs remained pending. This larger run retained the
same SQL count as the 32-job run because dates shared the same eight investor/scope
groups. The deployed worker was not run against all 500 jobs on Neon, so there is no
measured before/after Neon runtime ratio for that larger workload.

The application ran on the existing development host and connected to remote Neon.
These are real Neon timings, not local Postgres timings, and are not Vercel runtime
measurements. Unrelated source discovery and HTTP enrichment were disabled in both
runs to isolate claims, input reads, calculations, snapshot writes and completion.
Upload/parsing, import promotion, missing exchange-file downloads, gap research and
chart requests are outside this benchmark.

## What changed inside the batch

- Input rows are loaded once per investor/scope, with aliases and corporate actions
  shared across the batch. Prices are fetched for the relevant pending date range.
- Holdings replay runs at transaction, action or alias boundaries. Daily prices still
  value every required date using the existing financial rules.
- Snapshot queue jobs are claimed in groups of up to 64 dates across investor/scopes;
  output and completion are bulk written and committed per group.
- Catch-up uses chunks of up to 32 cached trading dates. Unsettled archive dates retain
  individual download checkpoints. Completed portfolios do not reload their ledgers.
- After a replay baseline exists, changed transactions invalidate only the affected
  date suffix. Older jobs without that baseline conservatively invalidate full history
  once. Immutable broker transactions remain authoritative.
- Import discovery skips unchanged work rather than rewriting every existing job.
- Sector enrichment is queued separately from valuation. Session-lock owners use a
  direct connection rather than a transaction-pooled connection.
- Cycle logs now include complete phase timings; worker logs include scheduling,
  preparation and snapshot group timings. This makes the next production cycle
  attributable rather than inferring timings from allocated budgets.

## Validation and release state

307 targeted regression tests passed, with one deselected. Ruff and whitespace checks
passed. Coverage includes old/new snapshot equivalence, aliases, revised/cancelled
corporate actions, price staleness and absence, off-exchange treatment, grouped queue
completion, suffix invalidation, scheduling and catch-up checkpoints.

The changes are in this development checkout. They have not been copied into
`trackr-deploy` or deployed to Vercel. The original production latency cannot be called
fixed live until the tested change is released and an actual import-to-chart cycle
is measured there.
