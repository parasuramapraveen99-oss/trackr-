"""Pydantic request/response DTOs for the routers.

EVERY FINANCIAL FIELD IS TYPED ``str``. Money, quantities, rates and ratios all cross the
JSON boundary as exact decimal strings, matching ``app/database.py``'s ``money()`` and
``rate()`` and ``schema/*.schema.json``'s ``$defs/decimal``.

Rates used to be the exception -- declared ``float`` here, serialised through
``float(value)`` there, on the argument that a display ratio does not need to be exact.
BE 02 removed the exception: an XIRR is the most-computed number in the system and was the
only one rounded on its way out. See ``rate()``'s docstring for what that cost.

INTEGERS AND BOOLEANS ARE NOT AFFECTED. A count, a holding-day span, a version and a flag
are not decimals and stay ``int``/``bool``. ``app/schemas/analytics.py`` is deliberately
still ``float``: those are the statistical measures (volatility, beta, Sharpe, drawdown)
computed by ``app/analytics/``, which is a float domain end to end by its own design and
not part of the ledger-derived financial contract. Converting its DTOs would claim a
precision the module behind them does not have.

These schemas describe that boundary; they do not change it.
"""
