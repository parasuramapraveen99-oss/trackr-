-- 020_document_sha256_scope.sql -- a content hash identifies bytes, not a tenant.
--
-- The same correction 016 made to import_batch.sha256, for the table underneath it. 003
-- made document.sha256 UNIQUE across the whole table and called it content addressing.
-- Content addressing is the right idea at the wrong scope: the second investor to send
-- identical bytes -- a joint account's contract note, an identical NIL statement, the
-- same fund factsheet -- was handed the FIRST investor's row by find_by_hash, complete
-- with their document id and their storage_key, and their own upload was never recorded.
--
-- Two consequences, both live:
--   * cross-tenant reference. The second investor's import_batch.document_id pointed into
--     someone else's filing cabinet, and the id came back out over the API -- the same
--     leak 016 closed one table up.
--   * erasure took the wrong bytes. document is ON DELETE CASCADE from investor, so
--     erasing the first investor deleted the row the second one's record depended on and
--     swept the object with it. tests/test_db_documents.py proved this: after erasing A,
--     investor B had zero documents, having uploaded one.
--
-- Scoped to investor_id, not account_id (which import_batch uses): document is the
-- investor's filing cabinet -- documents_for_investor, erasure and the storage-key prefix
-- are all per investor, and account_id here is nullable and often unset. Re-forwarding
-- the same statement for the same investor stays the no-op it must be, whichever of their
-- accounts it lands in.

ALTER TABLE document DROP CONSTRAINT document_sha256_key;

ALTER TABLE document
    ADD CONSTRAINT document_investor_sha256_uniq UNIQUE (investor_id, sha256);

COMMENT ON CONSTRAINT document_investor_sha256_uniq ON document IS
    'Per-investor, not global: re-storing the identical file FOR THE SAME INVESTOR is the '
    'no-op that makes forwarded duplicates free. The same bytes from another investor are '
    'another person''s document - matching them globally handed the second sender the '
    'first one''s row, and let erasing one investor delete the other''s statement.';

COMMENT ON COLUMN document.sha256 IS
    'Unique per investor (see document_investor_sha256_uniq). Content addressing is what '
    'makes re-storing a forwarded duplicate a no-op, enforced here rather than by '
    'convention - the same argument as transaction.dedupe_key, and scoped like it.';
