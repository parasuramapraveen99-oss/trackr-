Source: the owner-supplied `trackrplus-analytics-milestone1/trackr_analytics`,
version 0.2.0, local work dated 14 October 2026. Full integration 17 October 2026.

The eight calculation modules (`core`, `cross_section`, `stats`, `periods`,
`hypothetical`, `benchmarks`, `upstream`, `service`) are byte-for-byte copies.
SHA-256 fingerprints are in `docs/audits/ANALYTICS_ADVISOR_SOURCE.json`.
The adapter, contract and UI are maintained separately; the original package is unchanged.

Original authorship is retained. This internal integration is requested by the owner;
no third-party authorship or redistribution licence is asserted. No source licence was
present in the supplied folder. Formula/source definitions remain in its `FORMULAS.md`.

`app/analytics/milestone.py` runs all seven sections and projects their values and
quality envelopes. `app/routers/analytics_advisor.py` materialises owned, cached inputs;
all accounting still uses the existing financial engine. No advisor code fetches,
imports, modifies transactions, models tax, or resolves corporate actions.

The old offline legacy-projection refusal is retained in the original source. The
versioned adapter replaces it with the implemented `trackr-advisor-v2` consumer contract;
it does not manufacture health grades or fill unavailable metric values.

Original independent tests remain unchanged in the supplied folder. Tests in
`tests/analytics/test_milestone.py` pin source hashes and integration anchors;
`tests/test_db_advisor_analytics.py` exercises the API and ownership boundary.
