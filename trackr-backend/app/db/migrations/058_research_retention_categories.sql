-- 058_research_retention_categories.sql -- the three retention categories the drafted BE-Q05
-- mapping names (app/research/policy/proposal.json "retention").
--
-- A ROW HERE IS NOT AN APPROVAL. workspace_privacy.COVERAGE maps research tables onto these
-- entity types only when the owner's approval of proposal.json (bound to its exact SHA-256,
-- app/research/policy_approval.py) loads. Without it every research table stays UNMAPPED and
-- readiness_blockers keeps research writes closed, exactly as before this migration.
--
-- Nothing deletes on these rows: app/db/retention.py REPORTS investors past retention and a
-- human acts on the list; research rows cascade from the investor. readiness_blockers refuses
-- to open writes if retain_years here ever drifts from the approved mapping.
--
-- Additive and idempotent: ON CONFLICT DO NOTHING never overwrites an operator's value.

INSERT INTO retention_policy (entity_type, retain_years, note) VALUES
    ('workspace_research', 5,
     'BE-Q05 proposal-1: notes, theses, evidence, tasks, monitors and reports; from last '
     'activity; erased with the investor; legal hold suspends. Effective only once approved.'),
    ('workspace_investor_notebook', 1,
     'BE-Q05 proposal-1: Butler process notebook (sensitive); from last activity; purge on '
     'opt-out or investor erasure; legal hold suspends. Effective only once approved.'),
    ('workspace_hosted_job', 1,
     'BE-Q05 proposal-1: hosted report/reflection job, budget and checkpoint records; from last '
     'activity; erased with the investor. Effective only once approved.')
ON CONFLICT (entity_type) DO NOTHING;
