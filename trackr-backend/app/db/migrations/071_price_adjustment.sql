-- Exchange-discovered value factors for price-return series, starting with demergers.
--
-- On a demerger ex-date the parent's price falls because value moves into the new
-- company's shares -- ABFRL closed 268.95 cum and opened 98.00 ex on 22-May-2025. A
-- price series that ignores this reports a -66.6% "return"; the real move that day was
-- 98.00 -> 89.85 (-8.3%). NSE runs a special pre-open call auction on the ex-date to
-- discover the parent's ex-demerger price, and itself carries the spun-off company into
-- indices at (cum close - discovered price). factor = discovered open / cum close is the
-- share of value the parent retained; pre-ex closes are multiplied by it, exactly as a
-- verified split divides them by its ratio.
--
-- THIS IS NOT THE TAX COST RATIO. Section 49(2C) apportions cost by NET BOOK VALUE
-- (Edelweiss/Nuvama: 67.5/32.5) while the market discovered 52.6/47.4 -- different
-- questions, kept apart. Tax cost stays ENGINE_SPEC section 11 (demerger_cost_split).
-- Analytics only: no transaction, holding or money figure reads this table.
CREATE TABLE price_adjustment (
    isin             text NOT NULL REFERENCES security (isin),
    ex_date          date NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('DEMERGER')),
    factor           numeric(20,10) NOT NULL CHECK (factor > 0 AND factor < 1),
    price_cum        numeric(20,6) NOT NULL,
    price_discovered numeric(20,6) NOT NULL,
    -- The ISIN NSE's notice carried; can be a retired one (see traded_identifier).
    notice_isin      text NOT NULL,
    evidence         jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (isin, ex_date, kind)
);
