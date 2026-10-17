**Snapshot and holdings-gap progress — 18 October 2026**

This change uses the existing Vercel deployment and PostgreSQL queues. It requires no
database migration, Redis service, provider change or financial-engine change.

- Reference claims prioritize known identity/action application and account replay,
  followed by snapshot repairs, newest dates first. Every fifth claim gives background
  work a turn so price/reference discovery continues during a chart backlog.
- Replay records a hash of its normalized ledger inputs in its existing task payload.
  Unchanged inputs preserve the rebuilt chart and any active research checkpoint or
  source-outage backoff. Changed inputs still invalidate stale snapshots. Completed gap
  checks can run again after new observations.
- Gap closing first applies the existing structured evidence checks across the account.
  Remaining research is checkpointed in the existing GAP payload, one ISIN per claim.
  Each claim rechecks the current ledger and structured evidence for that ISIN before
  research. Rate limits preserve the checkpoint and existing retry delay. All original
  evidence, uniqueness, append-only and automatic-reversal rules remain in force.
- Mutual-fund snapshot rebuilds save their input fingerprint before committing partial
  points. Later calls resume those points rather than deleting them after a time limit.
- An owner-authorized `/investors/{id}/snapshots/status` read returns a snapshot version
  and pending-work flag without fetching prices or replaying a ledger. The chart checks
  it every five seconds while work is pending and every thirty seconds while idle;
  hidden tabs pause under the existing fetch hook. Full chart bodies refresh when the
  saved version changes, including corrections at unchanged dates. The existing live
  quote refresh remains in place.

This improves ordering, resumability and visibility. A single source/model call can still
take its configured timeout. Provider blocking, quotas and missing evidence can still
leave a security unresolved; no timing or completeness guarantee is introduced.

Regression coverage in Trackr-plus: `tests/test_reference_progress.py` uses disposable PostgreSQL and
offline sources to cover priority/fairness, replay invalidation, persistent checkpoints,
fund rebuild interruption/resume and owner-scoped chart versions. Existing gap/evidence,
snapshot, tax, import and engine suites remain applicable.
