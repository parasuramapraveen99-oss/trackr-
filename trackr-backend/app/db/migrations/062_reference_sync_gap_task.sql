-- 062_reference_sync_gap_task.sql -- the reference worker's GAP task (rule 20).
--
-- docs/ARCHITECTURE_INVARIANTS.md rule 20 (owner amendment 2026-10-08) and
-- docs/AUTO_GAP_RESOLUTION.md: after an account's REPLAY, whatever still does not
-- reconcile is handed to app.db.gap_resolution, which closes it from public exchange
-- records (rights / IPO allotments) or leaves it open and named. A new task kind, so the
-- CHECK from 036 is widened; nothing else about the queue changes. Additive and
-- re-runnable: dropping and re-adding a CHECK touches no row.

ALTER TABLE reference_sync_task DROP CONSTRAINT IF EXISTS reference_sync_task_kind_check;
ALTER TABLE reference_sync_task ADD CONSTRAINT reference_sync_task_kind_check
    CHECK (kind IN ('NSDL', 'NSE', 'IDENTITY', 'ACTION', 'REPLAY', 'SNAPSHOT', 'GAP'));
