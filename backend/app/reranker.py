"""Cross-encoder reranking of retrieval candidates.

The retrieval stage compares a query vector against pre-computed chunk
vectors, so the query and the passage never actually meet - each was
encoded with no knowledge of the other. A cross-encoder reads both
together and scores the pair directly, which is markedly more accurate;
it is far too slow to run over a whole library, so it reorders the
shortlist the first stage produced.

Optional by design: if the model can't be loaded (not downloaded, or
too little memory on the host), retrieval falls back to the fused
first-stage ranking rather than failing the request.
"""
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get(
    "RERANKER_MODEL", "jinaai/jina-reranker-v2-base-multilingual"
)
ENABLED = os.environ.get("RERANKER_ENABLED", "1") not in ("0", "false", "False")

# How many first-stage candidates to rerank. Larger recovers more
# relevant passages the first stage ranked poorly, at roughly linear
# CPU cost.
CANDIDATES = int(os.environ.get("RERANKER_CANDIDATES", "30"))

_load_failed = False


@lru_cache(maxsize=1)
def _get_model():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=MODEL_NAME)


def available() -> bool:
    return ENABLED and not _load_failed


def rerank(query: str, documents: list[str]) -> list[float] | None:
    """Scores each document against the query. Returns None if reranking
    is unavailable, so callers keep their existing order."""
    global _load_failed

    if not available() or not documents:
        return None

    try:
        return list(_get_model().rerank(query, documents))
    except Exception as exc:  # noqa: BLE001 - reranking is an enhancement
        _load_failed = True
        logger.warning(
            "Reranker unavailable (%s); falling back to first-stage ranking", exc
        )
        return None
