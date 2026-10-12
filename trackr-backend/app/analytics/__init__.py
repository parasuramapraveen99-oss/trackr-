"""Portfolio analytics: factor exposure, risk statistics, diversification structure.

WHY THIS IS NOT IN ``app/engine/``
----------------------------------
``make lint`` bans ``float(`` in ``app/engine/`` and that ban is correct: the engine moves
MONEY, and a binary float loses paise in ways that take days to find. This package moves
STATISTICS -- standard deviations, correlations, percentile ranks, betas. A correlation
coefficient is not money, does not reconcile against anything, and carrying it in Decimal
would mean re-implementing ``numpy.cov`` to satisfy the letter of a rule while breaking its
purpose.

So this is a separate layer with its own stated boundary, not a loophole in that one:

  1. Money and quantity ENTER as ``Decimal`` -- computed by ``app.engine``, never recomputed
     here. Nothing in this package derives a cost basis, a holding or a realised gain.
  2. Statistics are ``float`` and a float NEVER LEAVES AS MONEY. A VaR is a ratio; the rupee
     figure beside it is ``Decimal(worth) * Decimal(str(var_ratio))``, converted once, at
     the edge, by the caller.
  3. This package may import ``app.engine``. The reverse is forbidden, and so is importing
     ``app.db``, ``app.routers``, ``app.ingest`` or any framework -- ``make lint`` enforces
     all of it.

Everything here is a pure function over materialised data, in the same shape
``app.engine.benchmark`` uses: the router declares which dates it needs, a ``db`` module
reads them, and the result is passed in. No module here opens a connection or knows that
Yahoo, AMFI or the NSE exist.
"""
