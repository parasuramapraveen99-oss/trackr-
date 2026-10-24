-- Audited annual line items as filed with the exchange (SEBI LODR Reg 33 XBRL).
--
-- Statement figures, not ratios: ROCE, net margin, debt/equity, interest cover, P/E and
-- P/B are computed on read by app/engine/exchange_ratios.py, so every ratio on screen can
-- be traced to the filing it came from (source_url). Money is full rupees as filed.
-- A NULL line item means the filing did not report it; it is never read as zero.
-- Either missing borrowing component withholds total debt and ROCE.
CREATE TABLE exchange_financials (
    isin                  text NOT NULL,
    period_end            date NOT NULL,
    basis                 text NOT NULL CHECK (basis IN ('CONSOLIDATED', 'STANDALONE')),
    audited               boolean NOT NULL,
    -- SEBI XBRL format from the filing's namespace: INDAS | NONINDAS | NBFC | BANKING.
    filing_format         text NOT NULL,
    period_start          date NOT NULL,
    revenue               numeric(20, 2),
    other_income          numeric(20, 2),
    profit_before_tax     numeric(20, 2),
    finance_costs         numeric(20, 2),
    net_profit            numeric(20, 2),
    equity                numeric(20, 2),
    borrowings_current    numeric(20, 2),
    borrowings_noncurrent numeric(20, 2),
    share_capital         numeric(20, 2),
    face_value            numeric(20, 6),
    basic_eps             numeric(20, 6),
    source                text NOT NULL,
    source_url            text NOT NULL,
    filed_at              timestamptz,
    fetched_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, period_end, basis),
    CHECK (period_start < period_end)
);

COMMENT ON TABLE exchange_financials IS
    'Audited annual statement line items per ISIN from exchange XBRL filings. Ratios are '
    'derived on read (app/engine/exchange_ratios.py); see docs/PORTFOLIO_HEALTH.md.';

SELECT attach_analytics_input_trigger('public.exchange_financials'::regclass);

-- ROCE from the vendor's annual statements, for securities with no exchange filing.
ALTER TABLE security_fundamentals ADD COLUMN roce numeric(20, 6);
