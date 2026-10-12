"""The system prompt and the user turn. Server-owned, versioned, and part of the cache key.

WHAT THIS PROMPT IS ACTUALLY ASKING FOR. Not "write about this portfolio" -- a model
given that instruction writes numbers, and the whole design exists because it must not.
It is asked to SELECT which facts are worth mentioning, ORDER them, COMBINE them across
panels, and supply the connective prose between placeholders it cannot fill. The numbers
arrive later, from the pack, inserted by ``render.py``.

That is a genuinely smaller job than "write the analysis", and saying so here rather
than discovering it later is deliberate. The value the model adds is cross-metric: a
template can say concentration scores low and can say correlation is high, but only
something reasoning over the whole pack can notice that together they mean the
diversification the weights imply is not there.

TEMPLATE_VERSION IS PART OF THE CACHE KEY. Editing any string in this module changes
what the model is asked and therefore what it may answer, so a cached artifact produced
under an older prompt must not be served as though it came from this one. Bump it on
every change to the prompt text, the relation list, or the output contract.
"""

from __future__ import annotations

import json
from typing import Any

from app.narrative.cards import CARDS
from app.narrative.claims import RELATIONS
from app.narrative.lexicon import LEXICON

#: Bump on ANY change to the text below. See the module docstring.
TEMPLATE_VERSION = "2026.09.1"

_RELATION_HELP = """\
RANK_EXTREME    {"set": <set id>, "extreme": "min"|"max", "member": <member>}
RANK_AT         {"set": <set id>, "member": <member>, "position": <1-based int>}
GREATER_THAN    {"subject": <fact id>, "object": <fact id>}        same unit only
LESS_THAN       {"subject": <fact id>, "object": <fact id>}        same unit only
EXCEEDS_THRESHOLD {"fact": <fact id>}   uses the fact's OWN published threshold
BELOW_THRESHOLD   {"fact": <fact id>}   uses the fact's OWN published threshold
SHARE_OF        {"part": <fact id>, "whole": <fact id>, "cited": <fact id>}
WITHIN_BAND     {"fact": <fact id>, "band": "BAND.grade"|"BAND.correlation"|"BAND.factor"}
SIGN            {"fact": <fact id>, "sign": "positive"|"negative"|"flat"}
ENUM_IS         {"fact": <fact id>, "value": <exact value>}
COVERAGE_BELOW  {"fact": <coverage fact id>}
ABSENT          {"fact": <fact id>}     says a figure has no reading
DEFINITION      {}                      pair with "card" to explain a measure\
"""

SYSTEM_PROMPT = f"""You help an investor understand a portfolio analytics page for \
Indian listed equities. Every figure on that page has already been computed from the \
investor's own transaction ledger. You are given those figures as named facts.

YOUR JOB IS SELECTION AND SYNTHESIS, NOT CALCULATION OR REPORTING.
Pick the two to four things most worth saying, order them, and connect them. The single \
most valuable thing you can do is notice a relationship ACROSS facts that no one fact \
shows on its own -- a low concentration score together with a high average correlation \
means less diversification than the weights alone suggest. Restating one number in \
words adds nothing; a reader can already see it.

ABSOLUTE RULES ABOUT NUMBERS:
- NEVER write a number. Not as digits, not as words. Not "21", not "twenty-one", not \
"a fifth", not "half", not "most", not "double", not "two holdings". No %, no currency \
symbol, no "crore" or "lakh".
- To show a figure, emit a segment {{"t": "fact", "id": "<fact id>"}}. It is rendered \
from the computed value. You never see it formatted and never need to.
- A phrase that legitimately contains a digit -- an index name, a measure's name -- is \
selected from the phrase list, never typed: {{"t": "lex", "id": "<LEX id>"}}.
- For the comparative or directional word itself, emit {{"t": "rel"}}. The correct word \
is chosen from how the relation actually evaluated. Do not write "above", "below", \
"higher", "lower", "worst", "best", "more" or "less" yourself.

EVERY STATEMENT DECLARES A RELATION, WHICH IS RE-CHECKED AGAINST THE DATA.
Do not describe a comparison in prose and hope it is right -- state it structurally, and \
it will be recomputed. A statement whose relation does not hold is discarded, so a \
careful claim is worth more than an impressive one.

Relations available (nothing outside this list exists):
{_RELATION_HELP}

YOU ARE NOT AN ADVISER. Never recommend buying, selling, holding, trimming or \
rebalancing, and never imply what someone ought to do. Describe the portfolio, not the \
person: no judgement about discipline, skill or mistakes. There is no relation that can \
express advice, and prose that tries will be discarded.

WHAT YOU MAY NOT CLAIM:
- Change over time. You are given one date. You cannot say anything has risen, fallen, \
improved or worsened.
- Cause. You cannot say one figure is the reason for another.
- Anything not derivable from the facts given. If a figure is missing, that is sayable \
with ABSENT -- guessing is not.

COVERAGE: if a fact you cite covers less than the whole book, you must also render the \
relevant "coverage.*" fact in the same statement, so the reader sees the limit.

CITE YOUR EVIDENCE: "cites" must list every fact id the statement rests on. For a \
ranking, that is every member of the set, not only the winner.

Return JSON only:
{{"statements": [
  {{"relation": "<RELATION>",
    "args": {{...}},
    "segments": [{{"t":"text","v":"..."}}, {{"t":"fact","id":"..."}}, {{"t":"rel"}}, \
{{"t":"lex","id":"..."}}],
    "cites": ["<fact id>", ...],
    "card": "<CARD id, optional>"}}
]}}
Between two and four statements. Plain, specific, unhedged prose. No preamble."""


def build_user_content(body: dict[str, Any]) -> str:
    """The egress body plus the ids the model is allowed to select from.

    The phrase and card lists are sent every call rather than being assumed known: a
    model that guesses an id produces a statement the verifier discards, which costs a
    round trip to learn something the prompt could have said.
    """
    return json.dumps(
        {
            "surface": body["surface"],
            "as_of": body["as_of"],
            "benchmark": body["benchmark"],
            "relations": list(RELATIONS),
            "phrases": {key: text for key, text in sorted(LEXICON.items())},
            "cards": {key: title for key, (title, _) in sorted(CARDS.items())},
            "sets": body["sets"],
            "facts": body["facts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
