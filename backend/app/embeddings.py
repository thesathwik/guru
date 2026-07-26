"""Text embedding + subject-scoped similarity search.

Uses a single local multilingual model (no external API, no per-call
cost) for every subject. Isolation between subjects' tutors comes from
tagging every embedded chunk with subject_id and always filtering
retrieval to it - not from separate models or indexes.
"""
import json
from functools import lru_cache

import numpy as np

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@lru_cache(maxsize=1)
def _get_model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embeds a batch of chunk texts for storage."""
    if not texts:
        return []
    return [vec.tolist() for vec in _get_model().embed(texts)]


def embed_query(text: str) -> list[float]:
    """Embeds a single search query. Same model/space as embed_texts -
    this particular model is symmetric (no query/passage prefix needed,
    unlike e.g. the E5 model family)."""
    return next(iter(_get_model().embed([text]))).tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr = np.array(a)
    b_arr = np.array(b)
    denom = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if denom == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / denom)


def search_chunks(
    db,
    subject_id: int,
    query: str,
    top_k: int = 5,
    query_vector: list[float] | None = None,
) -> list[dict]:
    """Brute-force cosine similarity search over a single subject's
    chunks. Fine at this scale (a personal library of a few thousand
    chunks per subject at most) - no vector index needed.

    `query_vector` lets a caller that already embedded the query reuse
    it instead of paying for a second embedding pass."""
    from . import models

    rows = db.query(models.Chunk).filter_by(subject_id=subject_id).all()
    if not rows:
        return []

    query_vec = np.array(query_vector if query_vector is not None else embed_query(query))
    query_norm = np.linalg.norm(query_vec)

    scored = []
    for row in rows:
        chunk_vec = np.array(json.loads(row.embedding))
        denom = query_norm * np.linalg.norm(chunk_vec)
        score = float(np.dot(query_vec, chunk_vec) / denom) if denom else 0.0
        scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    return [
        {
            "material_id": row.material_id,
            "filename": row.material.filename,
            "chunk_index": row.chunk_index,
            "text": row.text,
            "page": row.page,
            "score": score,
        }
        for score, row in scored[:top_k]
    ]
