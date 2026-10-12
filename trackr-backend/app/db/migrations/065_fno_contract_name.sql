-- 065_fno_contract_name.sql -- the exchange's own display name for an F&O contract.
--
-- docs/FNO_PLAN.md Piece 1, docs/INGESTION.md "F&O adapter". The UDiFF F&O bhavcopy's
-- FinInstrmNm prints exactly the symbol a Zerodha contract note prints (verified on the
-- real 2024-08-12 / 2024-08-14 / 2024-08-28 / 2026-09-02 NSE files: ADANIENT24AUG3050PE,
-- NIFTY2481424200PE, NIFTY24AUG24200PE, NIFTY2690823850PE). Resolution uses it ONLY to pick
-- between contracts the decoded candidates already matched -- it closes the monthly-vs-
-- same-strike-weekly ambiguity -- and never as the contract's key (rule 21(a): keyed by
-- contract identity, never by display symbol).
--
-- NULL for contracts known only from the pre-2024 legacy NSE archive, which has no name;
-- those resolve exactly as before. The upsert never overwrites a name with NULL.
--
-- Additive and re-runnable.

ALTER TABLE fno_contract ADD COLUMN IF NOT EXISTS fin_instrm_nm text;

CREATE INDEX IF NOT EXISTS fno_contract_name_idx ON fno_contract (exchange, fin_instrm_nm);

COMMENT ON COLUMN fno_contract.fin_instrm_nm IS
    'Bhavcopy FinInstrmNm (the exchange display symbol). A tie-breaker between contracts '
    'the decoded symbol already matched, never the key. NULL from the legacy archive.';
