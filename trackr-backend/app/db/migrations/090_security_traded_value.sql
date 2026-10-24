-- Daily traded value per security per exchange, from the same bhavcopy the close comes from.
--
-- WHY: the Analytics liquidity view (docs/ANALYTICS_BASIC_NARRATIVE.md, RR.LIQUIDITY) needs
-- the 20-session median traded value to estimate days to exit a holding at 10% of daily
-- turnover. The UDiFF file already carries it (TtlTrfVal; legacy NSE TOTTRDVAL, legacy BSE
-- NET_TURNOV); only the close was being kept.
--
-- PER EXCHANGE, NOT MERGED: an investor can sell on either exchange, so the day's tradable
-- value is the SUM across exchanges, computed on read. Storing per exchange keeps that sum
-- reproducible and lets a re-run of one exchange's file correct only its own rows.
--
-- A day a security did not trade has NO row. It is never stored as zero: a zero would pull
-- the median down and report a liquid holding as illiquid (CLAUDE.md rule 4 in spirit).
CREATE TABLE security_traded_value (
    isin          text NOT NULL REFERENCES security(isin),
    price_date    date NOT NULL,
    exchange      text NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    traded_value  numeric(20, 2) NOT NULL CHECK (traded_value > 0),
    PRIMARY KEY (isin, price_date, exchange)
);

CREATE INDEX security_traded_value_date ON security_traded_value (isin, price_date DESC);

COMMENT ON TABLE security_traded_value IS
    'Rupee value traded per security per exchange per session, from the bhavcopy. '
    'Read by app/db/traded_value.py; summed across exchanges for liquidity.';

SELECT attach_analytics_input_trigger('security_traded_value');
