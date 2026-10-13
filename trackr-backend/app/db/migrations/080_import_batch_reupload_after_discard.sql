-- 080_import_batch_reupload_after_discard.sql -- a discarded file may be uploaded again.
--
-- 016 made (account_id, sha256) UNIQUE across every batch, whatever its status. So once a
-- batch was DISCARDED, the same file could never be staged for that account again: the
-- upload was answered with the discarded batch and told "already imported" although none
-- of its trades were in the ledger (seen 2026-10-13: a setup that failed after its first
-- file had the rest discarded, and re-uploading them changed nothing).
--
-- Discarding ends THAT batch; it is not a ban on the bytes. The rule now covers live
-- batches only (STAGED, PROMOTED): an identical file staged or already in the ledger is
-- still caught before parsing, and every discarded batch stays on file as the record of
-- what was rejected and why. Trades themselves are protected by dedupe_key regardless.

ALTER TABLE import_batch DROP CONSTRAINT import_batch_account_sha256_uniq;

CREATE UNIQUE INDEX import_batch_account_sha256_live
    ON import_batch (account_id, sha256)
    WHERE status <> 'DISCARDED';

COMMENT ON INDEX import_batch_account_sha256_live IS
    'Per-destination and LIVE batches only: re-uploading an identical file that is staged '
    'or promoted for the same account is caught before parsing; a DISCARDED batch does not '
    'block the same file from being staged again (migration 080).';
