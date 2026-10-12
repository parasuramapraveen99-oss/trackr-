-- 006_source_precedence.sql -- statuses for rows the precedence rule acts on.
--
-- INTERIM MEASURE. The real fix is a dedupe_key that collides across sources; see the
-- TODO in docs/DATA_MODEL.md. Until that lands, the same trade described by a tradebook
-- and by a contract note produces two different keys and would be inserted twice, so the
-- ingestion layer has to arbitrate. These statuses exist so that arbitration is VISIBLE
-- in the data rather than being a silent skip.

ALTER TYPE import_row_status ADD VALUE IF NOT EXISTS 'EXCLUDED';
ALTER TYPE import_row_status ADD VALUE IF NOT EXISTS 'SUPERSEDED';

COMMENT ON COLUMN import_row.status IS
    'STAGED -> PROMOTED (written), DUPLICATE (already in the ledger by dedupe_key), '
    'REJECTED (unparseable), EXCLUDED (a better source already describes this trade), '
    'SUPERSEDED (this row replaced a worse source, which was reversed). The last two are '
    'the source-precedence rule; `reason` always says which trade and why.';
