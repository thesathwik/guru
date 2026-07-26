"""Text embedding + subject-scoped similarity search.

Uses a single local multilingual model (no external API, no per-call
cost) for every subject. Isolation between subjects' tutors comes from
tagging every embedded chunk with subject_id and always filtering
retrieval to it - not from separate models or indexes.
"""
import json
import math
import re
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


_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def build_idf(documents: list[str]) -> dict[str, float]:
    """Inverse document frequency over a corpus. Rare words like
    'tyndall' end up weighted far above common ones like 'effect' or
    'is', which is the signal that distinguishes the right figure."""
    counts: dict[str, int] = {}
    for document in documents:
        for term in set(tokenize(document)):
            counts[term] = counts.get(term, 0) + 1

    total = max(len(documents), 1)
    return {term: math.log(1 + total / count) for term, count in counts.items()}


_subject_idf_cache: dict[int, tuple[int, dict[str, float]]] = {}


def get_subject_idf(db, subject_id: int) -> dict[str, float]:
    """IDF over a subject's chunk text, cached per subject and rebuilt
    when its chunk count changes (i.e. material was added or
    reprocessed)."""
    from . import models

    chunk_count = db.query(models.Chunk).filter_by(subject_id=subject_id).count()
    cached = _subject_idf_cache.get(subject_id)
    if cached and cached[0] == chunk_count:
        return cached[1]

    texts = [
        row.text for row in db.query(models.Chunk).filter_by(subject_id=subject_id).all()
    ]
    idf = build_idf(texts)
    _subject_idf_cache[subject_id] = (chunk_count, idf)
    return idf


def lexical_overlap(query: str, text: str, idf: dict[str, float]) -> float:
    """How much of the query's *distinctive* vocabulary appears in text,
    scored 0-1.

    The IDF must come from the subject's full text, not from the
    captions alone. Scoring against caption-derived IDF drops any query
    term missing from every caption - so a question about the Tyndall
    effect silently degrades to matching the word "effect", and returns
    a confident-looking score for an unrelated figure. Weighted against
    the subject's own vocabulary, a caption missing the rare term is
    correctly penalised, and when no caption has it every figure scores
    low and none is shown.

    Terms unknown even to the subject's text (typos, or words it simply
    never uses) are ignored - they cannot match anything.
    """
    query_terms = {term for term in tokenize(query) if term in idf}
    if not query_terms:
        return 0.0

    text_terms = set(tokenize(text))
    matched = sum(idf[term] for term in query_terms if term in text_terms)
    total = sum(idf[term] for term in query_terms)
    return matched / total if total else 0.0


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
