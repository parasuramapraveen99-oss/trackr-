-- Vendor fundamentals into the units the system declares (app/narrative/registry.py):
-- dividend_yield a FRACTION, debt_to_equity a RATIO -- like roe and net_margin already are.
--
-- Yahoo returns both as PERCENTS (dividendYield 3.86 for a 3.86% yield; debtToEquity 81.8
-- for 0.818x). Until 2026-10-09 they were stored unconverted, so the analytics page
-- rendered a 1.57% portfolio yield as 157% beside the index's 1.52%, and labelled a 0.33x
-- D/E "33.2". From this migration app/prices/yfinance_fundamentals.py converts at the
-- boundary and writes source 'YFINANCE:<ticker>'.
--
-- Only rows with the pre-fix provenance, exactly 'YFINANCE', are converted. Every such row
-- was fetched with yfinance >= 1.x, whose dividendYield is a percent (Yahoo changed it in
-- early 2025); this repository's first fundamentals fetch is dated 2026-09. This is a
-- cache of vendor ratios -- no ledger, holding or derived money figure is touched.
UPDATE security_fundamentals
   SET dividend_yield = dividend_yield / 100,
       debt_to_equity = debt_to_equity / 100
 WHERE source = 'YFINANCE';
