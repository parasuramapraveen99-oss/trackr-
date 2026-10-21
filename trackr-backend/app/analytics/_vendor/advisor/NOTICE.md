Source: the owner-supplied Trackr+ analytics package.

- 0.2.0 (`trackrplus-analytics-milestone1/trackr_analytics`, 14 October 2026), integrated
  17 October 2026: `core`, `cross_section`, `periods`, `hypothetical`, `benchmarks`,
  `upstream` are byte-for-byte copies and unchanged since.
- 0.4.0 (`portfolio-health-performance-update/trackr_analytics`, 15 October 2026),
  integrated 21 October 2026: `service`, `stats`, `health_evidence` are byte-for-byte
  copies, verified against that package's `UPDATE_MANIFEST.json`.
- `health.py` is **Trackr-authored** (21 October 2026). The 0.4.0 package imports it but
  did not include it. It implements `PORTFOLIO_HEALTH.md` (portfolio-health-v1) exactly
  and reproduces the package's own hand reference (43773/754, displayed 58, grade C).

SHA-256 fingerprints of every module are in `docs/audits/ANALYTICS_ADVISOR_SOURCE.json`
and are pinned by `tests/analytics/test_milestone.py`. Any edit is a recorded, versioned
change, never a silent patch.

Original authorship is retained. This internal integration is requested by the owner;
no third-party authorship or redistribution licence is asserted. No source licence was
present in the supplied folders. Formula/source definitions remain in their `FORMULAS.md`
and `PORTFOLIO_HEALTH.md`.

`app/analytics/milestone.py` runs all seven sections and projects their quality envelopes.
`app/routers/analytics_advisor.py` materialises owned, cached inputs (including the
exchange-sourced `security_health_profile`) and projects health, capital state and health
evidence; all accounting still uses the existing financial engine. No advisor code
fetches, imports, modifies transactions, models tax, or resolves corporate actions.

Health grade status: experimental and uncalibrated. A missing component withholds the
grade and exposes a missing-information range; nothing is imputed.

Tests: `tests/analytics/test_advisor_health_*.py` (ported supplier tests plus rubric
specification checks), `tests/test_db_health_profiles.py`, `tests/test_db_analytics_cache.py`
and `tests/test_db_advisor_analytics.py`.
