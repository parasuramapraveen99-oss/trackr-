"""DTOs for GET /investors/{id}/narrative and the narrative ops routes.

NO FIGURE CROSSES THIS BOUNDARY AS PROSE. A statement is a list of tokens; a ``fact``
token carries the value, its unit and the precision to render it at, and the browser
formats it through ``trackr-frontend/app/analytics/format.ts``. The server deliberately
does not produce a finished sentence -- see ``app/narrative/render.py`` for the two
reasons (one formatter, and privacy mode's <Figure> cannot mask a baked string).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class NarrativeCardOut(BaseModel):
    """A definition, written by us and selected by the model. Never paraphrased."""

    id: str
    title: str
    body: str


class NarrativeStatementOut(BaseModel):
    relation: str
    #: Ordered tokens. ``{"t":"text","v":...}`` or
    #: ``{"t":"fact","id":...,"value":...,"unit":...,"digits":...}``.
    tokens: list[dict[str, Any]]
    #: Every fact the statement rests on, as the claim itself declared. This is what the
    #: "how this was made" disclosure is built from, rather than reconstructed afterwards.
    cites: list[str]
    card: NarrativeCardOut | None = None


class NarrativeResponse(BaseModel):
    as_of: date
    surface: str
    #: ``ready`` -- statements below. ``disabled`` -- the feature is off on this
    #: deployment. ``not_generated`` -- nothing has been generated for this exact set of
    #: figures yet, which is the NORMAL state between a price move and the next run.
    #:
    #: Three states rather than an empty list, because "we have nothing to say" and "we
    #: have not looked" are different claims and the page should not present the second
    #: as the first.
    state: str
    generated_at: datetime | None = None
    statements: list[NarrativeStatementOut] = []
    #: Verifier rule -> how many claims it refused, for THIS render. Surfaced rather
    #: than logged: a verifier whose failures are invisible is one nobody maintains.
    rejected: dict[str, int] = {}
    #: The date any change language is measured from, when an earlier pack is stored.
    compared_with: date | None = None


class NarrativeRunResponse(BaseModel):
    """What a generation run actually did. Counts, not a status string.

    ``artifacts_stored`` is the number this repo learned to ask for: a registered route
    that nothing calls is indistinguishable from a working one until someone counts rows.
    """

    as_of: date
    investors: int
    surfaces_attempted: int
    artifacts_stored: int
    cache_hits: int
    claims_kept: int
    claims_rejected: int
    unavailable: list[str] = []
