"""Market reference data: facts about the market, not about anybody's portfolio.

WHY THIS ROUTER EXISTS. Every other read in this API is investor-scoped, which is right
for everything derived from a ledger -- but it left one real question unanswerable: "what
has the market done", asked by someone whose ledger is empty. The dashboard's value chart
had to answer that with "Not enough history to plot", because the only index series on the
wire was ``GET /investors/{id}/benchmark-series``, and that is cash-flow replication dated
to the investor's own snapshot dates (docs/ENGINE_SPEC.md section 6). With no snapshots it
correctly returns no points -- there is no portfolio to replicate cash flows for. It was
never the wrong endpoint; it was the wrong question.

WHAT MAKES THIS SAFE TO SERVE UNSCOPED. An index close is a published fact about a public
index. It contains nothing about any investor, is keyed by no ISIN, and sits in
``index_level``, a table with no foreign key into ``security`` or ``investor``. So there
is no tenancy to enforce -- only authentication, for the same reason ``GET /brokers`` has
it: it is not public data in the sense of "serve it to the internet", it is data with no
owner.

WHAT IT MUST NOT BECOME. It triggers an outbound fetch on a cache miss, so an uncapped
range would make it a proxy to niftyindices.com that anyone with a login could point at
any span. ``MAX_SERIES_YEARS`` bounds that, and the fetch stays behind the same
store-then-reread path the benchmark endpoint already uses, so a warmed range costs one
query.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.dependencies import get_current_user
from app.database import get_conn
from app.db.index_levels import list_index_levels
from app.prices.index_sources import (
    TRI_INDICES,
    IndexSourceFetchFailed,
    IndexSourceFormatError,
    fetch_and_store,
)
from app.schemas.market import IndexSeriesResponse

logger = logging.getLogger("app")

router = APIRouter(prefix="/market", tags=["market"])

#: The widest span one request may ask for. Long enough for any chart this product draws
#: (the dashboard's own longest range is "all"), short enough that the endpoint cannot be
#: used to pull decades out of the upstream source on demand.
MAX_SERIES_YEARS = 25

#: What an empty account's chart draws. NIFTY 500 for the same reason it is the
#: dashboard's benchmark default (app/routers/analytical.py DEFAULT_SERIES_BENCHMARK):
#: broad enough to stand in for "the market" rather than for the large-cap top of it.
DEFAULT_INDEX = "NIFTY500_TRI"


@router.get(
    "/index-series",
    response_model=IndexSeriesResponse,
    dependencies=[Depends(get_current_user)],
)
def index_series(
    request: Request,
    start: date,
    end: date,
    name: str = DEFAULT_INDEX,
    connection: Any = Depends(get_conn),
) -> dict[str, Any]:
    """Daily closes for one index over ``[start, end]``.

    Unlike the benchmark series this needs no investor, no snapshots and no transactions
    -- it is the published index and nothing else. That is exactly what makes it useful
    on a dashboard with an empty ledger.

    Degrades to an empty ``points`` list rather than erroring when the upstream source is
    unreachable, matching ``benchmark_series_endpoint``: a chart decoration must not take
    a page down with it. The caller is expected to keep its own empty state for that case.
    """
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must be on or before end")
    if (end - start).days > MAX_SERIES_YEARS * 366:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"range must not exceed {MAX_SERIES_YEARS} years",
        )

    index = TRI_INDICES.get(name)
    if index is None:
        # Same refusal as _reject_unsourced_benchmark, and for the same reason: a
        # contract enum member with no adapter behind it (SENSEX_TRI) must be told
        # plainly, not answered with an empty series that reads like a data gap.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{name} is not yet available: no reliable public TRI data source exists",
        )

    rows = list_index_levels(connection, index.storage_name, start, end)
    if not rows:
        # Cold cache. One fetch covers the whole span -- the source answers a range, not
        # a day at a time -- then the same read is retried, exactly as
        # app/routers/analytical.py::_index_levels does it.
        request_id = getattr(getattr(request, "state", None), "request_id", None)
        try:
            fetch_and_store(connection, start, end, index=index)
        except (IndexSourceFetchFailed, IndexSourceFormatError) as exc:
            logger.warning(
                "index series omitted: %s fetch failed for %s..%s (request_id=%s): %s",
                name,
                start,
                end,
                request_id,
                exc,
                exc_info=True,
            )
            return {"name": index.storage_name, "points": []}
        rows = list_index_levels(connection, index.storage_name, start, end)

    return {
        "name": index.storage_name,
        # str, not float: every money and level value crosses this API as a decimal
        # string, per CLAUDE.md's rule that nothing numeric round-trips through a float.
        "points": [{"as_of": d.isoformat(), "value": str(close)} for d, close in rows],
    }
