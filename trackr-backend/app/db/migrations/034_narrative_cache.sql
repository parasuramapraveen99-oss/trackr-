-- 034_narrative_cache.sql -- the generated analytics narrative, and the fact packs it was
-- generated from (app/narrative/, GET /investors/{id}/narrative).
--
-- BOTH TABLES ARE CACHES OF A DERIVATION, in exactly the sense 010_portfolio_snapshots.sql
-- means it, and both are safe to TRUNCATE: the page renders every figure without them.
-- There is one honest difference from portfolio_snapshot, and it is stated here rather
-- than glossed. A snapshot is reproducible: run compute_snapshot() again and you get the
-- same number. narrative_factpack has that property -- it is a projection of one ledger
-- replay. narrative_artifact does NOT: it holds prose from a language model, and no
-- temperature setting makes a batched serving stack bitwise deterministic.
--
-- That is why the two are separate tables rather than one. The reproducible half is what
-- every claim was checked against and what "what changed since yesterday" is computed
-- from; the unreproducible half is keyed to it by hash, so a narrative can always be
-- traced to the exact figures that were true when it was written, even though rerunning
-- the model would word it differently.
--
-- NOTHING DOWNSTREAM READS EITHER TABLE FOR A NUMBER. Every figure a reader sees comes
-- from a live /analytics call; the artifact carries fact IDS and prose, never values.

CREATE TABLE narrative_factpack (
    investor_id     uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    as_of           date NOT NULL,
    -- Part of the grain for the same reason portfolio_snapshot.advised_only is: these
    -- three produce genuinely different ledgers or different figures, not a display
    -- filter over one answer.
    advised_only    boolean NOT NULL DEFAULT false,
    benchmark       text    NOT NULL,
    universe        text    NOT NULL,

    -- The QUANTISED pack: every numeric fact rounded to the digits it will actually be
    -- rendered at. The contract this enforces, stated once: IF TWO PACKS WOULD RENDER
    -- IDENTICALLY, THE PROSE ABOUT THEM MUST BE IDENTICAL. Storing full precision would
    -- make the hash change on a move no reader can see, and every such change buys a
    -- paraphrase of a sentence that was already correct.
    facts           jsonb   NOT NULL,
    -- sha256 over the quantised pack. The join key to narrative_artifact, and the thing
    -- that makes a stale narrative detectable rather than merely old.
    factpack_sha256 text    NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (investor_id, as_of, advised_only, benchmark, universe)
);
COMMENT ON TABLE narrative_factpack IS
    'Quantised fact packs, one per investor per as_of. A cache of a derivation and always '
    'reproducible from the ledger and prices as they stood on as_of. Also the source of '
    'day-over-day deltas: portfolio_snapshot holds totals only, so this is the only place '
    'a component score or a factor tilt has a stored yesterday.';

-- Day-over-day lookups ("the most recent pack strictly before this one") walk backwards
-- per investor, which is the one access pattern the primary key does not already serve.
CREATE INDEX narrative_factpack_history
    ON narrative_factpack (investor_id, advised_only, benchmark, universe, as_of DESC);

CREATE TABLE narrative_artifact (
    investor_id         uuid NOT NULL REFERENCES investor(id) ON DELETE CASCADE,
    surface             text NOT NULL,
    as_of               date NOT NULL,
    advised_only        boolean NOT NULL DEFAULT false,
    benchmark           text NOT NULL,
    universe            text NOT NULL,

    -- EVERY INPUT THAT COULD CHANGE THE ANSWER IS IN THE KEY. Omitting any one of these
    -- means editing it silently serves output produced under the old one -- change a
    -- definition card and every cached row is wrong with no way to tell.
    factpack_sha256     text NOT NULL,   -- the figures
    rule_version        text NOT NULL,   -- app.analytics.verdict.RULE_VERSION
    template_version    text NOT NULL,   -- app.narrative.prompt.TEMPLATE_VERSION
    definition_pack_sha text NOT NULL,   -- the cards + phrase lexicon
    model               text NOT NULL,   -- the served model id

    -- THE CLAIMS, NOT THE RENDERED PROSE. Relation, args, segments, cites, card -- with
    -- fact IDS where figures go and no values anywhere.
    --
    -- Storing rendered output would be smaller and would be wrong. A cached artifact is
    -- served against a LIVE fact pack, so the claims are re-verified and re-rendered on
    -- every read: the figures a reader sees are always today's, and a comparison that
    -- stopped holding is dropped rather than served. The cache therefore saves the model
    -- call and nothing else, which is the only part that is expensive or nondeterministic.
    claims              jsonb NOT NULL,
    -- Which claims were refused and by which rule (V1-V7). Invariant 5 in spirit: a
    -- failed verification is a recorded exception, not a silently missing sentence.
    -- Without this nobody can tell a prompt regression from an unremarkable portfolio.
    rejections          jsonb NOT NULL DEFAULT '[]'::jsonb,
    generated_at        timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (investor_id, surface, as_of, advised_only, benchmark, universe,
                 factpack_sha256, rule_version, template_version,
                 definition_pack_sha, model)
);
COMMENT ON TABLE narrative_artifact IS
    'Generated analytics CLAIMS, keyed to the exact facts and prompt that produced them. '
    'Safe to truncate: the page renders every number without it. Re-verified and '
    're-rendered against a live pack on every read, so a stale comparison is dropped '
    'rather than shown. NOT reproducible bitwise -- see the migration header.';
COMMENT ON COLUMN narrative_artifact.rejections IS
    'Claims the verifier refused, with the rule that refused each. Kept so a rising '
    'rejection rate is visible; a verifier whose failures are invisible is a verifier '
    'nobody maintains.';

-- Serving a page asks "is there a current artifact for this surface", newest first.
CREATE INDEX narrative_artifact_lookup
    ON narrative_artifact (investor_id, surface, as_of DESC);
