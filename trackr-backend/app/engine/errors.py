"""The error taxonomy from docs/ENGINE_SPEC.md section 9.

Each of these exists because the silent alternative is worse. Failing loudly on a
missing price is the difference between a wrong XIRR nobody notices and a red screen
naming the ISIN that needs attention.
"""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """Base for every error the engine raises."""


class MissingPriceError(EngineError):
    """A held ISIN has no usable price within the staleness window.

    RAISE. Never substitute zero, never carry a stale price forward silently. Valuing
    an unpriced holding at zero understates XIRR catastrophically, and a stale price
    shown as current is worse than no price at all.

    The ISINs are carried on the exception so the UI can name them rather than showing
    a generic failure.
    """

    def __init__(self, isins: list[str] | None = None, as_of: object = None) -> None:
        self.isins = sorted(isins or [])
        self.as_of = as_of
        detail = ", ".join(self.isins) if self.isins else "unknown ISIN"
        suffix = f" as at {as_of}" if as_of is not None else ""
        super().__init__(f"No usable price for: {detail}{suffix}")


class MissingIndexLevelError(EngineError):
    """A benchmark replication needs an index level for a date that has none within the
    staleness window -- docs/ENGINE_SPEC.md section 6.

    RAISE, same posture as ``MissingPriceError`` and for the same reason: a missing
    index level silently defaulted to the nearest available one would produce a
    confidently wrong benchmark XIRR. Unlike a held ISIN's missing price, this is never
    allowed to fail the whole ``/performance`` response -- the router catches it and
    degrades ``benchmark`` to ``null``, since the benchmark is optional and everything
    else in the response is still valid.

    The dates are carried on the exception so the caller can log exactly which ones were
    missing rather than a generic failure.
    """

    def __init__(self, index_name: str, dates: list[Any] | None = None) -> None:
        self.index_name = index_name
        self.dates = sorted(dates or [])
        detail = ", ".join(str(d) for d in self.dates) if self.dates else "unknown date"
        super().__init__(f"No usable {index_name} level for: {detail}")


class MissingCIIError(EngineError):
    """A pre-cutoff MF_DEBT lot's indexed cost of acquisition needs a CBDT Cost
    Inflation Index value for its purchase and/or sale financial year
    (``docs/TAX_ENGINE.md`` "Indexation"), and one or both years are absent from the
    reference table.

    RAISE, same posture as ``MissingPriceError`` and ``MissingIndexLevelError``: a
    missing CII value silently defaulted to 1.0 would leave the cost unindexed while
    looking computed, and skipping indexation for the lot would understate the cost of
    acquisition -- both produce a confidently wrong (inflated) capital gain. CLAUDE.md
    rule 4 ("a missing price raises, never defaults to zero") applies here to the CII
    table exactly as it does to ``price``.

    The financial years actually missing are carried on the exception (one or both of
    the purchase/sale year) so the caller can name exactly what needs sourcing.
    """

    def __init__(self, financial_years: list[str] | None = None) -> None:
        self.financial_years = sorted(financial_years or [])
        detail = ", ".join(self.financial_years) if self.financial_years else "unknown FY"
        super().__init__(f"No CII value for financial year(s): {detail}")


class DuplicateTransactionError(EngineError):
    """A dedupe_key collision.

    Not a failure. The importer skips the row, counts it, and reports the count in the
    import summary. Email forwarding will deliver the same contract note twice; that is
    expected behaviour, not an exception path.
    """

    def __init__(self, dedupe_key: str | None = None) -> None:
        self.dedupe_key = dedupe_key
        super().__init__(f"Transaction already present: {dedupe_key}")


class UnparseableRowError(EngineError):
    """Ingestion could not map a source row to a Transaction.

    Reject THAT ROW with its row number and import the rest. One malformed line in a
    tradebook must not cost the user the other 323.
    """

    def __init__(self, row: int | None = None, reason: str = "") -> None:
        self.row = row
        self.reason = reason
        super().__init__(f"Row {row}: {reason}" if row is not None else reason)


class CapitalGainsNotApplicableError(EngineError):
    """The asset class has no capital-gains holding-period threshold.

    RAISE. ``schema/asset_classes.json`` marks ``long_term_days: null`` for classes
    like FD (interest income, not a capital gain), PPF_EPF (tax-exempt), and
    gold/jewellery/art (no threshold sourced yet) -- see
    ``docs/TAX_ENGINE.md`` "Holding-period thresholds". Returning SHORT or LONG for
    one of these would assert a capital-gains distinction that class's tax treatment
    does not have, which is worse than refusing to answer.
    """

    def __init__(self, asset_class: str) -> None:
        self.asset_class = asset_class
        super().__init__(
            f"{asset_class} has no capital-gains holding-period threshold "
            "(long_term_days is null in schema/asset_classes.json) -- "
            "SHORT/LONG classification does not apply"
        )


class DemergerAdjustmentError(EngineError):
    """A ``DEMERGER_COST_OUT``/``DEMERGER_COST_IN`` row (``docs/ENGINE_SPEC.md`` section
    11) replayed against an ISIN with no current position to adjust.

    RAISE. It means the paired ``DEMERGER_IN``/``BUY`` history for that ISIN is missing,
    or the two adjustment rows are dated or ordered wrong relative to it -- silently
    dropping the cost transfer would leave a confidently wrong pool, the same failure
    mode ``NegativeHoldingError`` guards against on the quantity side.
    """

    def __init__(self, isin: str | None = None) -> None:
        self.isin = isin
        super().__init__(
            f"Demerger cost adjustment for {isin} has no open position to adjust -- "
            "the paired DEMERGER_IN/BUY history is missing or the rows are mis-ordered"
        )


class NegativeHoldingError(EngineError):
    """Disposals exceed acquisitions for an ISIN.

    Flag loudly: this means buy history is missing, not that the investor is short.
    The usual cause is a tradebook downloaded with a date filter -- Console caps each
    download at 365 days. The fix is one file per financial year, going back until a
    download returns empty. A less common cause is a corporate action (merger, demerger,
    symbol change) that changed the ISIN's quantity without a matching ledger row.

    ``as_of`` -- the date of the disposal that first ran short -- is carried
    specifically so the caller can say "as of THIS date" rather than leaving the
    investor to guess which of possibly years of transactions is the culprit.
    """

    def __init__(
        self,
        isin: str | None = None,
        quantity: object = None,
        as_of: object = None,
    ) -> None:
        self.isin = isin
        self.quantity = quantity
        self.as_of = as_of
        # quantity arrives as (held - disposed) -- negative -- so this reports the
        # shortfall as a positive number rather than the raw negative balance.
        deficit = -quantity if quantity is not None else None
        when = f" as of {as_of}" if as_of is not None else ""
        super().__init__(
            f"Holding for {isin} went negative ({deficit} short){when}; buy history is incomplete"
        )


class CorporateActionEvidenceError(EngineError):
    """A derived corporate-action row (``auto_corporate_action_id``) whose reference
    evidence was withdrawn -- the action is no longer VERIFIED and screened, has gone
    from the reference table, or its entitlement is fractional. ``docs/ENGINE_SPEC.md``
    section 9 (OD-5, pending owner confirmation).

    RAISE from the strict replays (``compute_holdings``, ``compute_lots``,
    ``compute_realised``). ``compute_holdings_partitioned`` excludes the ISIN BY NAME
    instead, so one withdrawn action never blanks the rest of a portfolio. Never a
    guessed quantity: the effect is neither applied nor silently dropped.
    """

    def __init__(self, isin: str, reason_code: str) -> None:
        self.isin = isin
        self.reason_code = reason_code
        super().__init__(
            f"Corporate-action effect on {isin} has withdrawn evidence ({reason_code}); "
            "this ISIN cannot be valued until the action is re-verified or cancelled"
        )


class FnoMissingPriceError(MissingPriceError):
    """An open, unexpired F&O position has no ``fno_price`` on or before ``as_of`` --
    the ``MissingPrice`` of ``docs/ENGINE_SPEC.md`` section 15 rule 8.

    Same posture as ``MissingPriceError`` (CLAUDE.md rule 4), and a subclass of it so a
    caller catching the equity error catches this too. Keyed by contract id, because a
    derivative has no ISIN. ``app.engine.fno.compute_fno`` catches it per contract and
    lists the position in ``excluded`` by name; one unpriced contract never blanks the
    rest.
    """

    def __init__(self, contract_id: str, as_of: object = None) -> None:
        self.contract_id = contract_id
        self.isins = []
        self.as_of = as_of
        suffix = f" on or before {as_of}" if as_of is not None else ""
        EngineError.__init__(self, f"No usable F&O price for contract {contract_id}{suffix}")


class FnoQuantityError(EngineError):
    """An F&O trade with a zero or negative quantity -- ``docs/ENGINE_SPEC.md`` section
    15. Quantity is always positive; direction comes from ``side``.

    RAISE. A close larger than the open queue is NOT this error: the excess opens a new
    opposite-side lot (rule 2), because an F&O position may legitimately be short -- unless
    the close is an ``EXCHANGE_SETTLEMENT`` line, whose excess goes to ``excluded`` (rule 14).
    """

    def __init__(self, trade_id: str | None = None, quantity: object = None) -> None:
        self.trade_id = trade_id
        self.quantity = quantity
        super().__init__(f"F&O trade {trade_id} has non-positive quantity {quantity}")
