# Assistant recovery and snapshot release validation

## Assistant failure reproduced

On 19 October, an authenticated test through the production frontend/API returned
`answer_unavailable` for “is market busy today?”. The planner searched for general
market news instead of clarifying whether the question meant volume, price movement
or opening hours. “What is a mutual fund?” returned a SEBI registration condition
rather than explaining the concept. An earlier runtime failure also recorded
`unsafe_planner_reply`; the validator rejected valid public index names such as
“Nifty 50” merely because they contain digits.

## Change

- A malformed or rejected plan receives one bounded, checked repair. Provider errors
  are not retried. Planning and answering share the existing single rewrite allowance,
  so a request still makes at most three model calls and two searches.
- Bare ambiguous market-activity questions must clarify before search. Explicit
  measures and public context continue through research. The clarification is written
  by the model, rather than substituting canned wording.
- Known public Nifty index numbers are permitted in clarifying questions. Other
  numbers, unsupported claims, unsafe links, personal details and advice remain rejected.
- Source-sentence ranking includes the actual question as well as model search terms,
  preferring concept definitions when a definition was requested.
- Privacy admission, the pinned free model/provider policy, evidence/citation checking,
  the daily provider ceilings and the per-login message quota remain in force.

## Validation

The full backend/database regression selection passed **3,784 tests**, with 24 skips
for private document fixtures or the optional embedding cache and four network tests
deselected. The final assistant-focused selection passed **173 tests**, including
bounded plan repair, the shared rewrite ceiling, public-index clarification and
explicit activity questions. The Trackr+ production build passed with its HTTPS API
upstream configured. Lint and whitespace checks passed.

A 20 October live provider/search check of the changed application returned:

| Public test question | Result | Time |
| --- | --- | ---: |
| What is a mutual fund? | Checked definition with one cited source | 10.52 s |
| Is market busy today? | Model clarification, no search | 1.90 s |
| What is an ETF? | Checked definition with one cited source | 10.09 s |

These live checks ran the changed application from the development host using its
configured providers. They are distinct from verification of the subsequently deployed
Vercel release. Network/provider failures still surface as service errors; no fabricated
answer replaces a failed model response.

For snapshot worker measurements and financial equivalence checks, see
[the snapshot batch report](SNAPSHOT_BATCH_PERFORMANCE_20261019.md).
