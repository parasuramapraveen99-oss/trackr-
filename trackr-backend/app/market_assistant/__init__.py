"""Ask about markets: Trackr+'s public-only assistant (docs/AI_ASSISTANT_PRIVACY_PLAN.md).

Phase 1 of that plan. The assistant answers from a local concept catalogue and decides
locally whether a question is safe to handle at all. It makes NO external call: no Exa,
no model, no embedding. Raw question text never leaves this process.

THE BOUNDARY IS THE IMPORT GRAPH. Nothing in this package may import a portfolio
repository, the engine, analytics, narration, document storage, secrets or the database
layer. ``tests/test_market_assistant_boundary.py`` walks the imports and fails the build
if that changes. The one outside module allowed in is ``app.ingest.redaction``, which is
pure ``re`` and carries the identifier patterns already reviewed for the extraction path.
"""
