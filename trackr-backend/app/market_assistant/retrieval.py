"""Phase 5: passage ranking -- the lexical baseline, BM25, and hybrid BM25 + dense via RRF.

docs/AI_ASSISTANT_PRIVACY_PLAN.md section 7.3. Ranking decides which admitted official
passages are offered to the selector (and shown when the selector is off). It never
decides what is true: every passage still comes from an admitted official document and
every quote is still checked word for word downstream.

Three modes, chosen by ``MARKET_ASSISTANT_RETRIEVAL``:

- ``lexical`` (default): the Phase 2 baseline, query-word overlap. Unchanged behaviour.
- ``hybrid``: BM25 and a LOCAL dense embedding, fused by reciprocal rank fusion. RRF
  combines the two rankings without pretending their raw scores are comparable. BM25
  keeps exact terms (circular numbers, "2026-27", symbols); the dense side catches
  paraphrase ("policy rate" for "repo rate").
- If the embedder is missing or fails, ``hybrid`` degrades to BM25 alone, never to an
  error, and says so in the result (``RankResult.mode``).

Inputs are public only: the fixed catalogue query and admitted official passages. The
typed question never reaches this module (admission dropped it), so it can never be
embedded. Pure and network-free: the embedder is injected (``embedding.py`` holds the
local model adapter), so this file imports nothing that can talk to the outside.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger("market_assistant.retrieval")

#: Standard BM25 constants; the corpus per search is a handful of passages, so these are
#: not tuned to it (tuning on so few documents would be fitting noise).
BM25_K1 = 1.5
BM25_B = 0.75
#: The usual RRF constant (Cormack et al. 2009). Larger flattens the top of each list.
RRF_K = 60

#: Words and exact tokens: "repo", "2026-27", "sebi/ho/mrd/2026/12". Hyphen and slash are
#: kept inside a token so circular numbers and fiscal years stay one exact term.
_TOKEN = re.compile(r"[a-z0-9]+(?:[/-][a-z0-9]+)*")
_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "of", "to", "in", "on", "a", "an", "is", "by", "at",
     "as", "or", "be", "are", "this", "that", "it", "its", "was", "were", "has", "have"}
)


class RetrievalMode(StrEnum):
    LEXICAL = "lexical"
    BM25 = "bm25"
    HYBRID = "hybrid"


class Embedder(Protocol):
    """A local text embedder. Must not make network calls at embed time."""

    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class RankResult:
    #: Passage indices, best first. Every input index appears exactly once.
    order: tuple[int, ...]
    #: The mode actually used, which can be BM25 when HYBRID was asked for.
    mode: RetrievalMode


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.casefold()) if t not in _STOPWORDS and len(t) > 1]


def overlap_scores(query: str, passages: Sequence[str]) -> list[float]:
    """The Phase 2 baseline: distinct query words present, earlier passages breaking ties.
    Matches ``evidence._terms`` scoring so LEXICAL mode keeps its old order."""
    word = re.compile(r"[a-z]{3,}")
    stop = {"the", "and", "for", "with", "from", "of", "india", "indian"}
    q = set(word.findall(query.casefold())) - stop
    return [
        len(q & (set(word.findall(p.casefold())) - stop)) * 10 - i
        for i, p in enumerate(passages)
    ]


def bm25_scores(query: str, passages: Sequence[str]) -> list[float]:
    docs = [tokens(p) for p in passages]
    n = len(docs)
    if n == 0:
        return []
    avg_len = sum(len(d) for d in docs) / n or 1.0
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d))
    q_terms = set(tokens(query))
    scores: list[float] = []
    for d in docs:
        tf = Counter(d)
        norm = BM25_K1 * (1 - BM25_B + BM25_B * len(d) / avg_len)
        s = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (BM25_K1 + 1) / (tf[term] + norm)
        scores.append(s)
    return scores


def _order(scores: Sequence[float]) -> list[int]:
    """Best first; ties keep input order, so the ranking is deterministic."""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal rank fusion of several best-first index lists over the same items."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank + 1)
    return sorted(fused, key=lambda i: (-fused[i], i))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dense_scores(query: str, passages: Sequence[str], embedder: Embedder) -> list[float]:
    vectors = embedder.embed([query, *passages])
    if len(vectors) != len(passages) + 1:
        raise ValueError("embedder returned the wrong number of vectors")
    q, rest = vectors[0], vectors[1:]
    return [_cosine(q, v) for v in rest]


def rank(
    query: str,
    passages: Sequence[str],
    mode: RetrievalMode = RetrievalMode.LEXICAL,
    embedder: Embedder | None = None,
) -> RankResult:
    if not passages:
        return RankResult((), mode)
    if mode is RetrievalMode.LEXICAL:
        return RankResult(tuple(_order(overlap_scores(query, passages))), mode)
    lexical = _order(bm25_scores(query, passages))
    if mode is RetrievalMode.BM25:
        return RankResult(tuple(lexical), mode)
    if embedder is None:
        return RankResult(tuple(lexical), RetrievalMode.BM25)
    try:
        dense = _order(dense_scores(query, passages, embedder))
    except Exception as exc:  # a failed local model must never fail the answer
        logger.warning("market_assistant dense ranking failed: %s", type(exc).__name__)
        return RankResult(tuple(lexical), RetrievalMode.BM25)
    return RankResult(tuple(rrf([lexical, dense])), RetrievalMode.HYBRID)
