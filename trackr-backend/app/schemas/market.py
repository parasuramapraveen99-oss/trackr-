"""DTOs for GET /market/* -- market reference data, scoped to no investor.

Shapes deliberately mirror ``BenchmarkSeriesResponse`` in app/schemas/analytical.py so the
dashboard's chart can consume either without a second adapter: same ``name``, same
``points[{as_of, value}]``, same decimal-as-string convention.
"""

from __future__ import annotations

from pydantic import BaseModel


class IndexPoint(BaseModel):
    #: ISO date of the published close.
    as_of: str
    #: The index level as a decimal STRING. Never a float -- CLAUDE.md: no numeric value
    #: crosses a boundary as a float, because the one that does is the one that loses
    #: precision somewhere nobody is looking.
    value: str


class IndexSeriesResponse(BaseModel):
    """One index's published closes over a requested range.

    ``points`` is empty both when the range genuinely holds no trading day and when the
    upstream source was unreachable. The caller does not need to tell those apart -- both
    mean "there is no line to draw" -- and the server logs which it was.
    """

    name: str
    points: list[IndexPoint] = []
