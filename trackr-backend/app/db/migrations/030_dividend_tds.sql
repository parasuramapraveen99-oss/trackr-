-- 030_dividend_tds.sql -- tax withheld on a dividend, as its own column.
--
-- WHY A NEW COLUMN RATHER THAN REINTERPRETING dividend_amount. One field cannot serve
-- both readers, and it has two with OPPOSITE requirements:
--
--   * CASH FLOW wants NET. `dividend_amount` enters app.engine.xirr as a positive cash
--     flow and app.engine.twrr as a sub-period outflow. Both model money actually moving,
--     and what moved is what the bank credited -- net of tax withheld at source.
--   * TAXABLE INCOME wants GROSS. Dividends have been taxable in the shareholder's hands
--     at slab rate since FY 2020-21, and the assessable figure is the gross declared
--     amount; the TDS is a prepayment of the holder's own liability, creditable against
--     it, and it appears in Form 26AS as such. Reporting net as income would understate
--     the income AND silently discard a tax credit.
--
-- So: `dividend_amount` KEEPS ITS EXISTING MEANING AND IS NET. That is the only
-- arrangement that changes no number this system already computes -- every DIVIDEND row
-- already in any ledger keeps its meaning, XIRR and TWRR are untouched, and
-- fixtures/golden_portfolio.json's four headline values are unaffected. Redefining the
-- column as gross would silently alter every existing XIRR in every portfolio, with no
-- migration able to tell which historic rows had been recorded gross and which net.
--
-- GROSS IS DERIVED, NEVER STORED: dividend_amount + COALESCE(tds_amount, 0). Storing all
-- three would be storing a fact twice and inviting the two to disagree, the same reason
-- holdings are computed on read.
--
-- NULL MEANS UNKNOWN. NOT ZERO. This is the load-bearing property of the column and the
-- reason it has no DEFAULT. Zero asserts "no tax was withheld", which is a real and
-- different claim -- true below the section 194 threshold, true against a valid
-- 15G/15H. Unknown is the honest state for every dividend already in the ledger and for
-- anything sourced from a corporate-action feed, because NEITHER FEED CARRIES TAX AT ALL:
-- TDS appears only on a bank credit, a broker funds statement or a registrar's advice.
-- A default of 0 would have turned every one of those into a false assertion about tax
-- the moment this migration ran.
--
-- WHAT THIS DELIBERATELY IS NOT. It is not dividend income tax. Slab-rate computation, an
-- "income from other sources" schedule, and crediting TDS against total liability are a
-- real addition to a tax engine that is capital-gains only today (grandfathering,
-- indexation, Schedule 112A, ITR capital gains) and has no slab machinery anywhere. This
-- migration captures the INPUTS that work will need -- gross and withheld, per security
-- per financial year -- so the seam exists without the feature being built. See
-- docs/TAX_ENGINE.md.
--
-- SECTION 194, for whoever implements the rest: 10% where a company's dividend payments
-- to one holder exceed the financial-year threshold (Rs 5,000 historically; raised to
-- Rs 10,000 with effect from FY 2025-26), 20% without a valid PAN, nil against a valid
-- 15G/15H declaration. Note that the threshold is PER COMPANY PER FINANCIAL YEAR, so
-- whether TDS applies is not a property of one dividend in isolation -- which is exactly
-- why this column records what was ACTUALLY WITHHELD according to a document, and is
-- never computed by this system from a rate.

ALTER TABLE "transaction"
    ADD COLUMN tds_amount numeric(20,6)
        CONSTRAINT transaction_tds_non_negative CHECK (tds_amount IS NULL OR tds_amount >= 0);

COMMENT ON COLUMN transaction.dividend_amount IS
    'NET dividend received -- what actually reached the bank, after any tax withheld at '
    'source. This is the cash-flow figure app.engine.xirr and app.engine.twrr consume. '
    'Gross is dividend_amount + COALESCE(tds_amount, 0), derived and never stored. Keeping '
    'this column NET is what makes migration 030 change no number already computed.';

COMMENT ON COLUMN transaction.tds_amount IS
    'Tax withheld at source on a dividend, per section 194, AS RECORDED ON A DOCUMENT -- '
    'never computed by this system from a rate, because the threshold is per company per '
    'financial year and so is not a property of one dividend in isolation. NULL means '
    'UNKNOWN, not zero: zero asserts no tax was withheld, which is a different and '
    'checkable claim. No DEFAULT, deliberately -- neither corporate-action feed carries '
    'tax, so a default of 0 would have made a false assertion about every existing row.';
