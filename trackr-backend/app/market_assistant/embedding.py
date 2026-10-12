"""Phase 5: the local embedding model behind ``retrieval.Embedder``.

docs/AI_ASSISTANT_PRIVACY_PLAN.md sections 5 and 7.3: no remote embeddings, and never of
user text. This runs a small ONNX model in-process through ``fastembed`` (optional extra
``assistant-embeddings``). It embeds only the fixed catalogue query and admitted official
passages -- ``retrieval.rank`` is its only caller.

OFFLINE ONLY. The model is loaded from ``MARKET_ASSISTANT_EMBEDDING_CACHE_DIR`` with
``local_files_only=True`` and the Hugging Face hub forced offline, so a request can never
trigger a download. Weights are fetched once, deliberately, by
``scripts/fetch_market_assistant_embedding_model.py``. If the package or the files are
missing, ``load_embedder`` returns None and hybrid ranking degrades to BM25.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("market_assistant.embedding")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
#: Passages are at most ~900 characters; bge-small truncates at 512 tokens anyway.
MAX_TEXT_CHARS = 2000


class LocalEmbedder:
    def __init__(self, model_name: str, cache_dir: str):
        if not cache_dir or not Path(cache_dir).is_dir():
            raise FileNotFoundError("embedding cache directory is not configured or missing")
        # Before the import, so nothing in the hub client can reach the network.
        os.environ["HF_HUB_OFFLINE"] = "1"
        from fastembed import TextEmbedding

        self.name = model_name
        self._model = TextEmbedding(model_name, cache_dir=cache_dir, local_files_only=True)
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:  # onnxruntime sessions are not guaranteed re-entrant
            return [
                [float(x) for x in vector]
                for vector in self._model.embed([t[:MAX_TEXT_CHARS] for t in texts])
            ]


_lock = threading.Lock()
_loaded: dict[tuple[str, str], LocalEmbedder | None] = {}


def load_embedder(model_name: str, cache_dir: str) -> LocalEmbedder | None:
    """Process-wide, loaded once per (model, dir). A failure is remembered, so a missing
    model costs one warning, not one per request."""
    key = (model_name, cache_dir)
    with _lock:
        if key not in _loaded:
            try:
                _loaded[key] = LocalEmbedder(model_name, cache_dir)
            except Exception as exc:
                logger.warning(
                    "market_assistant embedder unavailable (%s); hybrid ranking uses BM25",
                    type(exc).__name__,
                )
                _loaded[key] = None
        return _loaded[key]
