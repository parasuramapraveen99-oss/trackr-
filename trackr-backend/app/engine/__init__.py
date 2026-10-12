"""The engine: layer 2, "what is true?".

Pure functions over plain data. No database calls, no network, no clock reads inside a
calculation -- ``as_of`` is always passed in explicitly.

The dependency rule, from docs/ARCHITECTURE.md: IMPORTS POINT DOWNWARD ONLY. This
package must never reach upward into the adapter or web layers, nor into any framework.
That single constraint is what keeps the fixtures runnable with zero infrastructure and
what makes a port to another language mechanical rather than archaeological.

Everything here is derived from transactions::

    Transactions -> Holdings -> Portfolio -> Performance -> Analytics -> Insights

Holdings are never stored. They are computed on read, every time.
"""

from __future__ import annotations

from app.engine.dedupe import dedupe, make_dedupe_key
from app.engine.errors import (
    DuplicateTransactionError,
    EngineError,
    MissingPriceError,
    NegativeHoldingError,
    UnparseableRowError,
)
from app.engine.holdings import MAX_PRICE_STALENESS_DAYS, compute_holdings
from app.engine.lots import compute_lots
from app.engine.realised import compute_realised
from app.engine.reconcile import reconcile
from app.engine.snapshot import compute_snapshot
from app.engine.types import (
    ACQUIRING_TYPES,
    DISPOSING_TYPES,
    FREE_SHARE_TYPES,
    AssetClass,
    Benchmark,
    Discrepancy,
    Exchange,
    Holding,
    ImportSummary,
    Lot,
    Performance,
    Realised,
    Snapshot,
    Source,
    Term,
    Transaction,
    TxnType,
)
from app.engine.xirr import (
    compute_fy_xirr,
    compute_period_xirr,
    compute_xirr,
    compute_xirr_holdings,
    financial_year_start,
)

__all__ = [
    "ACQUIRING_TYPES",
    "DISPOSING_TYPES",
    "FREE_SHARE_TYPES",
    "MAX_PRICE_STALENESS_DAYS",
    "AssetClass",
    "Benchmark",
    "Discrepancy",
    "DuplicateTransactionError",
    "EngineError",
    "Exchange",
    "Holding",
    "ImportSummary",
    "Lot",
    "MissingPriceError",
    "NegativeHoldingError",
    "Performance",
    "Realised",
    "Snapshot",
    "Source",
    "Term",
    "Transaction",
    "TxnType",
    "UnparseableRowError",
    "compute_fy_xirr",
    "compute_holdings",
    "compute_lots",
    "compute_period_xirr",
    "compute_realised",
    "compute_snapshot",
    "compute_xirr",
    "compute_xirr_holdings",
    "dedupe",
    "financial_year_start",
    "make_dedupe_key",
    "reconcile",
]
