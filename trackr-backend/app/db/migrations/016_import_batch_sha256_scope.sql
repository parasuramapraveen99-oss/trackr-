-- 016_import_batch_sha256_scope.sql -- a file hash identifies bytes, not a tenant.
--
-- 005 made import_batch.sha256 UNIQUE across the whole table. That made "re-uploading the
-- identical file is caught before parsing" true at the wrong scope: investor B uploading
-- the same bytes investor A had staged was told "already staged as batch X" and pointed at
-- a batch the ownership guard then refused to serve -- a dead end that also leaked another
-- account's batch id to an authenticated stranger. Transaction identity (dedupe_key) is
-- scoped by account_id; the file-hash short-circuit now matches that scope. The same bytes
-- for another investor, or for another account of the same investor, are a different
-- ledger's business and stage independently.

ALTER TABLE import_batch DROP CONSTRAINT import_batch_sha256_key;

ALTER TABLE import_batch
    ADD CONSTRAINT import_batch_account_sha256_uniq UNIQUE (account_id, sha256);

-- account_id is NULL only after its account was deleted (ON DELETE SET NULL); Postgres
-- treats NULLs as distinct in a UNIQUE constraint, so those rows collide with nothing --
-- which is correct: there is no account left for the file to be a duplicate of.
COMMENT ON CONSTRAINT import_batch_account_sha256_uniq ON import_batch IS
    'Per-destination, not global: re-uploading the identical file FOR THE SAME ACCOUNT is '
    'caught here before it is parsed. The same bytes staged under another investor or '
    'another account are a distinct staging - matching them globally blocked legitimate '
    'uploads and named batches their uploader could never open.';

COMMENT ON TABLE import_batch IS
    'One uploaded file. sha256 is unique per destination account (see '
    'import_batch_account_sha256_uniq), so re-uploading the identical file for the same '
    'account is caught before it is parsed - clients send overlapping Console exports by '
    'necessity, since each download is capped at 365 days.';
