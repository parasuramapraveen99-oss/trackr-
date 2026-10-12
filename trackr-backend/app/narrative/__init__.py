"""Turning computed analytics into statements a person can read.

THE ONE RULE THIS PACKAGE EXISTS TO ENFORCE: a language model never emits a number.

``app/routers/analytics.py`` already produces every figure the analytics screen shows,
each reproducible by replaying the ledger (invariant 15). A model asked to write about
those figures will, sooner or later, write one down wrong -- not because it is careless
but because generating a digit and looking a digit up are the same operation to it. So it
is not asked to. It selects which facts to talk about and in what order; the numerals,
the comparisons and the directional words are all resolved here, from the fact pack, by
code.

The layering, which ``make lint`` enforces:

    factpack.py   AnalyticsResponse -> {fact_id: Fact}. Pure projection, no new maths.
    registry.py   what each kind of fact IS -- unit, precision, how it may be compared.
    select.py     which fact_ids a given surface is allowed to cite.
    egress.py     the include-list serialiser: the only thing that builds a wire payload.

Nothing in this package may import ``app.db``, ``app.routers``, ``app.ingest``,
``app.schemas``, ``app.auth``, ``app.storage``, ``fastapi`` or ``pydantic`` -- the same
grep that already guards ``app/analytics/``. Nothing here may import ``app.ai`` either,
with one named exception when it arrives (``client.py``), so the half that decides what is
true is physically unable to call a network. That is the ``llm_extract`` / ``llm_verify``
split, and it is the reason this codebase can trust an LLM feature at all.

TENANCY IS STRUCTURAL, NOT CHECKED. A pack is built inside one request, from one
investor's own ledger replay, after ``require_owned_investor`` has run. There is no
lookup service keyed by ``fact_id`` and there must never be one: every id is a global
dotted path like ``health.component.concentration.score``, so a registry that could
resolve an id WITHOUT a pack in hand would resolve it against whoever asked last.
"""
