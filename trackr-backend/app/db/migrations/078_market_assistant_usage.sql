-- 078_market_assistant_usage.sql -- Ask about markets' daily external-call counters.
--
-- docs/AI_ASSISTANT_PRIVACY_PLAN.md section 10 / Phase 4: the daily ceilings on Exa searches
-- and model selections must hold across restarts and across processes, which a counter in
-- process memory does not. One row per UTC day and kind; a reserve is a single atomic
-- INSERT ... ON CONFLICT DO UPDATE ... WHERE used < limit (app.db.market_assistant_usage).
--
-- Deliberately carries NO user, investor, question, topic or request column: the plan
-- forbids identity or raw text in any quota store. Per-user rate limits stay in memory.
-- Operational, not financial; not part of the ledger. Additive and re-runnable.
CREATE TABLE IF NOT EXISTS market_assistant_usage (
    day  date    NOT NULL,
    kind text    NOT NULL CHECK (kind IN ('search', 'model')),
    used integer NOT NULL DEFAULT 0 CHECK (used >= 0),
    PRIMARY KEY (day, kind)
);
