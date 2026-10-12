-- Arrival order, so a ledger read back from the database replays the way it was written.
--
-- THE BUG THIS FIXES. engine/ledger.ordered() sorts on txn_date alone and relies on
-- Python's stable sort to hold rows sharing a date in their INPUT order -- its docstring
-- says so, and says the input order IS insertion order. db/repository.load_transactions
-- returned ORDER BY (txn_date, created_at, id), and neither tiebreak delivers that:
-- created_at defaults to now(), which is the TRANSACTION timestamp and therefore
-- identical for every row of a bulk import, so the tiebreak fell through to id -- a
-- random uuid4. Same-date rows came back shuffled differently on every read.
--
-- It is not a cosmetic ordering difference. Replaying the golden fixture from the
-- database raised NegativeHoldingError on one ISIN in that fixture: a SELL sorted ahead
-- of the BUY that supplied the shares. Where it does not raise, it silently changes
-- weighted-average cost and FIFO lot matching, because a disposal removes cost
-- proportionally to the pool as it stands AT THAT MOMENT. 67 date/ISIN groups in the
-- golden fixture hold more than one row.
--
-- WHY A COLUMN RATHER THAN A CLEVERER ORDER BY. There is nothing else on the row that
-- encodes intent. dedupe_key is a hash, source_ref is a broker trade id present on only
-- some sources, and price/quantity describe the trade rather than its position in the
-- file. Any of those would give a STABLE order that is still not the RECORDED order,
-- which is worse than the bug it replaced: reproducible, and reproducibly wrong.
--
-- Append-only is untouched. seq is assigned by the sequence on INSERT and never written
-- again; the BEFORE UPDATE OR DELETE trigger still refuses every other kind of change.
ALTER TABLE transaction ADD COLUMN seq bigserial NOT NULL;

COMMENT ON COLUMN transaction.seq IS
    'Arrival order. The replay tiebreak for rows sharing a txn_date, because '
    'engine/ledger.ordered() preserves input order and created_at is identical across a '
    'bulk import. Never a business identifier: id is the key, seq is the sequence.';

CREATE UNIQUE INDEX transaction_seq_uniq ON transaction (seq);
